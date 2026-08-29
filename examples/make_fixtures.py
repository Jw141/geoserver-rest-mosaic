#!/usr/bin/env python3
"""Generate small COG granules for the demo mosaic.

Produces a time series of tiled RGB Cloud Optimized GeoTIFFs whose filenames
carry a timestamp, so the same files exercise every granule path the client
supports: uploaded from the client, read from the GeoServer host, or fetched
remotely from S3.

The layout is a spatial grid repeated at several timestamps, which is what makes
the result a genuine mosaic (tiles stitched side by side) with a time dimension
(one stitched image per date) rather than a pile of unrelated rasters.

    uv run --extra examples python examples/make_fixtures.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds

#: Filenames embed this timestamp; it is what TimeRegex matches on.
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S"

#: Geographic extent covered by the whole mosaic (EPSG:4326).
WEST, SOUTH, EAST, NORTH = 10.0, 45.0, 14.0, 49.0

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "fixtures" / "tiles"


def tile_bounds(grid: int, row: int, col: int) -> tuple[float, float, float, float]:
    """Bounds of one cell in a ``grid`` x ``grid`` subdivision of the extent."""
    width = (EAST - WEST) / grid
    height = (NORTH - SOUTH) / grid
    west = WEST + col * width
    # Row 0 is the northern row, matching raster row order.
    north = NORTH - row * height
    return (west, north - height, west + width, north)


def tile_pixels(size: int, row: int, col: int, phase: int, bands: int = 3) -> np.ndarray:
    """Render a recognisable pattern.

    The gradient identifies the tile's position and the ``phase`` shifts colour
    per timestamp, so a WMS request for a specific time is visibly different
    from its neighbours -- which is how you confirm the time dimension works
    rather than merely that it was accepted.
    """
    y, x = np.mgrid[0:size, 0:size].astype(np.float32) / max(size - 1, 1)
    data = np.zeros((bands, size, size), dtype=np.uint8)
    data[0] = (40 + 200 * x) % 256
    data[1] = (40 + 200 * y) % 256
    data[2] = (40 + 60 * (row + col) + 50 * phase) % 256
    # A darker border makes individual granule edges visible in the mosaic,
    # so a gap or a misplaced tile is obvious at a glance.
    edge = max(size // 64, 1)
    data[:, :edge, :] = data[:, -edge:, :] = 30
    data[:, :, :edge] = data[:, :, -edge:] = 30
    return data


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
    clean: bool = True,
) -> list[Path]:
    if clean and output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    start = start or dt.datetime(2024, 3, 1, 10, 40, 21)
    written: list[Path] = []
    for phase in range(dates):
        # Five days apart, so the time dimension has clearly separate instants.
        stamp = (start + dt.timedelta(days=5 * phase)).strftime(TIMESTAMP_FORMAT)
        for row in range(grid):
            for col in range(grid):
                path = output / f"S2_{stamp}_r{row}c{col}.tif"
                write_cog(path, tile_pixels(size, row, col, phase), tile_bounds(grid, row, col))
                written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--grid", type=int, default=2, help="tiles per axis")
    parser.add_argument("--dates", type=int, default=3, help="number of timestamps")
    parser.add_argument("--size", type=int, default=512, help="pixels per tile axis")
    args = parser.parse_args()

    written = generate(args.output, grid=args.grid, dates=args.dates, size=args.size)
    total = sum(path.stat().st_size for path in written)
    print(f"Wrote {len(written)} granules to {args.output} ({total / 1024:.0f} KiB)")
    for path in written[:4]:
        print(f"  {path.name}")
    if len(written) > 4:
        print(f"  ... and {len(written) - 4} more")


if __name__ == "__main__":
    main()
