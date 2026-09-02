"""High-level ImageMosaic creation.

The bootstrap problem this module solves: an ImageMosaic store cannot be
configured from nothing, but the granules you want in it may live on S3 or on
the GeoServer host, where they cannot be uploaded through the REST API.  The
sequence that works for every granule location is:

1. Build a ZIP holding only the mosaic's ``.properties`` files and PUT it to
   ``file.imagemosaic?configure=none``.  ``CanBeEmpty=true`` in the indexer
   lets the store come up with an empty index, and ``configure=none`` stops
   GeoServer publishing a layer before the config is complete.
2. Harvest granules by location, whichever scheme they use.
3. POST the coverage explicitly, with dimensions and reader parameters.

Doing it in that order means the same code path serves uploaded files, files
already on the GeoServer host, and remote COGs.
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import payloads, properties
from .client import GeoServerClient
from .errors import GeoServerError, MosaicConfigurationError, NotFoundError
from .models import (
    CogSettings,
    DimensionInfo,
    IndexerConfig,
    IndexStore,
    ShapefileIndex,
    TimeRegex,
    is_remote_location,
    time_dimension,
    timestamp_collector,
)

log = logging.getLogger("geoserver_mosaic")

#: Reader parameters that are almost always right for a tiled mosaic.  Callers
#: can override or extend them through ``MosaicDefinition.parameters``.
DEFAULT_COVERAGE_PARAMETERS: dict[str, Any] = {
    "AllowMultithreading": True,
    "USE_JAI_IMAGEREAD": False,
    "SUGGESTED_TILE_SIZE": "512,512",
}


@dataclass
class MosaicDefinition:
    """Everything needed to stand up one ImageMosaic layer."""

    workspace: str
    store: str

    #: Granule index backend.  PostGIS is strongly preferred for anything that
    #: will be harvested over time.
    index: IndexStore = field(default_factory=ShapefileIndex)

    #: Published layer name.  Defaults to the store name.
    coverage: str | None = None

    #: Mosaic name written to ``indexer.properties``.  For a PostGIS index this
    #: is also the granule table name.  Defaults to the store name.
    mosaic_name: str | None = None

    #: Remote COG access.  Leave as ``None`` for plain local granules.
    cog: CogSettings | None = None

    #: Timestamp extraction from granule filenames.
    time_regex: TimeRegex | None = None

    #: Set explicitly to override the derived indexer; otherwise one is built
    #: from ``mosaic_name``, ``time_regex`` and ``cog``.
    indexer: IndexerConfig | None = None

    #: Locations to harvest after the store exists: remote URLs, or absolute
    #: paths / ``file:`` URLs that the GeoServer process can resolve.
    granules: Sequence[str] = ()

    #: Local files uploaded inside the configuration ZIP.  Only for granules on
    #: the machine running this client, not on the GeoServer host.
    upload_files: Sequence[Path] = ()

    #: Layer metadata.
    srs: str = "EPSG:4326"
    title: str | None = None
    abstract: str | None = None
    keywords: Sequence[str] = ()
    default_style: str | None = None

    #: WMS dimensions.  A time dimension is added automatically when the
    #: indexer has a time attribute and this is left empty.
    dimensions: dict[str, DimensionInfo] = field(default_factory=dict)

    #: Coverage reader parameters, merged over :data:`DEFAULT_COVERAGE_PARAMETERS`.
    parameters: dict[str, Any] = field(default_factory=dict)

    #: Extra files placed in the store directory alongside the properties
    #: files, e.g. a custom ``*.sld`` or an auxiliary regex.
    extra_files: dict[str, str] = field(default_factory=dict)

    def resolved_coverage(self) -> str:
        return self.coverage or self.store

    def resolved_mosaic_name(self) -> str:
        return self.mosaic_name or self.store

    def resolved_indexer(self) -> IndexerConfig:
        """Build the indexer if one was not supplied explicitly."""
        if self.indexer is not None:
            indexer = self.indexer
        else:
            collectors: list[str] = []
            time_attribute: str | None = None
            if self.time_regex is not None:
                time_attribute = "time"
                regex_stem = Path(self.time_regex.filename).stem
                collectors.append(timestamp_collector(time_attribute, regex_stem))
            indexer = IndexerConfig(
                name=self.resolved_mosaic_name(),
                time_attribute=time_attribute,
                property_collectors=collectors,
                mosaic_crs=self.srs,
            )
            if time_attribute is None:
                # Drop the time column from the default schema so the index
                # table matches what the collectors actually populate.
                indexer.schema = "*the_geom:Polygon,location:String"
        indexer.validate()
        return indexer

    def resolved_dimensions(self) -> dict[str, DimensionInfo]:
        if self.dimensions:
            return dict(self.dimensions)
        indexer = self.resolved_indexer()
        dimensions: dict[str, DimensionInfo] = {}
        if indexer.time_attribute:
            dimensions["time"] = time_dimension()
        if indexer.elevation_attribute:
            dimensions["elevation"] = DimensionInfo(units="EPSG:5030")
        return dimensions

    def validate(self) -> None:
        """Reject configurations that would fail confusingly on the server."""
        indexer = self.resolved_indexer()
        if self.cog is not None and not indexer.absolute_path:
            raise MosaicConfigurationError(
                "COG granules are addressed by URL, so the indexer must set "
                "absolute_path=True"
            )
        if indexer.time_attribute and self.time_regex is None and not indexer.property_collectors:
            raise MosaicConfigurationError(
                f"Indexer declares TimeAttribute {indexer.time_attribute!r} but "
                "no time_regex or property collector was given, so nothing "
                "would populate it"
            )
        missing = [str(p) for p in self.upload_files if not Path(p).is_file()]
        if missing:
            raise MosaicConfigurationError(f"upload_files not found: {missing}")
        remote = [g for g in self.granules if is_remote_location(g)]
        if remote and self.cog is None:
            raise MosaicConfigurationError(
                f"Granule locations {remote[:3]} are remote URLs but no "
                "CogSettings were provided; the mosaic reader cannot open them"
            )


def build_config_archive(definition: MosaicDefinition) -> bytes:
    """Render the mosaic's configuration files into an in-memory ZIP.

    Returns the bytes to PUT at ``file.imagemosaic``.
    """
    indexer = definition.resolved_indexer()
    indexer_props = indexer.to_properties()
    if definition.cog is not None:
        indexer_props.update(definition.cog.to_properties())

    members: dict[str, str] = {
        "indexer.properties": properties.dumps(
            indexer_props, header="Generated by geoserver-rest-mosaic"
        )
    }

    index_props = definition.index.to_properties()
    if index_props:
        members["datastore.properties"] = properties.dumps(index_props)

    if definition.time_regex is not None:
        members[definition.time_regex.filename] = properties.dumps(
            definition.time_regex.to_properties()
        )

    members.update(definition.extra_files)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)
        for path in definition.upload_files:
            archive.write(path, arcname=Path(path).name)
    return buffer.getvalue()


@dataclass
class MosaicResult:
    """Outcome of a :meth:`MosaicManager.create` call."""

    workspace: str
    store: str
    coverage: str
    #: True when the coverage (and therefore the WMS/WCS layer) exists.
    published: bool
    #: Locations successfully harvested into the index.
    harvested: list[str] = field(default_factory=list)
    #: ``(location, message)`` for granules that could not be harvested.
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def layer(self) -> str:
        """Qualified layer name, as used in WMS requests."""
        return f"{self.workspace}:{self.coverage}"

    @property
    def ok(self) -> bool:
        return self.published and not self.failed


class MosaicManager:
    """Mosaic-oriented operations on top of :class:`GeoServerClient`."""

    def __init__(self, client: GeoServerClient) -> None:
        self.client = client

    # -- creation ----------------------------------------------------------

    def create(
        self,
        definition: MosaicDefinition,
        *,
        create_workspace: bool = True,
        publish: bool = True,
        replace: bool = False,
        purge: str | None = None,
    ) -> "MosaicResult":
        """Create the store, harvest granules and publish the coverage.

        ``replace=True`` deletes an existing store of the same name first;
        without it, creating over an existing store updates its configuration
        in place, which GeoServer permits but which will not remove indexer
        keys that are no longer set.

        Note that deleting a store does **not** remove its data directory, and
        for a PostGIS-indexed mosaic it does not drop the index table either.
        Recreating a store under the same name therefore inherits whatever was
        left behind, which can leave the mosaic unable to initialise ("Failed to
        create reader from file:data/..."). Pass ``purge="all"`` to have
        GeoServer delete the store's files too, or use a fresh store name. Never
        pass ``purge="all"`` for a mosaic whose granules are files you need to
        keep -- it deletes them.

        Publishing is skipped when the mosaic ends up with no granules, since
        GeoServer derives the coverage's bands and envelope from the index and
        cannot describe an empty one.
        """
        definition.validate()
        workspace = definition.workspace
        store = definition.store

        if create_workspace:
            self.client.create_workspace(workspace, exist_ok=True)

        if replace and self.client.coverage_store_exists(workspace, store):
            log.info("Replacing existing store %s:%s", workspace, store)
            # Empty the granule index first.  Deleting a store leaves its index
            # behind -- for PostGIS the table simply survives -- so a recreated
            # mosaic would otherwise start out holding every granule of the old
            # one, including granules whose files are long gone.  That reads as
            # a successful build with a wrong granule count.
            self._empty_index(workspace, store, definition.resolved_coverage())
            self.client.delete_coverage_store(
                workspace, store, recurse=True, purge=purge
            )

        archive = build_config_archive(definition)
        self.client.upload_mosaic_archive(workspace, store, archive, configure="none")

        harvested, failed = self.add_granules(workspace, store, definition.granules)

        coverage_name = definition.resolved_coverage()
        published = False
        if publish:
            if self._index_is_populated(definition, harvested):
                self.publish_coverage(definition)
                published = True
            else:
                log.warning(
                    "Not publishing %s:%s -- the mosaic index is empty. Harvest "
                    "at least one granule, then call publish_coverage().",
                    workspace,
                    coverage_name,
                )

        return MosaicResult(
            workspace=workspace,
            store=store,
            coverage=coverage_name,
            published=published,
            harvested=harvested,
            failed=failed,
        )

    def _empty_index(self, workspace: str, store: str, coverage: str) -> None:
        """Drop every granule from a mosaic's index, if it has one yet.

        Best-effort: a store created but never populated has no coverage and no
        index, which is not an error here.  ``purge`` is deliberately not set --
        this removes index entries, never granule files.
        """
        if not self.client.coverage_exists(workspace, store, coverage):
            return
        try:
            self.client.delete_granules(workspace, store, coverage, purge=False)
            log.debug("Emptied granule index for %s:%s", workspace, coverage)
        except GeoServerError as exc:
            # Worth surfacing: the recreated mosaic may inherit stale granules.
            log.warning(
                "Could not empty the granule index for %s:%s (%s). The "
                "recreated mosaic may still hold granules from the old one.",
                workspace,
                coverage,
                exc,
            )

    def _index_is_populated(
        self, definition: MosaicDefinition, harvested: list[str]
    ) -> bool:
        if harvested or definition.upload_files:
            return True
        # A store built over a pre-existing index table starts non-empty.
        return definition.resolved_indexer().use_existing_schema

    def publish_coverage(self, definition: MosaicDefinition) -> None:
        """Create or update the coverage that exposes the mosaic as a layer."""
        indexer = definition.resolved_indexer()
        body = payloads.coverage(
            definition.resolved_coverage(),
            # The native name is how GeoServer finds the mosaic inside the
            # store; it must equal the indexer's Name.
            native_name=indexer.name,
            title=definition.title or definition.resolved_coverage(),
            abstract=definition.abstract,
            srs=definition.srs,
            keywords=list(definition.keywords) or None,
            dimensions=definition.resolved_dimensions(),
            parameters={**DEFAULT_COVERAGE_PARAMETERS, **definition.parameters},
        )
        workspace, store = definition.workspace, definition.store
        coverage = definition.resolved_coverage()
        if self.client.coverage_exists(workspace, store, coverage):
            self.client.update_coverage(workspace, store, coverage, body)
            log.info("Updated coverage %s:%s", workspace, coverage)
        else:
            self.client.create_coverage(workspace, store, body)
            log.info("Published coverage %s:%s", workspace, coverage)

        if definition.default_style:
            self.client.set_default_style(workspace, coverage, definition.default_style)

    # -- granules ----------------------------------------------------------

    def add_granules(
        self,
        workspace: str,
        store: str,
        locations: Iterable[str],
        *,
        stop_on_error: bool = False,
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Harvest granules one at a time.

        Returns ``(harvested, failures)`` where each failure is a
        ``(location, message)`` pair.  Harvesting continues past a bad granule
        by default: in a bulk load, one unreadable file should not cost you the
        other several hundred.
        """
        harvested: list[str] = []
        failures: list[tuple[str, str]] = []
        for location in locations:
            try:
                self.client.harvest(workspace, store, location)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                log.error("Failed to harvest %s: %s", location, exc)
                failures.append((location, str(exc)))
                if stop_on_error:
                    raise
            else:
                harvested.append(location)
        return harvested, failures

    def granule_count(self, definition: MosaicDefinition) -> int:
        """Number of granules currently in the index."""
        return sum(
            1
            for _ in self.client.iter_granules(
                definition.workspace, definition.store, definition.resolved_coverage()
            )
        )

    def remove_granules(
        self, definition: MosaicDefinition, *, filter: str, purge: bool = False
    ) -> None:
        """Drop granules matching a CQL ``filter`` from the index.

        ``filter`` is mandatory here even though the REST API allows omitting
        it, because "delete every granule" is rarely what a caller means and is
        not recoverable.
        """
        if not filter:
            raise MosaicConfigurationError(
                "A CQL filter is required; to empty the index deliberately, "
                "call client.delete_granules() directly."
            )
        self.client.delete_granules(
            definition.workspace,
            definition.store,
            definition.resolved_coverage(),
            filter=filter,
            purge=purge,
        )

    # -- inspection --------------------------------------------------------

    def describe(self, definition: MosaicDefinition) -> dict[str, Any]:
        """Summarise the live state of the mosaic, for diagnostics."""
        workspace = definition.workspace
        store = definition.store
        coverage = definition.resolved_coverage()
        info: dict[str, Any] = {
            "workspace": workspace,
            "store": store,
            "coverage": coverage,
            "store_exists": self.client.coverage_store_exists(workspace, store),
            "coverage_exists": False,
            "index_schema": None,
            "granule_count": None,
        }
        if not info["store_exists"]:
            return info
        info["coverage_exists"] = self.client.coverage_exists(workspace, store, coverage)
        if info["coverage_exists"]:
            try:
                info["index_schema"] = self.client.index_schema(workspace, store, coverage)
                info["granule_count"] = self.granule_count(definition)
            except NotFoundError:
                # A published coverage whose index is gone: worth surfacing as
                # "unknown" rather than crashing a diagnostic call.
                pass
        return info
