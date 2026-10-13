import subprocess
from pathlib import Path

import pytest

from agent import collectors

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- parse_* against fixtures ---------------------------------------------------


def test_parse_apt_upgradable_fixture():
    assert collectors.parse_apt_upgradable(load("apt_upgradable.txt")) == {
        "total": 8,
        "security": 4,
    }


def test_parse_apt_upgradable_counts_suite_not_package_name():
    text = (
        "Listing...\n"
        "foo-security/noble-updates 1.1 amd64 [upgradable from: 1.0]\n"
        "bar/noble-infra-security 2.1 amd64 [upgradable from: 2.0]\n"
    )
    assert collectors.parse_apt_upgradable(text) == {"total": 2, "security": 1}


def test_parse_docker_ps_fixture():
    containers = collectors.parse_docker_ps(load("docker_ps.txt"))
    by_name = {c["name"]: c for c in containers}
    assert list(by_name) == [
        "web",
        "db",
        "app",
        "worker",
        "backup-job",
        "init-migrate",
        "scratch",
        "cache",
    ]
    for container in containers:
        assert set(container) == {"name", "state", "status", "health", "exit_code"}
    assert by_name["app"] == {
        "name": "app",
        "state": "running",
        "status": "Up 2 hours (unhealthy)",
        "health": "unhealthy",
        "exit_code": None,
    }
    assert by_name["web"]["health"] == "healthy"
    assert by_name["cache"]["health"] == "starting"
    assert by_name["db"]["health"] is None  # no healthcheck defined
    assert by_name["backup-job"]["exit_code"] == 1
    assert by_name["init-migrate"]["exit_code"] == 0
    # Only "Exited (N)" sets exit_code; running, restarting and created don't.
    assert by_name["worker"]["state"] == "restarting"
    assert by_name["worker"]["exit_code"] is None
    assert by_name["scratch"]["exit_code"] is None
    assert by_name["scratch"]["health"] is None


def test_parse_docker_ps_negative_exit_code():
    text = '{"Names": "x", "State": "exited", "Status": "Exited (-1) 2 minutes ago"}\n'
    assert collectors.parse_docker_ps(text)[0]["exit_code"] == -1


def test_parse_docker_ps_skips_bad_lines_but_keeps_good_ones():
    text = (
        '{"Names": "ok", "State": "running", "Status": "Up 1 hour"}\n'
        "not json\n"
        "[1, 2, 3]\n"
        '{"Names": 42, "State": "running", "Status": "Up"}\n'
        '{"Names": "missing-status", "State": "running"}\n'
    )
    assert collectors.parse_docker_ps(text) == [
        {
            "name": "ok",
            "state": "running",
            "status": "Up 1 hour",
            "health": None,
            "exit_code": None,
        },
    ]


def test_parse_systemctl_failed_fixture():
    assert collectors.parse_systemctl_failed(load("systemctl_failed.txt")) == [
        "backup-nightly.service",
        "mnt-media.mount",
    ]


def test_parse_systemctl_failed_handles_bullet():
    text = "● nginx.service loaded failed failed A high performance web server\n"
    assert collectors.parse_systemctl_failed(text) == ["nginx.service"]


def test_parse_systemctl_timers_fixture():
    timers = collectors.parse_systemctl_timers(load("systemctl_timers.txt"))
    by_name = {t["timer"]: t for t in timers}
    assert len(timers) == 9
    assert by_name["apt-daily.timer"] == {
        "timer": "apt-daily.timer",
        "activates": "apt-daily.service",
        "last_run": "Sun 2026-10-11 08:02:17 UTC",
        "next_run": "Sun 2026-10-11 20:31:44 UTC",
    }
    # Never run: LAST and PASSED are "-".
    assert by_name["db-dump.timer"]["last_run"] is None
    assert by_name["db-dump.timer"]["next_run"] == "Mon 2026-10-12 04:00:00 UTC"
    # Inactive: NEXT and LEFT are "-", but it ran before.
    assert by_name["offsite-sync.timer"]["next_run"] is None
    assert by_name["offsite-sync.timer"]["last_run"] == "Fri 2026-10-09 02:00:04 UTC"


def test_parse_systemctl_timers_older_na_format():
    # Older systemd versions print "n/a" instead of "-" and "left" after LEFT.
    text = (
        "Mon 2026-10-12 04:00:00 UTC 8h left n/a n/a db-dump.timer db-dump.service\n"
        "n/a n/a Fri 2026-10-09 02:00:04 UTC 2 days ago offsite-sync.timer offsite-sync.service\n"
        "n/a n/a n/a n/a unused.timer unused.service\n"
    )
    assert collectors.parse_systemctl_timers(text) == [
        {
            "timer": "db-dump.timer",
            "activates": "db-dump.service",
            "last_run": None,
            "next_run": "Mon 2026-10-12 04:00:00 UTC",
        },
        {
            "timer": "offsite-sync.timer",
            "activates": "offsite-sync.service",
            "last_run": "Fri 2026-10-09 02:00:04 UTC",
            "next_run": None,
        },
        {
            "timer": "unused.timer",
            "activates": "unused.service",
            "last_run": None,
            "next_run": None,
        },
    ]


def test_parse_systemctl_timers_unparseable_activates_is_none():
    text = (
        "Mon 2026-10-12 04:00:00 UTC 8h - - no-activates.timer\n"
        "Mon 2026-10-12 04:00:00 UTC 8h - - garbled.timer not/a$unit\n"
    )
    assert collectors.parse_systemctl_timers(text) == [
        {
            "timer": "no-activates.timer",
            "activates": None,
            "last_run": None,
            "next_run": "Mon 2026-10-12 04:00:00 UTC",
        },
        {
            "timer": "garbled.timer",
            "activates": None,
            "last_run": None,
            "next_run": "Mon 2026-10-12 04:00:00 UTC",
        },
    ]


def test_parse_df_fixture():
    assert collectors.parse_df(load("df.txt")) == [
        {"mount": "/", "percent": 44},
        {"mount": "/boot", "percent": 11},
        {"mount": "/boot/efi", "percent": 1},
        {"mount": "/mnt/data", "percent": 96},
    ]


def test_parse_df_mount_point_with_spaces():
    text = "/dev/sdc1 ext4 1000 500 500 50% /mnt/my disk\n"
    assert collectors.parse_df(text) == [{"mount": "/mnt/my disk", "percent": 50}]


# --- check_reboot_required ------------------------------------------------------


def test_reboot_required_true(tmp_path):
    marker = tmp_path / "reboot-required"
    marker.write_text("*** System restart required ***\n")
    assert collectors.check_reboot_required(str(marker)) is True


def test_reboot_required_false(tmp_path):
    assert collectors.check_reboot_required(str(tmp_path / "reboot-required")) is False


# --- empty and garbage input for every parser -----------------------------------

PARSERS_AND_EMPTY_RESULTS = [
    (collectors.parse_apt_upgradable, {"total": 0, "security": 0}),
    (collectors.parse_docker_ps, []),
    (collectors.parse_systemctl_failed, []),
    (collectors.parse_systemctl_timers, []),
    (collectors.parse_df, []),
]

GARBAGE_INPUTS = [
    "",
    "\n\n   \n",
    "hello world",
    "Listing...",
    "\x00\x01\x02�� binary junk \x7f",
    "{not json",
    "[" * 100_000,  # deeply nested JSON must not crash the docker parser
    "a b c d e f g h i j k l m n o p",
    "x x x x x ²% /mnt",  # unicode digit that str.isdigit() would accept
    "Exited (1) 3 hours ago",
    "Sun 2026-10-11 19:42:10 UTC",
    "- - - - not-a-unit nope",
    "word " * 50_000,
]


@pytest.mark.parametrize(("parser", "empty_result"), PARSERS_AND_EMPTY_RESULTS)
@pytest.mark.parametrize("text", GARBAGE_INPUTS, ids=range(len(GARBAGE_INPUTS)))
def test_parsers_survive_empty_and_garbage(parser, empty_result, text):
    assert parser(text) == empty_result


# --- run_* with subprocess mocked -----------------------------------------------

RUNNERS_AND_COMMANDS = [
    (collectors.run_apt_upgradable, ["apt", "list", "--upgradable"]),
    (collectors.run_docker_ps, ["docker", "ps", "-a", "--format", "{{json .}}"]),
    (collectors.run_systemctl_failed, ["systemctl", "--failed", "--no-legend", "--plain"]),
    (
        collectors.run_systemctl_timers,
        ["systemctl", "list-timers", "--all", "--no-legend", "--plain"],
    ),
    (collectors.run_df, ["df", "-P", "-T"]),
]


@pytest.fixture
def fake_run(monkeypatch):
    """Replace subprocess.run; tests set .result (or .error) and inspect .calls."""

    class FakeRun:
        def __init__(self):
            self.calls = []
            self.result = None
            self.error = None

        def __call__(self, args, **kwargs):
            self.calls.append((args, kwargs))
            if self.error:
                raise self.error
            return subprocess.CompletedProcess(args, *self.result)

    fake = FakeRun()
    monkeypatch.setattr(collectors.subprocess, "run", fake)
    return fake


@pytest.mark.parametrize(("runner", "command"), RUNNERS_AND_COMMANDS)
def test_run_success_returns_stdout(fake_run, runner, command):
    fake_run.result = (0, "some output\n", "")
    assert runner() == "some output\n"

    [(args, kwargs)] = fake_run.calls
    assert args == command
    assert kwargs["timeout"] == 15
    assert kwargs["check"] is False
    assert kwargs.get("shell", False) is False
    assert kwargs["env"]["LC_ALL"] == "C"


@pytest.mark.parametrize(("runner", "command"), RUNNERS_AND_COMMANDS)
def test_run_command_not_found_returns_none(fake_run, runner, command):
    fake_run.error = FileNotFoundError(2, "No such file or directory", command[0])
    assert runner() is None


@pytest.mark.parametrize(("runner", "command"), RUNNERS_AND_COMMANDS)
def test_run_permission_denied_returns_none(fake_run, runner, command):
    fake_run.error = PermissionError(13, "Permission denied", command[0])
    assert runner() is None


@pytest.mark.parametrize(("runner", "command"), RUNNERS_AND_COMMANDS)
def test_run_timeout_returns_none(fake_run, runner, command):
    fake_run.error = subprocess.TimeoutExpired(command, 15)
    assert runner() is None


@pytest.mark.parametrize(("runner", "command"), RUNNERS_AND_COMMANDS)
def test_run_nonzero_exit_returns_none(fake_run, runner, command):
    fake_run.result = (1, "partial output\n", "something went wrong\n")
    assert runner() is None
