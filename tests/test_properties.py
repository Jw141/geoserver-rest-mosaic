import pytest

from geoserver_mosaic import properties


def test_key_with_space_is_escaped():
    # PostGIS mosaic options like "Loose bbox" are silently truncated at the
    # space unless escaped, which is a classic broken-mosaic cause.
    assert properties.dumps({"Loose bbox": True}) == "Loose\\ bbox=true\n"


def test_backslash_in_value_is_escaped():
    rendered = properties.dumps({"path": r"C:\data\mosaic"})
    assert rendered == "path=C:\\\\data\\\\mosaic\n"
    assert properties.loads(rendered)["path"] == r"C:\data\mosaic"


def test_none_values_are_omitted():
    assert properties.dumps({"a": 1, "b": None}) == "a=1\n"


def test_booleans_render_java_style():
    assert properties.dumps({"a": True, "b": False}) == "a=true\nb=false\n"


@pytest.mark.parametrize(
    "mapping",
    [
        {"simple": "value"},
        {"Loose bbox": "true", "Estimated extends": "false"},
        {"passwd": "p@ss:word=x", "user": "gis"},
        {"regex": r"[0-9]{8}T[0-9]{6}Z"},
        {"leading": " space"},
    ],
)
def test_round_trip(mapping):
    assert properties.loads(properties.dumps(mapping)) == mapping


def test_loads_handles_comments_and_separators():
    parsed = properties.loads(
        "# a comment\n! another\n\nkey1=v1\nkey2 : v2\nkey3 v3\n"
    )
    assert parsed == {"key1": "v1", "key2": "v2", "key3": "v3"}


def test_loads_handles_line_continuation():
    assert properties.loads("key=one\\\ntwo\n") == {"key": "onetwo"}


def test_loads_decodes_unicode_escapes():
    assert properties.loads("k=caf\\u00e9\n") == {"k": "café"}


def test_header_is_commented():
    assert properties.dumps({"a": 1}, header="line one\nline two").startswith(
        "# line one\n# line two\n"
    )
