import pytest

from geoserver_mosaic import Features, UnsupportedVersionError, Version
from geoserver_mosaic.compat import extract_version


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2.28.4", (2, 28, 4)),
        ("3.0.0", (3, 0, 0)),
        ("3.0", (3, 0, 0)),
        ("3.0.0-SNAPSHOT", (3, 0, 0)),
        ("2.28-RC1", (2, 28, 0)),
    ],
)
def test_version_parsing(text, expected):
    assert Version.parse(text).tuple == expected


def test_versions_order():
    assert Version.parse("2.28.4") < Version.parse("3.0.0")
    assert Version.parse("2.28.10") > Version.parse("2.28.4")


def test_unparseable_version_raises():
    with pytest.raises(UnsupportedVersionError):
        Version.parse("unknown")


@pytest.mark.parametrize(
    "payload",
    [
        {"about": {"resource": [{"@name": "GeoServer", "Version": "2.28.4"}]}},
        {"about": {"resource": {"@name": "GeoServer", "Version": "2.28.4"}}},
        {"resource": [{"name": "GeoServer", "version": "2.28.4"}]},
        [{"@name": "GeoServer", "Version": "2.28.4"}],
    ],
)
def test_extract_version_handles_response_shapes(payload):
    assert extract_version(payload) == Version(2, 28, 4)


def test_extract_version_prefers_geoserver_over_other_components():
    payload = {
        "about": {
            "resource": [
                {"@name": "GeoWebCache", "Version": "1.28.0"},
                {"@name": "GeoServer", "Version": "2.28.4"},
            ]
        }
    }
    assert extract_version(payload) == Version(2, 28, 4)


def test_missing_version_raises():
    with pytest.raises(UnsupportedVersionError, match="No GeoServer version"):
        extract_version({"about": {}})


def test_supported_range():
    assert Features(Version.parse("2.28.4")).check_supported() == []
    assert Features(Version.parse("3.0.0")).check_supported() == []
    with pytest.raises(UnsupportedVersionError):
        Features(Version.parse("2.14.0")).check_supported()


def test_future_major_warns_but_proceeds():
    warnings = Features(Version.parse("4.0.0")).check_supported()
    assert warnings and "newer than this client" in warnings[0]


def test_feature_flags_for_both_targets():
    for text in ("2.28.4", "3.0.0"):
        features = Features(Version.parse(text))
        assert features.supports_cog
        assert features.supports_can_be_empty
