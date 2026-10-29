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
