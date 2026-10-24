"""GET /readyz (readiness) and the Host header Kubernetes probes send.

/healthz itself is covered in test_deploy.py.
"""

import logging

import pytest
from django.db import OperationalError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, override_settings

from cronrack import health

pytestmark = pytest.mark.django_db

POD_IP = "10.42.0.17"


@pytest.fixture(autouse=True)
def fresh_migration_cache(monkeypatch):
    # Each test starts as a newly started process would.
    monkeypatch.setattr(health, "_migrations_applied", False)


@pytest.fixture
def database_down(monkeypatch):
    def broken_cursor(*args, **kwargs):
        raise OperationalError("unable to open database file /data/db.sqlite3")

    monkeypatch.setattr(connection, "cursor", broken_cursor)


@pytest.fixture
def pending_migrations(monkeypatch):
    monkeypatch.setattr(
        MigrationExecutor, "migration_plan", lambda self, targets: [("hosts.0099", False)]
    )


def test_ready_when_database_answers_and_is_migrated():
    response = Client().get("/readyz")
    assert response.status_code == 200
    assert response.content == b"ready"
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert "no-cache" in response["Cache-Control"]
    assert not response.cookies


def test_not_ready_when_the_database_is_down(database_down, caplog):
    with caplog.at_level(logging.WARNING, logger="cronrack.health"):
        response = Client().get("/readyz")
    assert response.status_code == 503
    # Nothing about the cause in the response; only the error's type in the log.
    assert response.content == b"not ready"
    assert "OperationalError" in caplog.text
    assert "/data/db.sqlite3" not in caplog.text


def test_not_ready_while_migrations_are_pending(pending_migrations, caplog):
    with caplog.at_level(logging.WARNING, logger="cronrack.health"):
        response = Client().get("/readyz")
    assert response.status_code == 503
    assert response.content == b"not ready"
    assert "migrations are pending" in caplog.text


def test_pending_migrations_are_checked_again_until_applied(monkeypatch):
    plans = iter([[("hosts.0099", False)], []])
    monkeypatch.setattr(MigrationExecutor, "migration_plan", lambda self, targets: next(plans))
    assert Client().get("/readyz").status_code == 503
    assert Client().get("/readyz").status_code == 200


def test_migrations_are_checked_once_then_only_the_database(monkeypatch):
    assert Client().get("/readyz").status_code == 200

    def must_not_run(self, targets):
        raise AssertionError("the migration graph was loaded again")

    monkeypatch.setattr(MigrationExecutor, "migration_plan", must_not_run)
    assert Client().get("/readyz").status_code == 200


def test_database_is_checked_every_time(monkeypatch):
    assert Client().get("/readyz").status_code == 200

    def broken_cursor(*args, **kwargs):
        raise OperationalError("gone")

    monkeypatch.setattr(connection, "cursor", broken_cursor)
    assert Client().get("/readyz").status_code == 503


def test_readyz_is_get_only():
    assert Client().post("/readyz").status_code == 405


def test_readyz_works_before_first_run_setup():
    # No account exists: every other page redirects to /setup/.
    assert Client().get("/readyz").status_code == 200
    assert Client().get("/").status_code == 302


@override_settings(SECURE_SSL_REDIRECT=True)
def test_readyz_is_not_redirected_to_https():
    # Probes call it over plain http on the pod IP.
    assert Client().get("/readyz").status_code == 200


# --- the Host header of Kubernetes probes ---------------------------------------------


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
@pytest.mark.parametrize("host", [POD_IP, f"{POD_IP}:8000"])
@override_settings(ALLOWED_HOSTS=["cronrack.lan"])
def test_probes_work_with_the_pod_ip_as_host(alice, path, host):
    # kubelet's httpGet probe sends Host: <pod IP>:<port>, never CRONRACK_DOMAIN.
    assert Client(HTTP_HOST=host).get(path).status_code == 200


@pytest.mark.parametrize("path", ["/login/", "/", "/setup/", "/readyz/", "/healthz/", "/static/x"])
@pytest.mark.parametrize("host", [POD_IP, f"{POD_IP}:8000"])
@override_settings(ALLOWED_HOSTS=["cronrack.lan"])
def test_every_other_path_rejects_the_pod_ip_as_host(alice, path, host):
    assert Client(HTTP_HOST=host).get(path).status_code == 400
