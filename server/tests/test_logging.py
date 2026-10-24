"""Logging: stdout only, plain or JSON, quiet probes, and nothing secret in any log line."""

import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.management import call_command

from accounts.setup_code import _hash
from agents.consumers import error_summary
from agents.serializers import ReportMessageSerializer
from cronrack import health
from cronrack.logformat import PROBE_PATHS, JsonFormatter, SkipProbeSuccess

from .sample import make_report

SERVER_DIR = Path(__file__).resolve().parents[1]
ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'


def access_record(path: str, status: int) -> logging.LogRecord:
    """A record shaped like the ones uvicorn's access logger makes."""
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, ACCESS_FORMAT,
        ("10.42.0.1:0", "GET", path, "1.1", status), None,
    )  # fmt: skip


# --- the probe filter ------------------------------------------------------------------


def test_probe_paths_match_the_health_checks():
    assert {health.HEALTHZ_PATH, health.READYZ_PATH} == PROBE_PATHS


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/readyz?probe=1"])
@pytest.mark.parametrize("status", [200, 204])
def test_successful_probes_are_not_logged(path, status):
    assert SkipProbeSuccess().filter(access_record(path, status)) is False


@pytest.mark.parametrize("status", [301, 400, 404, 500, 503])
@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_failed_probes_are_logged(path, status):
    assert SkipProbeSuccess().filter(access_record(path, status)) is True


@pytest.mark.parametrize("path", ["/", "/login/", "/healthzx", "/readyz/", "/static/healthz"])
def test_other_requests_are_logged(path):
    assert SkipProbeSuccess().filter(access_record(path, 200)) is True


def test_other_log_records_pass_untouched():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "plain", None, None)
    assert SkipProbeSuccess().filter(record) is True


# --- the JSON format -------------------------------------------------------------------


def test_json_lines():
    record = logging.LogRecord(
        "hosts", logging.WARNING, __file__, 1, "host %s: %s", (7, "line1\nline2"), None
    )
    line = JsonFormatter().format(record)
    assert "\n" not in line  # one record, one line: a newline can't fake a second entry
    entry = json.loads(line)
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "hosts"
    assert entry["message"] == "host 7: line1\nline2"
    assert entry["time"].endswith("+00:00")


def test_json_includes_the_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "django.request", logging.ERROR, __file__, 1, "Internal Server Error", None,
            sys.exc_info(),
        )  # fmt: skip
    entry = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in entry["exception"]


# --- the real configuration, in a fresh process -----------------------------------------

LOG_SOME_LINES = f"""
import logging, django
django.setup()
logging.getLogger("django.request").error("Internal Server Error: /x")
logging.getLogger("hosts").info("hello from hosts")
logging.getLogger("hosts").debug("debug detail")
access = logging.getLogger("uvicorn.access")
access.info({ACCESS_FORMAT!r}, "10.42.0.1:0", "GET", "/readyz", "1.1", 200)
access.info({ACCESS_FORMAT!r}, "10.42.0.1:0", "GET", "/readyz", "1.1", 503)
logging.getLogger("uvicorn.error").info("Shutting down")
"""


def log_output(**env: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("LOG_", "DJANGO_"))}
    return subprocess.run(
        [sys.executable, "-c", LOG_SOME_LINES],
        cwd=SERVER_DIR,
        env={
            **clean,
            "DJANGO_SETTINGS_MODULE": "cronrack.settings",
            "SECRET_KEY": "test-only",
            "DEBUG": "0",
            **env,
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def test_everything_goes_to_stdout_in_plain_text():
    result = log_output()
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    # With DEBUG=0 Django's default printed none of these; 500 errors were lost.
    assert "ERROR django.request: Internal Server Error: /x" in lines
    assert "INFO hosts: hello from hosts" in lines
    assert "INFO uvicorn.error: Shutting down" in lines
    assert 'INFO uvicorn.access: 10.42.0.1:0 - "GET /readyz HTTP/1.1" 503' in lines
    assert not any('"GET /readyz HTTP/1.1" 200' in line for line in lines)
    assert not any("debug detail" in line for line in lines)  # LOG_LEVEL info


def test_json_format():
    result = log_output(LOG_FORMAT="json")
    entries = [json.loads(line) for line in result.stdout.splitlines()]
    assert {"level": "ERROR", "logger": "django.request"}.items() <= entries[0].items()
    assert [e["logger"] for e in entries] == [
        "django.request",
        "hosts",
        "uvicorn.access",
        "uvicorn.error",
    ]


def test_log_level_debug_shows_more():
    assert "DEBUG hosts: debug detail" in log_output(LOG_LEVEL="debug").stdout


def test_unknown_log_format_is_an_error():
    result = subprocess.run(
        [sys.executable, "-c", "import cronrack.settings"],
        cwd=SERVER_DIR,
        env={**os.environ, "SECRET_KEY": "x", "DEBUG": "0", "LOG_FORMAT": "xml"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert "LOG_FORMAT must be one of plain, json" in result.stderr


# --- never in a log: report contents, tokens, the setup code's hash ---------------------


def summary_of(report: dict) -> str:
    serializer = ReportMessageSerializer(data={"type": "report", "report": report})
    assert not serializer.is_valid()
    return "; ".join(error_summary(serializer.errors))


def test_invalid_report_summary_has_paths_and_codes_only():
    summary = summary_of(make_report(updates={"total": "LEAKED-VALUE", "security": 0}))
    assert summary == "report.updates.total: invalid"


def test_unknown_keys_from_the_agent_are_not_logged():
    summary = summary_of(make_report(nginx_secret_name=1, **{"Weird Key\n": 2}))
    assert "nginx_secret_name" not in summary
    assert "Weird" not in summary
    assert "report.?: invalid" in summary


def test_list_positions_are_kept():
    summary = summary_of(make_report(containers=[{"name": "LEAKED-NAME"}]))
    assert summary.startswith("report.containers[0].")
    assert "LEAKED-NAME" not in summary


@pytest.mark.django_db
def test_startup_prints_the_code_once_and_never_logs_its_hash(caplog):
    out = io.StringIO()
    with caplog.at_level(logging.DEBUG):
        call_command("startup", stdout=out)
    printed = out.getvalue()
    code = printed.split("setup code: ", 1)[1].split()[0]
    assert printed.count(code) == 1
    assert _hash(code) not in printed
    assert _hash(code) not in caplog.text
    assert code not in caplog.text  # printed on purpose, but not through logging
