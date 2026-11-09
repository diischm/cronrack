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
        "--network",
        "--network-gateway auto",
        "--network-gateway=192.0.2.1",
        "--network-hop ISP-box=192.0.2.254",
        "--network-hop=ISP-box=192.0.2.254",
        "--network-before Switch=192.0.2.2",
        "--network-before=Switch=192.0.2.2",
        "--no-metrics",
        "--metrics",
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


# --- [network] ---------------------------------------------------------------------------

NETWORK_CONFIG = (
    "import sys; sys.path.insert(0, '/opt/cronrack-agent/app');"
    " from agent.config import load_config; n = load_config().network;"
    " print(n.enabled, n.gateway, [(h.name, h.address) for h in n.hops], n.internet)"
)


def agent_network(box) -> str:
    """[network] as the installed agent itself reads it."""
    return box.ok(
        f'runuser -u cronrack -- /opt/cronrack-agent/venv/bin/python -c "{NETWORK_CONFIG}"'
    ).strip()


def test_network_writes_the_table_the_agent_reads(box, packages):
    box.install('--network --network-gateway 192.0.2.1 --network-hop "ISP box=192.0.2.254"')

    assert "[network]" in box.read(CONFIG)
    assert agent_network(box) == (
        "True 192.0.2.1 [('ISP box', '192.0.2.254')] ['1.1.1.1', '8.8.8.8']"
    )
    assert box.mode(CONFIG) == "root:cronrack 640"


def test_network_asks_for_the_router_when_none_is_detected(box, packages):
    # The test container has no network, so there is no default route to confirm.
    result = box.install("--network", f"192.0.2.1\n{box.token}\n")  # asked before the token

    assert "The router's address" in result.out
    assert agent_network(box).startswith("True 192.0.2.1 []")


def test_network_without_a_terminal_needs_the_gateway(box, packages):
    box.install()
    box.clear_systemctl_calls()

    result = box.sh("setsid -w bash /install.sh --server https://localhost --network <&-")

    assert result.code == 2, result.out
    assert "--network-gateway" in result.out
    assert "[network]" not in box.read(CONFIG)
    assert "Downloading" not in result.out  # refused before anything was done


@pytest.mark.parametrize(
    "hop",
    [
        "no-equals-sign",
        "Internet=8.8.8.8",  # an extra point is local: a private address
        "x=192.0.2.300",
        "x=010.0.0.1",  # the agent's parser refuses leading zeros
        "Router=192.0.2.9",  # a name the chain already uses
        "1.2.3.4=192.0.2.9",
        'quote"d=192.0.2.9',
        "=192.0.2.9",
        "a-name-that-is-much-longer-than-32=192.0.2.9",
    ],
)
def test_bad_network_hops_are_refused_before_anything_happens(box, hop):
    result = box.sh(f"bash /install.sh --server https://localhost --network-hop '{hop}' <&-")

    assert result.code == 2, result.out
    assert "--network-hop" in result.out
    assert box.systemctl_calls() == []
    assert not box.exists(CONFIG)


def test_at_most_two_extra_points(box):
    hops = " ".join(f"--network-hop p{i}=192.0.2.{i}" for i in range(1, 4))
    result = box.sh(f"bash /install.sh --server https://localhost {hops} <&-")

    assert result.code == 2, result.out
    assert "at most 2" in result.out


def test_two_extra_points_need_their_own_names(box):
    result = box.sh(
        "bash /install.sh --server https://localhost"
        " --network-hop a=192.0.2.2 --network-hop a=192.0.2.3 <&-"
    )

    assert result.code == 2, result.out
    assert "two points named" in result.out


@pytest.mark.parametrize(
    "point",
    [
        "no-equals-sign",
        "Switch=8.8.8.8",  # a point before the router is local: a private address
        "x=192.0.2.300",
        "x=010.0.0.1",
        "Router=192.0.2.9",
        "you=192.0.2.9",
        "INTERNET=192.0.2.9",
        "1.2.3.4=192.0.2.9",
        'quote"d=192.0.2.9',
        "=192.0.2.9",
        "a-name-that-is-much-longer-than-32=192.0.2.9",
    ],
)
def test_bad_points_before_the_router_are_refused_before_anything_happens(box, point):
    result = box.sh(f"bash /install.sh --server https://localhost --network-before '{point}' <&-")

    assert result.code == 2, result.out
    assert "--network-before" in result.out
    assert box.systemctl_calls() == []
    assert not box.exists(CONFIG)


def test_at_most_two_points_before_the_router(box):
    points = " ".join(f"--network-before p{i}=192.0.2.{i}" for i in range(1, 4))
    result = box.sh(f"bash /install.sh --server https://localhost {points} <&-")

    assert result.code == 2, result.out
    assert "at most 2" in result.out


def test_a_name_can_only_be_used_once_across_both_lists(box):
    result = box.sh(
        "bash /install.sh --server https://localhost"
        " --network-before a=192.0.2.2 --network-hop a=192.0.2.3 <&-"
    )

    assert result.code == 2, result.out
    assert "two points named" in result.out


def test_a_point_before_the_router_cant_be_the_router(box):
    result = box.sh(
        "bash /install.sh --server https://localhost --network-gateway 192.0.2.1"
        " --network-before Switch=192.0.2.1 <&-"
    )

    assert result.code == 2, result.out
    assert "router's address" in result.out
    assert box.systemctl_calls() == []
    assert not box.exists(CONFIG)


def test_the_flag_is_in_the_usage_text(box):
    assert "--network-before NAME=IP" in box.sh("bash /install.sh --help").out


NETWORK_BEFORE = (
    "import sys; sys.path.insert(0, '/opt/cronrack-agent/app');"
    " from agent.config import load_config; n = load_config().network;"
    " print([(h.name, h.address) for h in n.before], [(h.name, h.address) for h in n.hops])"
)


def agent_network_points(box) -> str:
    return box.ok(
        f'runuser -u cronrack -- /opt/cronrack-agent/venv/bin/python -c "{NETWORK_BEFORE}"'
    ).strip()


def test_points_before_the_router_are_written_in_order(box, packages):
    box.install(
        "--network-gateway auto"  # implies --network, like the flags below
        ' --network-before "Switch=192.0.2.2" --network-before "Access point=192.0.2.3"'
        ' --network-hop "ISP box=192.0.2.254"'
    )

    assert agent_network_points(box) == (
        "[('Switch', '192.0.2.2'), ('Access point', '192.0.2.3')] [('ISP box', '192.0.2.254')]"
    )
    assert box.mode(CONFIG) == "root:cronrack 640"


def test_a_rerun_replaces_only_the_list_it_was_given(box, packages):
    box.install(
        '--network-gateway auto --network-before "Switch=192.0.2.2"'
        ' --network-hop "ISP box=192.0.2.254"'
    )

    box.install('--network-gateway auto --network-before "AP=192.0.2.3"')

    assert agent_network_points(box) == "[('AP', '192.0.2.3')] [('ISP box', '192.0.2.254')]"
    assert box.read(CONFIG).count("before =") == 1


@pytest.mark.parametrize(
    ("rerun", "agent_says"),
    [
        # A name the file's `before` list already uses, given as a point after the router.
        ('--network-gateway auto --network-hop "Switch=192.0.2.3"', "needs its own name"),
        # The mirror case: the file's `hops` list already uses the name.
        ('--network-gateway auto --network-before "ISP box=192.0.2.3"', "needs its own name"),
        # The router's address, which the file's `before` list already uses.
        ("--network-gateway 192.0.2.2", "router's address"),
    ],
)
def test_a_rerun_the_agent_would_refuse_changes_nothing(box, packages, rerun, agent_says):
    box.install(
        '--network-gateway auto --network-before "Switch=192.0.2.2"'
        ' --network-hop "ISP box=192.0.2.254"'
    )
    before = snapshot(box)
    box.clear_systemctl_calls()

    result = box.interactive(f"--server https://localhost --ca-file /srv/tls/ca.crt {rerun}", "")

    assert result.code == 1, result.out
    assert agent_says in result.out  # the agent's own words, not a guess of the installer's
    assert "put back" in result.out
    assert "Done." not in result.out
    assert snapshot(box) == before  # agent.toml is exactly as it was
    assert restarts(box) == 0


def test_a_name_the_agent_cant_print_is_refused_before_the_restart(box, packages):
    box.install()
    before = snapshot(box)
    box.clear_systemctl_calls()

    result = box.interactive(
        "--server https://localhost --ca-file /srv/tls/ca.crt --network-gateway auto"
        " --network-before 'a b=192.0.2.2'",  # a no-break space: bash lets it through
        "",
    )

    assert result.code == 1, result.out
    assert "printable" in result.out
    assert "Done." not in result.out
    assert snapshot(box) == before
    assert restarts(box) == 0


def test_upgrade_keeps_points_before_the_router_byte_for_byte(box, packages):
    box.install('--network-gateway auto --network-before "Switch=192.0.2.2"')
    before = snapshot(box)
    box.use_package("next")

    result = box.upgrade()

    assert result.code == 0, result.out
    assert_asked_nothing(result.out)
    assert snapshot(box) == before
    assert 'before = [{ name = "Switch", address = "192.0.2.2" }]' in box.read(CONFIG)


def test_rerun_with_network_keeps_the_other_network_keys(box, packages):
    box.install("--network --network-gateway auto")
    box.ok(f"printf 'public_ip_check = true\\n' >>{CONFIG}")  # [network] is the last table

    box.install('--network --network-gateway 192.0.2.1 --network-hop "Modem=192.0.2.200"')

    config = box.read(CONFIG)
    assert config.count("[network]") == 1
    assert "public_ip_check = true" in config
    assert 'gateway = "192.0.2.1"' in config and 'gateway = "auto"' not in config
    assert agent_network(box).startswith("True 192.0.2.1 [('Modem', '192.0.2.200')]")


def test_upgrade_keeps_the_network_table(box, packages):
    box.install('--network --network-gateway auto --network-hop "ISP box=192.0.2.254"')
    before = snapshot(box)
    box.use_package("next")

    result = box.upgrade()

    assert result.code == 0, result.out
    assert_asked_nothing(result.out)
    assert snapshot(box) == before
    kept = next(line for line in result.out.splitlines() if "Kept:" in line)
    assert "[network] settings" in kept


# --- [metrics] (agent 0.4.0) -------------------------------------------------------------

METRICS_CONFIG = (
    "import sys; sys.path.insert(0, '/opt/cronrack-agent/app');"
    " from agent.config import load_config; print(load_config().metrics.enabled)"
)


def agent_metrics(box) -> str:
    """[metrics] enabled, as the installed agent itself reads it."""
    return box.ok(
        f'runuser -u cronrack -- /opt/cronrack-agent/venv/bin/python -c "{METRICS_CONFIG}"'
    ).strip()


def test_metrics_are_on_by_default_and_need_no_table(box, packages):
    box.install()

    assert "[metrics]" not in box.read(CONFIG)
    assert agent_metrics(box) == "True"


def test_no_metrics_writes_the_table_the_agent_reads(box, packages):
    result = box.install("--no-metrics")

    assert "[metrics] enabled = false" in result.out
    assert "[metrics]\nenabled = false" in box.read(CONFIG)
    assert agent_metrics(box) == "False"
    assert box.mode(CONFIG) == "root:cronrack 640"


def test_metrics_turns_them_back_on_and_keeps_everything_else(box, packages):
    box.install("--no-metrics --network --network-gateway auto")

    box.install("--metrics")

    config = box.read(CONFIG)
    assert config.count("[metrics]") == 1
    assert "enabled = false" not in config.split("[metrics]")[1]
    assert agent_metrics(box) == "True"
    assert agent_network(box).startswith("True auto")
    assert "Token kept" in box.install("").out  # a plain re-run leaves [metrics] alone
    assert agent_metrics(box) == "True"


def test_no_metrics_and_metrics_together_are_refused(box):
    result = box.sh("bash /install.sh --server https://localhost --no-metrics --metrics <&-")

    assert result.code == 2, result.out
    assert "can't be used together" in result.out
    assert box.systemctl_calls() == []


def test_upgrade_keeps_metrics_off(box, packages):
    box.install("--no-metrics")
    before = snapshot(box)
    box.use_package("next")

    result = box.upgrade()

    assert result.code == 0, result.out
    assert_asked_nothing(result.out)
    assert snapshot(box) == before  # agent.toml byte for byte, [metrics] enabled = false
    kept = next(line for line in result.out.splitlines() if "Kept:" in line)
    assert "[metrics] settings" in kept
    assert agent_metrics(box) == "False"


def test_upgrade_of_a_config_without_metrics_changes_nothing_in_it(box, packages):
    # An agent.toml from before 0.4.0 has no [metrics]: the upgrade leaves the file alone
    # and the new agent collects metrics (the default).
    box.install()
    before = snapshot(box)
    box.use_package("next")

    result = box.upgrade()

    assert result.code == 0, result.out
    assert snapshot(box) == before
    assert "[metrics]" not in box.read(CONFIG)
