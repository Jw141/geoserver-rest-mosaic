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

#: Filenames look like S2_20240301T104021_r0c0.tif.
NAME_RE = re.compile(r"S2_(\d{8}T\d{6})_r(\d+)c(\d+)\.tif$")

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "fixtures" / "tiles"


def tile_bounds(grid: int, row: int, col: int) -> tuple[float, float, float, float]:
    """Bounds of one cell in a ``grid`` x ``grid`` subdivision of the extent."""
    width = (EAST - WEST) / grid
    height = (NORTH - SOUTH) / grid
    west = WEST + col * width
    # Row 0 is the northern row, matching raster row order.
    north = NORTH - row * height
    return (west, north - height, west + width, north)


def step_index(when: dt.datetime) -> int:
    """Which time step a timestamp is, counted from :data:`EPOCH`.

    Derived from the date rather than from a loop counter, so a given date
    always renders identically whether it was written in the first batch or
    appended months later.
    """
    return round((when - EPOCH).total_seconds() / (INTERVAL_DAYS * 86400))


def tile_pixels(size: int, row: int, col: int, step: int, bands: int = 3) -> np.ndarray:
    """Render a pattern that identifies both the tile and its time step.

    The gradient shows the tile's position within the grid, the colour shifts
    per time step, and a run of marker squares along the top edge counts the
    step out explicitly -- so a WMS preview at two different ``time=`` values is
    unmistakably different rather than subtly so.
    """
    y, x = np.mgrid[0:size, 0:size].astype(np.float32) / max(size - 1, 1)
    shift = 47 * step
    data = np.zeros((bands, size, size), dtype=np.uint8)
    data[0] = (40 + 200 * x + shift) % 256
    data[1] = (40 + 200 * y + 2 * shift) % 256
    data[2] = (40 + 60 * (row + col) + 3 * shift) % 256

    # Marker squares: one per time step, so you can read the step off the
    # image without checking which time= you requested.
    box = max(size // 16, 4)
    gap = max(box // 3, 2)
    for n in range(min(step + 1, (size - gap) // (box + gap))):
        left = gap + n * (box + gap)
        data[:, gap : gap + box, left : left + box] = 245

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

    written: list[Path] = []
    for phase in range(dates):
        when = start + dt.timedelta(days=INTERVAL_DAYS * phase)
        stamp = when.strftime(TIMESTAMP_FORMAT)
        step = step_index(when)
        for row in range(grid):
            for col in range(grid):
                path = output / f"S2_{stamp}_r{row}c{col}.tif"
                write_cog(path, tile_pixels(size, row, col, step), tile_bounds(grid, row, col))
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
    parser.add_argument("--grid", type=int, default=2, help="tiles per axis")
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
    if not args.prune:
        print("  (additive -- delete files yourself, or pass --prune)")


if __name__ == "__main__":
    main()
