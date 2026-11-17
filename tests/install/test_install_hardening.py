"""install.sh and uninstall.sh: verifiable install, read-only Docker proxy, egress lock, clean
removal. Run for real as root in throwaway containers (see conftest.py).

- install.sh prints what it will do and asks "Continue?" before it changes anything;
  --yes answers that for scripts, and without a terminal and without --yes it refuses.
- --expect-sha256 checks the download against a hash from somewhere other than the server.
- Docker access is the read-only proxy (cronrack-docker-proxy.socket), never the docker group.
- --egress-lock limits the agent to the server's addresses; --network checks rule it out.
- uninstall.sh removes everything it installed and lists what it removed.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONFIG = "/etc/cronrack/agent.toml"
UNITS = "/etc/systemd/system"
AGENT_UNIT = f"{UNITS}/cronrack-agent.service"
PROXY_SOCKET = f"{UNITS}/cronrack-docker-proxy.socket"
PROXY_SERVICE = f"{UNITS}/cronrack-docker-proxy.service"
DROPIN_DIR = f"{UNITS}/cronrack-agent.service.d"
OLD_DOCKER_DROPIN = f"{DROPIN_DIR}/docker.conf"
EGRESS_DROPIN = f"{DROPIN_DIR}/egress.conf"
HTTPS = "--server https://localhost --ca-file /srv/tls/ca.crt"


def config(box) -> str:
    return box.read(CONFIG)


def groups_of(box, user: str) -> list[str]:
    return box.ok(f"id -nG {user}").split()


def nothing_installed(box) -> None:
    assert box.sh("id -u cronrack").code != 0
    assert not box.exists("/opt/cronrack-agent")
    assert not box.exists(CONFIG)
    assert not box.exists(AGENT_UNIT)


# --- "Continue?" -----------------------------------------------------------------------------


def test_plan_is_shown_and_no_stops_before_anything_changes(box):
    result = box.interactive(HTTPS, "n\n", confirm=True)

    assert result.code != 0, result.out
    assert "This will:" in result.out
    assert "Continue? [Y/n]" in result.out
    assert "Nothing was changed" in result.out
    # The plan names every kind of change, before the first one happens.
    plan = result.out.split("This will:", 1)[1].split("Continue?", 1)[0]
    for item in ("cronrack", "/opt/cronrack-agent", CONFIG, "cronrack-agent.service"):
        assert item in plan
    assert result.out.index("This will:") < result.out.index("Continue?")
    nothing_installed(box)


def test_enter_at_continue_installs(box):
    result = box.interactive(HTTPS, f"\n{box.token}\n", confirm=True)

    assert result.code == 0, result.out
    assert box.exists(AGENT_UNIT)
    assert box.is_active()


def test_no_terminal_and_no_yes_refuses_at_once(box):
    result = box.no_tty(HTTPS)

    assert result.code == 2, result.out
    assert "--yes" in result.out
    assert "no terminal" in result.out
    nothing_installed(box)


def test_yes_without_a_terminal_reruns_and_keeps_everything(box):
    box.install()
    before = config(box)

    result = box.no_tty(f"{HTTPS} --yes")

    assert result.code == 0, result.out
    assert "Continue?" not in result.out
    assert config(box) == before
    assert box.is_active()


def test_yes_without_a_terminal_cannot_invent_a_token(box):
    result = box.no_tty(f"{HTTPS} --yes")

    assert result.code != 0, result.out
    assert "token" in result.out
    nothing_installed(box)


def test_upgrade_prints_the_plan_but_never_asks(box, packages):
    box.install()
    box.use_package("next")

    result = box.upgrade()

    assert result.code == 0, result.out
    assert "This will:" in result.out
    assert "Continue?" not in result.out


# --- --expect-sha256: a hash from somewhere other than the server -------------------------------


def test_expect_sha256_that_matches(box, packages):
    result = box.install(f"--expect-sha256 {packages.sha256['current']}")

    assert "matches --expect-sha256" in result.out


def test_expect_sha256_that_does_not_match_installs_nothing(box, packages):
    wrong = packages.sha256["next"]

    result = box.interactive(f"{HTTPS} --expect-sha256 {wrong}", f"{box.token}\n")

    assert result.code != 0, result.out
    assert "does not match --expect-sha256" in result.out
    assert not box.exists("/opt/cronrack-agent/app")
    assert not box.exists(AGENT_UNIT)


@pytest.mark.parametrize("value", ["abc", "G" * 64, "a" * 63, "a" * 65, ""])
def test_malformed_expect_sha256_is_a_usage_error(box, value):
    result = box.interactive(f"{HTTPS} --expect-sha256 '{value}'", "")

    assert result.code == 2, result.out
    nothing_installed(box)


def test_upgrade_with_expect_sha256(box, packages):
    box.install()
    box.use_package("next")

    result = box.upgrade(f"--server https://localhost --expect-sha256 {packages.sha256['next']}")

    assert result.code == 0, result.out
    assert "matches --expect-sha256" in result.out


def test_installer_prints_the_hash_and_how_to_check_it(box, packages):
    result = box.install()

    assert packages.sha256["current"] in result.out
    assert "scripts/agent-package-sha256.py" in result.out


# --- Docker: the read-only proxy, never the docker group --------------------------------------


def test_docker_access_installs_the_proxy_not_the_group(box):
    box.ok("groupadd docker")

    result = box.install(answers=f"{box.token}\ny\n")

    assert "read-only Docker proxy" in result.out
    assert box.exists(PROXY_SOCKET) and box.exists(PROXY_SERVICE)
    assert box.mode(PROXY_SOCKET) == "root:root 644"
    assert not box.exists(OLD_DOCKER_DROPIN)
    assert "docker" not in groups_of(box, "cronrack")
    assert re.search(r"(?m)^\[docker\]\nenabled = true$", config(box))
    calls = box.systemctl_calls()
    assert any("enable" in c and "cronrack-docker-proxy.socket" in c for c in calls)
    # The installed unit files are the ones from the verified package, byte for byte.
    shipped = (REPO / "deploy" / "cronrack-docker-proxy.service").read_text()
    assert box.read(PROXY_SERVICE) == shipped


def test_no_docker_access_installs_no_proxy(box):
    box.ok("groupadd docker")

    box.install(answers=f"{box.token}\nn\n")

    assert not box.exists(PROXY_SOCKET) and not box.exists(PROXY_SERVICE)
    assert "enabled = true" not in config(box).split("[docker]")[-1]


def test_turning_docker_access_off_removes_the_proxy(box):
    box.ok("groupadd docker")
    box.install(answers=f"{box.token}\ny\n")

    result = box.interactive("--server https://localhost", "n\n")

    assert result.code == 0, result.out
    assert not box.exists(PROXY_SOCKET) and not box.exists(PROXY_SERVICE)
    assert re.search(r"(?m)^\[docker\]\nenabled = false$", config(box))


def test_upgrade_moves_an_old_docker_group_dropin_to_the_proxy(box, packages):
    """Agents before 0.5.0 got Docker access through the docker group. An upgrade keeps the
    choice (on) but switches it to the proxy and removes the root-equivalent group drop-in."""
    box.ok("groupadd docker")
    box.install(answers=f"{box.token}\nn\n")
    # What an older installer left behind for "Docker access: on".
    box.ok(
        f"install -d {DROPIN_DIR} && printf '[Service]\\nSupplementaryGroups=docker\\n' "
        f"> {OLD_DOCKER_DROPIN} && sed -i '/^\\[docker\\]/,$d' {CONFIG}"
    )
    box.use_package("next")

    result = box.upgrade()

    assert result.code == 0, result.out
    assert not box.exists(OLD_DOCKER_DROPIN)
    assert box.exists(PROXY_SOCKET)
    assert re.search(r"(?m)^\[docker\]\nenabled = true$", config(box))
    assert "Docker access (on)" in result.out
    assert "docker group" in result.out  # it says what it removed


# --- --egress-lock -----------------------------------------------------------------------------


def test_egress_lock_allows_only_the_server(box):
    result = box.install("--egress-lock")

    dropin = box.read(EGRESS_DROPIN)
    assert "IPAddressDeny=any" in dropin
    allow = next(line for line in dropin.splitlines() if line.startswith("IPAddressAllow="))
    # Exact addresses only: "localhost" would open every local port (e.g. a Docker API on
    # 127.0.0.1:2375). The server here is localhost, so its 127.0.0.1 is in.
    assert "localhost" not in allow
    assert "127.0.0.1" in allow
    assert box.mode(EGRESS_DROPIN) == "root:root 644"
    assert "if the server's address changes" in result.out
    assert "This will:" in result.out and "lock the agent's outgoing traffic to" in result.out


def test_egress_lock_is_refused_with_network_checks(box):
    result = box.interactive(f"{HTTPS} --egress-lock --network --network-gateway auto", "")

    assert result.code == 2, result.out
    assert "--egress-lock" in result.out and "network" in result.out
    nothing_installed(box)


def test_egress_lock_is_refused_when_network_checks_are_already_on(box):
    box.install("--network --network-gateway auto")

    result = box.interactive(f"{HTTPS} --egress-lock", "")

    assert result.code == 2, result.out
    assert not box.exists(EGRESS_DROPIN)


def test_a_rerun_refreshes_the_egress_lock(box):
    box.install("--egress-lock")
    box.ok(f"sed -i 's/^IPAddressAllow=.*/IPAddressAllow=192.0.2.99/' {EGRESS_DROPIN}")

    result = box.interactive("--server https://localhost", "")

    assert result.code == 0, result.out
    assert "Egress lock refreshed" in result.out
    assert "192.0.2.99" not in box.read(EGRESS_DROPIN)


def test_egress_lock_for_a_name_that_does_not_resolve_changes_nothing(box):
    result = box.interactive(
        "--server https://no-such-host.invalid --egress-lock", f"{box.token}\n"
    )

    assert result.code != 0, result.out
    assert "cannot resolve" in result.out
    nothing_installed(box)


def test_uninstall_keeps_drop_ins_it_did_not_write(box):
    full_install(box)
    box.ok(f"printf '[Service]\\nMemoryMax=200M\\n' > {DROPIN_DIR}/mine.conf")

    result = box.sh("setsid -w bash /install.sh --uninstall <&-")

    assert result.code == 0, result.out
    assert box.exists(f"{DROPIN_DIR}/mine.conf")
    assert not box.exists(EGRESS_DROPIN)
    assert "mine.conf" in result.out


def test_no_egress_lock_removes_it(box):
    box.install("--egress-lock")

    result = box.interactive("--server https://localhost --no-egress-lock", "")

    assert result.code == 0, result.out
    assert not box.exists(EGRESS_DROPIN)


def test_upgrade_keeps_the_egress_lock(box, packages):
    box.install("--egress-lock")
    before = box.read(EGRESS_DROPIN)
    box.use_package("next")

    result = box.upgrade()

    assert result.code == 0, result.out
    assert box.read(EGRESS_DROPIN) == before
    assert "egress lock (on)" in result.out


# --- uninstall.sh --------------------------------------------------------------------------------


def full_install(box) -> None:
    box.ok("groupadd docker")
    box.install("--egress-lock", f"{box.token}\ny\n")
    box.ok("install -d /run/cronrack && touch /run/cronrack/docker.sock")


def test_uninstall_removes_everything_and_lists_it(box):
    full_install(box)

    result = box.sh("setsid -w bash /install.sh --uninstall <&-")

    assert result.code == 0, result.out
    for path in (
        AGENT_UNIT,
        PROXY_SOCKET,
        PROXY_SERVICE,
        DROPIN_DIR,
        "/opt/cronrack-agent",
        "/run/cronrack",
    ):
        assert not box.exists(path), path
        assert f"Removed {path}" in result.out
    assert box.sh("id -u cronrack").code != 0
    assert "Removed user cronrack" in result.out
    # No terminal to ask on: the config (it holds the token) is kept, and given to root.
    assert box.exists(CONFIG)
    assert box.mode("/etc/cronrack").startswith("root:root")
    assert "Kept /etc/cronrack" in result.out
    calls = box.systemctl_calls()
    assert any("disable" in c and "cronrack-docker-proxy.socket" in c for c in calls)


def test_uninstall_purge_also_removes_the_config(box):
    full_install(box)

    result = box.sh("setsid -w bash /install.sh --uninstall --purge <&-")

    assert result.code == 0, result.out
    assert not box.exists("/etc/cronrack")
    assert "Removed /etc/cronrack" in result.out


def test_uninstall_sh_runs_on_its_own(box):
    """The copy shipped in the verified package works without install.sh."""
    full_install(box)
    box.ok("cp /opt/cronrack-agent/app/deploy/uninstall.sh /root/uninstall.sh")

    result = box.sh("setsid -w bash /root/uninstall.sh --purge <&-")

    assert result.code == 0, result.out
    assert not box.exists("/opt/cronrack-agent")
    assert not box.exists("/etc/cronrack")


def test_uninstall_on_a_clean_host_is_harmless(box):
    result = box.sh(
        "curl -fsS -o /root/uninstall.sh http://127.0.0.1:8000/agent/uninstall.sh"
        " && setsid -w bash /root/uninstall.sh <&-"
    )

    assert result.code == 0, result.out
    assert "Nothing to remove" in result.out


def test_uninstall_needs_root(box):
    full_install(box)
    box.ok("cp /opt/cronrack-agent/app/deploy/uninstall.sh /tmp/u.sh && chmod 0644 /tmp/u.sh")

    result = box.sh("setsid -w su -s /bin/bash nobody -c 'bash /tmp/u.sh' <&-")

    assert result.code != 0
    assert box.exists(AGENT_UNIT)


# --- pip: PyPI only, whatever the host's pip config says ---------------------------------------

VENV_PIP = "/opt/cronrack-agent/venv/bin/pip"
WEBSOCKETS = "websockets==17.2"


def test_pip_ignores_the_hosts_pip_config_and_environment(box):
    """The test image sets no-index in /etc/pip.conf and in PIP_NO_INDEX (see the Dockerfile),
    the way a Pi's /etc/pip.conf adds piwheels. install.sh's pip must ignore both."""
    assert "no-index = true" in box.read("/etc/pip.conf")
    assert box.ok("printenv PIP_NO_INDEX").strip() == "1"

    box.install()

    version = box.ok(
        "/opt/cronrack-agent/venv/bin/python -c 'import websockets; print(websockets.__version__)'"
    )
    assert version.strip() == "17.2"
    # Control: the same pip, without the isolation, does obey that config and so finds nothing.
    # Without this, a broken test image would make the install above prove nothing.
    plain = box.sh(
        f"{VENV_PIP} download -q --no-deps -d /tmp/plain"
        f" --index-url https://pypi.org/simple {WEBSOCKETS}"
    )
    assert plain.code != 0, plain.out
    isolated = box.sh(
        f"PIP_CONFIG_FILE=/dev/null {VENV_PIP} download -q --isolated --no-deps -d /tmp/isolated"
        f" --index-url https://pypi.org/simple {WEBSOCKETS}"
    )
    assert isolated.code == 0, isolated.out


# --- static checks: no remote code, shellcheck clean -------------------------------------------

SCRIPTS = [REPO / "deploy" / "install.sh", REPO / "deploy" / "uninstall.sh"]


def code_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_no_remote_script_is_ever_run(path):
    """Nothing downloaded is piped into a shell, sourced or eval'ed."""
    pattern = re.compile(
        r"\|\s*(sudo\s+)?(ba|z|da)?sh\b|\bsource\s+<\(|\.\s+<\(|\b(ba)?sh\s+<\(|\beval\b"
    )
    offending = [line for line in code_lines(path) if pattern.search(line)]
    assert offending == []


def test_uninstall_sh_downloads_nothing():
    assert not any(
        re.search(r"\b(curl|wget)\b", line) for line in code_lines(REPO / "deploy" / "uninstall.sh")
    )


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_shellcheck_clean(path):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed (CI runs it in its own job)")
    result = subprocess.run(
        ["shellcheck", str(path)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout


def test_ci_shellchecks_the_uninstaller():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    line = next(line for line in ci.splitlines() if "run: shellcheck deploy/" in line)
    assert "deploy/uninstall.sh" in line or "deploy/*.sh" in line
