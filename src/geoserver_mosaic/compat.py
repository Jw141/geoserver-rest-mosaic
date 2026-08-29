"""GeoServer version detection and version-dependent behaviour.

This library targets GeoServer 2.28.x and 3.0.x from one code path.  The REST
surface used here -- workspaces, coverage stores, coverages, the
``file.imagemosaic`` / ``external.imagemosaic`` upload endpoints and the
granule index -- is stable across that range, so the compatibility work is
mostly about being tolerant rather than branching:

* ask for JSON but accept XML, and parse coverage payloads as XML where the
  JSON mapping of ``metadata``/``parameters`` entries is ambiguous;
* tolerate both the ``{"about": {"resource": [...]}}`` shape and a bare list
  from ``/rest/about/version``;
* treat a missing optional endpoint as "feature absent", not as a hard error.

Confirmed 3.0 differences:

* **No trailing slash on REST paths.**  ``/rest/workspaces/`` is not the same
  route as ``/rest/workspaces`` and 3.0 rejects the former, where 2.x tolerated
  it.  Handled unconditionally in ``client._clean_path`` rather than by a
  version branch, since 2.x is equally happy without the slash.

Where a difference genuinely needs different behaviour per version, add a flag
to :class:`Features` and branch on that, rather than sprinkling version checks
through the client.  Prefer a single tolerant code path where one exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Any

from .errors import UnsupportedVersionError

#: Oldest release this client is tested against.
MINIMUM_SUPPORTED = (2, 28)
#: Newest major release this client knows about.  Later majors are allowed but
#: warned about, since REST changes cannot be anticipated here.
MAXIMUM_KNOWN_MAJOR = 3


@total_ordering
@dataclass(frozen=True)
class Version:
    """A parsed GeoServer version.

    ``raw`` keeps the original string, which may carry qualifiers such as
    ``2.28.4`` or ``3.0.0-SNAPSHOT``.
    """

    major: int
    minor: int
    patch: int = 0
    raw: str = ""

    @classmethod
    def parse(cls, text: str) -> "Version":
        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
        if not match:
            raise UnsupportedVersionError(f"Could not parse GeoServer version {text!r}")
        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3) or 0),
            raw=(text or "").strip(),
        )

    def __str__(self) -> str:
        return self.raw or f"{self.major}.{self.minor}.{self.patch}"

    @property
    def tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self.tuple < other.tuple

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self.tuple == other.tuple


@dataclass(frozen=True)
class Features:
    """Version-dependent capabilities, resolved once per connection."""

    version: Version

    @property
    def is_v3(self) -> bool:
        return self.version.major >= 3

    @property
    def supports_cog(self) -> bool:
        """COG range readers exist from 2.17 onward (as a community module)."""
        return self.version >= Version(2, 17)

    @property
    def supports_can_be_empty(self) -> bool:
        """``CanBeEmpty`` lets a mosaic be created with zero granules."""
        return self.version >= Version(2, 15)

    @property
    def supports_nearest_match(self) -> bool:
        return self.version >= Version(2, 15)

    def check_supported(self) -> list[str]:
        """Return warnings; raise only when the version is clearly too old."""
        warnings: list[str] = []
        if self.version.tuple[:2] < MINIMUM_SUPPORTED:
            raise UnsupportedVersionError(
                f"GeoServer {self.version} is older than the minimum supported "
                f"{MINIMUM_SUPPORTED[0]}.{MINIMUM_SUPPORTED[1]}"
            )
        if self.version.major > MAXIMUM_KNOWN_MAJOR:
            warnings.append(
                f"GeoServer {self.version} is newer than this client knows "
                f"about (up to {MAXIMUM_KNOWN_MAJOR}.x); REST calls may have "
                f"changed. Proceeding on the assumption they have not."
            )
        return warnings


def extract_version(payload: Any) -> Version:
    """Pull the GeoServer version out of an ``/about/version`` response.

    Handles the documented shape as well as the variations seen in practice:
    the resource list may be a bare object when only one component is present,
    and the version key has been observed as ``Version`` and ``version``.
    """
    resources = _resource_list(payload)
    # Prefer the component actually named GeoServer; some deployments list
    # GeoWebCache and GeoTools alongside it, with different version numbers.
    for wanted in ("geoserver", None):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            name = str(resource.get("@name") or resource.get("name") or "").lower()
            if wanted is not None and name != wanted:
                continue
            for key in ("Version", "version", "Build-Timestamp"):
                value = resource.get(key)
                if isinstance(value, str) and re.search(r"\d+\.\d+", value):
                    return Version.parse(value)
    raise UnsupportedVersionError(
        f"No GeoServer version found in /about/version response: {payload!r}"
    )


def _resource_list(payload: Any) -> list[Any]:
    node = payload
    if isinstance(node, dict) and "about" in node:
        node = node["about"]
    if isinstance(node, dict) and "resource" in node:
        node = node["resource"]
    if isinstance(node, dict):
        return [node]
    if isinstance(node, list):
        return node
    return []
