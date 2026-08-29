import pytest

from geoserver_mosaic import (
    CogSettings,
    ExistingIndexStore,
    IndexerConfig,
    MosaicConfigurationError,
    PostgisIndex,
    TimeRegex,
    timestamp_collector,
)
from geoserver_mosaic.models import POSTGIS_SPI


def test_postgis_index_uses_the_geotools_factory():
    props = PostgisIndex(
        host="db", database="gis", user="u", password="p"
    ).to_properties()
    assert props["SPI"] == POSTGIS_SPI
    assert props["passwd"] == "p"
    assert props["Estimated extends"] is False


def test_postgis_optional_pool_settings_are_dropped_when_unset():
    props = PostgisIndex(host="db", database="gis", user="u", password="p").to_properties()
    assert props["max connections"] is None  # dumps() omits None entries


def test_existing_index_store_references_by_name():
    assert ExistingIndexStore("imagery:index").to_properties() == {
        "StoreName": "imagery:index"
    }


def test_cog_settings_map_scheme_to_range_reader():
    props = CogSettings(range_reader="S3").to_properties()
    assert props["Cog"] is True
    assert props["CogRangeReader"].endswith("S3RangeReader")


def test_unknown_range_reader_is_rejected():
    with pytest.raises(MosaicConfigurationError, match="range reader"):
        CogSettings(range_reader="FTP").to_properties()  # type: ignore[arg-type]


def test_indexer_rejects_time_attribute_missing_from_schema():
    indexer = IndexerConfig(name="m", schema="*the_geom:Polygon,location:String")
    with pytest.raises(MosaicConfigurationError, match="TimeAttribute"):
        indexer.validate()


def test_indexer_requires_location_attribute():
    indexer = IndexerConfig(
        name="m", schema="*the_geom:Polygon", time_attribute=None
    )
    with pytest.raises(MosaicConfigurationError, match="location"):
        indexer.validate()


def test_indexer_defaults_suit_a_harvested_time_mosaic():
    props = IndexerConfig(name="m").to_properties()
    # Harvesting requires a live index and absolute granule paths.
    assert props["Caching"] is False
    assert props["AbsolutePath"] is True
    assert props["CanBeEmpty"] is True


def test_property_collectors_are_comma_joined():
    indexer = IndexerConfig(
        name="m",
        schema="*the_geom:Polygon,location:String,time:java.util.Date,elev:Double",
        property_collectors=[timestamp_collector(), "DoubleFileNameExtractorSPI[e](elev)"],
    )
    assert props_of(indexer)["PropertyCollectors"] == (
        "TimestampFileNameExtractorSPI[timeregex](time),"
        "DoubleFileNameExtractorSPI[e](elev)"
    )


def test_time_regex_omits_format_when_absent():
    assert TimeRegex(regex="x").to_properties() == {"regex": "x"}
    assert TimeRegex(regex="x", date_format="yyyy").to_properties()["format"] == "yyyy"


def props_of(indexer):
    return indexer.to_properties()
