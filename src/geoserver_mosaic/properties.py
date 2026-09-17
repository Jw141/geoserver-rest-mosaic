"""Serialisation of Java ``.properties`` files.

The ImageMosaic reader is configured entirely through properties files
(``indexer.properties``, ``datastore.properties``, ``timeregex.properties``).
Java's format has escaping rules that bite in practice -- notably that a space
inside a *key* terminates the key unless escaped, which is exactly what happens
with the PostGIS options ``Loose bbox`` and ``Estimated extends``.
"""

from __future__ import annotations

from collections.abc import Mapping

# Characters that must be escaped when they appear in a key.  In a value only
# the backslash and a leading space are significant, because everything after
# the first unescaped separator is taken verbatim.
_KEY_SPECIALS = {"=", ":", " ", "\t", "#", "!"}


def escape_key(key: str) -> str:
    out: list[str] = []
    for char in key:
        if char == "\\":
            out.append("\\\\")
        elif char in _KEY_SPECIALS:
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        else:
            out.append(char)
    return "".join(out)


def escape_value(value: str) -> str:
    out: list[str] = []
    for index, char in enumerate(value):
        if char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif char == " " and index == 0:
            # Leading whitespace would otherwise be stripped as padding.
            out.append("\\ ")
        else:
            out.append(char)
    return "".join(out)


def _coerce(value: object) -> str:
    """Render a Python value the way the Java-side parser expects it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def dumps(mapping: Mapping[str, object], *, header: str | None = None) -> str:
    """Render a mapping as a ``.properties`` document.

    Entries whose value is ``None`` are dropped, which lets the config
    dataclasses express "leave this at the GeoServer default" by simply not
    setting the field.
    """
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}" for line in header.splitlines())
    for key, value in mapping.items():
        if value is None:
            continue
        lines.append(f"{escape_key(key)}={escape_value(_coerce(value))}")
    return "\n".join(lines) + "\n"


def loads(text: str) -> dict[str, str]:
    """Parse a ``.properties`` document.

    Only what this library needs: comments, blank lines, the three separators
    and backslash escapes including line continuations.  Unicode ``\\uXXXX``
    escapes are decoded as well since GeoServer writes them for non-ASCII.
    """
    result: dict[str, str] = {}
    for logical_line in _logical_lines(text):
        stripped = logical_line.strip()
        if not stripped or stripped[0] in "#!":
            continue
        key, value = _split_entry(logical_line)
        result[_unescape(key)] = _unescape(value)
    return result


def _logical_lines(text: str) -> list[str]:
    """Join lines ending in an odd number of backslashes with the next line."""
    lines = text.splitlines()
    joined: list[str] = []
    buffer = ""
    for line in lines:
        candidate = buffer + line
        trailing = len(candidate) - len(candidate.rstrip("\\"))
        if trailing % 2 == 1:
            # Odd trailing backslash: continuation, drop it and keep going.
            buffer = candidate[:-1]
            continue
        joined.append(candidate)
        buffer = ""
    if buffer:
        joined.append(buffer)
    return joined


def _split_entry(line: str) -> tuple[str, str]:
    """Split on the first unescaped ``=``, ``:`` or whitespace run."""
    index = 0
    line = line.lstrip()
    while index < len(line):
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char in "=: \t":
            key = line[:index]
            rest = line[index:]
            # Skip the separator plus any padding around it.
            rest = rest.lstrip(" \t")
            if rest[:1] in ("=", ":"):
                rest = rest[1:].lstrip(" \t")
            return key, rest
        index += 1
    return line, ""


def _unescape(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(text):
            break
        marker = text[index]
        if marker == "u" and index + 4 < len(text):
            out.append(chr(int(text[index + 1 : index + 5], 16)))
            index += 5
            continue
        out.append({"n": "\n", "r": "\r", "t": "\t", "f": "\f"}.get(marker, marker))
        index += 1
    return "".join(out)
