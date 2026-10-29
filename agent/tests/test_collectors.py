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


# --- Kubernetes node role ---------------------------------------------------------


def test_parse_unit_active():
    assert collectors.parse_unit_active("active\n") is True
    assert collectors.parse_unit_active("inactive\n") is False
    assert collectors.parse_unit_active("activating\n") is False
    assert collectors.parse_unit_active("") is False


def test_parse_k3s_version():
    text = "k3s version v1.31.2+k3s1 (6f4217a7)\ngo version go1.22.8\n"
    assert collectors.parse_k3s_version(text) == "v1.31.2+k3s1"


def test_parse_kubelet_version():
    assert collectors.parse_kubelet_version("Kubernetes v1.31.2\n") == "v1.31.2"
    assert collectors.parse_kubelet_version("Kubernetes v1.31.2-rc.1\n") == "v1.31.2-rc.1"


def test_parse_helm_version_drops_the_git_sha():
    assert collectors.parse_helm_version("v3.16.2+g13654a5\n") == "v3.16.2"
    assert collectors.parse_helm_version("v3.16.2\n") == "v3.16.2"


def fake_node_tools(monkeypatch, *, active=(), k3s="k3s version v1.31.2+k3s1 (abc)\n"):
    """Pretend these systemd units are active and the version commands print this."""
    monkeypatch.setattr(
        collectors, "run_unit_active", lambda unit: "active\n" if unit in active else None
    )
    monkeypatch.setattr(collectors, "run_k3s_version", lambda: k3s)
    monkeypatch.setattr(collectors, "run_kubelet_version", lambda: "Kubernetes v1.30.1\n")
    monkeypatch.setattr(collectors, "run_helm_version", lambda: "v3.16.2+g13654a5\n")
    monkeypatch.setattr(collectors.socket, "gethostname", lambda: "Server-1.home.lan")


def test_node_role_k3s_server(monkeypatch):
    fake_node_tools(monkeypatch, active={"k3s"})
    assert collectors.collect_node_role() == {
        "role": "server",
        "dist": "k3s",
        "name": "server-1",
        "k3s": "v1.31.2+k3s1",
        "kubelet": None,
        "helm": "v3.16.2",
    }


def test_node_role_k3s_agent(monkeypatch):
    fake_node_tools(monkeypatch, active={"k3s-agent"})
    node = collectors.collect_node_role()
    assert (node["role"], node["dist"], node["k3s"]) == ("agent", "k3s", "v1.31.2+k3s1")


def test_node_role_plain_kubelet(monkeypatch):
    fake_node_tools(monkeypatch, active={"kubelet"})
    node = collectors.collect_node_role()
    assert (node["role"], node["dist"]) == ("node", "kubernetes")
    assert node["kubelet"] == "v1.30.1"
    assert node["k3s"] is None


def test_node_role_none(monkeypatch):
    fake_node_tools(monkeypatch, active=set())
    node = collectors.collect_node_role()
    assert (node["role"], node["dist"], node["k3s"], node["kubelet"]) == ("none", None, None, None)


def test_node_role_server_wins_when_both_units_are_active(monkeypatch):
    fake_node_tools(monkeypatch, active={"k3s", "k3s-agent", "kubelet"})
    assert collectors.collect_node_role()["role"] == "server"


def test_node_role_missing_version_tools_give_none(monkeypatch):
    fake_node_tools(monkeypatch, active={"k3s"}, k3s=None)
    monkeypatch.setattr(collectors, "run_helm_version", lambda: None)
    node = collectors.collect_node_role()
    assert node["role"] == "server"
    assert node["k3s"] is None
    assert node["helm"] is None


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
    (collectors.parse_unit_active, False),
    (collectors.parse_k3s_version, None),
    (collectors.parse_kubelet_version, None),
    (collectors.parse_helm_version, None),
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
    (lambda: collectors.run_unit_active("k3s"), ["systemctl", "is-active", "k3s"]),
    (lambda: collectors.run_unit_active("k3s-agent"), ["systemctl", "is-active", "k3s-agent"]),
    (lambda: collectors.run_unit_active("kubelet"), ["systemctl", "is-active", "kubelet"]),
    (collectors.run_k3s_version, ["k3s", "--version"]),
    (collectors.run_kubelet_version, ["kubelet", "--version"]),
    (collectors.run_helm_version, ["helm", "version", "--short"]),
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
    # Only the system's own programs, whatever PATH the agent was started with.
    assert kwargs["env"]["PATH"] == (
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
    )


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
