#!/usr/bin/env python3
"""Generate small COG granules for the demo mosaic.

Produces a time series of tiled RGB Cloud Optimized GeoTIFFs whose filenames
carry a timestamp, so the same files exercise every granule path the client
supports: uploaded from the client, read from the GeoServer host, or fetched
remotely from S3.

The layout is a spatial grid repeated at several timestamps, which is what makes
the result a genuine mosaic (tiles stitched side by side) with a time dimension
(one stitched image per date) rather than a pile of unrelated rasters.

Each date gets its own colour and a run of marker squares along the top edge --
one square per time step -- so a WMS preview at different ``time=`` values is
obviously different rather than subtly so.

**Writing is additive.** Existing granules are left alone unless the same
filename is regenerated, and nothing is ever deleted without ``--prune``. The
directory is bind-mounted into LocalStack, so it is only ever written through.

    # the base set: 3 dates x a 2x2 grid
    uv run --extra examples python examples/make_fixtures.py

    # add two more dates after whatever already exists, then re-seed
    uv run --extra examples python examples/make_fixtures.py --append --dates 2
    make seed-s3
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import re
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds

#: Filenames embed this timestamp; it is what TimeRegex matches on.
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"

#: Geographic extent covered by the whole mosaic (EPSG:4326).
WEST, SOUTH, EAST, NORTH = 10.0, 45.0, 14.0, 49.0

#: First timestamp of the demo series, and the reference the per-date colour is
#: derived from -- so a given date always renders the same way, whether it was
#: written in the first batch or appended later.
EPOCH = dt.datetime(2024, 3, 1, 10, 40, 21)

#: Days between consecutive time steps.
INTERVAL_DAYS = 5

#: Filenames look like S2_20240301T104021_r0c0.tif (grid) or
#: S2_20240301T104021_p03.tif (scatter).
NAME_RE = re.compile(r"S2_(\d{8}T\d{6})_([a-z0-9]+)\.tif$")

#: Default size of a scattered granule, in degrees.
TILE_DEGREES = 1.0

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "fixtures" / "tiles"


def tile_bounds(
    grid: int,
    row: int,
    col: int,
    extent: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float, float]:
    """Bounds of one cell in a ``grid`` x ``grid`` subdivision of the extent."""
    west_, south_, east_, north_ = extent or (WEST, SOUTH, EAST, NORTH)
    width = (east_ - west_) / grid
    height = (north_ - south_) / grid
    west = west_ + col * width
    # Row 0 is the northern row, matching raster row order.
    north = north_ - row * height
    return (west, north - height, west + width, north)


def scatter_bounds(
    rng: random.Random,
    tile_deg: float,
    extent: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """A random tile-sized box inside ``extent``.

    Granules may overlap or leave gaps, which is the point: a mosaic of
    scattered footprints exercises the reader far more than a neat tiling.
    """
    west, south, east, north = extent
    left = rng.uniform(west, max(west, east - tile_deg))
    bottom = rng.uniform(south, max(south, north - tile_deg))
    return (left, bottom, left + tile_deg, bottom + tile_deg)


def step_index(when: dt.datetime) -> int:
    """Which time step a timestamp is, counted from :data:`EPOCH`.

    Derived from the date rather than from a loop counter, so a given date
    always renders identically whether it was written in the first batch or
    appended months later.
    """
    return round((when - EPOCH).total_seconds() / (INTERVAL_DAYS * 86400))


def _hsv_to_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Vectorised HSV -> RGB, all channels in [0, 1]."""
    i = np.floor(h * 6).astype(int) % 6
    f = h * 6 - np.floor(h * 6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    conds = [i == n for n in range(6)]
    r = np.select(conds, [v, q, p, p, t, v])
    g = np.select(conds, [t, v, v, q, p, p])
    b = np.select(conds, [p, p, t, v, v, q])
    return np.stack([r, g, b])


def tile_pixels(size: int, shade: int, step: int, bands: int = 3) -> np.ndarray:
    """Render a pattern that identifies both the tile and its time step.

    Colour is one hue per time step, so every tile of a given date belongs to
    the same colour family and a single time slice reads as one coherent image.
    Hues are spaced by the golden angle, which keeps consecutive steps far apart
    on the colour wheel.

    Within a tile, saturation and value form a gradient, so the repeating
    gradient plus the dark border makes the tile grid legible. A run of marker
    squares along the top edge counts the time step, so you can read the date
    off the image without checking which ``time=`` you asked for.

    Channels are computed in floating point and scaled once, never wrapped --
    a modulo here would make tiles of the same date look unrelated.
    """
    y, x = np.mgrid[0:size, 0:size].astype(np.float32) / max(size - 1, 1)
    hue = np.full((size, size), (0.61803398875 * step) % 1.0, dtype=np.float32)
    saturation = 0.40 + 0.50 * x
    # A slight per-granule shade so a duplicated or misplaced one is spottable,
    # small enough that the date still reads as one colour.
    value = 0.55 + 0.40 * (1.0 - y) - 0.06 * (shade % 4)
    data = (_hsv_to_rgb(hue, saturation, value) * 255).clip(0, 255).astype(np.uint8)
    if bands != 3:
        data = np.repeat(data[:1], bands, axis=0)

    # Marker squares: one per time step.
    box = max(size // 16, 4)
    gap = max(box // 3, 2)
    for n in range(min(step + 1, (size - gap) // (box + gap))):
        left = gap + n * (box + gap)
        data[:, gap : gap + box, left : left + box] = 250

    # A darker border makes individual granule edges visible in the mosaic,
    # so a gap or a misplaced tile is obvious at a glance.
    edge = max(size // 64, 1)
    data[:, :edge, :] = data[:, -edge:, :] = 30
    data[:, :, :edge] = data[:, :, -edge:] = 30
    return data


def existing_timestamps(output: Path) -> list[dt.datetime]:
    """Timestamps already present in the fixtures directory, sorted."""
    stamps = set()
    for path in output.glob("*.tif"):
        match = NAME_RE.search(path.name)
        if match:
            stamps.add(dt.datetime.strptime(match.group(1), TIMESTAMP_FORMAT))
    return sorted(stamps)


def write_cog(path: Path, data: np.ndarray, bounds: tuple[float, float, float, float]) -> None:
    bands, height, width = data.shape
    profile = {
        "driver": "COG",
        "dtype": "uint8",
        "count": bands,
        "height": height,
        "width": width,
        "crs": CRS.from_epsg(4326),
        "transform": from_bounds(*bounds, width, height),
        "compress": "DEFLATE",
        # Block size drives the HTTP range requests the COG reader issues;
        # keeping it small means the demo exercises real partial reads.
        "blocksize": 256,
        "overview_resampling": "average",
    }
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(data)


def generate(
    output: Path,
    *,
    grid: int = 2,
    scatter: int = 0,
    tile_deg: float = TILE_DEGREES,
    extent: tuple[float, float, float, float] | None = None,
    seed: int = 0,
    dates: int = 3,
    size: int = 512,
    start: dt.datetime | None = None,
    append: bool = False,
    prune: bool = False,
    pruned: list[str] | None = None,
) -> list[Path]:
    """Write ``dates`` time steps of a ``grid`` x ``grid`` tiling.

    Additive by design.  The directory is bind-mounted into LocalStack, so it is
    only ever written through: granules with the same name are overwritten in
    place, granules from other dates are left alone, and nothing is removed
    unless ``prune`` is set.

    With ``append``, the series starts one interval after the newest granule
    already present, so repeated calls extend the time dimension instead of
    rewriting it.
    """
    output.mkdir(parents=True, exist_ok=True)
    pruned = pruned if pruned is not None else []

    if start is None:
        existing = existing_timestamps(output) if append else []
        start = (
            existing[-1] + dt.timedelta(days=INTERVAL_DAYS) if existing else EPOCH
        )

    extent = extent or (WEST, SOUTH, EAST, NORTH)

    written: list[Path] = []
    for phase in range(dates):
        when = start + dt.timedelta(days=INTERVAL_DAYS * phase)
        stamp = when.strftime(TIMESTAMP_FORMAT)
        step = step_index(when)

        if scatter:
            # Seeded per date, so a given date always lands in the same places
            # however often it is regenerated -- and appended dates land
            # somewhere new rather than retracing the first batch.
            rng = random.Random(f"{seed}:{step}")
            for n in range(scatter):
                path = output / f"S2_{stamp}_p{n:02d}.tif"
                write_cog(
                    path,
                    tile_pixels(size, n, step),
                    scatter_bounds(rng, tile_deg, extent),
                )
                written.append(path)
        else:
            for row in range(grid):
                for col in range(grid):
                    path = output / f"S2_{stamp}_r{row}c{col}.tif"
                    write_cog(
                        path,
                        tile_pixels(size, row + col, step),
                        tile_bounds(grid, row, col, extent),
                    )
                    written.append(path)

    if prune:
        # Opt-in only.  Granules outside the set just written -- typically left
        # over from a different --grid or --dates -- would otherwise be seeded
        # and harvested alongside the current ones.
        keep = {path.name for path in written}
        for stale in sorted(output.glob("*.tif")):
            if stale.name not in keep:
                stale.unlink()
                pruned.append(stale.name)
    return written


def covered_extent(paths: list[Path]) -> tuple[float, float, float, float] | None:
    """Union of the granules' bounds, for use as a WMS bbox."""
    boxes = []
    for path in paths:
        with rasterio.open(path) as dataset:
            boxes.append(dataset.bounds)
    if not boxes:
        return None
    return (
        min(b.left for b in boxes),
        min(b.bottom for b in boxes),
        max(b.right for b in boxes),
        max(b.top for b in boxes),
    )


def parse_start(value: str) -> dt.datetime:
    for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", TIMESTAMP_FORMAT):
        try:
            return dt.datetime.strptime(value, pattern)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"{value!r} is not a date; expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--grid", type=int, default=2, help="tiles per axis (grid layout)")
    parser.add_argument(
        "--scatter",
        type=int,
        metavar="N",
        default=0,
        help="place N granules at random points per date instead of tiling a "
        "grid, so the mosaic is sparse and spread out",
    )
    parser.add_argument(
        "--tile-deg",
        type=float,
        default=TILE_DEGREES,
        help=f"size of a scattered granule in degrees (default {TILE_DEGREES})",
    )
    parser.add_argument(
        "--extent",
        type=float,
        nargs=4,
        metavar=("W", "S", "E", "N"),
        default=[WEST, SOUTH, EAST, NORTH],
        help="region the granules cover or are scattered within",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="RNG seed for --scatter placement"
    )
    parser.add_argument("--dates", type=int, default=3, help="number of timestamps")
    parser.add_argument("--size", type=int, default=512, help="pixels per tile axis")
    parser.add_argument(
        "--start", type=parse_start, help="first timestamp (default: the demo epoch)"
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="start one interval after the newest granule already present, "
        "extending the time series instead of rewriting it",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="delete granules outside the set just written (off by default: "
        "generation is additive, delete files yourself when ready)",
    )
    args = parser.parse_args()

    before = {path.name for path in args.output.glob("*.tif")} if args.output.is_dir() else set()
    pruned: list[str] = []
    written = generate(
        args.output,
        grid=args.grid,
        scatter=args.scatter,
        tile_deg=args.tile_deg,
        extent=tuple(args.extent),
        seed=args.seed,
        dates=args.dates,
        size=args.size,
        start=args.start,
        append=args.append,
        prune=args.prune,
        pruned=pruned,
    )

    names = {path.name for path in written}
    added = sorted(names - before)
    total = sorted(args.output.glob("*.tif"))
    stamps = existing_timestamps(args.output)

    print(f"Wrote {len(written)} granule(s) to {args.output}")
    print(f"  new: {len(added)}   overwritten: {len(names) - len(added)}   kept: {len(before - names)}")
    if pruned:
        print(f"  pruned: {len(pruned)}")
    for name in added[:4]:
        print(f"    + {name}")
    if len(added) > 4:
        print(f"    + ... and {len(added) - 4} more")
    size_kib = sum(path.stat().st_size for path in total) / 1024
    print(f"  directory now holds {len(total)} granule(s), {size_kib:.0f} KiB")
    print(f"  time steps: {', '.join(s.strftime('%Y-%m-%d') for s in stamps)}")
    covered = covered_extent(total)
    if covered:
        west, south, east, north = covered
        print(f"  extent: {west:.2f},{south:.2f},{east:.2f},{north:.2f}")
    if not args.prune:
        print("  (additive -- delete files yourself, or pass --prune)")


if __name__ == "__main__":
    main()
