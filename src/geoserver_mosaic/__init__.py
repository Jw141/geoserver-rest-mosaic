"""A GeoServer REST client focused on ImageMosaic layers.

Supports GeoServer 2.28.x and 3.0.x from a single code path.  Granules may be
uploaded, referenced on the GeoServer host, or read remotely as COGs, and the
granule index may be a shapefile or PostGIS.

    from geoserver_mosaic import (
        GeoServerClient, MosaicManager, MosaicDefinition,
        PostgisIndex, CogSettings, TimeRegex,
    )

    with GeoServerClient("https://host/geoserver", "admin", "pw") as gs:
        mosaics = MosaicManager(gs)
        result = mosaics.create(MosaicDefinition(
            workspace="imagery",
            store="sentinel2",
            index=PostgisIndex(
                host="db", database="gis", user="gis", password="secret",
            ),
            cog=CogSettings(range_reader="S3"),
            time_regex=TimeRegex(
                regex=r"[0-9]{8}T[0-9]{6}", date_format="yyyyMMdd'T'HHmmss",
            ),
            granules=["s3://bucket/S2_20240301T104021.tif"],
            srs="EPSG:32633",
        ))
        print(result.layer, result.published)
"""

from .client import GeoServerClient
from .compat import Features, Version
from .errors import (
    AuthenticationError,
    ConflictError,
    GeoServerError,
    GeoServerHTTPError,
    MosaicConfigurationError,
    NotFoundError,
    UnsupportedVersionError,
)
from .models import (
    RANGE_READERS,
    CogSettings,
    DimensionInfo,
    ExistingIndexStore,
    IndexerConfig,
    IndexStore,
    PostgisIndex,
    ShapefileIndex,
    TimeRegex,
    time_dimension,
    timestamp_collector,
)
from .mosaic import (
    DEFAULT_COVERAGE_PARAMETERS,
    MosaicDefinition,
    MosaicManager,
    MosaicResult,
    build_config_archive,
    build_granule_archive,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_COVERAGE_PARAMETERS",
    "RANGE_READERS",
    "AuthenticationError",
    "CogSettings",
    "ConflictError",
    "DimensionInfo",
    "ExistingIndexStore",
    "Features",
    "GeoServerClient",
    "GeoServerError",
    "GeoServerHTTPError",
    "IndexStore",
    "IndexerConfig",
    "MosaicConfigurationError",
    "MosaicDefinition",
    "MosaicManager",
    "MosaicResult",
    "NotFoundError",
    "PostgisIndex",
    "ShapefileIndex",
    "TimeRegex",
    "UnsupportedVersionError",
    "Version",
    "__version__",
    "build_config_archive",
    "build_granule_archive",
    "time_dimension",
    "timestamp_collector",
]
