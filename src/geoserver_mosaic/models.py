"""Configuration objects for ImageMosaic stores.

These dataclasses render themselves into the ``.properties`` files that the
ImageMosaic reader consumes.  Keeping the knowledge here means the REST layer
never has to know what a "property collector" is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import MosaicConfigurationError

#: URL schemes that denote a granule GeoServer must fetch over the network
#: rather than open from its own filesystem.  These go to a different REST
#: endpoint, so the distinction is load-bearing rather than cosmetic.
REMOTE_SCHEMES = ("http://", "https://", "s3://", "gs://", "azure://", "wasb://")


def is_remote_location(location: str) -> bool:
    """True when a granule location is a remote URL rather than a local path."""
    return str(location).startswith(REMOTE_SCHEMES)


# --------------------------------------------------------------------------
# Index stores -- where the granule footprints are recorded
# --------------------------------------------------------------------------

#: The GeoTools factory that backs a PostGIS-indexed mosaic.
POSTGIS_SPI = "org.geotools.data.postgis.PostgisNGDataStoreFactory"


@dataclass
class PostgisIndex:
    """A PostGIS-backed granule index, configured inline.

    The connection details are written into ``datastore.properties`` inside the
    store.  If you would rather keep credentials out of the mosaic and manage
    them as a normal GeoServer store, use :class:`ExistingIndexStore` instead.
    """

    host: str
    database: str
    user: str
    password: str
    port: int = 5432
    schema: str = "public"

    # Connection pool / behaviour knobs.  These defaults follow the settings
    # GeoServer's own documentation recommends for mosaic indexes.
    loose_bbox: bool = True
    estimated_extends: bool = False
    validate_connections: bool = True
    connection_timeout: int = 10
    prepared_statements: bool = True
    max_connections: int | None = None
    min_connections: int | None = None
    fetch_size: int | None = None
    ssl_mode: str | None = None

    #: Extra raw keys merged into ``datastore.properties`` verbatim.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_properties(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "SPI": POSTGIS_SPI,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "schema": self.schema,
            "user": self.user,
            "passwd": self.password,
            # Keys below contain spaces; the properties writer escapes them.
            "Loose bbox": self.loose_bbox,
            "Estimated extends": self.estimated_extends,
            "validate connections": self.validate_connections,
            "Connection timeout": self.connection_timeout,
            "preparedStatements": self.prepared_statements,
            "max connections": self.max_connections,
            "min connections": self.min_connections,
            "fetch size": self.fetch_size,
            "SSL mode": self.ssl_mode,
        }
        props.update(self.extra)
        return props


@dataclass
class ExistingIndexStore:
    """Reference a PostGIS store already configured in GeoServer.

    The mosaic then borrows that store's connection pool, so credentials live
    in one place and are not duplicated into every mosaic's data directory.
    """

    #: Either ``"store"`` or ``"workspace:store"``.
    store_name: str

    def to_properties(self) -> dict[str, Any]:
        return {"StoreName": self.store_name}


@dataclass
class ShapefileIndex:
    """The default file-based index.

    Fine for small, static mosaics; it does not survive concurrent harvesting
    well, which is why PostGIS is the recommended choice for anything that
    grows over time.
    """

    def to_properties(self) -> dict[str, Any]:
        return {}


IndexStore = PostgisIndex | ExistingIndexStore | ShapefileIndex


# --------------------------------------------------------------------------
# Cloud Optimized GeoTIFF access
# --------------------------------------------------------------------------

#: Range readers shipped with GeoServer's COG support, keyed by scheme.
RANGE_READERS: dict[str, str] = {
    "HTTP": "it.geosolutions.imageioimpl.plugins.cog.HttpRangeReader",
    "S3": "it.geosolutions.imageioimpl.plugins.cog.S3RangeReader",
    "AZURE": "it.geosolutions.imageioimpl.plugins.cog.AzureRangeReader",
    "GS": "it.geosolutions.imageioimpl.plugins.cog.GSRangeReader",
}

RangeReader = Literal["HTTP", "S3", "AZURE", "GS"]


@dataclass
class CogSettings:
    """Enable remote COG granule access for the mosaic.

    Requires the ``gs-cog`` community/extension module to be installed on the
    server; without it the store creation succeeds but granule reads fail.
    """

    range_reader: RangeReader = "HTTP"
    #: Basic-auth credentials for the object store, when it needs them.
    user: str | None = None
    password: str | None = None
    #: Cache fetched byte ranges in memory.  Helps repeated reads of the same
    #: granule, costs heap.
    use_caching_stream: bool = False

    def to_properties(self) -> dict[str, Any]:
        reader = RANGE_READERS.get(self.range_reader.upper())
        if reader is None:
            raise MosaicConfigurationError(
                f"Unknown COG range reader {self.range_reader!r}; "
                f"expected one of {sorted(RANGE_READERS)}"
            )
        return {
            "Cog": True,
            "CogRangeReader": reader,
            "CogUser": self.user,
            "CogPassword": self.password,
            "CogUseCachingStream": self.use_caching_stream,
        }


# --------------------------------------------------------------------------
# Dimensions
# --------------------------------------------------------------------------


@dataclass
class TimeRegex:
    """Extract a timestamp from each granule filename.

    ``regex`` matches the timestamp substring; ``date_format`` is the Java
    ``SimpleDateFormat`` pattern that parses it.  Omit ``date_format`` only if
    the timestamps are already in a format GeoServer recognises unaided.
    """

    regex: str
    date_format: str | None = None
    #: Match against the full path rather than just the filename.
    full_path: bool = False

    #: Filename the regex is written to inside the store.
    filename: str = "timeregex.properties"

    def to_properties(self) -> dict[str, Any]:
        props: dict[str, Any] = {"regex": self.regex}
        if self.date_format:
            props["format"] = self.date_format
        if self.full_path:
            props["fullPath"] = True
        return props


@dataclass
class DimensionInfo:
    """A WMS dimension exposed on the published layer."""

    enabled: bool = True
    presentation: Literal["LIST", "CONTINUOUS_INTERVAL", "DISCRETE_INTERVAL"] = "LIST"
    units: str | None = None
    unit_symbol: str | None = None
    #: One of MINIMUM, MAXIMUM, NEAREST, FIXED -- which value a client gets
    #: when it does not ask for a specific one.
    default_strategy: Literal["MINIMUM", "MAXIMUM", "NEAREST", "FIXED"] = "MAXIMUM"
    default_value: str | None = None
    #: Only meaningful for the interval presentations.
    resolution: str | None = None
    #: Emit nearest-match support (GeoServer 2.15+).
    nearest_match_enabled: bool = False


def time_dimension(
    presentation: str = "LIST",
    default_strategy: str = "MAXIMUM",
    **kwargs: Any,
) -> DimensionInfo:
    """A time dimension with the ISO8601 units GeoServer expects."""
    return DimensionInfo(
        presentation=presentation,  # type: ignore[arg-type]
        units="ISO8601",
        default_strategy=default_strategy,  # type: ignore[arg-type]
        **kwargs,
    )


# --------------------------------------------------------------------------
# The indexer itself
# --------------------------------------------------------------------------


@dataclass
class IndexerConfig:
    """Contents of ``indexer.properties``.

    The defaults describe a time-enabled mosaic over absolute granule paths,
    which is what both the remote-COG and the local-file cases need.
    """

    #: Mosaic name.  This becomes the coverage's ``nativeName`` and, for a
    #: PostGIS index, the name of the granule table.
    name: str

    #: Feature type of the index table.  ``location`` and the geometry column
    #: are mandatory; add one attribute per extra dimension.
    schema: str = "*the_geom:Polygon,location:String,time:java.util.Date"

    #: Attribute holding the timestamp.  Set to ``None`` for a mosaic with no
    #: time dimension.
    time_attribute: str | None = "time"
    elevation_attribute: str | None = None

    #: Store absolute granule locations.  Required for remote COG URLs, and
    #: for local granules that live outside the store directory.
    absolute_path: bool = True

    #: Allow the store to be created before any granule exists.  Without this
    #: an empty mosaic is rejected, which makes bootstrap-then-harvest
    #: workflows impossible.
    can_be_empty: bool = True

    #: Reuse an index table that already exists instead of creating one.  Set
    #: this when pointing several mosaics at a shared table, or when the table
    #: was provisioned by a migration.
    use_existing_schema: bool = False

    #: Keep the granule index in memory.  Must be false for a mosaic that is
    #: harvested while running, or new granules stay invisible.
    caching: bool = False

    #: Declared CRS for the mosaic; granules are expected to match.
    mosaic_crs: str | None = None

    #: Recurse into subdirectories when harvesting a directory.
    recursive: bool = True
    wildcard: str | None = None

    #: ``PropertyCollector`` specs.  Usually generated from the time regex, but
    #: can be supplied directly for custom dimensions.
    property_collectors: list[str] = field(default_factory=list)

    #: Extra raw keys merged verbatim, for anything not modelled above.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_properties(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "Name": self.name,
            "Schema": self.schema,
            "TimeAttribute": self.time_attribute,
            "ElevationAttribute": self.elevation_attribute,
            "AbsolutePath": self.absolute_path,
            "CanBeEmpty": self.can_be_empty,
            "UseExistingSchema": self.use_existing_schema,
            "Caching": self.caching,
            "MosaicCRS": self.mosaic_crs,
            "Recursive": self.recursive,
            "Wildcard": self.wildcard,
        }
        if self.property_collectors:
            props["PropertyCollectors"] = ",".join(self.property_collectors)
        props.update(self.extra)
        return props

    def validate(self) -> None:
        """Catch configurations the mosaic reader would reject at runtime."""
        if self.time_attribute and self.time_attribute not in self.schema:
            raise MosaicConfigurationError(
                f"TimeAttribute {self.time_attribute!r} is not present in the "
                f"index schema {self.schema!r}; add it, e.g. "
                f"'...,{self.time_attribute}:java.util.Date'"
            )
        if self.elevation_attribute and self.elevation_attribute not in self.schema:
            raise MosaicConfigurationError(
                f"ElevationAttribute {self.elevation_attribute!r} is not "
                f"present in the index schema {self.schema!r}"
            )
        if "location" not in self.schema:
            raise MosaicConfigurationError(
                "The index schema must contain a 'location:String' attribute"
            )


def timestamp_collector(
    attribute: str = "time",
    regex_file: str = "timeregex",
) -> str:
    """A collector that fills ``attribute`` from the filename timestamp regex.

    ``regex_file`` names the properties file without its extension, matching
    the convention the mosaic reader uses to resolve collector arguments.
    """
    return f"TimestampFileNameExtractorSPI[{regex_file}]({attribute})"
