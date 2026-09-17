"""Exception hierarchy for the GeoServer client."""

from __future__ import annotations


class GeoServerError(Exception):
    """Base class for every error raised by this library."""


class GeoServerHTTPError(GeoServerError):
    """A REST call returned a non-success status code.

    GeoServer is inconsistent about error bodies: some endpoints return an HTML
    error page, some a bare text line, some a stack trace.  ``message`` holds a
    best-effort extraction, ``body`` the raw text.
    """

    def __init__(self, status_code: int, method: str, url: str, body: str) -> None:
        self.status_code = status_code
        self.method = method
        self.url = url
        self.body = body
        self.message = _summarise(body)
        detail = f": {self.message}" if self.message else ""
        super().__init__(f"{method} {url} -> HTTP {status_code}{detail}")


class NotFoundError(GeoServerHTTPError):
    """The requested resource does not exist (HTTP 404)."""


class ConflictError(GeoServerHTTPError):
    """The resource already exists, or the server refused a duplicate (HTTP 409)."""


class AuthenticationError(GeoServerHTTPError):
    """Credentials were rejected or insufficient (HTTP 401/403)."""


class UnsupportedVersionError(GeoServerError):
    """The connected GeoServer is outside the range this client supports."""


class MosaicConfigurationError(GeoServerError):
    """The requested mosaic configuration is internally inconsistent.

    Raised before any HTTP traffic happens, so the caller gets a clear message
    instead of an opaque 500 from the mosaic reader.
    """


def _summarise(body: str, limit: int = 500) -> str:
    """Pull a human-readable line out of a GeoServer error body."""
    if not body:
        return ""
    text = body.strip()
    # HTML error pages: grab the <title> or the first <h1>/<h2>, which usually
    # carry the actual message ("Store 'foo' already exists in workspace 'bar'").
    if text.lstrip().lower().startswith(("<!doctype", "<html")):
        import re

        for pattern in (r"<title>(.*?)</title>", r"<h[12][^>]*>(.*?)</h[12]>"):
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                stripped = re.sub(r"<[^>]+>", " ", match.group(1))
                collapsed = " ".join(stripped.split())
                if collapsed:
                    return collapsed[:limit]
        return "(HTML error page)"
    # Java stack traces: the first line is the useful part.
    first_line = text.splitlines()[0].strip()
    return (first_line or text)[:limit]
