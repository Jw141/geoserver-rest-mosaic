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
    assert route.calls.last.request.url.params["purge"] == "false"


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


@respx.mock
def test_create_runs_the_full_bootstrap_sequence(client):
    respx.get(f"{BASE}/workspaces/imagery").mock(return_value=httpx.Response(404))
    respx.post(f"{BASE}/workspaces").mock(return_value=httpx.Response(201))
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
    respx.get(f"{BASE}/workspaces/imagery/coveragestores/s2").mock(
        return_value=httpx.Response(200)
    )
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
    client.create_store_from_external_dir("imagery", "s2", "file:///data/tiles/")
    assert route.calls.last.request.content == b"file:///data/tiles/"


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
    respx.get(f"{BASE}/workspaces/imagery/coveragestores/s2").mock(
        return_value=httpx.Response(200)
    )
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
