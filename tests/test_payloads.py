from xml.etree import ElementTree as ET

from geoserver_mosaic import payloads
from geoserver_mosaic.models import DimensionInfo, time_dimension


def parse(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def test_coverage_omits_unset_fields_so_puts_are_safe():
    root = parse(payloads.coverage("c"))
    assert {child.tag for child in root} == {"name", "nativeName", "enabled"}


def test_native_name_defaults_to_the_coverage_name():
    root = parse(payloads.coverage("c"))
    assert root.findtext("nativeName") == "c"
    assert parse(payloads.coverage("c", native_name="mosaic")).findtext("nativeName") == "mosaic"


def test_dimension_entry_shape():
    root = parse(payloads.coverage("c", dimensions={"time": time_dimension()}))
    entry = root.find("metadata/entry")
    assert entry.get("key") == "time"
    assert entry.findtext("dimensionInfo/units") == "ISO8601"
    assert entry.findtext("dimensionInfo/defaultValue/strategy") == "MAXIMUM"


def test_fixed_default_carries_a_reference_value():
    dimension = DimensionInfo(default_strategy="FIXED", default_value="2024-01-01T00:00:00Z")
    root = parse(payloads.coverage("c", dimensions={"time": dimension}))
    assert root.findtext("metadata/entry/dimensionInfo/defaultValue/referenceValue") == (
        "2024-01-01T00:00:00Z"
    )


def test_parameters_render_as_string_pairs():
    root = parse(payloads.coverage("c", parameters={"AllowMultithreading": True}))
    strings = [element.text for element in root.findall("parameters/entry/string")]
    assert strings == ["AllowMultithreading", "true"]


def test_bounding_box_uses_the_declared_srs():
    root = parse(
        payloads.coverage("c", srs="EPSG:3857", lat_lon_bounding_box=(0, 1, 2, 3))
    )
    assert root.findtext("latLonBoundingBox/crs") == "EPSG:3857"


def test_postgis_datastore_connection_parameters():
    root = parse(
        payloads.postgis_datastore(
            "index", host="db", port=5432, database="gis", user="u", password="p"
        )
    )
    entries = {e.get("key"): e.text for e in root.findall("connectionParameters/entry")}
    assert entries["dbtype"] == "postgis"
    assert entries["passwd"] == "p"
    assert entries["port"] == "5432"


def test_coverage_store_declares_the_imagemosaic_type():
    root = parse(payloads.coverage_store("s2", "imagery"))
    assert root.findtext("type") == "ImageMosaic"
    assert root.findtext("workspace/name") == "imagery"


def test_layer_body_sets_default_style():
    assert parse(payloads.layer(default_style="raster")).findtext("defaultStyle/name") == "raster"
