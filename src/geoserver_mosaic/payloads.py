"""XML request bodies for the GeoServer REST API.

Coverage configuration is sent as XML rather than JSON on purpose.  GeoServer
serialises ``<metadata>`` and ``<parameters>`` as repeated ``<entry>``
elements, and the JSON projection of that structure is both ambiguous (a
single entry collapses to an object, several become a list) and has shifted
between releases.  The XML form has been stable for years and is accepted
identically by 2.28 and 3.0, so it is the safer wire format for the parts of
the config that matter to a mosaic.

Simple resources with flat bodies (workspaces, store shells) are sent as JSON
by the client; only the structured ones live here.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

from .models import DimensionInfo


def _sub(parent: ET.Element, tag: str, text: Any = None) -> ET.Element:
    element = ET.SubElement(parent, tag)
    if text is not None:
        element.text = "true" if text is True else "false" if text is False else str(text)
    return element


def _render(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode")


def workspace(name: str, *, isolated: bool = False) -> str:
    root = ET.Element("workspace")
    _sub(root, "name", name)
    if isolated:
        _sub(root, "isolated", True)
    return _render(root)


def namespace(prefix: str, uri: str) -> str:
    root = ET.Element("namespace")
    _sub(root, "prefix", prefix)
    _sub(root, "uri", uri)
    return _render(root)


def coverage_store(
    name: str,
    workspace_name: str,
    *,
    store_type: str = "ImageMosaic",
    url: str | None = None,
    enabled: bool = True,
    description: str | None = None,
) -> str:
    root = ET.Element("coverageStore")
    _sub(root, "name", name)
    _sub(root, "type", store_type)
    _sub(root, "enabled", enabled)
    if description:
        _sub(root, "description", description)
    workspace_element = _sub(root, "workspace")
    _sub(workspace_element, "name", workspace_name)
    if url:
        _sub(root, "url", url)
    return _render(root)


def postgis_datastore(
    name: str,
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    schema: str = "public",
    extra: dict[str, Any] | None = None,
) -> str:
    """A standalone PostGIS store, usable as a shared mosaic index.

    Pair this with :class:`~geoserver_mosaic.models.ExistingIndexStore` so the
    mosaic references the store by name instead of embedding credentials.
    """
    root = ET.Element("dataStore")
    _sub(root, "name", name)
    _sub(root, "enabled", True)
    entries = {
        "dbtype": "postgis",
        "host": host,
        "port": port,
        "database": database,
        "schema": schema,
        "user": user,
        "passwd": password,
        "Expose primary keys": "true",
        "validate connections": "true",
        "Loose bbox": "true",
        "Estimated extends": "false",
        "preparedStatements": "true",
        **(extra or {}),
    }
    connection = _sub(root, "connectionParameters")
    for key, value in entries.items():
        if value is None:
            continue
        entry = _sub(connection, "entry", value)
        entry.set("key", key)
    return _render(root)


def _dimension(parent: ET.Element, key: str, info: DimensionInfo) -> None:
    entry = _sub(parent, "entry")
    entry.set("key", key)
    dimension = _sub(entry, "dimensionInfo")
    _sub(dimension, "enabled", info.enabled)
    _sub(dimension, "presentation", info.presentation)
    if info.units:
        _sub(dimension, "units", info.units)
    if info.unit_symbol:
        _sub(dimension, "unitSymbol", info.unit_symbol)
    if info.resolution:
        _sub(dimension, "resolution", info.resolution)
    default = _sub(dimension, "defaultValue")
    _sub(default, "strategy", info.default_strategy)
    if info.default_value is not None:
        _sub(default, "referenceValue", info.default_value)
    if info.nearest_match_enabled:
        _sub(dimension, "nearestMatchEnabled", True)


def coverage(
    name: str,
    *,
    native_name: str | None = None,
    title: str | None = None,
    abstract: str | None = None,
    srs: str | None = None,
    native_crs: str | None = None,
    enabled: bool = True,
    keywords: list[str] | None = None,
    dimensions: dict[str, DimensionInfo] | None = None,
    metadata: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
    native_bounding_box: tuple[float, float, float, float] | None = None,
    lat_lon_bounding_box: tuple[float, float, float, float] | None = None,
    projection_policy: str | None = None,
) -> str:
    """Build a ``<coverage>`` body.

    ``native_name`` must match the mosaic's ``Name`` from ``indexer.properties``
    -- that is how GeoServer links the published coverage to the index.
    Anything left as ``None`` is omitted so the server keeps its own value,
    which makes this body safe for PUT updates as well as POST creates.
    """
    root = ET.Element("coverage")
    _sub(root, "name", name)
    _sub(root, "nativeName", native_name or name)
    if title:
        _sub(root, "title", title)
    if abstract:
        _sub(root, "abstract", abstract)
    if srs:
        _sub(root, "srs", srs)
    if native_crs:
        _sub(root, "nativeCRS", native_crs)
    _sub(root, "enabled", enabled)
    if projection_policy:
        _sub(root, "projectionPolicy", projection_policy)

    if keywords:
        keyword_element = _sub(root, "keywords")
        for keyword in keywords:
            _sub(keyword_element, "string", keyword)

    for tag, box in (
        ("nativeBoundingBox", native_bounding_box),
        ("latLonBoundingBox", lat_lon_bounding_box),
    ):
        if box is None:
            continue
        box_element = _sub(root, tag)
        minx, miny, maxx, maxy = box
        _sub(box_element, "minx", minx)
        _sub(box_element, "maxx", maxx)
        _sub(box_element, "miny", miny)
        _sub(box_element, "maxy", maxy)
        _sub(box_element, "crs", srs or "EPSG:4326")

    if dimensions or metadata:
        metadata_element = _sub(root, "metadata")
        for key, value in (metadata or {}).items():
            entry = _sub(metadata_element, "entry", value)
            entry.set("key", key)
        for key, info in (dimensions or {}).items():
            _dimension(metadata_element, key, info)

    if parameters:
        parameters_element = _sub(root, "parameters")
        for key, value in parameters.items():
            entry = _sub(parameters_element, "entry")
            _sub(entry, "string", key)
            _sub(entry, "string", value)

    return _render(root)


def layer(
    *,
    default_style: str | None = None,
    enabled: bool | None = None,
    queryable: bool | None = None,
    advertised: bool | None = None,
) -> str:
    root = ET.Element("layer")
    if default_style:
        style = _sub(root, "defaultStyle")
        _sub(style, "name", default_style)
    if enabled is not None:
        _sub(root, "enabled", enabled)
    if queryable is not None:
        _sub(root, "queryable", queryable)
    if advertised is not None:
        _sub(root, "advertised", advertised)
    return _render(root)
