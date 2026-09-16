"""Tests for the example driver.

The driver is the documented entry point for the docker-compose stack, so its
logic is worth pinning even though the stack itself is not running here.  A
fake GeoServer stands in for the containers.
"""

import json
import re

import httpx
import pytest
import respx

import driver

BASE = "http://gs.test/geoserver"
REST = f"{BASE}/rest"

VERSION_PAYLOAD = {
    "about": {"resource": [{"@name": "GeoServer", "Version": "2.28.4"}]}
}
GRANULE_FEATURES = {
    "features": [
        {"properties": {"location": "a.tif", "time": "2024-03-01T10:40:21.000Z"}},
        {"properties": {"location": "b.tif", "time": "2024-03-06T10:40:21.000Z"}},
    ]
}


@pytest.fixture
def fixtures(tmp_path, monkeypatch):
    """Stand-in granule files, so tests do not need the real fixtures."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    for name in ("S2_20240301T104021_r0c0.tif", "S2_20240306T104021_r0c0.tif"):
        (tiles / name).write_bytes(b"fake")
    monkeypatch.setattr(driver, "FIXTURES", tiles)
    return tiles


@pytest.fixture
def settings(monkeypatch):
    for key in list(driver.os.environ):
        if key.startswith(("GEOSERVER_", "PG_", "S3_", "GRANULE_")):
            monkeypatch.delenv(key, raising=False)
    return driver.Settings.from_env(BASE)


def fake_geoserver(request: httpx.Request) -> httpx.Response:
    """Minimal GeoServer stand-in, dispatching on method and path."""
    path = request.url.path
    method = request.method
    if path.endswith("/about/version.json"):
        return httpx.Response(200, json=VERSION_PAYLOAD)
    if path.endswith("/about/manifest.json"):
        # Answer the manifest regex the driver actually asked about, so the
        # per-plugin probe is exercised rather than blanket-satisfied.
        pattern = request.url.params.get("manifest", "")
        name = "gs-cog-s3-2.28.4" if pattern.startswith("gs-cog-s3") else "gs-cog-http-2.28.4"
        return httpx.Response(200, json={"about": {"resource": [{"@name": name}]}})
    if path.endswith("/workspaces.json"):
        return httpx.Response(200, json={"workspaces": ""})
    if re.search(r"/index/granules\.json$", path):
        # A by-name verification lookup (location IN (...)) is answered with
        # exactly what it asked for, as a server that indexed everything would.
        cql = request.url.params.get("filter", "")
        if cql.startswith("location IN (") or cql.startswith("location LIKE "):
            names = re.findall(r"'((?:[^']|'')*)'", cql)
            # LIKE patterns arrive as '%/<name>'; answer with a plausible path.
            features = [
                {"properties": {"location": n.replace("''", "'").replace("%/", "/data/")}}
                for n in names
            ]
            return httpx.Response(200, json={"features": features})
        return httpx.Response(200, json=GRANULE_FEATURES)
    if path.endswith("/coverages.json"):
        # The external case discovers the mosaic's native name from here.
        if request.url.params.get("list") == "all":
            return httpx.Response(200, json={"list": {"string": ["tiles-upload"]}})
        return httpx.Response(200, json={"coverages": ""})
    if method == "GET":
        # Nothing exists yet, so every existence probe is a miss.
        return httpx.Response(404, text="not found")
    return httpx.Response(201)


# ---------------------------------------------------------------------------
# Settings and definitions
# ---------------------------------------------------------------------------


def test_settings_default_to_compose_service_names(settings):
    # These are resolved by GeoServer inside the network, not by the driver,
    # so localhost would silently point at the wrong host.
    assert settings.pg_host == "postgis"
    assert settings.s3_base == "http://localstack:4566/mosaic-tiles"
    assert settings.granule_dir == "/opt/granules"


def test_settings_read_the_environment(monkeypatch):
    monkeypatch.setenv("PG_HOST_FROM_GEOSERVER", "db.internal")
    monkeypatch.setenv("S3_BASE_FROM_GEOSERVER", "https://minio/bucket")
    monkeypatch.setenv("PG_SCHEMA", "gs3")
    settings = driver.Settings.from_env(BASE)
    assert settings.pg_host == "db.internal"
    assert settings.s3_base == "https://minio/bucket"
    assert settings.postgis_index().schema == "gs3"


def test_wms_base_strips_the_rest_suffix():
    assert driver.Settings.from_env(f"{BASE}/rest").wms_base == BASE


@pytest.mark.parametrize("name", ["local", "remote", "upload"])
def test_every_demo_definition_is_valid(name, settings, fixtures):
    builder = driver.BUILDERS[name]
    definition = builder(settings) if name != "remote" else builder(settings)
    definition.validate()  # raises MosaicConfigurationError if inconsistent


def test_local_mosaic_harvests_the_directory_not_each_file(settings, fixtures):
    definition = driver.local_mosaic(settings)
    assert definition.granules == ["/opt/granules"]
    assert definition.cog is None  # local files need no COG reader


def test_remote_mosaic_builds_http_urls_and_enables_cog(settings, fixtures):
    definition = driver.remote_mosaic(settings)
    assert definition.cog is not None
    assert definition.cog.range_reader == "HTTP"
    assert definition.granules == [
        "http://localstack:4566/mosaic-tiles/S2_20240301T104021_r0c0.tif",
        "http://localstack:4566/mosaic-tiles/S2_20240306T104021_r0c0.tif",
    ]


def test_remote_mosaic_can_switch_to_s3_urls(settings, fixtures):
    definition = driver.remote_mosaic(settings, range_reader="S3")
    assert definition.cog.range_reader == "S3"
    assert definition.granules[0] == "s3://mosaic-tiles/S2_20240301T104021_r0c0.tif"


def test_upload_mosaic_ships_files_rather_than_locations(settings, fixtures):
    definition = driver.upload_mosaic(settings)
    assert definition.granules == ()
    assert len(definition.upload_files) == 2


def test_external_mosaic_registers_the_upload_stores_directory(settings, fixtures):
    definition = driver.external_mosaic(settings)
    assert definition.location == "/opt/geoserver/data_dir/data/mosaic-demo/tiles-upload/"
    assert "time" in definition.dimensions  # not derivable, so declared


@respx.mock
def test_external_run_registers_the_directory_without_a_zip(settings, fixtures):
    register = respx.put(
        f"{REST}/workspaces/mosaic-demo/coveragestores/tiles-external/external.imagemosaic"
    ).mock(return_value=httpx.Response(201))
    upload = respx.put(
        f"{REST}/workspaces/mosaic-demo/coveragestores/tiles-external/file.imagemosaic"
    ).mock(return_value=httpx.Response(201))
    publish = respx.post(
        f"{REST}/workspaces/mosaic-demo/coveragestores/tiles-external/coverages"
    ).mock(return_value=httpx.Response(201))
    respx.route().mock(side_effect=fake_geoserver)

    assert driver.main(["--url", BASE, "external"]) == 0

    assert register.calls.last.request.content == (
        b"/opt/geoserver/data_dir/data/mosaic-demo/tiles-upload/"
    )
    assert not upload.called
    assert "<nativeName>tiles-upload</nativeName>" in publish.calls.last.request.content.decode()


def test_missing_fixtures_give_an_actionable_message(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "FIXTURES", tmp_path / "absent")
    with pytest.raises(SystemExit, match="make_fixtures"):
        driver.granule_names()


def test_time_regex_matches_the_fixture_filenames():
    assert re.search(driver.TIME_REGEX.regex, "S2_20240301T104021_r0c0.tif")


def test_wms_url_includes_layer_and_time(settings):
    url = driver.wms_url(settings, "mosaic-demo:tiles-remote", time="2024-03-01T10:40:21Z")
    assert url.startswith(f"{BASE}/wms?")
    assert "layers=mosaic-demo%3Atiles-remote" in url
    assert "time=2024-03-01T10%3A40%3A21Z" in url


# ---------------------------------------------------------------------------
# Commands, against a fake server
# ---------------------------------------------------------------------------


@respx.mock
def test_check_reports_version_and_cog_modules(settings, capsys):
    respx.route().mock(side_effect=fake_geoserver)
    assert driver.main(["--url", BASE, "check"]) == 0
    out = capsys.readouterr().out
    assert "2.28.4" in out
    # Each connection type is an independent community package.
    assert "gs-cog-http installed (gs-cog-http-2.28.4)" in out
    assert "gs-cog-s3  installed (gs-cog-s3-2.28.4)" in out


@respx.mock
def test_check_flags_a_missing_s3_plugin_independently(settings, capsys):
    """cog-http-plugin without cog-s3-plugin is a real and easy-to-hit state."""

    def without_s3(request):
        params = request.url.params
        if request.url.path.endswith("/about/manifest.json"):
            if params.get("manifest", "").startswith("gs-cog-s3"):
                return httpx.Response(200, json={"about": {"resource": []}})
        return fake_geoserver(request)

    respx.route().mock(side_effect=without_s3)
    driver.main(["--url", BASE, "check"])
    out = capsys.readouterr().out
    assert "gs-cog-http installed" in out
    # A missing extension creates stores happily and only fails on read, so
    # the driver has to say so loudly.
    assert "gs-cog-s3  NOT INSTALLED -- s3:// granule URLs will fail" in out


@respx.mock
def test_check_flags_a_missing_cog_module(settings, capsys):
    def without_cog(request):
        if request.url.path.endswith("/about/manifest.json"):
            return httpx.Response(200, json={"about": {"resource": []}})
        return fake_geoserver(request)

    respx.route().mock(side_effect=without_cog)
    driver.main(["--url", BASE, "check"])
    out = capsys.readouterr().out
    assert "gs-cog-http NOT INSTALLED -- remote COG granules over HTTP will fail" in out


@respx.mock
def test_all_builds_every_mosaic(settings, fixtures, capsys):
    respx.route().mock(side_effect=fake_geoserver)
    assert driver.main(["--url", BASE, "all"]) == 0
    out = capsys.readouterr().out
    for store in ("tiles-local", "tiles-remote", "tiles-upload", "tiles-external"):
        assert f"mosaic-demo:{store}" in out
    assert "published  True" in out
    assert "index      2 granule(s)" in out
    assert "times      2024-03-01T10:40:21.000Z" in out


@respx.mock
def test_uploaded_archive_carries_the_granules(settings, fixtures):
    # Specific routes must be registered before the catch-all: respx matches
    # in registration order.
    upload = respx.put(
        f"{REST}/workspaces/mosaic-demo/coveragestores/tiles-upload/file.imagemosaic"
    ).mock(return_value=httpx.Response(201))
    respx.route().mock(side_effect=fake_geoserver)
    driver.main(["--url", BASE, "upload"])

    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(upload.calls.last.request.content)) as archive:
        names = set(archive.namelist())
    assert "indexer.properties" in names
    assert "S2_20240301T104021_r0c0.tif" in names
    # Shapefile index: no database credentials are written.
    assert "datastore.properties" not in names


@respx.mock
def test_remote_run_harvests_each_granule_url(settings, fixtures):
    harvest = respx.post(
        f"{REST}/workspaces/mosaic-demo/coveragestores/tiles-remote/remote.imagemosaic"
    ).mock(return_value=httpx.Response(202))
    respx.route().mock(side_effect=fake_geoserver)
    driver.main(["--url", BASE, "remote"])
    posted = [call.request.content.decode() for call in harvest.calls]
    assert posted == [
        "http://localstack:4566/mosaic-tiles/S2_20240301T104021_r0c0.tif",
        "http://localstack:4566/mosaic-tiles/S2_20240306T104021_r0c0.tif",
    ]


@respx.mock
def test_a_failing_granule_makes_the_driver_exit_nonzero(settings, fixtures, capsys):
    def flaky(request):
        if request.url.path.endswith(".imagemosaic") and request.method == "POST":
            return httpx.Response(500, text="cannot read granule")
        return fake_geoserver(request)

    respx.route().mock(side_effect=flaky)
    assert driver.main(["--url", BASE, "remote"]) == 1
    assert "FAILED" in capsys.readouterr().out


@respx.mock
def test_clean_deletes_each_store_with_its_index_then_the_workspace(settings):
    respx.get(f"{REST}/workspaces/mosaic-demo").mock(return_value=httpx.Response(200))
    respx.get(f"{REST}/workspaces/mosaic-demo/coveragestores.json").mock(
        return_value=httpx.Response(
            200, json={"coverageStores": {"coverageStore": [{"name": "tiles-local"}]}}
        )
    )
    respx.get(f"{REST}/workspaces/mosaic-demo/coveragestores/tiles-local").mock(
        return_value=httpx.Response(200)
    )
    respx.get(f"{REST}/workspaces/mosaic-demo/coveragestores/tiles-local.json").mock(
        return_value=httpx.Response(
            200, json={"coverageStore": {"name": "tiles-local", "url": "file:data/mosaic-demo/tiles-local"}}
        )
    )
    respx.get(f"{REST}/workspaces/mosaic-demo/coveragestores/tiles-local/coverages.json").mock(
        return_value=httpx.Response(200, json={"coverages": {"coverage": [{"name": "tiles-local"}]}})
    )
    emptied = respx.delete(
        f"{REST}/workspaces/mosaic-demo/coveragestores/tiles-local/coverages/tiles-local/index/granules"
    ).mock(return_value=httpx.Response(200))
    store_delete = respx.delete(f"{REST}/workspaces/mosaic-demo/coveragestores/tiles-local").mock(
        return_value=httpx.Response(200)
    )
    dir_delete = respx.delete(f"{REST}/resource/data/mosaic-demo/tiles-local").mock(
        return_value=httpx.Response(200)
    )
    delete = respx.delete(f"{REST}/workspaces/mosaic-demo").mock(
        return_value=httpx.Response(200)
    )
    assert driver.main(["--url", BASE, "clean"]) == 0
    # Index emptied row by row, store deleted, directory kept, workspace last.
    # Never a purge: on a PostGIS index GeoServer would try to drop the database.
    assert emptied.called
    assert "purge" not in store_delete.calls.last.request.url.params
    assert not dir_delete.called
    assert delete.calls.last.request.url.params["recurse"] == "true"


@respx.mock
def test_clean_is_a_no_op_when_nothing_exists(settings, capsys):
    respx.route().mock(side_effect=fake_geoserver)
    assert driver.main(["--url", BASE, "clean"]) == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_unreachable_server_explains_how_to_start_the_stack(settings, capsys):
    with respx.mock:
        respx.route().mock(side_effect=httpx.ConnectError("refused"))
        assert driver.main(["--url", BASE, "check"]) == 2
    assert "docker compose" in capsys.readouterr().err
