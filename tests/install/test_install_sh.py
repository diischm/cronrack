"""deploy/install.sh, run for real as root in throwaway containers (see conftest.py).

Mostly --upgrade: it must ask nothing, keep every setting, refuse what it can't do
safely, and roll back code that doesn't start. Plus the INSTALLED record and the
Docker question's default on a normal re-run.
"""

import re

import pytest

CONFIG = "/etc/cronrack/agent.toml"
SETTINGS_FILES = (
    CONFIG,
    "/etc/cronrack/ca.crt",
    "/etc/cronrack/k8s-token",
    "/etc/cronrack/k8s-ca.crt",
)
DROPIN = "/etc/systemd/system/cronrack-agent.service.d/docker.conf"
INSTALLED = "/opt/cronrack-agent/INSTALLED"
PROMPTS = ("Token:", "[y/N]", "[Y/n]", "Replace it?", "Kubernetes token:")

K8S_INSTALL = "--k8s-cluster-view --k8s-ca /srv/tls/ca.crt"


def snapshot(box) -> dict[str, tuple[str, str]]:
    """Content, owner and mode of every settings file that exists."""
    return {path: (box.read(path), box.mode(path)) for path in SETTINGS_FILES if box.exists(path)}


def installed(box) -> dict[str, str]:
    lines = [line for line in box.read(INSTALLED).splitlines() if not line.startswith("#")]
    return dict(line.split("=", 1) for line in lines)


def agent_version(box) -> str:
    text = box.read("/opt/cronrack-agent/app/agent/__init__.py")
    return re.search(r'^AGENT_VERSION = "([^"]+)"$', text, re.MULTILINE).group(1)


def restarts(box) -> int:
    return sum(call.startswith("restart") for call in box.systemctl_calls())


def assert_asked_nothing(out: str) -> None:
    for prompt in PROMPTS:
        assert prompt not in out
    assert "internal error" not in out
    assert "/dev/tty" not in out


# --- refusals ---------------------------------------------------------------------------


def test_upgrade_refuses_when_nothing_is_installed(box):
    result = box.upgrade()

    assert result.code == 3, result.out
    assert "no CronRack agent is installed here" in result.out
    assert "without --upgrade" in result.out
    assert not box.exists("/opt/cronrack-agent")
    assert box.sh("id -u cronrack").code != 0
    assert box.systemctl_calls() == []


def test_upgrade_refuses_when_the_service_is_missing(box):
    box.install()
    box.ok("rm /etc/systemd/system/cronrack-agent.service")

    result = box.upgrade()

    assert result.code == 3, result.out
    assert "missing: the cronrack-agent.service service" in result.out


@pytest.mark.parametrize(
    "flag",
    [
        "--new-token",
        "--k8s",
        "--k8s-cluster-view",
        "--k8s-ca /srv/tls/ca.crt",
        "--k8s-ca=/srv/tls/ca.crt",
        "--k8s-api https://127.0.0.1:6443",
        "--k8s-api=https://127.0.0.1:6443",
        "--uninstall",
    ],
)
def test_upgrade_refuses_options_that_change_settings(box, flag):
    result = box.upgrade(f"--server https://localhost {flag}")

    assert result.code == 2, result.out
    assert "those change settings; run without --upgrade" in result.out
    assert flag.split()[0].split("=")[0] in result.out
    assert box.systemctl_calls() == []


def test_upgrade_needs_server(box):
    result = box.upgrade(args="")

    assert result.code == 2, result.out
    assert "--server is required" in result.out


def test_server_change_needs_upgrade(box):
    result = box.sh("bash /install.sh --server https://localhost --server-change </dev/null")

    assert result.code == 2, result.out
    assert "--server-change only works with --upgrade" in result.out


@pytest.mark.parametrize(
    "other",
    [
        "https://127.0.0.1",
        "https://localhost:8443",
        "https://localhost/cronrack",
        "https://other.example.lan",
        "http://localhost",
    ],
)
def test_upgrade_refuses_another_server(box, other):
    box.install()
    before = snapshot(box)
    box.clear_systemctl_calls()

    result = box.upgrade(f"--server {other}")

    assert result.code == 4, result.out
    assert "is not the server this agent uses" in result.out
    assert "add --server-change" in result.out
    assert snapshot(box) == before
    assert box.systemctl_calls() == []


@pytest.mark.parametrize(
    "same",
    ["https://LOCALHOST", "https://localhost:443", "https://LocalHost:443/", "https://localhost/"],
)
def test_server_urls_equal_after_normalizing_are_the_same_server(box, same):
    box.install()
    before = snapshot(box)

    result = box.upgrade(f"--server {same}")

    assert result.code == 0, result.out
    assert "Already up to date" in result.out
    assert snapshot(box) == before


def test_server_url_in_the_config_is_normalized_too(box):
    box.install()
    box.ok(f"""sed -i 's|^server_url = .*|server_url = "HTTPS://LocalHost:443/"|' {CONFIG}""")
    before = snapshot(box)

    result = box.upgrade("--server https://localhost")

    assert result.code == 0, result.out
    assert snapshot(box) == before


def test_server_change_moves_the_agent(box, packages):
    box.interactive("--server http://127.0.0.1:8000", f"{box.token}\n")
    token_line = next(line for line in box.read(CONFIG).splitlines() if line.startswith("token ="))

    result = box.upgrade("--server http://localhost:8000 --server-change")

    assert result.code == 0, result.out
    assert "Changed: server_url is now http://localhost:8000" in result.out
    config = box.read(CONFIG).splitlines()
    assert 'server_url = "http://localhost:8000"' in config
    assert token_line in config
    assert box.mode(CONFIG) == "root:cronrack 640"
    assert installed(box)["server_host"] == "localhost"


def test_upgrade_refuses_docker_access_without_a_docker_group(box, packages):
    box.ok("groupadd docker")
    box.install(answers=f"{box.token}\ny\n")
    box.ok("groupdel docker")
    box.use_package("next")
    box.clear_systemctl_calls()

    result = box.upgrade()

    assert result.code == 1, result.out
    assert "the docker group no longer exists" in result.out
    assert box.exists(DROPIN)
    assert installed(box)["version"] == packages.current_version
    assert box.systemctl_calls() == []


# --- what an upgrade keeps --------------------------------------------------------------


@pytest.mark.parametrize("docker_access", [True, False], ids=["docker-on", "docker-off"])
def test_upgrade_keeps_every_setting(box, packages, docker_access):
    box.ok("groupadd docker")
    answer = "y" if docker_access else "n"
    box.install(K8S_INSTALL, f"{box.k8s_token}\n{box.token}\n{answer}\n")
    assert box.exists(DROPIN) is docker_access
    before = snapshot(box)
    assert set(before) == set(SETTINGS_FILES)
    assert "[kubernetes]" in before[CONFIG][0]
    box.use_package("next")
    box.clear_systemctl_calls()

    result = box.upgrade()

    assert result.code == 0, result.out
    assert_asked_nothing(result.out)
    assert snapshot(box) == before  # token, k8s token, [kubernetes], CA: byte for byte
    assert box.exists(DROPIN) is docker_access
    assert f"Upgraded the CronRack agent: {packages.current_version} -> 0.99.0" in result.out
    kept = next(line for line in result.out.splitlines() if "Kept:" in line)
    for item in ("token", "CA certificate", "Kubernetes token", "[kubernetes] settings"):
        assert item in kept
    assert ("Docker access (on)" if docker_access else "Docker access (off)") in kept
    assert agent_version(box) == packages.next_version
    assert installed(box)["version"] == packages.next_version
    assert installed(box)["sha256"] == packages.sha256["next"]
    assert restarts(box) == 1
    assert box.is_active()
    assert not box.exists("/opt/cronrack-agent/app.prev")
    assert not box.exists("/opt/cronrack-agent/INSTALLED.prev")
    assert box.mode("/opt/cronrack-agent/app") == "root:root 755"


def test_upgrade_with_stdin_closed_never_reads_the_terminal(box, packages):
    box.install(K8S_INSTALL, k8s_view=True)
    box.use_package("next")

    # -x traces every command: a question would show up as a traced `read -p`/`read -s`.
    result = box.sh("setsid -w bash -x /install.sh --server https://localhost --upgrade <&-")

    assert result.code == 0, result.out
    assert "Upgraded the CronRack agent" in result.out
    assert not re.search(r"^\++ *read -r -[ps]", result.out, re.MULTILINE)
    assert not re.search(r"^\++ *(ask_yes_no|ask_token|ask_k8s_token)\b", result.out, re.MULTILINE)
    assert_asked_nothing(result.out)
    # Reading agent.toml is kept out of the trace: no token in a debug log.
    assert box.token not in result.out
    assert box.k8s_token not in result.out


def test_server_change_under_bash_x_does_not_trace_the_token(box):
    # --server-change rewrites agent.toml, which holds the token.
    box.interactive("--server http://127.0.0.1:8000", f"{box.token}\n")

    result = box.sh(
        "setsid -w bash -x /install.sh --server http://localhost:8000 --upgrade --server-change <&-"
    )

    assert result.code == 0, result.out
    assert "server_url is now http://localhost:8000" in result.out
    assert box.token not in result.out


def test_a_cut_off_download_runs_nothing(box):
    # curl | bash with the connection dropped half-way: bash gets half the script.
    result = box.sh(
        "head -c $(( $(wc -c </install.sh) / 2 )) /install.sh"
        " | setsid -w bash -s -- --server https://localhost --upgrade"
    )

    assert result.code != 0
    assert "==>" not in result.out
    assert box.systemctl_calls() == []


def test_only_one_installer_runs_at_a_time(box):
    box.install()
    box.ok("(flock /run/cronrack-agent-install.lock sleep 60 &) ; sleep 0.5")

    result = box.upgrade()

    assert result.code == 1, result.out
    assert "another CronRack install or upgrade is running" in result.out


def test_an_upgrade_interrupted_after_the_swap_is_not_up_to_date(box, packages):
    box.install()
    old_record = box.read(INSTALLED)
    box.use_package("next")
    box.ok("touch /etc/fake-systemctl-reload-fail")

    result = box.upgrade()  # fails after the new code is in place

    assert result.code == 1, result.out
    assert box.read(INSTALLED) == old_record  # never claims the unverified version

    box.ok("rm /etc/fake-systemctl-reload-fail")
    result = box.upgrade()

    assert result.code == 0, result.out
    assert "Already up to date" not in result.out
    assert installed(box)["version"] == packages.next_version


def test_already_up_to_date_changes_and_restarts_nothing(box, packages):
    box.install()
    before = (snapshot(box), box.read(INSTALLED), box.ok("ls -l --full-time /opt/cronrack-agent"))
    box.clear_systemctl_calls()

    result = box.upgrade()

    assert result.code == 0, result.out
    assert f"Already up to date ({packages.current_version})" in result.out
    after = (snapshot(box), box.read(INSTALLED), box.ok("ls -l --full-time /opt/cronrack-agent"))
    assert after == before
    assert restarts(box) == 0
    assert not any(c.startswith(("daemon-reload", "enable")) for c in box.systemctl_calls())


def test_already_up_to_date_but_not_running_exits_7_and_starts_nothing(box, packages):
    box.install()
    box.ok("systemctl stop cronrack-agent.service")
    before = (snapshot(box), box.read(INSTALLED))
    box.clear_systemctl_calls()

    result = box.upgrade()

    assert result.code == 7, result.out
    assert f"already up to date ({packages.current_version})" in result.out
    assert "the agent service is not running" in result.out
    assert "systemctl status cronrack-agent" in result.out
    assert "journalctl -u cronrack-agent" in result.out
    assert (snapshot(box), box.read(INSTALLED)) == before
    calls = box.systemctl_calls()
    assert not any(c.startswith(("restart", "start", "enable", "daemon-reload")) for c in calls)
    assert not box.is_active()


def test_upgrade_keeps_a_disabled_service_disabled(box, packages):
    box.install()
    box.ok("systemctl disable cronrack-agent.service")  # without --now: still running
    assert box.is_active()
    box.use_package("next")
    box.clear_systemctl_calls()

    result = box.upgrade()

    assert result.code == 0, result.out
    assert installed(box)["version"] == packages.next_version
    assert restarts(box) == 1
    assert not any(c.startswith("enable") for c in box.systemctl_calls())
    assert not box.is_enabled()
    assert box.is_active()


def test_upgrade_keeps_an_enabled_service_enabled(box, packages):
    box.install()
    assert box.is_enabled()
    box.use_package("next")

    result = box.upgrade()

    assert result.code == 0, result.out
    assert box.is_enabled()
    assert box.is_active()


def test_an_agent_without_installed_record_is_upgraded(box, packages):
    box.install()
    box.ok(f"rm {INSTALLED}")  # as installed by an older install.sh

    result = box.upgrade()

    assert result.code == 0, result.out
    assert f"{packages.current_version} -> {packages.current_version}" in result.out
    assert installed(box)["sha256"] == packages.sha256["current"]


def test_upgrade_can_replace_the_ca(box, packages):
    box.install()

    result = box.upgrade("--server https://localhost --ca-file /srv/tls/ca.crt")

    assert result.code == 0, result.out
    assert "Changed: the CA certificate was replaced" in result.out
    assert box.mode("/etc/cronrack/ca.crt") == "root:cronrack 640"


# --- the INSTALLED record ---------------------------------------------------------------


def test_installed_record_is_written_without_secrets(box, packages):
    box.install(K8S_INSTALL, k8s_view=True)

    assert box.mode(INSTALLED) == "root:root 644"
    text = box.read(INSTALLED)
    record = installed(box)
    assert list(record) == ["format", "version", "sha256", "server_host", "installed_at"]
    assert record["format"] == "1"
    assert record["version"] == packages.current_version
    assert record["sha256"] == packages.sha256["current"]
    assert record["server_host"] == "localhost"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record["installed_at"])
    assert box.token not in text
    assert box.k8s_token not in text
    assert "token" not in text.lower()


# --- rollback ---------------------------------------------------------------------------


def test_failed_upgrade_rolls_back(box, packages):
    box.install()
    old_record = box.read(INSTALLED)
    box.use_package("broken")
    box.clear_systemctl_calls()

    result = box.upgrade()

    assert result.code == 6, result.out
    assert f"upgrade failed, rolled back to {packages.current_version}" in result.out
    assert "journalctl -u cronrack-agent" in result.out
    assert agent_version(box) == packages.current_version
    assert box.read(INSTALLED) == old_record
    assert not box.exists("/opt/cronrack-agent/app.prev")
    assert restarts(box) == 2
    assert box.is_active()


def test_rollback_that_fails_too_exits_5(box, packages):
    box.install()
    box.ok("touch /etc/fake-systemctl-fail")
    box.use_package("next")

    result = box.upgrade()

    assert result.code == 5, result.out
    assert "the rollback" in result.out
    assert "the agent is NOT running" in result.out
    assert "journalctl -u cronrack-agent -n 50" in result.out
    assert not box.is_active()


# --- the Docker question on a normal re-run -----------------------------------------------


def test_enter_keeps_docker_access_and_n_removes_it(box):
    box.ok("groupadd docker")
    box.install(answers=f"{box.token}\ny\n")
    assert box.exists(DROPIN)

    result = box.interactive("--server https://localhost", "\n")

    assert result.code == 0, result.out
    assert "Docker access is on. Keep it?" in result.out
    assert "[Y/n]" in result.out
    assert box.exists(DROPIN)

    result = box.interactive("--server https://localhost", "n\n")

    assert result.code == 0, result.out
    assert not box.exists(DROPIN)


def test_enter_keeps_docker_access_off(box):
    box.ok("groupadd docker")
    box.install(answers=f"{box.token}\n\n")
    assert not box.exists(DROPIN)

    result = box.interactive("--server https://localhost", "\n")

    assert result.code == 0, result.out
    assert "Give the agent Docker access?" in result.out
    assert "[y/N]" in result.out
    assert not box.exists(DROPIN)
