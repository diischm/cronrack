"""docker-entrypoint.sh: one image, several roles (migrate, web, scheduler, pass-through).

The real script runs under /bin/sh, but with stand-ins for `python` and `uvicorn` first
on PATH: they only write down how they were called. So these tests check exactly which
commands each role runs, in which order, without Docker or a database.
"""

import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"

# Appends "<program> <args...>" to $CALLS; exits 3 if that line equals $FAIL_ON.
STUB = """#!/bin/sh
name="$(basename "$0")"
echo "$name $*" >> "$CALLS"
if [ "${FAIL_ON:-}" = "$name $*" ]; then exit 3; fi
exit 0
"""


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for program in ("python", "uvicorn"):
        stub = bin_dir / program
        stub.write_text(STUB)
        stub.chmod(0o755)
    calls = tmp_path / "calls"

    def run(*args: str, fail_on: str = "", **env: str):
        calls.write_text("")
        result = subprocess.run(
            ["/bin/sh", str(ENTRYPOINT), *args],
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "CALLS": str(calls),
                "FAIL_ON": fail_on,
                "FORWARDED_ALLOW_IPS": "10.42.0.0/16",
                **env,
            },
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return result, calls.read_text().splitlines()

    return run


def test_migrate_runs_migrations_and_exits(run):
    result, calls = run("migrate")
    assert result.returncode == 0
    assert calls == ["python manage.py migrate --noinput"]


def test_migrate_failure_is_the_exit_code(run):
    # docker compose only starts app and scheduler after migrate exited 0.
    result, _ = run("migrate", fail_on="python manage.py migrate --noinput")
    assert result.returncode != 0


def test_web_never_migrates(run):
    result, calls = run("web")
    assert result.returncode == 0
    assert not any("migrate" in call for call in calls)
    assert calls[0] == "python manage.py startup"
    assert calls[1].startswith("uvicorn cronrack.asgi:application ")
    assert len(calls) == 2


def test_no_arguments_means_web(run):
    _, with_role = run("web")
    _, without = run()
    assert without == with_role


def test_web_shuts_down_gracefully_within_20_seconds(run):
    _, calls = run("web")
    assert calls[1].endswith(" --timeout-graceful-shutdown 20")


def test_web_trusts_forwarded_headers_only_from_the_proxy(run):
    _, calls = run("web", FORWARDED_ALLOW_IPS="172.29.80.10")
    assert "--proxy-headers --forwarded-allow-ips 172.29.80.10 " in calls[1]


def test_web_refuses_to_start_without_forwarded_allow_ips(run):
    result, calls = run("web", FORWARDED_ALLOW_IPS="")
    assert result.returncode != 0
    assert "FORWARDED_ALLOW_IPS" in result.stderr
    assert not any(call.startswith("uvicorn") for call in calls)


def test_web_does_not_start_uvicorn_when_startup_fails(run):
    # e.g. a bad setting: the error is printed by startup, and nothing serves traffic.
    result, calls = run("web", fail_on="python manage.py startup")
    assert result.returncode != 0
    assert calls == ["python manage.py startup"]


def test_web_takes_no_arguments(run):
    result, calls = run("web", "--reload")
    assert result.returncode != 0
    assert calls == []


def test_scheduler_runs_the_loop_with_a_heartbeat(run):
    result, calls = run("scheduler")
    assert result.returncode == 0
    assert calls == [
        "python manage.py check_hosts --loop --every 30 --heartbeat-file /tmp/check_hosts.alive"
    ]


def test_scheduler_passes_extra_arguments_on(run):
    _, calls = run("scheduler", "--every", "60")
    assert calls[0].endswith("--heartbeat-file /tmp/check_hosts.alive --every 60")


@pytest.mark.parametrize(
    "command",
    [
        ["python", "manage.py", "createsuperuser"],
        ["python", "manage.py", "check_hosts", "--once"],
        ["uvicorn", "--version"],
    ],
)
def test_other_commands_run_as_given(run, command):
    result, calls = run(*command)
    assert result.returncode == 0
    assert calls == [" ".join(command)]


def test_every_role_execs_so_the_program_is_pid_1():
    # `exec` replaces the shell, so the real program gets SIGTERM directly. Every line
    # that starts the long-running program (or migrate) must use it.
    script = ENTRYPOINT.read_text()
    starts = [
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith(
            ("python manage.py migrate", "uvicorn", "python manage.py check_hosts")
        )
    ]
    assert starts == []  # none of them without exec
    assert script.count("exec ") >= 4
