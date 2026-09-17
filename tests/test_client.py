import httpx
import pytest
import respx

from geoserver_mosaic import (
    AuthenticationError,
    CogSettings,
    ConflictError,
    GeoServerClient,
    GeoServerHTTPError,
    MosaicDefinition,
    MosaicManager,
    NotFoundError,
    PostgisIndex,
    TimeRegex,
    Version,
)

BASE = "http://gs.test/geoserver/rest"


@pytest.fixture
def client():
    with GeoServerClient("http://gs.test/geoserver", "admin", "geoserver", retries=0) as gs:
        yield gs


def test_base_url_accepts_both_forms():
    for given in ("http://gs.test/geoserver", "http://gs.test/geoserver/", "http://gs.test/geoserver/rest"):
        assert GeoServerClient(given, "u", "p").base_url == BASE


@respx.mock
def test_basic_auth_is_sent(client):
    route = respx.get(f"{BASE}/workspaces.json").mock(
        return_value=httpx.Response(200, json={"workspaces": ""})
    )
    client.list_workspaces()
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")


@respx.mock
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"workspaces": ""}, []),  # empty list serialises as an empty string
        ({"workspaces": {"workspace": {"name": "solo"}}}, ["solo"]),  # single collapses
        ({"workspaces": {"workspace": [{"name": "a"}, {"name": "b"}]}}, ["a", "b"]),
    ],
)
def test_list_tolerates_geoservers_list_shapes(client, payload, expected):
    respx.get(f"{BASE}/workspaces.json").mock(return_value=httpx.Response(200, json=payload))
    assert client.list_workspaces() == expected


@respx.mock
def test_version_is_read_from_the_geoserver_component(client):
    respx.get(f"{BASE}/about/version.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "about": {
                    "resource": [
                        {"@name": "GeoTools", "Version": "33.4"},
                        {"@name": "GeoServer", "Version": "3.0.0"},
                    ]
                }
            },
        )
    )
    assert client.version() == Version(3, 0, 0)
    assert client.features.is_v3


@respx.mock
def test_create_workspace_is_idempotent(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    create = respx.post(f"{BASE}/workspaces").mock(return_value=httpx.Response(201))
    assert client.create_workspace("imagery") is False
    assert not create.called


@respx.mock
def test_create_workspace_posts_xml(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(404))
    create = respx.post(f"{BASE}/workspaces").mock(return_value=httpx.Response(201))
    assert client.create_workspace("imagery") is True
    assert create.calls.last.request.content == b"<workspace><name>imagery</name></workspace>"


@respx.mock
def test_create_workspace_with_uri_goes_through_namespaces(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(404))
    route = respx.post(f"{BASE}/namespaces").mock(return_value=httpx.Response(201))
    client.create_workspace("imagery", uri="http://example.com/imagery")
    assert b"<uri>http://example.com/imagery</uri>" in route.calls.last.request.content


@respx.mock
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (404, "no such workspace", NotFoundError),
        (401, "", AuthenticationError),
        (403, "", AuthenticationError),
        (409, "", ConflictError),
        (500, "Store 'x' already exists in workspace", ConflictError),
        (500, "java.lang.NullPointerException", GeoServerHTTPError),
    ],
)
def test_status_codes_map_to_exceptions(client, status, body, expected):
    respx.get(f"{BASE}/workspaces.json").mock(return_value=httpx.Response(status, text=body))
    with pytest.raises(expected):
        client.list_workspaces()


@respx.mock
def test_html_error_pages_are_summarised(client):
    respx.get(f"{BASE}/workspaces.json").mock(
        return_value=httpx.Response(
            500, text="<html><head><title>Resource not writable</title></head></html>"
        )
    )
    with pytest.raises(GeoServerHTTPError) as info:
        client.list_workspaces()
    assert info.value.message == "Resource not writable"


@respx.mock
def test_transient_status_is_retried():
    with GeoServerClient("http://gs.test/geoserver", "u", "p", retries=2, backoff=0) as gs:
        route = respx.get(f"{BASE}/workspaces.json").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"workspaces": ""}),
            ]
        )
        assert gs.list_workspaces() == []
        assert route.call_count == 2


@respx.mock
def test_connection_errors_are_retried_then_raised():
    with GeoServerClient("http://gs.test/geoserver", "u", "p", retries=1, backoff=0) as gs:
        respx.get(f"{BASE}/workspaces.json").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(httpx.ConnectError):
            gs.list_workspaces()


@respx.mock
def test_harvest_posts_the_location_as_plain_text(client):
    route = respx.post(
        f"{BASE}/workspaces/imagery/coveragestores/s2/remote.imagemosaic"
    ).mock(return_value=httpx.Response(202))
    client.harvest("imagery", "s2", "s3://bucket/a.tif")
    request = route.calls.last.request
    assert request.content == b"s3://bucket/a.tif"
    assert request.headers["content-type"] == "text/plain"


@respx.mock
def test_upload_sends_zip_with_configure_none(client):
    route = respx.put(
        f"{BASE}/workspaces/imagery/coveragestores/s2/file.imagemosaic"
    ).mock(return_value=httpx.Response(201))
    client.upload_mosaic_archive("imagery", "s2", b"PK\x03\x04")
    request = route.calls.last.request
    assert request.headers["content-type"] == "application/zip"
    assert request.url.params["configure"] == "none"


@respx.mock
def test_names_with_special_characters_are_encoded(client):
    route = respx.get(f"{BASE}/workspaces/my%2Fspace").mock(return_value=httpx.Response(200))
    assert client.workspace_exists("my/space") is True
    assert route.called


@respx.mock
def test_available_coverages_lists_unpublished_names(client):
    respx.get(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages.json").mock(
        return_value=httpx.Response(200, json={"list": {"string": ["sentinel2"]}})
    )
    assert client.list_coverages("imagery", "s2", available=True) == ["sentinel2"]


@respx.mock
def test_iter_granules_pages_until_short_batch(client):
    url = f"{BASE}/workspaces/imagery/coveragestores/s2/coverages/s2/index/granules.json"
    page = lambda n: httpx.Response(200, json={"features": [{"id": f"g{i}"} for i in range(n)]})
    respx.get(url).mock(side_effect=[page(2), page(2), page(1)])
    granules = list(client.iter_granules("imagery", "s2", "s2", page_size=2))
    assert len(granules) == 5


@respx.mock
def test_delete_granules_passes_cql_filter(client):
    route = respx.delete(
        f"{BASE}/workspaces/imagery/coveragestores/s2/coverages/s2/index/granules"
    ).mock(return_value=httpx.Response(200))
    client.delete_granules("imagery", "s2", "s2", filter="time BEFORE 2024-01-01T00:00:00Z")
    assert route.calls.last.request.url.params["filter"] == "time BEFORE 2024-01-01T00:00:00Z"
    assert route.calls.last.request.url.params["purge"] == "none"


# ---------------------------------------------------------------------------
# End-to-end mosaic creation
# ---------------------------------------------------------------------------


def cog_mosaic() -> MosaicDefinition:
    return MosaicDefinition(
        workspace="imagery",
        store="s2",
        index=PostgisIndex(host="db", database="gis", user="gis", password="pw"),
        cog=CogSettings(range_reader="S3"),
        time_regex=TimeRegex(regex=r"[0-9]{8}", date_format="yyyyMMdd"),
        granules=["s3://bucket/20240301.tif", "s3://bucket/20240302.tif"],
        srs="EPSG:32633",
        title="Sentinel-2",
    )


STORE = f"{BASE}/workspaces/imagery/coveragestores/s2"


STORE_DIR = f"{BASE}/resource/data/imagery/s2"
CONFIG = f"{STORE_DIR}/s2.properties"  # GeoServer's own mosaic config, named after the indexer


def store_absent(*, directory: bool = False):
    respx.get(STORE).mock(return_value=httpx.Response(404))
    respx.get(CONFIG).mock(return_value=httpx.Response(404))
    respx.get(STORE_DIR).mock(return_value=httpx.Response(200 if directory else 404))


def directory_deleted():
    return respx.delete(STORE_DIR).mock(return_value=httpx.Response(200))


def config_present(text: str = "Name=s2\nCogRangeReader=old.Http\nLevels=1,1\n"):
    """A kept store directory with GeoServer's derived mosaic config in it."""
    respx.get(CONFIG).mock(return_value=httpx.Response(200, text=text))
    return respx.put(CONFIG).mock(return_value=httpx.Response(201))


def index_holds(*locations: str):
    """The published coverage's granule index, as create() verifies it."""
    features = [{"properties": {"location": l}} for l in locations]
    return respx.get(f"{STORE}/coverages/s2/index/granules.json").mock(
        return_value=httpx.Response(200, json={"features": features})
    )


COG_GRANULES = ("s3://bucket/20240301.tif", "s3://bucket/20240302.tif")


def store_present(*coverages: str):
    """An existing store, publishing the given coverages, with a kept directory."""
    respx.get(STORE).mock(return_value=httpx.Response(200))
    respx.get(STORE_DIR).mock(return_value=httpx.Response(200))
    config_present()
    for coverage in coverages:
        respx.delete(f"{STORE}/coverages/{coverage}/index/granules").mock(
            return_value=httpx.Response(200)
        )
    listing = {"coverages": {"coverage": [{"name": c} for c in coverages]}} if coverages else {"coverages": ""}
    respx.get(f"{STORE}/coverages.json").mock(return_value=httpx.Response(200, json=listing))


@respx.mock
def test_create_runs_the_full_bootstrap_sequence(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(404))
    respx.post(f"{BASE}/workspaces").mock(return_value=httpx.Response(201))
    store_absent()
    index_holds(*COG_GRANULES)
    upload = respx.put(
        f"{BASE}/workspaces/imagery/coveragestores/s2/file.imagemosaic"
    ).mock(return_value=httpx.Response(201))
    harvest = respx.post(
        f"{BASE}/workspaces/imagery/coveragestores/s2/remote.imagemosaic"
    ).mock(return_value=httpx.Response(202))
    respx.get(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages/s2").mock(
        return_value=httpx.Response(404)
    )
    publish = respx.post(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages").mock(
        return_value=httpx.Response(201)
    )

    result = MosaicManager(client).create(cog_mosaic())

    assert upload.called
    assert harvest.call_count == 2
    assert result.harvested == ["s3://bucket/20240301.tif", "s3://bucket/20240302.tif"]
    assert result.published is True
    assert result.created is True
    assert result.ok
    assert result.layer == "imagery:s2"

    body = publish.calls.last.request.content.decode()
    # nativeName must match the indexer Name or GeoServer cannot find the mosaic.
    assert "<nativeName>s2</nativeName>" in body
    assert "<srs>EPSG:32633</srs>" in body
    assert '<entry key="time">' in body
    assert "<units>ISO8601</units>" in body


@respx.mock
def test_a_failed_granule_does_not_abort_the_rest(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    index_holds("s3://bucket/20240302.tif")
    respx.put(f"{BASE}/workspaces/imagery/coveragestores/s2/file.imagemosaic").mock(
        return_value=httpx.Response(201)
    )
    respx.post(f"{BASE}/workspaces/imagery/coveragestores/s2/remote.imagemosaic").mock(
        side_effect=[httpx.Response(500, text="unreadable granule"), httpx.Response(202)]
    )
    respx.get(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages/s2").mock(
        return_value=httpx.Response(404)
    )
    respx.post(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages").mock(
        return_value=httpx.Response(201)
    )

    result = MosaicManager(client).create(cog_mosaic())

    assert result.harvested == ["s3://bucket/20240302.tif"]
    assert [location for location, _ in result.failed] == ["s3://bucket/20240301.tif"]
    assert result.published is True
    assert result.ok is False  # failures are surfaced, not hidden


@respx.mock
def test_empty_mosaic_is_created_but_not_published(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    upload = respx.put(
        f"{BASE}/workspaces/imagery/coveragestores/s2/file.imagemosaic"
    ).mock(return_value=httpx.Response(201))
    publish = respx.post(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages").mock(
        return_value=httpx.Response(201)
    )

    definition = cog_mosaic()
    definition.granules = []
    result = MosaicManager(client).create(definition)

    assert upload.called
    # GeoServer derives bands and envelope from the index, so an empty mosaic
    # cannot be published yet.
    assert not publish.called
    assert result.published is False


@respx.mock
def test_publishing_an_existing_coverage_updates_it(client):
    respx.get(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages/s2").mock(
        return_value=httpx.Response(200)
    )
    update = respx.put(
        f"{BASE}/workspaces/imagery/coveragestores/s2/coverages/s2"
    ).mock(return_value=httpx.Response(200))
    MosaicManager(client).publish_coverage(cog_mosaic())
    assert update.called


@respx.mock
def test_replace_deletes_the_store_first(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_present()
    index_holds(*COG_GRANULES)
    delete = respx.delete(f"{BASE}/workspaces/imagery/coveragestores/s2").mock(
        return_value=httpx.Response(200)
    )
    respx.put(f"{BASE}/workspaces/imagery/coveragestores/s2/file.imagemosaic").mock(
        return_value=httpx.Response(201)
    )
    respx.post(f"{BASE}/workspaces/imagery/coveragestores/s2/remote.imagemosaic").mock(
        return_value=httpx.Response(202)
    )
    respx.get(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages/s2").mock(
        return_value=httpx.Response(404)
    )
    respx.post(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages").mock(
        return_value=httpx.Response(201)
    )

    MosaicManager(client).create(cog_mosaic(), replace=True)
    assert delete.calls.last.request.url.params["recurse"] == "true"


@respx.mock
def test_remove_granules_requires_a_filter(client):
    from geoserver_mosaic import MosaicConfigurationError

    with pytest.raises(MosaicConfigurationError, match="filter is required"):
        MosaicManager(client).remove_granules(cog_mosaic(), filter="")


# ---------------------------------------------------------------------------
# GeoServer 3.0 rejects a trailing slash on REST paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("workspaces", "workspaces"),
        ("workspaces/", "workspaces"),
        ("/workspaces/", "workspaces"),
        ("workspaces///", "workspaces"),
        ("workspaces//imagery", "workspaces/imagery"),
        ("/workspaces/imagery/coveragestores/", "workspaces/imagery/coveragestores"),
        ("", ""),
        ("/", ""),
    ],
)
def test_clean_path_strips_trailing_and_empty_segments(given, expected):
    from geoserver_mosaic.client import _clean_path

    assert _clean_path(given) == expected


@respx.mock
def test_trailing_slash_is_stripped_from_the_request_url(client):
    # 3.0 routes /workspaces and /workspaces/ differently and 404s the latter.
    route = respx.get(f"{BASE}/workspaces").mock(return_value=httpx.Response(200))
    client.request("GET", "workspaces/")
    assert str(route.calls.last.request.url) == f"{BASE}/workspaces"


@respx.mock
def test_empty_path_addresses_the_rest_root_without_a_slash(client):
    route = respx.get(BASE).mock(return_value=httpx.Response(200))
    client.request("GET", "")
    assert str(route.calls.last.request.url) == BASE


@respx.mock
def test_no_generated_endpoint_ends_in_a_slash(client):
    """Every path this client builds must survive 3.0's stricter routing."""
    respx.route().mock(return_value=httpx.Response(200, json={}))

    manager = MosaicManager(client)
    definition = cog_mosaic()
    client.list_workspaces()
    client.workspace_exists("imagery")
    client.delete_workspace("imagery")
    client.list_coverage_stores("imagery")
    client.get_coverage_store("imagery", "s2")
    client.delete_coverage_store("imagery", "s2")
    client.upload_mosaic_archive("imagery", "s2", b"PK")
    client.create_store_from_external_dir("imagery", "s2", "file:///data/tiles/")
    client.harvest("imagery", "s2", "s3://bucket/a.tif")
    client.list_coverages("imagery", "s2")
    client.list_coverages("imagery", "s2", available=True)
    client.list_native_coverages("imagery", "s2")
    client.get_coverage("imagery", "s2", "s2")
    client.delete_coverage("imagery", "s2", "s2")
    client.index_schema("imagery", "s2", "s2")
    client.list_granules("imagery", "s2", "s2")
    client.delete_granules("imagery", "s2", "s2", filter="x")
    client.set_default_style("imagery", "s2", "raster")
    client.create_postgis_datastore(
        "imagery", "idx", host="db", database="gis", user="u", password="p"
    )
    client.reload()
    client.reset()
    manager.publish_coverage(definition)

    assert respx.calls.call_count > 20
    for call in respx.calls:
        assert not call.request.url.path.endswith("/"), call.request.url


@respx.mock
def test_external_dir_body_keeps_its_trailing_slash(client):
    # The rule applies to REST paths, not to directory locations in bodies.
    route = respx.put(
        f"{BASE}/workspaces/imagery/coveragestores/s2/external.imagemosaic"
    ).mock(return_value=httpx.Response(201))
    client.create_store_from_external_dir("imagery", "s2", "/data/tiles/")
    assert route.calls.last.request.content == b"/data/tiles/"


# ---------------------------------------------------------------------------
# Harvest endpoint routing
#
# GeoServer has two harvest endpoints and they are not interchangeable:
# external.imagemosaic resolves its body as a local file (a remote URL fails
# with "Failed to locate the input file"), while remote.imagemosaic takes a URL
# GeoServer fetches itself.  Sending a location to the wrong one is a 400.
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    "location,expected",
    [
        ("s3://bucket/a.tif", "remote"),
        ("S3://bucket/a.tif", "remote"),
        ("http://host/a.tif", "remote"),
        ("https://host/a.tif", "remote"),
        ("gs://bucket/a.tif", "remote"),
        ("azure://container/a.tif", "remote"),
        ("/opt/granules/a.tif", "external"),
        ("/opt/granules", "external"),
        ("file:///opt/granules/a.tif", "external"),
        ("C:\\data\\a.tif", "external"),
    ],
)
def test_harvest_routes_by_location_scheme(client, location, expected):
    routes = {
        name: respx.post(
            f"{BASE}/workspaces/imagery/coveragestores/s2/{name}.imagemosaic"
        ).mock(return_value=httpx.Response(202))
        for name in ("remote", "external")
    }
    client.harvest("imagery", "s2", location)
    assert routes[expected].called, f"{location} should route to {expected}.imagemosaic"
    other = "external" if expected == "remote" else "remote"
    assert not routes[other].called


@respx.mock
def test_harvest_endpoint_can_be_forced(client):
    route = respx.post(
        f"{BASE}/workspaces/imagery/coveragestores/s2/external.imagemosaic"
    ).mock(return_value=httpx.Response(202))
    client.harvest("imagery", "s2", "http://host/a.tif", endpoint="external")
    assert route.called


def test_harvest_rejects_an_unknown_endpoint(client):
    with pytest.raises(ValueError, match="remote' or 'external'"):
        client.harvest("imagery", "s2", "/opt/a.tif", endpoint="url")


@respx.mock
def test_mixed_local_and_remote_granules_each_go_to_their_own_endpoint(client):
    remote = respx.post(
        f"{BASE}/workspaces/imagery/coveragestores/s2/remote.imagemosaic"
    ).mock(return_value=httpx.Response(202))
    external = respx.post(
        f"{BASE}/workspaces/imagery/coveragestores/s2/external.imagemosaic"
    ).mock(return_value=httpx.Response(202))

    harvested, failed = MosaicManager(client).add_granules(
        "imagery", "s2", ["s3://bucket/a.tif", "/opt/granules/b.tif"]
    )

    assert not failed
    assert len(harvested) == 2
    assert remote.calls.last.request.content == b"s3://bucket/a.tif"
    assert external.calls.last.request.content == b"/opt/granules/b.tif"


@respx.mock
def test_replace_can_purge_the_stores_files(client):
    """Deleting a store leaves its data directory; purge removes it.

    Recreating a store under a name whose directory survived can leave the
    mosaic unable to initialise, so the option has to be reachable.
    """
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_present()
    index_holds(*COG_GRANULES)
    delete = respx.delete(f"{BASE}/workspaces/imagery/coveragestores/s2").mock(
        return_value=httpx.Response(200)
    )
    respx.put(f"{BASE}/workspaces/imagery/coveragestores/s2/file.imagemosaic").mock(
        return_value=httpx.Response(201)
    )
    respx.post(f"{BASE}/workspaces/imagery/coveragestores/s2/remote.imagemosaic").mock(
        return_value=httpx.Response(202)
    )
    respx.get(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages/s2").mock(
        return_value=httpx.Response(404)
    )
    respx.post(f"{BASE}/workspaces/imagery/coveragestores/s2/coverages").mock(
        return_value=httpx.Response(201)
    )

    MosaicManager(client).create(cog_mosaic(), replace=True, purge="all")
    assert delete.calls.last.request.url.params["purge"] == "all"


# ---------------------------------------------------------------------------
# purge is a vocabulary, not a boolean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        (False, "none"), (None, "none"), (True, "all"),
        ("none", "none"), ("metadata", "metadata"), ("all", "all"), ("ALL", "all"),
    ],
)
def test_purge_values_map_to_geoservers_vocabulary(given, expected):
    from geoserver_mosaic.client import _purge_value

    assert _purge_value(given) == expected


def test_purge_rejects_values_geoserver_would_400_on():
    from geoserver_mosaic.client import _purge_value

    # Sending purge=false is rejected by GeoServer with a bare 400, so a bool
    # must never reach the wire verbatim.
    with pytest.raises(ValueError, match="none.*metadata.*all"):
        _purge_value("false")


@respx.mock
def test_delete_granules_sends_a_valid_purge_and_a_match_all_filter(client):
    route = respx.delete(
        f"{BASE}/workspaces/imagery/coveragestores/s2/coverages/s2/index/granules"
    ).mock(return_value=httpx.Response(200))
    client.delete_granules("imagery", "s2", "s2")
    params = route.calls.last.request.url.params
    # A filterless delete is a 400; INCLUDE is CQL for "everything".
    assert params["filter"] == "INCLUDE"
    assert params["purge"] == "none"








# ---------------------------------------------------------------------------
# Re-running create() on a store that already exists
#
# GeoServer has no REST route that reconfigures an ImageMosaic in place, and a
# config ZIP uploaded to an existing store is harvested, not applied.  So a
# repeat run keeps the configuration, harvests what is new, and refreshes the
# coverage metadata.
# ---------------------------------------------------------------------------


@respx.mock
def test_existing_store_is_harvested_into_not_reconfigured(client, tmp_path):
    granule = tmp_path / "20240303.tif"
    granule.write_bytes(b"tiff")
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_present("s2")
    index_holds(*COG_GRANULES, "/srv/gs/data/imagery/s2/20240303.tif")
    upload = respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(202))
    harvest = respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(200))
    update = respx.put(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(200))

    definition = cog_mosaic()
    definition.upload_files = [granule]
    result = MosaicManager(client).create(definition)

    assert result.created is False
    assert result.published is True
    assert harvest.call_count == 2
    assert update.called
    # The archive PUT to an existing store is a harvest: granules only, no
    # configuration files that GeoServer would ignore anyway.
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(upload.calls.last.request.content)) as archive:
        assert archive.namelist() == ["20240303.tif"]


@respx.mock
def test_existing_published_store_with_nothing_new_still_refreshes_metadata(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_present("s2")
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(200))
    update = respx.put(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(200))

    definition = cog_mosaic()
    definition.granules = []
    result = MosaicManager(client).create(definition)

    assert result.created is False
    assert result.published is True
    assert update.called


@respx.mock
def test_existing_unpublished_store_with_nothing_new_stays_unpublished(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_present()
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    publish = respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))

    definition = cog_mosaic()
    definition.granules = []
    result = MosaicManager(client).create(definition)

    assert result.created is False
    assert result.published is False
    assert not publish.called


@respx.mock
def test_add_files_uploads_a_granule_only_archive(client, tmp_path):
    granule = tmp_path / "a.tif"
    granule.write_bytes(b"tiff")
    upload = respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(202))

    assert MosaicManager(client).add_files("imagery", "s2", [granule]) == ["a.tif"]

    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(upload.calls.last.request.content)) as archive:
        assert archive.namelist() == ["a.tif"]




# ---------------------------------------------------------------------------
# A pre-provisioned mosaic directory on the GeoServer host
# ---------------------------------------------------------------------------


def external_mosaic(**overrides) -> MosaicDefinition:
    defaults = dict(
        workspace="imagery",
        store="s2",
        location="/data/mosaics/s2/",
        title="Pre-built mosaic",
    )
    return MosaicDefinition(**{**defaults, **overrides})


@respx.mock
def test_external_directory_is_registered_rather_than_uploaded(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    register = respx.put(f"{STORE}/external.imagemosaic").mock(return_value=httpx.Response(201))
    upload = respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    listing = respx.get(f"{STORE}/coverages.json").mock(
        return_value=httpx.Response(200, json={"list": {"string": ["s2_mosaic"]}})
    )
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    publish = respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))

    result = MosaicManager(client).create(external_mosaic())

    assert result.published and result.created
    assert not upload.called
    request = register.calls.last.request
    # A plain path: 2.28.4 rejects the file: URL form with "Failed to locate".
    assert request.content == b"/data/mosaics/s2/"
    # list=all, not list=available, which came back empty on a live server.
    assert listing.calls.last.request.url.params["list"] == "all"
    assert request.url.params["configure"] == "none"
    # The native name is whatever the directory's own indexer calls the mosaic.
    assert "<nativeName>s2_mosaic</nativeName>" in publish.calls.last.request.content.decode()


@respx.mock
def test_external_directory_native_name_can_be_pinned(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    respx.put(f"{STORE}/external.imagemosaic").mock(return_value=httpx.Response(201))
    listing = respx.get(f"{STORE}/coverages.json").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    publish = respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))

    MosaicManager(client).create(external_mosaic(mosaic_name="pinned"))

    assert not listing.called
    assert "<nativeName>pinned</nativeName>" in publish.calls.last.request.content.decode()


@respx.mock
def test_external_directory_with_several_coverages_needs_mosaic_name(client):
    from geoserver_mosaic import MosaicConfigurationError

    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    respx.put(f"{STORE}/external.imagemosaic").mock(return_value=httpx.Response(201))
    respx.get(f"{STORE}/coverages.json").mock(
        return_value=httpx.Response(200, json={"list": {"string": ["a", "b"]}})
    )
    with pytest.raises(MosaicConfigurationError, match="mosaic_name"):
        MosaicManager(client).create(external_mosaic())


@respx.mock
def test_external_file_url_is_reduced_to_a_path(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    register = respx.put(f"{STORE}/external.imagemosaic").mock(return_value=httpx.Response(201))
    MosaicManager(client).create(
        external_mosaic(location="file:///srv/mosaic", mosaic_name="m"), publish=False
    )
    assert register.calls.last.request.content == b"/srv/mosaic"


@pytest.mark.parametrize(
    "given,expected",
    [
        ("/srv/mosaic/", "/srv/mosaic/"),
        ("file:///srv/mosaic/", "/srv/mosaic/"),
        ("file:/srv/mosaic", "/srv/mosaic"),
        ("FILE:///srv/mosaic", "/srv/mosaic"),
        ("file:data/imagery/s2", "data/imagery/s2"),  # as GeoServer reports its own stores
        ("file://nas/share/mosaic", "file://nas/share/mosaic"),  # not local: left alone
        ("C:\\mosaic", "C:\\mosaic"),
    ],
)
def test_host_path_reduces_file_urls(given, expected):
    from geoserver_mosaic.client import host_path

    assert host_path(given) == expected


# ---------------------------------------------------------------------------
# Harvest verification
#
# GeoServer answers every harvest POST with 202 and an empty body -- a bogus
# key, an unreachable bucket and a misconfigured range reader all look like
# success on the wire.  The index is the only evidence, so create() asks it.
# ---------------------------------------------------------------------------


@respx.mock
def test_granules_accepted_but_not_indexed_are_reported_as_failed(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    # Only the first granule made it into the index.
    lookup = index_holds("s3://bucket/20240301.tif")

    result = MosaicManager(client).create(cog_mosaic())

    assert result.published
    assert result.harvested == ["s3://bucket/20240301.tif"]
    assert [l for l, _ in result.failed] == ["s3://bucket/20240302.tif"]
    assert "absent from the index" in result.failed[0][1]
    assert result.ok is False
    # Looked up by name, so the cost scales with the batch, not the index.
    cql = lookup.calls.last.request.url.params["filter"]
    assert cql == "location IN ('s3://bucket/20240301.tif','s3://bucket/20240302.tif')"


@respx.mock
def test_verification_can_be_switched_off(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    lookup = index_holds()

    result = MosaicManager(client).create(cog_mosaic(), verify=False)

    assert not lookup.called
    assert len(result.harvested) == 2


@respx.mock
def test_directory_harvest_is_verified_by_a_non_empty_index(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/external.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    lookup = index_holds()  # nothing indexed

    definition = cog_mosaic()
    definition.cog = None
    definition.granules = ["/opt/granules"]
    result = MosaicManager(client).create(definition)

    assert result.harvested == []
    assert [l for l, _ in result.failed] == ["/opt/granules"]
    assert "index is empty" in result.failed[0][1]
    # A directory cannot be matched by name; only the emptiness check runs.
    assert "filter" not in lookup.calls.last.request.url.params
    assert lookup.calls.last.request.url.params["limit"] == "1"


def test_cql_string_escapes_quotes():
    from geoserver_mosaic.mosaic import _cql_string, _index_location, _names_one_granule

    assert _cql_string("it's") == "'it''s'"
    assert _names_one_granule("s3://b/a.tif") and _names_one_granule("/d/a.tif")
    assert not _names_one_granule("/opt/granules") and not _names_one_granule("file:///opt/granules/")
    assert _index_location("file:///opt/granules/a.tif") == "/opt/granules/a.tif"
    assert _index_location("s3://b/a.tif") == "s3://b/a.tif"


# ---------------------------------------------------------------------------
# The store directory outlives the store, and its contents outrank a new
# indexer.properties.  Verified on 2.28.4: a mosaic re-created with the S3
# range reader kept reading through HTTP because <store>.properties from the
# old directory said so.  Only the resource API can remove it.
# ---------------------------------------------------------------------------












@respx.mock
def test_resource_paths_are_encoded_per_segment(client):
    route = respx.delete(f"{BASE}/resource/data/my%20ws/s2").mock(return_value=httpx.Response(200))
    client.delete_resource("data/my ws/s2")
    assert route.called


@respx.mock
def test_uploaded_files_are_verified_by_basename(client, tmp_path):
    """The index holds the server-side path, so only the name can be matched."""
    granule = tmp_path / "20240303.tif"
    granule.write_bytes(b"tiff")
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    lookup = index_holds("/opt/geoserver/data_dir/data/imagery/s2/20240303.tif")

    definition = cog_mosaic()
    definition.granules = []
    definition.upload_files = [granule]
    result = MosaicManager(client).create(definition)

    assert result.failed == []
    assert lookup.calls.last.request.url.params["filter"] == "location LIKE '%/20240303.tif'"


@respx.mock
def test_missing_uploaded_file_is_reported_with_its_local_path(client, tmp_path):
    granule = tmp_path / "20240303.tif"
    granule.write_bytes(b"tiff")
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    index_holds()

    definition = cog_mosaic()
    definition.granules = []
    definition.upload_files = [granule]
    result = MosaicManager(client).create(definition)

    assert [l for l, _ in result.failed] == [str(granule)]


# ---------------------------------------------------------------------------
# An index table left behind by an earlier store of the same name.  The new
# store accepts every harvest, indexes nothing, and cannot be published.  The
# only REST route to the table is purging the store, so create() does that
# once and rebuilds.
# ---------------------------------------------------------------------------




@respx.mock
def test_orphaned_index_heal_is_not_attempted_for_a_shapefile_index(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/external.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(
        return_value=httpx.Response(500, text="The specified coverageName is unavailable")
    )
    definition = MosaicDefinition(workspace="imagery", store="s2", granules=["/opt/granules"])
    with pytest.raises(GeoServerHTTPError):
        MosaicManager(client).create(definition)


@respx.mock
def test_other_publish_errors_are_raised_untouched(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(500, text="NullPointerException"))
    purge = respx.delete(STORE).mock(return_value=httpx.Response(200))
    with pytest.raises(GeoServerHTTPError):
        MosaicManager(client).create(cog_mosaic())
    assert not purge.called


# ---------------------------------------------------------------------------
# Deleting cleanly, so a name can be rebuilt
# ---------------------------------------------------------------------------




@respx.mock
def test_delete_leaves_an_external_stores_index_and_directory_alone(client):
    respx.get(STORE).mock(return_value=httpx.Response(200))
    respx.get(f"{STORE}.json").mock(
        return_value=httpx.Response(200, json={"coverageStore": {"url": "file:/srv/mosaics/s2/"}})
    )
    drop = respx.delete(STORE).mock(return_value=httpx.Response(200))
    probe = respx.get(STORE_DIR).mock(return_value=httpx.Response(200))

    MosaicManager(client).delete("imagery", "s2")
    assert "purge" not in drop.calls.last.request.url.params
    assert not probe.called


@respx.mock
def test_delete_of_a_missing_store_is_a_no_op(client):
    respx.get(STORE).mock(return_value=httpx.Response(404))
    assert MosaicManager(client).delete("imagery", "s2") is False








@respx.mock
def test_an_explicit_purge_500_with_the_store_still_present_is_raised(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(STORE).mock(return_value=httpx.Response(200))
    respx.get(f"{STORE}/coverages.json").mock(return_value=httpx.Response(200, json={"coverages": ""}))
    respx.delete(STORE).mock(return_value=httpx.Response(500, text="Unable to drop the database: "))

    with pytest.raises(GeoServerHTTPError):
        MosaicManager(client).create(cog_mosaic(), replace=True, purge="metadata")









# ---------------------------------------------------------------------------
# replace: empty the index, delete the store, keep the directory, patch the
# mosaic configuration GeoServer wrote there, build again over it.
# ---------------------------------------------------------------------------


@respx.mock
def test_replace_keeps_the_directory_and_patches_the_mosaic_config(client):
    from geoserver_mosaic import properties

    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(STORE).mock(return_value=httpx.Response(200))
    respx.get(f"{STORE}/coverages.json").mock(
        return_value=httpx.Response(200, json={"coverages": {"coverage": [{"name": "s2"}]}})
    )
    emptied = respx.delete(f"{STORE}/coverages/s2/index/granules").mock(return_value=httpx.Response(200))
    drop = respx.delete(STORE).mock(return_value=httpx.Response(200))
    rmdir = respx.delete(STORE_DIR).mock(return_value=httpx.Response(200))
    patched = config_present("Name=s2\nCogRangeReader=old.Http\nLevels=1,1\nEnvelope2D=0,0,1,1\n")
    upload = respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    pruned = respx.delete(f"{STORE}/coverages/s2/index/granules")
    index_holds(*COG_GRANULES)

    result = MosaicManager(client).create(cog_mosaic(), replace=True)

    assert result.ok and result.created
    assert emptied.called
    assert "purge" not in drop.calls.last.request.url.params
    assert not rmdir.called
    assert upload.called
    written = properties.loads(patched.calls.last.request.content.decode())
    assert written["CogRangeReader"].endswith("S3RangeReader")  # this definition's
    assert written["Levels"] == "1,1" and written["Envelope2D"] == "0,0,1,1"  # GeoServer's, kept
    # The index was emptied, so nothing is stale and nothing is pruned.
    assert emptied.call_count == 1


@respx.mock
def test_replace_of_an_unpublished_store_prunes_after_verification(client):
    """No coverage means the index could not be emptied; prune what did not land."""
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(STORE).mock(return_value=httpx.Response(200))
    respx.get(f"{STORE}/coverages.json").mock(return_value=httpx.Response(200, json={"coverages": ""}))
    respx.delete(STORE).mock(return_value=httpx.Response(200))
    config_present()
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    pruned = respx.delete(f"{STORE}/coverages/s2/index/granules").mock(return_value=httpx.Response(200))
    index_holds(*COG_GRANULES, "s3://bucket/old.tif")

    MosaicManager(client).create(cog_mosaic(), replace=True)

    assert pruned.calls.last.request.url.params["filter"] == "location IN ('s3://bucket/old.tif')"


@respx.mock
def test_a_leftover_directory_with_a_config_is_reused_and_pruned(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(STORE).mock(return_value=httpx.Response(404))
    respx.get(STORE_DIR).mock(return_value=httpx.Response(200))
    patched = config_present()
    rmdir = respx.delete(STORE_DIR).mock(return_value=httpx.Response(200))
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    pruned = respx.delete(f"{STORE}/coverages/s2/index/granules").mock(return_value=httpx.Response(200))
    index_holds(*COG_GRANULES, "s3://bucket/old.tif")

    result = MosaicManager(client).create(cog_mosaic())

    assert result.ok
    assert patched.called and not rmdir.called
    assert pruned.called


@respx.mock
def test_a_leftover_directory_without_a_config_is_removed(client, caplog):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent(directory=True)
    removed = directory_deleted()
    index_holds(*COG_GRANULES)
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))

    MosaicManager(client).create(cog_mosaic())

    assert removed.called
    assert "left behind" in caplog.text


@respx.mock
def test_reused_index_after_a_directory_harvest_is_kept_with_a_warning(client, caplog):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(STORE).mock(return_value=httpx.Response(404))
    respx.get(STORE_DIR).mock(return_value=httpx.Response(200))
    config_present()
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/external.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    pruned = respx.delete(f"{STORE}/coverages/s2/index/granules").mock(return_value=httpx.Response(200))
    index_holds("/opt/granules/a.tif")

    definition = cog_mosaic()
    definition.cog = None
    definition.granules = ["/opt/granules"]
    result = MosaicManager(client).create(definition)

    assert result.published
    assert not pruned.called
    assert "keeps its existing rows" in caplog.text


@respx.mock
def test_uploaded_files_are_re_sent_when_a_directory_is_reused(client, tmp_path):
    granule = tmp_path / "20240303.tif"
    granule.write_bytes(b"tiff")
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(STORE).mock(return_value=httpx.Response(404))
    respx.get(STORE_DIR).mock(return_value=httpx.Response(200))
    config_present()
    upload = respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    index_holds("/srv/data/imagery/s2/20240303.tif")

    definition = cog_mosaic()
    definition.granules = []
    definition.upload_files = [granule]
    result = MosaicManager(client).create(definition)

    assert result.ok
    # The configuration archive, then the granule-only harvest archive.
    assert upload.call_count == 2


# ---------------------------------------------------------------------------
# A leftover index table with no directory: the fresh store is dead.  Once,
# bootstrap a configuration from the table (UseExistingSchema) and build over it.
# ---------------------------------------------------------------------------


@respx.mock
def test_create_bootstraps_a_config_over_a_leftover_table(client, caplog):
    import io
    import zipfile

    from geoserver_mosaic import properties

    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(STORE).mock(return_value=httpx.Response(404))
    # No directory at first; after the bootstrap store ran, its config exists.
    respx.get(STORE_DIR).mock(return_value=httpx.Response(404))
    bootstrapped = httpx.Response(200, text="Name=s2\nUseExistingSchema=true\n")
    respx.get(CONFIG).mock(
        # dead store: absent; bootstrap store: absent; real store: exists, then read
        side_effect=[httpx.Response(404), httpx.Response(404), bootstrapped, bootstrapped]
    )
    patched = respx.put(CONFIG).mock(return_value=httpx.Response(201))
    upload = respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    harvest = respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    publish = respx.post(f"{STORE}/coverages").mock(
        side_effect=[
            httpx.Response(500, text="The specified coverageName is unavailable"),
            httpx.Response(201),  # the bootstrap store
            httpx.Response(201),  # the real one
        ]
    )
    drop = respx.delete(STORE).mock(return_value=httpx.Response(200))
    pruned = respx.delete(f"{STORE}/coverages/s2/index/granules").mock(return_value=httpx.Response(200))
    index_holds(*COG_GRANULES, "s3://bucket/old.tif")

    result = MosaicManager(client).create(cog_mosaic())

    assert result.ok and result.created
    assert drop.call_count == 2
    assert all("purge" not in c.request.url.params for c in drop.calls)
    # dead store, bootstrap store, real store
    assert upload.call_count == 3
    with zipfile.ZipFile(io.BytesIO(upload.calls[1].request.content)) as archive:
        bootstrap = properties.loads(archive.read("indexer.properties").decode())
    assert bootstrap["UseExistingSchema"] == "true"
    with zipfile.ZipFile(io.BytesIO(upload.calls[2].request.content)) as archive:
        final = properties.loads(archive.read("indexer.properties").decode())
    assert final["UseExistingSchema"] == "false"
    written = properties.loads(patched.calls.last.request.content.decode())
    assert written["UseExistingSchema"] == "false"
    # 2 dead-store harvests + 1 bootstrap probe + 2 real harvests
    assert harvest.call_count == 5
    assert publish.call_count == 3
    assert pruned.calls.last.request.url.params["filter"] == "location IN ('s3://bucket/old.tif')"
    assert "left behind" in caplog.text


@respx.mock
def test_bootstrap_failure_names_the_table_to_drop(client, caplog):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    store_absent()
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(
        return_value=httpx.Response(500, text="The specified coverageName is unavailable")
    )
    respx.delete(STORE).mock(return_value=httpx.Response(200))

    with pytest.raises(GeoServerHTTPError):
        MosaicManager(client).create(cog_mosaic())
    assert "drop table 's2'" in caplog.text


# ---------------------------------------------------------------------------
# Deleting cleanly
# ---------------------------------------------------------------------------


@respx.mock
def test_delete_empties_the_index_and_keeps_the_directory(client):
    respx.get(STORE).mock(return_value=httpx.Response(200))
    respx.get(f"{STORE}.json").mock(
        return_value=httpx.Response(200, json={"coverageStore": {"url": "file:data/imagery/s2"}})
    )
    respx.get(f"{STORE}/coverages.json").mock(
        return_value=httpx.Response(200, json={"coverages": {"coverage": [{"name": "s2"}]}})
    )
    emptied = respx.delete(f"{STORE}/coverages/s2/index/granules").mock(return_value=httpx.Response(200))
    drop = respx.delete(STORE).mock(return_value=httpx.Response(200))
    rmdir = respx.delete(STORE_DIR).mock(return_value=httpx.Response(200))

    assert MosaicManager(client).delete("imagery", "s2") is True
    assert emptied.calls.last.request.url.params["filter"] == "INCLUDE"
    assert "purge" not in drop.calls.last.request.url.params
    assert not rmdir.called


@respx.mock
def test_delete_can_remove_the_directory_on_request(client):
    respx.get(STORE).mock(return_value=httpx.Response(200))
    respx.get(f"{STORE}.json").mock(
        return_value=httpx.Response(200, json={"coverageStore": {"url": "file:data/imagery/s2"}})
    )
    respx.get(f"{STORE}/coverages.json").mock(return_value=httpx.Response(200, json={"coverages": ""}))
    respx.delete(STORE).mock(return_value=httpx.Response(200))
    respx.get(STORE_DIR).mock(return_value=httpx.Response(200))
    rmdir = respx.delete(STORE_DIR).mock(return_value=httpx.Response(200))

    MosaicManager(client).delete("imagery", "s2", remove_directory=True)
    assert rmdir.called


@respx.mock
def test_delete_workspace_deletes_each_store_first(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(f"{BASE}/workspaces/imagery/coveragestores.json").mock(
        return_value=httpx.Response(200, json={"coverageStores": {"coverageStore": [{"name": "s2"}]}})
    )
    respx.get(STORE).mock(return_value=httpx.Response(200))
    respx.get(f"{STORE}.json").mock(
        return_value=httpx.Response(200, json={"coverageStore": {"url": "file:data/imagery/s2"}})
    )
    respx.get(f"{STORE}/coverages.json").mock(return_value=httpx.Response(200, json={"coverages": ""}))
    respx.delete(STORE).mock(return_value=httpx.Response(200))
    ws = respx.delete(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))

    assert MosaicManager(client).delete_workspace("imagery") is True
    assert ws.calls.last.request.url.params["recurse"] == "true"


@respx.mock
def test_external_store_replace_never_touches_the_data_directory(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(STORE).mock(return_value=httpx.Response(200))
    probe = respx.get(STORE_DIR).mock(return_value=httpx.Response(200))
    config = respx.get(CONFIG).mock(return_value=httpx.Response(200, text="Name=m\n"))
    respx.delete(STORE).mock(return_value=httpx.Response(200))
    respx.put(f"{STORE}/external.imagemosaic").mock(return_value=httpx.Response(201))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))

    MosaicManager(client).create(external_mosaic(mosaic_name="m"), replace=True)

    assert not probe.called and not config.called


@respx.mock
def test_an_explicit_purge_500_with_the_store_gone_is_tolerated(client, caplog):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(STORE).mock(side_effect=[httpx.Response(200), httpx.Response(404)])
    respx.get(f"{STORE}/coverages.json").mock(return_value=httpx.Response(200, json={"coverages": ""}))
    respx.delete(STORE).mock(return_value=httpx.Response(500, text="Unable to drop the database: "))
    respx.get(CONFIG).mock(return_value=httpx.Response(404))
    respx.get(STORE_DIR).mock(return_value=httpx.Response(404))
    index_holds(*COG_GRANULES)
    respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.post(f"{STORE}/remote.imagemosaic").mock(return_value=httpx.Response(202))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))

    result = MosaicManager(client).create(cog_mosaic(), replace=True, purge="metadata")

    assert result.created
    assert "the store is gone" in caplog.text


@respx.mock
def test_shapefile_indexed_replace_starts_from_a_fresh_directory(client, tmp_path):
    """An emptied shapefile index cannot be harvested into; the directory goes."""
    granule = tmp_path / "20240303.tif"
    granule.write_bytes(b"tiff")
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(200))
    respx.get(STORE).mock(return_value=httpx.Response(200))
    listing = respx.get(f"{STORE}/coverages.json")
    drop = respx.delete(STORE).mock(return_value=httpx.Response(200))
    respx.get(STORE_DIR).mock(return_value=httpx.Response(200))
    rmdir = respx.delete(STORE_DIR).mock(return_value=httpx.Response(200))
    config = respx.get(CONFIG).mock(return_value=httpx.Response(200, text="Name=s2\n"))
    upload = respx.put(f"{STORE}/file.imagemosaic").mock(return_value=httpx.Response(201))
    respx.get(f"{STORE}/coverages/s2").mock(return_value=httpx.Response(404))
    respx.post(f"{STORE}/coverages").mock(return_value=httpx.Response(201))
    index_holds("/srv/data/imagery/s2/20240303.tif")

    definition = MosaicDefinition(workspace="imagery", store="s2", upload_files=[granule])
    result = MosaicManager(client).create(definition, replace=True)

    assert result.ok
    assert not listing.called  # nothing to empty: the index goes with the directory
    assert drop.called and rmdir.called
    assert not config.called
    assert upload.call_count == 1  # the archive carries the files; no re-send
