"""Version comparison for the update rules. Plain Python, no packaging dependency.

Understands what shows up in practice: v1.31.2, v2.13.0-rc1, v1.31.2+k3s1 and
v3.16.2+g13654a5. Anything else is "unknown" (None), and the rules that use it
stay quiet instead of guessing.
"""

import re

MAX_LENGTH = 64

_VERSION = re.compile(
    r"v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:-([0-9A-Za-z.-]+))?(?:\+(?:k3s(\d{1,6})|[0-9A-Za-z.-]+))?"
)


def parse_version(text) -> tuple[int, int, int, int, int] | None:
    """(major, minor, patch, release flag, k3s revision), or None if it isn't a version.

    The release flag is 0 for a prerelease and 1 for a release, so v2.13.0-rc1 sorts
    before v2.13.0. Any other "+build" suffix (a git sha) is ignored.
    """
    if not isinstance(text, str) or len(text) > MAX_LENGTH:
        return None
    match = _VERSION.fullmatch(text)
    if match is None:
        return None
    major, minor, patch, prerelease, k3s_revision = match.groups()
    return (
        int(major),
        int(minor),
        int(patch),
        0 if prerelease else 1,
        int(k3s_revision) if k3s_revision else 0,
    )


def compare(a, b) -> int | None:
    """-1, 0 or 1 like a three-way compare, or None if either side isn't a version."""
    left, right = parse_version(a), parse_version(b)
    if left is None or right is None:
        return None
    return (left > right) - (left < right)


def is_behind(current, latest) -> bool:
    """True only when both are versions and `current` is older than `latest`."""
    return compare(current, latest) == -1


def minor_of(text) -> tuple[int, int] | None:
    """(major, minor), e.g. (1, 31) for v1.31.2+k3s1."""
    parsed = parse_version(text)
    return None if parsed is None else parsed[:2]


def k3s_update(installed, channels) -> dict:
    """What the k3s release channels say about an installed k3s version.

    {"state", "latest"}: "patch" (a newer patch exists in its own minor line; latest is
    that patch), "minor" (patched, but a newer minor is the stable one), "current", or None
    (not a k3s version, or no channel data). The same logic as the k3s_patch_behind and
    k3s_minor_behind rules, so the pages and the findings agree.
    """
    unknown = {"state": None, "latest": None}
    minor = minor_of(installed)
    if minor is None or "+k3s" not in installed:
        return unknown
    patch = channels.get(f"v{minor[0]}.{minor[1]}")
    stable = channels.get("stable")
    if is_behind(installed, patch):
        return {"state": "patch", "latest": patch}
    stable_minor = minor_of(stable)
    if stable_minor is not None and minor < stable_minor:
        return {"state": "minor", "latest": stable}
    newest = patch if minor_of(patch) is not None else stable
    if minor_of(newest) is None:
        return unknown
    return {"state": "current", "latest": newest}
