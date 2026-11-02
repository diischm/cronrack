"""Version comparison without a dependency: semver, prereleases, k3s and Helm suffixes."""

import pytest

from rules.versions import compare, is_behind, k3s_update, minor_of, parse_version


def test_k3s_revision_orders_within_the_same_version():
    assert compare("v1.31.2+k3s1", "v1.31.2+k3s2") == -1
    assert compare("v1.31.2+k3s2", "v1.31.2+k3s1") == 1


def test_patch_wins_over_the_k3s_revision():
    assert compare("v1.31.2+k3s9", "v1.31.3+k3s1") == -1


def test_numbers_are_compared_as_numbers_not_text():
    assert compare("v1.31.10", "v1.31.9") == 1
    assert compare("v1.100.0", "v1.99.0") == 1


def test_prerelease_is_older_than_the_release():
    assert compare("v2.13.0-rc1", "v2.13.0") == -1
    assert compare("v2.13.0", "v2.13.0-rc1") == 1


def test_build_metadata_with_a_git_sha_is_ignored():
    assert compare("v3.16.2+g13654a5", "v3.16.2") == 0


def test_the_v_prefix_is_optional():
    assert compare("1.2.3", "v1.2.3") == 0


@pytest.mark.parametrize(
    "garbage",
    [
        None,
        5,
        [],
        "",
        "latest",
        "v1",
        "v1.2",
        "1.2.3.4",
        "v1.2.x",
        "v1.2.3 ",
        "v1.2.3\n",
        "v1.2.3;rm",
    ],
)
def test_garbage_is_none(garbage):
    assert parse_version(garbage) is None
    assert compare(garbage, "v1.0.0") is None
    assert compare("v1.0.0", garbage) is None
    assert is_behind(garbage, "v1.0.0") is False
    assert is_behind("v1.0.0", garbage) is False


def test_very_long_input_is_refused():
    assert parse_version("v1.2.3+" + "a" * 500) is None


def test_is_behind():
    assert is_behind("v1.31.2+k3s1", "v1.31.5+k3s1") is True
    assert is_behind("v1.31.5+k3s1", "v1.31.5+k3s1") is False
    assert is_behind("v1.32.0+k3s1", "v1.31.5+k3s1") is False


def test_minor_of():
    assert minor_of("v1.31.2+k3s1") == (1, 31)
    assert minor_of("nonsense") is None


CHANNELS = {"stable": "v1.33.1+k3s1", "v1.31": "v1.31.9+k3s1", "v1.33": "v1.33.1+k3s1"}


@pytest.mark.parametrize(
    ("installed", "channels", "state", "latest"),
    [
        # A newer patch in its own line comes first: that's the one to install.
        ("v1.31.2+k3s1", CHANNELS, "patch", "v1.31.9+k3s1"),
        # Patched, but a newer minor is the stable one: plan an upgrade.
        ("v1.31.9+k3s1", CHANNELS, "minor", "v1.33.1+k3s1"),
        ("v1.33.1+k3s1", CHANNELS, "current", "v1.33.1+k3s1"),
        ("v1.33.0+k3s1", {"stable": "v1.33.0+k3s1"}, "current", "v1.33.0+k3s1"),
        ("v1.33.1+k3s1", {}, None, None),  # no channel data: say nothing
        ("v1.31.2", CHANNELS, None, None),  # not k3s: never compared with k3s channels
        (None, CHANNELS, None, None),
        ("garbage", CHANNELS, None, None),
    ],
)
def test_k3s_update(installed, channels, state, latest):
    assert k3s_update(installed, channels) == {"state": state, "latest": latest}
