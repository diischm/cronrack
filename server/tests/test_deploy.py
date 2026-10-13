"""Running behind Caddy in docker compose: /healthz, proxy trust, domain settings and
the install command the "Add host" page shows."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.test import Client, override_settings

from .sample import make_report

pytestmark = pytest.mark.django_db

SERVER_DIR = Path(__file__).resolve().parents[1]
DEPLOY_ENV_VARS = {"DEBUG", "SECRET_KEY", "ALLOWED_HOSTS", "CRONRACK_DOMAIN", "BEHIND_PROXY"}


# --- /healthz --------------------------------------------------------------------------


def test_healthz_works_before_first_run_setup():
    # No account exists yet: every other page redirects to /setup/.
    response = Client().get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok"


def test_healthz_needs_no_login(alice):
    response = Client().get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok"


def test_healthz_gives_away_nothing(alice):
    response = Client().get("/healthz")
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert not response.cookies  # no session, no CSRF cookie
    assert "no-cache" in response["Cache-Control"]


def test_healthz_is_get_only():
    assert Client().post("/healthz").status_code == 405


@pytest.mark.parametrize("host", ["100.109.5.47", "192.168.1.50", "not-allowed.example", ""])
@override_settings(ALLOWED_HOSTS=["cronrack.lan"])
def test_healthz_ignores_the_host_header(alice, host):
    # Docker's health check must pass whatever CRONRACK_DOMAIN is, so /healthz
    # never looks at Host. Every other page still rejects an unknown one.
    assert Client(HTTP_HOST=host).get("/healthz").content == b"ok"
    assert Client(HTTP_HOST="not-allowed.example").get("/login/").status_code == 400


def test_healthz_is_the_first_middleware():
    from django.conf import settings

    assert settings.MIDDLEWARE[0] == "cronrack.health.HealthCheckMiddleware"


def test_only_the_exact_path_is_special(alice):
    # Only "/healthz" itself is answered early; anything else goes through Django.
    assert Client().get("/healthzx").status_code == 404


@override_settings(SECURE_SSL_REDIRECT=True)
def test_healthz_is_not_redirected_to_https(alice):
    # Docker's health check calls it over plain http inside the container.
    assert Client().get("/healthz").status_code == 200
    # Everything else still is.
    assert Client().get("/login/").status_code == 301


# --- settings from the environment ----------------------------------------------------


def settings_with(env: dict[str, str]) -> subprocess.CompletedProcess:
    """Load the real settings in a fresh Python with exactly these deploy variables."""
    clean = {k: v for k, v in os.environ.items() if k not in DEPLOY_ENV_VARS}
    code = (
        "import json, cronrack.settings as s; print(json.dumps({"
        "'proxy': getattr(s, 'SECURE_PROXY_SSL_HEADER', None),"
        "'hosts': s.ALLOWED_HOSTS,"
        "'csrf': getattr(s, 'CSRF_TRUSTED_ORIGINS', []),"
        "}))"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=SERVER_DIR,
        env={**clean, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def loaded(env: dict[str, str]) -> dict:
    result = settings_with({"SECRET_KEY": "test-only", "DEBUG": "0", **env})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_refuses_to_start_without_secret_key():
    result = settings_with({"DEBUG": "0", "CRONRACK_DOMAIN": "cronrack.lan"})
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_proxy_header_trusted_only_with_behind_proxy():
    assert loaded({"BEHIND_PROXY": "1"})["proxy"] == ["HTTP_X_FORWARDED_PROTO", "https"]
    assert loaded({})["proxy"] is None
    assert loaded({"BEHIND_PROXY": "0"})["proxy"] is None


def test_domain_sets_allowed_hosts_and_csrf_origins():
    settings = loaded({"CRONRACK_DOMAIN": "CronRack.home.lan"})
    assert settings["hosts"] == ["cronrack.home.lan"]
    assert settings["csrf"] == ["https://cronrack.home.lan"]


def test_domain_comes_first_and_extra_hosts_are_kept():
    settings = loaded({"CRONRACK_DOMAIN": "cronrack.lan", "ALLOWED_HOSTS": "cronrack.lan,nas"})
    assert settings["hosts"] == ["cronrack.lan", "nas"]


def test_no_domain_no_csrf_origins():
    assert loaded({"ALLOWED_HOSTS": "nas"})["csrf"] == []


@pytest.mark.parametrize(
    "bad", ["https://cronrack.lan", "cronrack.lan:8443", "cronrack.lan/x", "a b", "-x.lan"]
)
def test_domain_must_be_a_bare_host_name(bad):
    result = settings_with({"SECRET_KEY": "test-only", "DEBUG": "0", "CRONRACK_DOMAIN": bad})
    assert result.returncode != 0
    assert "CRONRACK_DOMAIN" in result.stderr


# --- the install command on the "Add host" page ------------------------------------------


@pytest.fixture
def domain_client(alice):
    with override_settings(CRONRACK_DOMAIN="cronrack.lan", ALLOWED_HOSTS=["cronrack.lan"]):
        client = Client(HTTP_HOST="cronrack.lan")
        client.force_login(alice)
        yield client


def add_host_page(client) -> str:
    response = client.post("/hosts/add/", {"name": "pi"}, follow=True)
    assert response.status_code == 200
    return response.content.decode()


def test_install_command_uses_https_domain(domain_client):
    html = add_host_page(domain_client)
    assert (
        "curl -fsSL https://cronrack.lan/agent/install.sh"
        " | sudo bash -s -- --server https://cronrack.lan"
    ) in html
    assert "http://cronrack.lan" not in html


def test_install_command_has_a_ca_file_variant(domain_client):
    html = add_host_page(domain_client)
    assert (
        "curl -fsSL --cacert cronrack-ca.crt https://cronrack.lan/agent/install.sh"
        " | sudo bash -s -- --server https://cronrack.lan --ca-file cronrack-ca.crt"
    ) in html
    # Where to get Caddy's root certificate (">" is escaped in HTML).
    assert (
        "docker compose exec -T caddy cat /data/caddy/pki/authorities/local/root.crt"
        " &gt; cronrack-ca.crt"
    ) in html


# --- versions ---------------------------------------------------------------------------


def test_server_version_is_0_1_0():
    from agent import AGENT_VERSION
    from cronrack import SERVER_VERSION

    assert SERVER_VERSION == "0.1.0"
    assert AGENT_VERSION == "0.1.0"


def test_sidebar_footer_shows_server_version(alice_client):
    html = alice_client.get("/").content.decode()
    assert '<p class="side-version">CronRack v0.1.0</p>' in html


def test_healthz_does_not_show_the_version(alice):
    assert b"0.1.0" not in Client().get("/healthz").content


def test_host_detail_shows_agent_and_server_version(alice_client, host):
    host.latest_report = make_report(agent_version="0.0.9")
    host.save()
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    assert "Agent v0.0.9 · Server v0.1.0" in html


def test_host_detail_before_first_report(alice_client, host):
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    assert "Agent version unknown (no report yet) · Server v0.1.0" in html


def test_agent_version_is_escaped(alice_client, host):
    # The serializer already rejects odd values; the template must escape anyway.
    host.latest_report = make_report(agent_version="<b>x</b>")
    host.save()
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
