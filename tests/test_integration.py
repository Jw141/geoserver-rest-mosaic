"""Integration tests against the docker-compose stack.

These are skipped unless a GeoServer is actually reachable, so the default
``pytest`` run stays hermetic::

    make up                 # or: make up-gs3
    make integration        # or: make integration URL=http://localhost:8081/geoserver

They assert the thing unit tests cannot: that GeoServer accepts the generated
configuration, indexes the granules, and renders the mosaic.
"""

from __future__ import annotations

import os

import httpx
import pytest

from geoserver_mosaic import GeoServerClient, MosaicManager

URL = os.environ.get("GEOSERVER_URL", "http://localhost:8080/geoserver")
USER = os.environ.get("GEOSERVER_USER", "admin")
PASSWORD = os.environ.get("GEOSERVER_PASSWORD", "geoserver")


def _reachable() -> bool:
    try:
        response = httpx.get(f"{URL}/rest/about/version.json", auth=(USER, PASSWORD), timeout=5)
        return response.status_code < 500
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _reachable(), reason=f"No GeoServer at {URL}"),
]


@pytest.fixture(scope="module")
def client():
    with GeoServerClient(URL, USER, PASSWORD, timeout=180) as gs:
        yield gs


@pytest.fixture(scope="module")
def settings():
    import driver

    return driver.Settings.from_env(URL)


@pytest.fixture(scope="module")
def manager(client):
    return MosaicManager(client)


@pytest.fixture(scope="module", autouse=True)
def clean_workspace(client):
    """Start from a clean slate, and leave one behind on the way out."""
    import driver

    def drop() -> None:
        if client.workspace_exists(driver.WORKSPACE):
            # recurse drops the stores and layers too; granule files are never
            # purged, so the fixtures survive.
            client.delete_workspace(driver.WORKSPACE, recurse=True)

    drop()
    yield
    drop()


def test_server_is_a_supported_version(client):
    version = client.version()
    assert version.major in (2, 3), version
    assert client.features.check_supported() == []


@pytest.mark.parametrize(
    "label,pattern",
    [("gs-cog-http", "gs-cog-http.*"), ("gs-cog-s3", "gs-cog-s3.*")],
)
def test_cog_modules_are_installed(label, pattern, client):
    """Remote COG granules silently fail at read time without these.

    The Kartoza image installs them from COMMUNITY_EXTENSIONS; cog-http carries
    the HTTP range reader and cog-s3 the S3 one -- independent packages.
    """
    import driver

    assert driver.installed_modules(client, pattern), (
        f"{label} is not installed; check GS_COMMUNITY_EXTENSIONS in .env "
        f"(expected cog-http-plugin,cog-s3-plugin) and recreate the container"
    )


@pytest.mark.parametrize("which", ["local", "remote", "upload"])
def test_mosaic_is_created_and_indexed(which, client, manager, settings):
    import driver

    definition = driver.BUILDERS[which](settings)
    result = manager.create(definition, replace=True)

    assert not result.failed, result.failed
    assert result.published, "coverage was not published"

    granules = list(client.iter_granules(result.workspace, result.store, result.coverage))
    assert len(granules) == 12, f"expected 12 granules, indexed {len(granules)}"

    # Three timestamps, four spatial tiles each: the time dimension must have
    # collapsed the tiles into three distinct instants.
    times = {g["properties"]["time"] for g in granules}
    assert len(times) == 3, times


@pytest.mark.parametrize("which", ["local", "remote", "upload"])
def test_mosaic_renders_through_wms(which, settings):
    import driver

    definition = driver.BUILDERS[which](settings)
    layer = f"{driver.WORKSPACE}:{definition.resolved_coverage()}"
    response = httpx.get(driver.wms_url(settings, layer), auth=(USER, PASSWORD), timeout=120)

    response.raise_for_status()
    # GeoServer reports rendering failures as an XML ServiceException with a
    # 200 status, so the content type is the real assertion.
    assert response.headers["content-type"].startswith("image/png"), response.text[:500]
    assert response.content.startswith(b"\x89PNG")
    assert len(response.content) > 1000, "suspiciously small image"


def test_time_dimension_is_advertised_in_capabilities(settings):
    import driver

    response = httpx.get(
        f"{settings.wms_base}/wms",
        params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"},
        auth=(USER, PASSWORD),
        timeout=120,
    )
    response.raise_for_status()
    assert 'name="time"' in response.text or "<Dimension" in response.text


def test_granules_can_be_filtered_and_removed(client, manager, settings):
    import driver

    definition = driver.BUILDERS["local"](settings)
    coverage = definition.resolved_coverage()

    before = list(client.iter_granules(definition.workspace, definition.store, coverage))
    assert before

    # purge=False: drop the index entries, leave the files alone.
    manager.remove_granules(
        definition, filter="time BEFORE 2024-03-03T00:00:00Z", purge=False
    )
    after = list(client.iter_granules(definition.workspace, definition.store, coverage))
    assert len(after) < len(before)
    assert all(g["properties"]["time"] > "2024-03-03" for g in after)
