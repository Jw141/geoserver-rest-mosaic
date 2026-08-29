import io
import zipfile

import pytest

from geoserver_mosaic import (
    CogSettings,
    ExistingIndexStore,
    IndexerConfig,
    MosaicConfigurationError,
    MosaicDefinition,
    PostgisIndex,
    TimeRegex,
    build_config_archive,
    properties,
)


def archive_members(definition) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(build_config_archive(definition))) as zf:
        return {name: zf.read(name).decode() for name in zf.namelist()}


def cog_time_mosaic(**overrides) -> MosaicDefinition:
    defaults = dict(
        workspace="imagery",
        store="sentinel2",
        index=PostgisIndex(host="db", database="gis", user="gis", password="s3cret"),
        cog=CogSettings(range_reader="S3"),
        time_regex=TimeRegex(regex=r"[0-9]{8}T[0-9]{6}", date_format="yyyyMMdd'T'HHmmss"),
        granules=["s3://bucket/S2_20240301T104021.tif"],
        srs="EPSG:32633",
    )
    return MosaicDefinition(**{**defaults, **overrides})


def test_archive_contains_the_three_config_files():
    assert set(archive_members(cog_time_mosaic())) == {
        "indexer.properties",
        "datastore.properties",
        "timeregex.properties",
    }


def test_cog_settings_are_merged_into_the_indexer():
    indexer = properties.loads(archive_members(cog_time_mosaic())["indexer.properties"])
    assert indexer["Cog"] == "true"
    assert indexer["CogRangeReader"].endswith("S3RangeReader")
    assert indexer["AbsolutePath"] == "true"


def test_time_regex_wires_up_a_property_collector():
    members = archive_members(cog_time_mosaic())
    indexer = properties.loads(members["indexer.properties"])
    assert indexer["TimeAttribute"] == "time"
    # The collector argument names the regex file without its extension.
    assert indexer["PropertyCollectors"] == "TimestampFileNameExtractorSPI[timeregex](time)"
    assert properties.loads(members["timeregex.properties"]) == {
        "regex": "[0-9]{8}T[0-9]{6}",
        "format": "yyyyMMdd'T'HHmmss",
    }


def test_postgis_credentials_land_in_datastore_properties():
    store = properties.loads(archive_members(cog_time_mosaic())["datastore.properties"])
    assert store["host"] == "db"
    assert store["passwd"] == "s3cret"
    assert store["Loose bbox"] == "true"


def test_shapefile_index_writes_no_datastore_properties():
    members = archive_members(MosaicDefinition(workspace="w", store="s"))
    assert "datastore.properties" not in members


def test_existing_index_store_emits_only_a_store_reference():
    definition = cog_time_mosaic(index=ExistingIndexStore("imagery:index"))
    assert properties.loads(archive_members(definition)["datastore.properties"]) == {
        "StoreName": "imagery:index"
    }


def test_mosaic_without_time_drops_the_time_column_from_the_schema():
    indexer = MosaicDefinition(workspace="w", store="s").resolved_indexer()
    assert indexer.time_attribute is None
    assert "time" not in indexer.schema


def test_extra_files_are_added_to_the_archive():
    definition = cog_time_mosaic(extra_files={"elevregex.properties": "regex=x\n"})
    assert archive_members(definition)["elevregex.properties"] == "regex=x\n"


def test_local_files_are_uploaded_inside_the_archive(tmp_path):
    granule = tmp_path / "tile_20240301T104021.tif"
    granule.write_bytes(b"not really a tiff")
    definition = cog_time_mosaic(cog=None, granules=(), upload_files=[granule])
    assert archive_members(definition)["tile_20240301T104021.tif"] == "not really a tiff"


def test_remote_granules_without_cog_settings_are_rejected():
    with pytest.raises(MosaicConfigurationError, match="no CogSettings"):
        cog_time_mosaic(cog=None).validate()


def test_cog_requires_absolute_paths():
    definition = cog_time_mosaic(
        indexer=IndexerConfig(name="m", absolute_path=False, time_attribute=None,
                              schema="*the_geom:Polygon,location:String")
    )
    with pytest.raises(MosaicConfigurationError, match="absolute_path"):
        definition.validate()


def test_time_attribute_without_a_collector_is_rejected():
    definition = MosaicDefinition(
        workspace="w", store="s", indexer=IndexerConfig(name="m")
    )
    with pytest.raises(MosaicConfigurationError, match="no time_regex"):
        definition.validate()


def test_missing_upload_file_is_reported_before_any_request(tmp_path):
    definition = cog_time_mosaic(upload_files=[tmp_path / "absent.tif"])
    with pytest.raises(MosaicConfigurationError, match="not found"):
        definition.validate()


def test_time_dimension_is_derived_from_the_indexer():
    dimensions = cog_time_mosaic().resolved_dimensions()
    assert dimensions["time"].units == "ISO8601"
    assert dimensions["time"].default_strategy == "MAXIMUM"


def test_names_default_to_the_store_name():
    definition = MosaicDefinition(workspace="w", store="s")
    assert definition.resolved_coverage() == "s"
    assert definition.resolved_mosaic_name() == "s"
