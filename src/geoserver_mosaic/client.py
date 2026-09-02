"""Low-level REST transport for GeoServer."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx

from . import payloads
from .compat import Features, Version, extract_version
from .models import is_remote_location
from .errors import (
    AuthenticationError,
    ConflictError,
    GeoServerHTTPError,
    NotFoundError,
)

log = logging.getLogger("geoserver_mosaic")

#: Status codes worth retrying: GeoServer behind a proxy returns these while
#: it is reloading the catalog, which happens routinely after a store create.
RETRY_STATUSES = frozenset({502, 503, 504})


def _q(segment: str) -> str:
    """Percent-encode one path segment.

    Workspace and store names may legitimately contain characters that would
    otherwise change the path shape.
    """
    return quote(str(segment), safe="")


def _clean_path(path: str) -> str:
    """Normalise a REST path so it never carries a trailing slash.

    GeoServer 3.0 rejects a trailing ``/`` on REST endpoints -- ``/workspaces/``
    is not the same route as ``/workspaces`` -- where 2.x tolerated it.  Since
    2.x is equally happy without one, stripping unconditionally keeps a single
    code path for both versions.

    Empty interior segments are collapsed for the same reason: they arise only
    from a caller joining path fragments that both carry a separator, and they
    would produce the same 404.
    """
    segments = [segment for segment in path.split("/") if segment]
    return "/".join(segments)


class GeoServerClient:
    """A connection to one GeoServer instance.

    ``base_url`` may be given with or without the trailing ``/rest``; both
    ``https://host/geoserver`` and ``https://host/geoserver/rest`` work.

    The client is a context manager and holds a pooled connection, so reuse one
    instance rather than creating one per call::

        with GeoServerClient("https://host/geoserver", "admin", "pw") as gs:
            gs.create_workspace("imagery")
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 60.0,
        verify: bool | str = True,
        retries: int = 3,
        backoff: float = 0.5,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = self._normalise(base_url)
        self.retries = max(0, retries)
        self.backoff = backoff
        self._features: Features | None = None
        self._client = httpx.Client(
            auth=httpx.BasicAuth(username, password),
            timeout=timeout,
            verify=verify,
            follow_redirects=True,
            headers={"Accept": "application/json", **(headers or {})},
            transport=transport,
        )

    @staticmethod
    def _normalise(base_url: str) -> str:
        url = base_url.rstrip("/")
        if not url.endswith("/rest"):
            url = f"{url}/rest"
        return url

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GeoServerClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- core request ------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        content: bytes | str | None = None,
        json_body: Any = None,
        content_type: str | None = None,
        accept: str | None = None,
        expected: tuple[int, ...] | None = None,
    ) -> httpx.Response:
        """Issue one REST call, retrying transient failures.

        Raises the appropriate :class:`~geoserver_mosaic.errors.GeoServerHTTPError`
        subclass on an unexpected status.
        """
        cleaned = _clean_path(path)
        # An empty path addresses the REST root, which must not gain a slash.
        url = f"{self.base_url}/{cleaned}" if cleaned else self.base_url
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        if accept:
            headers["Accept"] = accept

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    content=content,
                    json=json_body,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise
                self._sleep(attempt, f"{method} {url}: {exc}")
                continue

            if response.status_code in RETRY_STATUSES and attempt < self.retries:
                self._sleep(attempt, f"{method} {url}: HTTP {response.status_code}")
                continue

            self._check(response, method, url, expected)
            return response

        raise last_error or RuntimeError("unreachable")

    def _sleep(self, attempt: int, reason: str) -> None:
        delay = self.backoff * (2**attempt)
        log.warning("Retrying in %.1fs after %s", delay, reason)
        time.sleep(delay)

    @staticmethod
    def _check(
        response: httpx.Response,
        method: str,
        url: str,
        expected: tuple[int, ...] | None,
    ) -> None:
        if expected is not None:
            if response.status_code in expected:
                return
        elif response.is_success:
            return
        body = response.text
        status = response.status_code
        if status == 404:
            raise NotFoundError(status, method, url, body)
        if status in (401, 403):
            raise AuthenticationError(status, method, url, body)
        if status in (409, 500) and "already exists" in body.lower():
            # GeoServer answers a duplicate create with 500 on some endpoints.
            raise ConflictError(status, method, url, body)
        raise GeoServerHTTPError(status, method, url, body)

    def get_json(self, path: str, **kwargs: Any) -> Any:
        response = self.request("GET", path, accept="application/json", **kwargs)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def exists(self, path: str) -> bool:
        try:
            self.request("GET", path)
        except NotFoundError:
            return False
        return True

    # -- server info -------------------------------------------------------

    @property
    def features(self) -> Features:
        """Version-derived capabilities, fetched once and cached."""
        if self._features is None:
            self._features = Features(self.version())
            for warning in self._features.check_supported():
                log.warning("%s", warning)
        return self._features

    def version(self) -> Version:
        """The connected server's GeoServer version."""
        return extract_version(self.get_json("about/version.json"))

    def reload(self) -> None:
        """Force a catalog + configuration reload from disk."""
        self.request("POST", "reload")

    def reset(self) -> None:
        """Drop cached store connections without re-reading the catalog.

        Useful after granules change on disk behind GeoServer's back.
        """
        self.request("POST", "reset")

    # -- workspaces --------------------------------------------------------

    def list_workspaces(self) -> list[str]:
        payload = self.get_json("workspaces.json")
        return _names(payload, "workspaces", "workspace")

    def workspace_exists(self, name: str) -> bool:
        return self.exists(f"workspaces/{_q(name)}")

    def create_workspace(
        self,
        name: str,
        *,
        uri: str | None = None,
        isolated: bool = False,
        exist_ok: bool = True,
    ) -> bool:
        """Create a workspace; returns True if it was created here.

        With ``uri`` set, the workspace's namespace URI is set too, which
        matters if the layers are consumed over WFS/WMS by clients that pin
        namespaces.
        """
        if self.workspace_exists(name):
            if exist_ok:
                log.debug("Workspace %s already exists", name)
                return False
            raise ConflictError(409, "POST", f"{self.base_url}/workspaces", f"Workspace {name} already exists")
        if uri:
            body = payloads.namespace(name, uri)
            self.request("POST", "namespaces", content=body, content_type="text/xml")
        else:
            body = payloads.workspace(name, isolated=isolated)
            self.request("POST", "workspaces", content=body, content_type="text/xml")
        log.info("Created workspace %s", name)
        return True

    def delete_workspace(self, name: str, *, recurse: bool = False) -> None:
        self.request(
            "DELETE", f"workspaces/{_q(name)}", params={"recurse": str(recurse).lower()}
        )

    # -- data stores (used as shared mosaic indexes) ------------------------

    def datastore_exists(self, workspace: str, name: str) -> bool:
        return self.exists(f"workspaces/{_q(workspace)}/datastores/{_q(name)}")

    def create_postgis_datastore(
        self,
        workspace: str,
        name: str,
        *,
        host: str,
        database: str,
        user: str,
        password: str,
        port: int = 5432,
        schema: str = "public",
        extra: Mapping[str, Any] | None = None,
        exist_ok: bool = True,
    ) -> bool:
        """Register a PostGIS store, so mosaics can share one connection pool."""
        if self.datastore_exists(workspace, name):
            if exist_ok:
                return False
            raise ConflictError(
                409, "POST", f"{self.base_url}/workspaces/{workspace}/datastores",
                f"Data store {name} already exists",
            )
        body = payloads.postgis_datastore(
            name,
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            schema=schema,
            extra=dict(extra or {}),
        )
        self.request(
            "POST",
            f"workspaces/{_q(workspace)}/datastores",
            content=body,
            content_type="text/xml",
        )
        log.info("Created PostGIS data store %s:%s", workspace, name)
        return True

    # -- coverage stores ---------------------------------------------------

    def list_coverage_stores(self, workspace: str) -> list[str]:
        payload = self.get_json(f"workspaces/{_q(workspace)}/coveragestores.json")
        return _names(payload, "coverageStores", "coverageStore")

    def coverage_store_exists(self, workspace: str, store: str) -> bool:
        return self.exists(f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}")

    def get_coverage_store(self, workspace: str, store: str) -> dict[str, Any]:
        payload = self.get_json(
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}.json"
        )
        return payload.get("coverageStore", payload) if isinstance(payload, dict) else {}

    def delete_coverage_store(
        self, workspace: str, store: str, *, recurse: bool = True, purge: str | None = None
    ) -> None:
        """Delete a coverage store.

        ``purge`` controls granule file removal: ``"none"`` (default server
        behaviour) keeps files, ``"metadata"`` drops the index only,
        ``"all"`` deletes the granule files too.  Only ``"none"`` is safe for
        remote COG granules you do not own.
        """
        params: dict[str, Any] = {"recurse": str(recurse).lower()}
        if purge is not None:
            params["purge"] = _purge_value(purge)
        self.request(
            "DELETE", f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}", params=params
        )

    def upload_mosaic_archive(
        self,
        workspace: str,
        store: str,
        archive: bytes,
        *,
        configure: str = "none",
    ) -> None:
        """PUT a ZIP to ``file.imagemosaic``, creating or updating the store.

        ``configure`` is GeoServer's publish policy: ``none`` creates the store
        without publishing any layer (which is what you want when the coverage
        is configured explicitly afterwards), ``first`` publishes the first
        coverage found, ``all`` publishes every one.
        """
        self.request(
            "PUT",
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}/file.imagemosaic",
            params={"configure": configure},
            content=archive,
            content_type="application/zip",
        )
        log.info("Uploaded mosaic configuration to %s:%s", workspace, store)

    def create_store_from_external_dir(
        self,
        workspace: str,
        store: str,
        directory_url: str,
        *,
        configure: str = "none",
    ) -> None:
        """Point a store at a directory already present on the GeoServer host.

        ``directory_url`` must be a ``file:`` URL that GeoServer itself can
        resolve, e.g. ``file:///var/geoserver/mosaics/rgb/``.  It travels in the
        request body, not the REST path, so a trailing slash is fine here and is
        conventional for a directory -- the no-trailing-slash rule that
        :func:`_clean_path` enforces applies only to endpoint paths.
        """
        self.request(
            "PUT",
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}/external.imagemosaic",
            params={"configure": configure},
            content=directory_url,
            content_type="text/plain",
        )
        log.info("Created store %s:%s from %s", workspace, store, directory_url)

    def harvest(
        self, workspace: str, store: str, location: str, *, endpoint: str | None = None
    ) -> None:
        """Add a granule to a mosaic without uploading it.

        GeoServer has two harvest endpoints and they are not interchangeable:

        ``external.imagemosaic``
            For data on the GeoServer host -- an absolute path, a ``file:`` URL,
            or a directory to scan recursively.  It resolves its body as a local
            file and rejects anything it cannot open, so a remote URL fails here
            with "Failed to locate the input file".
        ``remote.imagemosaic``
            For granules GeoServer must fetch over the network: ``http(s)://``,
            ``s3://``, ``gs://`` or Azure URLs, on a ``Cog=true`` mosaic.  The
            first granule posted here also initialises an empty mosaic, creating
            the index table and the coverage.

        The endpoint is chosen from the location's scheme; pass ``endpoint`` as
        ``"remote"`` or ``"external"`` to override that.
        """
        if endpoint is None:
            endpoint = "remote" if is_remote_location(location) else "external"
        if endpoint not in ("remote", "external"):
            raise ValueError(f"endpoint must be 'remote' or 'external', got {endpoint!r}")
        self.request(
            "POST",
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}/{endpoint}.imagemosaic",
            content=location,
            content_type="text/plain",
        )
        log.info("Harvested %s into %s:%s via %s", location, workspace, store, endpoint)

    # -- coverages ---------------------------------------------------------

    def list_coverages(
        self, workspace: str, store: str, *, available: bool = False
    ) -> list[str]:
        """List coverages in a store.

        ``available=True`` lists what the store *could* publish but has not yet,
        which is how you discover the mosaic's native name after creating a
        store with ``configure=none``.
        """
        params = {"list": "available"} if available else None
        payload = self.get_json(
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}/coverages.json",
            params=params,
        )
        if available:
            return _names(payload, "list", "string", plain=True)
        return _names(payload, "coverages", "coverage")

    def coverage_exists(self, workspace: str, store: str, coverage: str) -> bool:
        return self.exists(
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}/coverages/{_q(coverage)}"
        )

    def create_coverage(self, workspace: str, store: str, body: str) -> None:
        self.request(
            "POST",
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}/coverages",
            content=body,
            content_type="text/xml",
        )

    def update_coverage(
        self, workspace: str, store: str, coverage: str, body: str
    ) -> None:
        self.request(
            "PUT",
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}/coverages/{_q(coverage)}",
            content=body,
            content_type="text/xml",
        )

    def get_coverage(self, workspace: str, store: str, coverage: str) -> dict[str, Any]:
        payload = self.get_json(
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}/coverages/{_q(coverage)}.json"
        )
        return payload.get("coverage", payload) if isinstance(payload, dict) else {}

    def delete_coverage(
        self, workspace: str, store: str, coverage: str, *, recurse: bool = True
    ) -> None:
        self.request(
            "DELETE",
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}/coverages/{_q(coverage)}",
            params={"recurse": str(recurse).lower()},
        )

    # -- granule index -----------------------------------------------------

    def index_schema(self, workspace: str, store: str, coverage: str) -> dict[str, Any]:
        """The mosaic index's attribute schema, as GeoServer sees it."""
        payload = self.get_json(
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}"
            f"/coverages/{_q(coverage)}/index.json"
        )
        return payload if isinstance(payload, dict) else {}

    def list_granules(
        self,
        workspace: str,
        store: str,
        coverage: str,
        *,
        filter: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """List granules as GeoJSON features.

        ``filter`` is a CQL expression evaluated against the index, e.g.
        ``"time AFTER 2024-01-01T00:00:00Z"``.
        """
        params: dict[str, Any] = {}
        if filter:
            params["filter"] = filter
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        payload = self.get_json(
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}"
            f"/coverages/{_q(coverage)}/index/granules.json",
            params=params or None,
        )
        if isinstance(payload, dict):
            features = payload.get("features") or []
            return [f for f in features if isinstance(f, dict)]
        return []

    def iter_granules(
        self,
        workspace: str,
        store: str,
        coverage: str,
        *,
        filter: str | None = None,
        page_size: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        """Page through the granule index.

        Large mosaics can hold millions of granules; asking for all of them in
        one response is a reliable way to time out.
        """
        offset = 0
        while True:
            batch = self.list_granules(
                workspace, store, coverage, filter=filter, limit=page_size, offset=offset
            )
            if not batch:
                return
            yield from batch
            if len(batch) < page_size:
                return
            offset += len(batch)

    def delete_granules(
        self,
        workspace: str,
        store: str,
        coverage: str,
        *,
        filter: str | None = None,
        purge: bool | str = False,
    ) -> None:
        """Remove granules from the index.

        ``filter`` is a CQL expression evaluated against the index.  Omitting it
        empties the whole index: GeoServer rejects a filterless delete with 400,
        so the CQL match-everything filter ``INCLUDE`` is sent instead.

        ``purge`` takes GeoServer's own vocabulary -- ``"none"``, ``"metadata"``
        or ``"all"`` -- and accepts booleans for convenience.  It is *not* a
        boolean on the wire: sending ``purge=false`` is rejected with 400.
        ``"all"`` deletes the underlying granule files, so never use it on
        remote granules, or on local ones you do not own.
        """
        params: dict[str, Any] = {
            "purge": _purge_value(purge),
            "filter": filter or "INCLUDE",
        }
        self.request(
            "DELETE",
            f"workspaces/{_q(workspace)}/coveragestores/{_q(store)}"
            f"/coverages/{_q(coverage)}/index/granules",
            params=params,
        )

    # -- layers ------------------------------------------------------------

    def layer_exists(self, workspace: str, name: str) -> bool:
        return self.exists(f"layers/{_q(f'{workspace}:{name}')}")

    def update_layer(self, workspace: str, name: str, body: str) -> None:
        self.request(
            "PUT",
            f"layers/{_q(f'{workspace}:{name}')}",
            content=body,
            content_type="text/xml",
        )

    def set_default_style(self, workspace: str, name: str, style: str) -> None:
        self.update_layer(workspace, name, payloads.layer(default_style=style))


def _purge_value(purge: bool | str | None) -> str:
    """Map a purge argument onto GeoServer's ``none|metadata|all`` vocabulary.

    Booleans are accepted because they read naturally, but they must never
    reach the wire: GeoServer rejects ``purge=false`` with a bare 400.
    """
    if purge is None or purge is False:
        return "none"
    if purge is True:
        return "all"
    value = str(purge).lower()
    if value not in ("none", "metadata", "all"):
        raise ValueError(
            f"purge must be one of 'none', 'metadata', 'all' (or a bool), got {purge!r}"
        )
    return value


def _names(payload: Any, container: str, item: str, *, plain: bool = False) -> list[str]:
    """Extract names from a GeoServer list response.

    An empty list is serialised as the empty string rather than ``[]``, and a
    single-element list may collapse to an object, so both shapes are handled.
    """
    if not isinstance(payload, dict):
        return []
    node = payload.get(container)
    if not isinstance(node, dict):
        return []
    entries = node.get(item)
    if entries in (None, ""):
        return []
    if isinstance(entries, dict) or isinstance(entries, str):
        entries = [entries]
    result: list[str] = []
    for entry in entries:
        if plain and isinstance(entry, str):
            result.append(entry)
        elif isinstance(entry, dict) and "name" in entry:
            result.append(str(entry["name"]))
    return result
