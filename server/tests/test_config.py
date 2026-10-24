"""Settings from environment variables: safe defaults and clear errors for bad values.

Each case loads the real settings in a fresh Python process with exactly the given
variables, the way a container starts.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
# Removed from the inherited environment so the developer's shell can't change a result.
CONFIG_ENV_VARS = {
    "DEBUG",
    "SECRET_KEY",
    "ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    "CRONRACK_DOMAIN",
    "CRONRACK_ENABLE_ADMIN",
    "BEHIND_PROXY",
    "FORWARDED_ALLOW_IPS",
    "SQLITE_PATH",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "DJANGO_SETTINGS_MODULE",
}
SETTINGS = (
    "DEBUG",
    "ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    "FORWARDED_ALLOW_IPS",
    "SQLITE_PATH",
    "LOG_LEVEL",
    "CRONRACK_ENABLE_ADMIN",
    "BEHIND_PROXY",
)


def run_settings(env: dict[str, str]) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if k not in CONFIG_ENV_VARS}
    code = (
        "import json, cronrack.settings as s; "
        f"print(json.dumps({{n: getattr(s, n) for n in {SETTINGS!r}}}))"
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


def loaded(**env: str) -> dict:
    result = run_settings({"SECRET_KEY": "test-only", "DEBUG": "0", **env})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def refused(**env: str) -> str:
    """The error text when the settings refuse to load."""
    result = run_settings({"SECRET_KEY": "test-only", "DEBUG": "0", **env})
    assert result.returncode != 0, "settings loaded but should have refused"
    assert "ImproperlyConfigured" in result.stderr
    return result.stderr


# --- safe defaults -------------------------------------------------------------------


def test_defaults_without_optional_variables():
    settings = loaded()
    assert settings["DEBUG"] is False
    assert settings["ALLOWED_HOSTS"] == []
    assert settings["CSRF_TRUSTED_ORIGINS"] == []
    assert settings["FORWARDED_ALLOW_IPS"] == []
    assert settings["SQLITE_PATH"] == str(SERVER_DIR / "data" / "db.sqlite3")
    assert settings["LOG_LEVEL"] == "INFO"
    assert settings["CRONRACK_ENABLE_ADMIN"] is False
    assert settings["BEHIND_PROXY"] is False


def test_empty_values_mean_the_default():
    # docker compose turns `BEHIND_PROXY=` in .env into an empty string, not "unset".
    settings = loaded(BEHIND_PROXY="", LOG_LEVEL="", SQLITE_PATH="", CRONRACK_ENABLE_ADMIN="")
    assert settings["BEHIND_PROXY"] is False
    assert settings["LOG_LEVEL"] == "INFO"
    assert settings["SQLITE_PATH"].endswith("db.sqlite3")


# --- booleans --------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["1", "true", "YES", " on "])
def test_true_words(word):
    assert loaded(BEHIND_PROXY=word)["BEHIND_PROXY"] is True


@pytest.mark.parametrize("word", ["0", "false", "No", "off"])
def test_false_words(word):
    assert loaded(BEHIND_PROXY=word)["BEHIND_PROXY"] is False


@pytest.mark.parametrize("name", ["DEBUG", "BEHIND_PROXY", "CRONRACK_ENABLE_ADMIN"])
def test_a_typo_in_a_boolean_is_an_error_not_off(name):
    error = refused(**{name: "ture"})
    assert f"{name} must be 1 or 0" in error
    assert "'ture'" in error


# --- hosts and origins -----------------------------------------------------------------


def test_extra_csrf_origins_come_after_the_domain():
    settings = loaded(
        CRONRACK_DOMAIN="cronrack.lan",
        CSRF_TRUSTED_ORIGINS="https://cronrack.lan, https://cr.example.com:8443",
    )
    assert settings["CSRF_TRUSTED_ORIGINS"] == [
        "https://cronrack.lan",
        "https://cr.example.com:8443",
    ]


def test_extra_csrf_origins_without_a_domain():
    settings = loaded(CSRF_TRUSTED_ORIGINS="https://cr.example.com")
    assert settings["CSRF_TRUSTED_ORIGINS"] == ["https://cr.example.com"]


@pytest.mark.parametrize("bad", ["cronrack.lan", "https://cronrack.lan/", "ftp://x", "https://"])
def test_csrf_origins_must_be_origins(bad):
    assert "CSRF_TRUSTED_ORIGINS entries must be origins" in refused(CSRF_TRUSTED_ORIGINS=bad)


def test_extra_allowed_hosts_are_a_comma_list():
    settings = loaded(CRONRACK_DOMAIN="cronrack.lan", ALLOWED_HOSTS="nas, , 10.0.0.5")
    assert settings["ALLOWED_HOSTS"] == ["cronrack.lan", "nas", "10.0.0.5"]


# --- proxy trust -----------------------------------------------------------------------


def test_forwarded_allow_ips_accepts_addresses_and_networks():
    settings = loaded(FORWARDED_ALLOW_IPS="172.29.80.10, 10.42.0.0/16,fd00::/8")
    assert settings["FORWARDED_ALLOW_IPS"] == ["172.29.80.10", "10.42.0.0/16", "fd00::/8"]


@pytest.mark.parametrize("bad", ["*", "10.42.0.1/16", "traefik", "10.42.0.0/33"])
def test_forwarded_allow_ips_refuses_anything_else(bad):
    error = refused(FORWARDED_ALLOW_IPS=bad)
    assert "FORWARDED_ALLOW_IPS must be IP addresses or networks" in error


# --- database and logging --------------------------------------------------------------


def test_sqlite_path_from_the_environment():
    assert loaded(SQLITE_PATH="/data/db.sqlite3")["SQLITE_PATH"] == "/data/db.sqlite3"


def test_sqlite_path_must_be_absolute():
    assert "SQLITE_PATH must be an absolute path" in refused(SQLITE_PATH="data/db.sqlite3")


@pytest.mark.parametrize("level", ["debug", "WARNING", "error"])
def test_log_level(level):
    assert loaded(LOG_LEVEL=level)["LOG_LEVEL"] == level.upper()


def test_unknown_log_level_is_an_error():
    assert "LOG_LEVEL must be one of" in refused(LOG_LEVEL="verbose")
