#!/usr/bin/env python3
"""Worked example and smoke-test driver for geoserver-rest-mosaic.

Builds three mosaics against the docker-compose stack, one per granule
location, so each supported path is demonstrated end to end:

  local    granules on the GeoServer host, harvested by directory path
  remote   COGs fetched from LocalStack S3 over HTTP, indexed in PostGIS
  upload   granules shipped from this machine inside the configuration ZIP

Typical run::

    make fixtures
    docker compose --profile gs2 up -d
    uv run --extra examples python examples/driver.py all

Point it at the 3.0 container with ``--url http://localhost:8081/geoserver`` to
check the same code against both releases.

Hostnames are resolved by *GeoServer*, not by this script, so the PostGIS host
and the S3 base URL default to compose service names.  Override them via the
environment if you run GeoServer elsewhere.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

from geoserver_mosaic import (
    CogSettings,
    GeoServerClient,
    GeoServerError,
    MosaicDefinition,
    MosaicManager,
    MosaicResult,
    PostgisIndex,
    ShapefileIndex,
    TimeRegex,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "tiles"

WORKSPACE = "mosaic-demo"

#: Filenames look like ``S2_20240301T104021_r0c0.tif``.
TIME_REGEX = TimeRegex(regex=r"[0-9]{8}T[0-9]{6}", date_format="yyyyMMdd'T'HHmmss")

#: Extent the fixtures cover, for the sample WMS URLs.
DEMO_BBOX = (10.0, 45.0, 14.0, 49.0)

log = logging.getLogger("driver")


@dataclass
class Settings:
    """Everything the driver needs, resolved from the environment."""

    url: str
    user: str
    password: str
    #: PostGIS host *as GeoServer sees it* -- a compose service name by default.
    pg_host: str
    pg_port: int
    pg_database: str
    pg_user: str
    pg_password: str
    #: Base URL of the granule bucket, again from GeoServer's perspective.
    s3_base: str
    #: Directory inside the GeoServer container holding the same granules.
    granule_dir: str

    @classmethod
    def from_env(cls, url: str | None = None) -> "Settings":
        return cls(
            url=url or os.environ.get("GEOSERVER_URL", "http://localhost:8080/geoserver"),
            user=os.environ.get("GEOSERVER_USER", "admin"),
            password=os.environ.get("GEOSERVER_PASSWORD", "geoserver"),
            pg_host=os.environ.get("PG_HOST_FROM_GEOSERVER", "postgis"),
            pg_port=int(os.environ.get("PG_PORT_FROM_GEOSERVER", "5432")),
            pg_database=os.environ.get("PG_DATABASE", "gis"),
            pg_user=os.environ.get("PG_USER", "gis"),
            pg_password=os.environ.get("PG_PASSWORD", "gis"),
            s3_base=os.environ.get(
                "S3_BASE_FROM_GEOSERVER", "http://localstack:4566/mosaic-tiles"
            ),
            granule_dir=os.environ.get("GRANULE_DIR_IN_GEOSERVER", "/opt/granules"),
        )

    def postgis_index(self) -> PostgisIndex:
        return PostgisIndex(
            host=self.pg_host,
            port=self.pg_port,
            database=self.pg_database,
            user=self.pg_user,
            password=self.pg_password,
        )

    @property
    def wms_base(self) -> str:
        return self.url.rstrip("/").removesuffix("/rest")


# ---------------------------------------------------------------------------
# Mosaic definitions -- one per granule location
# ---------------------------------------------------------------------------


def granule_names() -> list[str]:
    """Fixture filenames, used to build the remote granule URLs."""
    if not FIXTURES.is_dir():
        raise SystemExit(
            f"No fixtures at {FIXTURES}. Run: "
            f"uv run --extra examples python examples/make_fixtures.py"
        )
    names = sorted(path.name for path in FIXTURES.glob("*.tif"))
    if not names:
        raise SystemExit(f"No .tif files in {FIXTURES}; regenerate the fixtures.")
    return names


def local_mosaic(settings: Settings) -> MosaicDefinition:
    """Granules already on the GeoServer host, indexed in PostGIS.

    Only the directory is harvested -- the mosaic reader walks it recursively,
    so a bulk load does not need one REST call per file.
    """
    return MosaicDefinition(
        workspace=WORKSPACE,
        store="tiles-local",
        index=settings.postgis_index(),
        time_regex=TIME_REGEX,
        granules=[settings.granule_dir],
        srs="EPSG:4326",
        title="Local granules (PostGIS index)",
    )


def remote_mosaic(settings: Settings, range_reader: str = "HTTP") -> MosaicDefinition:
    """Remote COGs read from object storage, indexed in PostGIS.

    The default HTTP range reader fetches from LocalStack's S3 endpoint over
    plain HTTP, which needs no credentials because the seed script makes the
    bucket publicly readable.  ``--range-reader S3`` switches to the AWS SDK
    path instead; see the note in docker/README.md about endpoint overrides.
    """
    base = settings.s3_base.rstrip("/")
    if range_reader.upper() == "S3":
        bucket = base.rsplit("/", 1)[-1]
        locations = [f"s3://{bucket}/{name}" for name in granule_names()]
    else:
        locations = [f"{base}/{name}" for name in granule_names()]

    return MosaicDefinition(
        workspace=WORKSPACE,
        store="tiles-remote",
        index=settings.postgis_index(),
        cog=CogSettings(range_reader=range_reader.upper()),  # type: ignore[arg-type]
        time_regex=TIME_REGEX,
        granules=locations,
        srs="EPSG:4326",
        title=f"Remote COGs via {range_reader.upper()} (PostGIS index)",
    )


def upload_mosaic(settings: Settings) -> MosaicDefinition:
    """Granules on *this* machine, shipped inside the configuration ZIP.

    Uses the default shapefile index rather than PostGIS, to show the
    no-database path: the whole mosaic is then self-contained in the store
    directory.
    """
    return MosaicDefinition(
        workspace=WORKSPACE,
        store="tiles-upload",
        index=ShapefileIndex(),
        time_regex=TIME_REGEX,
        upload_files=sorted(FIXTURES.glob("*.tif")),
        srs="EPSG:4326",
        title="Uploaded granules (shapefile index)",
    )


BUILDERS = {
    "local": local_mosaic,
    "remote": remote_mosaic,
    "upload": upload_mosaic,
}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def layer_bbox(
    client: GeoServerClient, workspace: str, store: str, coverage: str
) -> tuple[float, float, float, float]:
    """The coverage's own lat/lon extent, falling back to the demo box.

    Scattered granules cover far more ground than the tidy grid does, so a
    hardcoded bbox would frame the preview on empty space.
    """
    try:
        box = client.get_coverage(workspace, store, coverage).get("latLonBoundingBox")
        if box:
            return (
                float(box["minx"]), float(box["miny"]),
                float(box["maxx"]), float(box["maxy"]),
            )
    except (GeoServerError, KeyError, TypeError, ValueError):
        pass
    return DEMO_BBOX


def wms_url(
    settings: Settings,
    layer: str,
    *,
    time: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> str:
    """A GetMap URL, so the result can be eyeballed in a browser."""
    west, south, east, north = bbox or DEMO_BBOX
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetMap",
        "layers": layer,
        "bbox": f"{west},{south},{east},{north}",
        "width": "512",
        "height": "512",
        "srs": "EPSG:4326",
        "format": "image/png",
    }
    if time:
        params["time"] = time
    return f"{settings.wms_base}/wms?{urlencode(params)}"


#: COG community modules to probe for, as (label, manifest regex, what it enables).
COG_MODULES = [
    ("gs-cog-http", "gs-cog-http.*", "remote COG granules over HTTP"),
    ("gs-cog-s3", "gs-cog-s3.*", "s3:// granule URLs"),
]


def installed_modules(client: GeoServerClient, pattern: str) -> list[str]:
    """Names of deployed jars matching a manifest regex."""
    manifest = client.get_json("about/manifest.json", params={"manifest": pattern})
    resources = (manifest or {}).get("about", {}).get("resource", [])
    if isinstance(resources, dict):
        resources = [resources]
    return [str(r.get("@name")) for r in resources if isinstance(r, dict)]


def report(settings: Settings, manager: MosaicManager, result: MosaicResult) -> None:
    print(f"\n  layer      {result.layer}")
    print(f"  published  {result.published}")
    print(f"  harvested  {len(result.harvested)} granule location(s)")
    for location, message in result.failed:
        print(f"  FAILED     {location}\n             {message}")
    if not result.published:
        return
    try:
        count = manager.client.iter_granules(
            result.workspace, result.store, result.coverage
        )
        granules = list(count)
    except GeoServerError as exc:
        print(f"  index      unreadable: {exc}")
        return
    print(f"  index      {len(granules)} granule(s)")
    times = sorted(
        {
            str(g.get("properties", {}).get("time"))
            for g in granules
            if g.get("properties", {}).get("time")
        }
    )
    if times:
        print(f"  times      {', '.join(times)}")
    # One URL per time step: the default preview shows a single slice (the
    # newest), so without these it is easy to conclude the extra granules did
    # not land when they are simply at another date.
    box = layer_bbox(manager.client, result.workspace, result.store, result.coverage)
    pad = max((box[2] - box[0]), (box[3] - box[1])) * 0.05
    box = (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
    print(f"  extent     {box[0]:.2f},{box[1]:.2f},{box[2]:.2f},{box[3]:.2f}")
    print(f"  default    {wms_url(settings, result.layer, bbox=box)}")
    for stamp in times:
        print(f"  {stamp[:10]} {wms_url(settings, result.layer, time=stamp, bbox=box)}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_check(client: GeoServerClient, settings: Settings, args) -> int:
    version = client.version()
    features = client.features
    print(f"GeoServer  {version}  ({settings.url})")
    print(f"  major 3    {features.is_v3}")
    print(f"  COG        {features.supports_cog} (by version)")
    print(f"  CanBeEmpty {features.supports_can_be_empty}")

    # Version says COG *should* exist; confirm the modules are actually
    # deployed, since a missing extension creates stores happily and only fails
    # on read.  Each connection type is an independent package, so they are
    # probed and reported separately.
    for label, pattern, needed_for in COG_MODULES:
        try:
            found = installed_modules(client, pattern)
        except GeoServerError as exc:
            print(f"  {label:<10} could not check: {exc}")
            continue
        if found:
            print(f"  {label:<10} installed ({found[0]})")
        else:
            print(f"  {label:<10} NOT INSTALLED -- {needed_for} will fail")
            print(f"  {'':<10} add it to GS_COMMUNITY_EXTENSIONS and recreate the container")

    print(f"  workspaces {', '.join(client.list_workspaces()) or '(none)'}")
    return 0


def cmd_build(client: GeoServerClient, settings: Settings, args) -> int:
    manager = MosaicManager(client)
    failures = 0
    for name in args.which:
        builder = BUILDERS[name]
        definition = (
            builder(settings, args.range_reader) if name == "remote" else builder(settings)
        )
        print(f"\n=== {name}: {definition.title} ===")
        try:
            result = manager.create(definition, replace=args.replace)
        except GeoServerError as exc:
            print(f"  ERROR      {exc}")
            failures += 1
            continue
        report(settings, manager, result)
        if not result.ok:
            failures += 1
    if failures:
        print(f"\n{failures} of {len(args.which)} mosaic(s) had problems.")
    return 1 if failures else 0


def cmd_inspect(client: GeoServerClient, settings: Settings, args) -> int:
    manager = MosaicManager(client)
    for name in args.which:
        builder = BUILDERS[name]
        definition = (
            builder(settings, args.range_reader) if name == "remote" else builder(settings)
        )
        info = manager.describe(definition)
        print(f"\n=== {name} ===")
        for key in ("store", "store_exists", "coverage", "coverage_exists", "granule_count"):
            print(f"  {key:<15} {info[key]}")
        if info["coverage_exists"]:
            layer = f"{WORKSPACE}:{info['coverage']}"
            box = layer_bbox(client, WORKSPACE, info["store"], info["coverage"])
            print(f"  {'preview':<15} {wms_url(settings, layer, bbox=box)}")
    return 0


def cmd_clean(client: GeoServerClient, settings: Settings, args) -> int:
    if not client.workspace_exists(WORKSPACE):
        print(f"Workspace {WORKSPACE} does not exist; nothing to remove.")
        return 0
    # recurse=True drops the stores, coverages and layers with it.  Granule
    # files are untouched: no purge is requested anywhere in this driver.
    client.delete_workspace(WORKSPACE, recurse=True)
    print(f"Deleted workspace {WORKSPACE} (granule files left in place).")
    return 0


def cmd_all(client: GeoServerClient, settings: Settings, args) -> int:
    args.which = list(BUILDERS)
    status = cmd_check(client, settings, args)
    return cmd_build(client, settings, args) or status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", help="GeoServer base URL (default $GEOSERVER_URL)")
    parser.add_argument(
        "--range-reader",
        default=os.environ.get("COG_RANGE_READER", "HTTP"),
        choices=["HTTP", "S3"],
        help="COG range reader for the remote mosaic (default $COG_RANGE_READER, else HTTP)",
    )
    parser.add_argument(
        "--replace", action="store_true", help="delete and recreate existing stores"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "command",
        choices=["check", "local", "remote", "upload", "all", "inspect", "clean"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = Settings.from_env(args.url)
    args.which = [args.command] if args.command in BUILDERS else list(BUILDERS)

    handlers = {
        "check": cmd_check,
        "all": cmd_all,
        "inspect": cmd_inspect,
        "clean": cmd_clean,
    }
    handler = handlers.get(args.command, cmd_build)

    try:
        with GeoServerClient(settings.url, settings.user, settings.password) as client:
            return handler(client, settings, args)
    except GeoServerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (httpx.TransportError, OSError) as exc:
        # httpx raises its own TransportError hierarchy, which does not inherit
        # from OSError, so both have to be caught to report a down stack.
        print(f"ERROR: cannot reach {settings.url}: {exc}", file=sys.stderr)
        print("Is the stack up?  docker compose --profile gs2 up -d", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
