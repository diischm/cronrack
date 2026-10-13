"""install.sh and the agent tarball: public, complete, and free of tests and secrets."""

import gzip
import hashlib
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from django.test import Client

from agents import package

pytestmark = pytest.mark.django_db

SERVER_DIR = Path(__file__).resolve().parents[1]

EXPECTED_FILES = {
    "agent/__init__.py",
    "agent/__main__.py",
    "agent/collectors.py",
    "agent/config.py",
    "agent/connection.py",
    "agent/protocol.py",
    "agent/report.py",
    "agent/requirements.txt",
    "deploy/cronrack-agent.service",
}


@pytest.fixture
def anonymous(alice) -> Client:
    # An account exists (so first-run setup is done), but this client isn't logged in.
    return Client()


def members(data: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {info.name: info for info in tar.getmembers()}


def test_install_sh_is_public_plain_text(anonymous):
    response = anonymous.get("/agent/install.sh")

    assert response.status_code == 200
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert response.content == package.INSTALL_SCRIPT.read_bytes()
    assert response.content.startswith(b"#!/usr/bin/env bash")


def test_tarball_is_public(anonymous):
    response = anonymous.get("/agent/download/cronrack-agent.tar.gz")

    assert response.status_code == 200
    assert response["Content-Type"] == "application/gzip"
    assert "cronrack-agent.tar.gz" in response["Content-Disposition"]


def test_tarball_contains_exactly_the_agent(anonymous):
    data = anonymous.get("/agent/download/cronrack-agent.tar.gz").content

    files = {name for name, info in members(data).items() if info.isfile()}
    assert files == EXPECTED_FILES


def test_tarball_has_no_tests_or_secrets(anonymous):
    data = anonymous.get("/agent/download/cronrack-agent.tar.gz").content

    for name, info in members(data).items():
        assert "tests" not in name and "fixtures" not in name
        assert not name.endswith((".toml", ".env", ".pem", ".key", ".sqlite3", ".pyc"))
        assert not name.startswith(("/", "..")) and ".." not in name.split("/")
        assert info.uid == 0 and info.gid == 0 and info.mtime == 0
        assert info.isfile() or info.isdir()  # no links or devices


def test_sha256_matches_the_tarball(anonymous):
    data = anonymous.get("/agent/download/cronrack-agent.tar.gz").content
    response = anonymous.get("/agent/download/cronrack-agent.tar.gz.sha256")

    assert response.status_code == 200
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    # sha256sum format: "<hex>  <filename>"
    assert response.content.decode() == (
        f"{hashlib.sha256(data).hexdigest()}  cronrack-agent.tar.gz\n"
    )


def test_tarball_is_deterministic():
    first, second = package.build_package(), package.build_package()

    assert first == second
    # No file name or timestamp in the gzip header either.
    assert gzip.decompress(first.data)  # valid gzip
    assert first.data[4:8] == b"\x00\x00\x00\x00"  # gzip mtime field


@pytest.mark.parametrize(
    "url",
    [
        "/agent/install.sh",
        "/agent/download/cronrack-agent.tar.gz",
        "/agent/download/cronrack-agent.tar.gz.sha256",
    ],
)
def test_downloads_are_get_only(anonymous, url):
    assert anonymous.post(url).status_code == 405


def test_unit_file_in_the_tarball_matches_the_repo(anonymous):
    data = anonymous.get("/agent/download/cronrack-agent.tar.gz").content
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        unit = tar.extractfile("deploy/cronrack-agent.service").read()

    assert unit == (package.REPO_ROOT / "deploy" / "cronrack-agent.service").read_bytes()
    assert b"NoNewPrivileges=yes" in unit
    assert b"\n#SupplementaryGroups=docker" in unit  # Docker stays opt-in


# --- the "token rotated" page shows the install command too ---------------------------


def test_rotated_token_page_shows_command_and_token(alice_client, host):
    from hosts.models import AgentToken

    AgentToken.issue(host)
    response = alice_client.post(f"/hosts/{host.pk}/rotate/")
    html = response.content.decode()

    assert "curl -fsSL http://testserver/agent/install.sh | sudo bash -s --" in html
    assert "server_url = &quot;http://testserver&quot;" in html


# --- settings: DEBUG default for ALLOWED_HOSTS -----------------------------------------


def allowed_hosts_with(env: dict[str, str]) -> str:
    clean = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DEBUG", "ALLOWED_HOSTS", "CRONRACK_DOMAIN", "BEHIND_PROXY"}
    }
    code = "import cronrack.settings as s; print(','.join(s.ALLOWED_HOSTS))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SERVER_DIR,
        env={**clean, "SECRET_KEY": "test-only", **env},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def test_debug_without_allowed_hosts_defaults_to_this_machine():
    assert allowed_hosts_with({"DEBUG": "1"}) == "localhost,127.0.0.1,[::1]"


def test_explicit_allowed_hosts_win():
    assert allowed_hosts_with({"DEBUG": "1", "ALLOWED_HOSTS": "nas.lan"}) == "nas.lan"


def test_no_default_without_debug():
    assert allowed_hosts_with({"DEBUG": "0"}) == ""
