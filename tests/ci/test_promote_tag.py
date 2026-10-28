"""Tests for scripts/ci/promote-tag.sh, run on temporary copies inside a throwaway git repo."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "promote-tag.sh"
NEW_TAG = "sha-abc1234"

BASIC = """\
image:
  repository: ghcr.io/diischm/cronrack
  tag: sha-079b616

logLevel: info
"""

# Ignore the user's and the system's git config (hooks, autocrlf, ...).
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def make_values(tmp_path, content, *, track=True):
    """Write content as bytes (no newline conversion) into a fresh git repo."""
    git(tmp_path, "init", "-q")
    path = tmp_path / "values.yaml"
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    if track:
        git(tmp_path, "add", "values.yaml")
    return path


def promote(path, tag=NEW_TAG):
    return subprocess.run(
        [str(SCRIPT), str(path), tag],
        env=GIT_ENV,
        check=False,  # the tests check the exit code themselves
        capture_output=True,
        text=True,
        timeout=30,
    )


def numstat(path):
    return git(path.parent, "diff", "--numstat", "--", path.name).strip()


def test_normal_bump_changes_exactly_one_line(tmp_path):
    path = make_values(tmp_path, BASIC)
    result = promote(path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"updated sha-079b616 -> {NEW_TAG}"
    assert numstat(path) == "1\t1\tvalues.yaml"
    assert path.read_text() == BASIC.replace("sha-079b616", NEW_TAG)


def test_already_set_changes_nothing(tmp_path):
    content = BASIC.replace("sha-079b616", NEW_TAG)
    path = make_values(tmp_path, content)
    result = promote(path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "already set"
    assert path.read_text() == content
    assert numstat(path) == ""


@pytest.mark.parametrize(
    "tag",
    [
        "latest",
        "sha-ABC1234",  # upper case
        "sha-abc123",  # 6 hex digits
        "sha-0123456789abc",  # 13 hex digits
        "sha-abc123g",
        "sha-abc1234\n",
        "sha-abc1234/x",
        "sha-abc1234;id",
        "",
    ],
)
def test_invalid_tag_fails_without_change(tmp_path, tag):
    path = make_values(tmp_path, BASIC)
    result = promote(path, tag)
    assert result.returncode != 0
    assert "invalid tag" in result.stderr
    assert path.read_text() == BASIC


def test_only_image_tag_changes_with_other_tag_keys(tmp_path):
    content = """\
# tag: sha-0000000 in a comment
image:
  # tag: sha-1111111 commented out
  repository: ghcr.io/diischm/cronrack
  extra:
    tag: sha-2222222
  tag: sha-079b616
  pullPolicy: IfNotPresent
sidecar:
  image:
    tag: sha-3333333
tag: sha-4444444
"""
    path = make_values(tmp_path, content)
    result = promote(path)
    assert result.returncode == 0, result.stderr
    assert numstat(path) == "1\t1\tvalues.yaml"
    assert path.read_text() == content.replace("  tag: sha-079b616\n", f"  tag: {NEW_TAG}\n")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('  tag: "sha-079b616"\n', f'  tag: "{NEW_TAG}"\n'),
        ("  tag: 'sha-079b616'\n", f"  tag: '{NEW_TAG}'\n"),
        ('  tag: "sha-079b616"  # bumped by CI\n', f'  tag: "{NEW_TAG}"  # bumped by CI\n'),
        ("  tag: sha-079b616 # bumped by CI\n", f"  tag: {NEW_TAG} # bumped by CI\n"),
        ('  tag: ""\n', f'  tag: "{NEW_TAG}"\n'),
    ],
)
def test_quotes_and_comments_preserved(tmp_path, before, after):
    content = f"image:\n  repository: ghcr.io/diischm/cronrack\n{before}logLevel: info\n"
    path = make_values(tmp_path, content)
    result = promote(path)
    assert result.returncode == 0, result.stderr
    assert path.read_text() == content.replace(before, after)
    assert numstat(path) == "1\t1\tvalues.yaml"


def test_crlf_and_missing_final_newline_preserved(tmp_path):
    content = b"logLevel: info\r\nimage:\r\n    repository: x\r\n    tag: sha-079b616"
    path = make_values(tmp_path, content)
    result = promote(path)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == content.replace(b"sha-079b616", NEW_TAG.encode())


@pytest.mark.parametrize(
    "content",
    [
        "image:\n  repository: ghcr.io/diischm/cronrack\nlogLevel: info\n",
        "logLevel: info\n",
        "image:\n  repository: x\n  extra:\n    tag: sha-079b616\n",
        "sidecar:\n  image:\n    tag: sha-079b616\n",
        "image: {repository: x, tag: sha-079b616}\n",
    ],
)
def test_missing_image_tag_fails_clearly(tmp_path, content):
    path = make_values(tmp_path, content)
    result = promote(path)
    assert result.returncode != 0
    assert "no tag: key directly under a top-level image: block" in result.stderr
    assert path.read_text() == content


@pytest.mark.parametrize(
    "line",
    [
        "  tag: sha-079b616 extra\n",
        "  tag: [sha-079b616]\n",
        '  tag: "sha-079b616\n',
        "  tag:\n",
    ],
)
def test_unsupported_tag_line_fails(tmp_path, line):
    content = f"image:\n  repository: x\n{line}"
    path = make_values(tmp_path, content)
    result = promote(path)
    assert result.returncode != 0
    assert "unsupported format" in result.stderr
    assert path.read_text() == content


def test_duplicate_image_tag_fails(tmp_path):
    content = "image:\n  tag: sha-079b616\n  tag: sha-079b616\n"
    path = make_values(tmp_path, content)
    result = promote(path)
    assert result.returncode != 0
    assert "more than one image.tag" in result.stderr
    assert path.read_text() == content


def test_untracked_file_fails(tmp_path):
    path = make_values(tmp_path, BASIC, track=False)
    result = promote(path)
    assert result.returncode != 0
    assert "not tracked by git" in result.stderr
    assert path.read_text() == BASIC


def test_uncommitted_changes_fail(tmp_path):
    path = make_values(tmp_path, BASIC)
    path.write_text(BASIC + "extra: 1\n")
    result = promote(path)
    assert result.returncode != 0
    assert "has uncommitted changes" in result.stderr
    assert path.read_text() == BASIC + "extra: 1\n"
