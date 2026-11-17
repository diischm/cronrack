"""The shipped systemd units (deploy/*.service, *.socket) carry the full sandbox.

install.sh copies these files as they are, so what is asserted here is what runs on a host.
`systemd-analyze security cronrack-agent` on the host shows the resulting score; the numbers
are in docs/trust-the-agent.md.
"""

from pathlib import Path

import pytest

from agent import docker_api, docker_proxy

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
AGENT_UNIT = DEPLOY / "cronrack-agent.service"
PROXY_UNIT = DEPLOY / "cronrack-docker-proxy.service"
PROXY_SOCKET = DEPLOY / "cronrack-docker-proxy.socket"


def parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """{section: {key: [values in order]}}. systemd allows a key more than once."""
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        assert current is not None, f"{path.name}: setting outside a section: {line}"
        key, sep, value = line.partition("=")
        assert sep, f"{path.name}: not key=value: {line}"
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def service(path: Path) -> dict[str, list[str]]:
    return parse_unit(path)["Service"]


# Every unit we ship gets at least these (one value each, exactly as written).
COMMON_SANDBOX = {
    "NoNewPrivileges": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "CapabilityBoundingSet": "",
    "AmbientCapabilities": "",
    "ProtectKernelModules": "yes",
    "ProtectKernelLogs": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectControlGroups": "yes",
    "ProtectClock": "yes",
    "ProtectHostname": "yes",
    "ProtectProc": "invisible",
    "LockPersonality": "yes",
    "RestrictRealtime": "yes",
    "RestrictSUIDSGID": "yes",
    "RestrictNamespaces": "yes",
    "MemoryDenyWriteExecute": "yes",
    "RemoveIPC": "yes",
    "UMask": "0077",
    "SystemCallArchitectures": "native",
    "SystemCallErrorNumber": "EPERM",
}

SYSCALL_ALLOW = "@system-service"
SYSCALL_DENY = {
    "@privileged",
    "@resources",
    "@mount",
    "@swap",
    "@reboot",
    "@module",
    "@raw-io",
    "@debug",
    "@cpu-emulation",
    "@obsolete",
    "@clock",
}


@pytest.mark.parametrize("unit", [AGENT_UNIT, PROXY_UNIT], ids=lambda p: p.name)
@pytest.mark.parametrize("key", sorted(COMMON_SANDBOX))
def test_sandbox_setting_present(unit, key):
    assert service(unit).get(key) == [COMMON_SANDBOX[key]], f"{unit.name}: {key}"


@pytest.mark.parametrize("unit", [AGENT_UNIT, PROXY_UNIT], ids=lambda p: p.name)
def test_syscall_filter(unit):
    filters = service(unit)["SystemCallFilter"]
    assert filters[0] == SYSCALL_ALLOW
    denied = [f for f in filters[1:] if f.startswith("~")]
    assert len(denied) == 1
    assert set(denied[0][1:].split()) == SYSCALL_DENY


def test_agent_runs_as_its_own_user():
    svc = service(AGENT_UNIT)
    assert svc["User"] == ["cronrack"]
    assert svc["Group"] == ["cronrack"]
    assert "DynamicUser" not in svc


def test_agent_is_not_in_the_docker_group():
    """The docker group is root-equivalent. The agent reaches Docker only through the proxy."""
    svc = service(AGENT_UNIT)
    assert "SupplementaryGroups" not in svc
    # docker.sock appears only to be hidden (InaccessiblePaths), never as something to use.
    used = {k: v for k, v in svc.items() if k != "InaccessiblePaths"}
    assert "docker.sock" not in str(used)


def test_agent_cannot_see_container_engine_sockets():
    """Even a world-writable docker.sock (a common shortcut) stays out of reach."""
    (paths,) = service(AGENT_UNIT)["InaccessiblePaths"]
    hidden = set(paths.split())
    for path in ("-/run/docker.sock", "-/run/containerd", "-/run/podman", "-/run/snapd.socket"):
        assert path in hidden
    assert not any(p.lstrip("-").startswith("/run/cronrack") for p in hidden)


def test_agent_address_families():
    assert service(AGENT_UNIT)["RestrictAddressFamilies"] == ["AF_INET AF_INET6 AF_UNIX AF_NETLINK"]


def test_agent_unit_has_no_host_specific_egress_rules():
    """IPAddressAllow depends on the host's server address: install.sh writes it as a drop-in."""
    svc = service(AGENT_UNIT)
    assert "IPAddressAllow" not in svc and "IPAddressDeny" not in svc


def test_agent_starts_the_agent_and_nothing_else():
    svc = service(AGENT_UNIT)
    assert svc["ExecStart"] == [
        "/opt/cronrack-agent/venv/bin/python -m agent --config /etc/cronrack/agent.toml"
    ]
    for key in ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecReload"):
        assert key not in svc


# --- the Docker proxy ----------------------------------------------------------------------


def test_proxy_service_identity_and_network():
    svc = service(PROXY_UNIT)
    assert svc["DynamicUser"] == ["yes"]
    assert svc["SupplementaryGroups"] == ["docker"]
    # No network at all: its only input is the unix socket systemd hands it.
    assert svc["PrivateNetwork"] == ["yes"]
    assert svc["IPAddressDeny"] == ["any"]
    assert svc["RestrictAddressFamilies"] == ["AF_UNIX"]
    assert svc["ProcSubset"] == ["pid"]
    assert "ListenStream" not in svc


def test_proxy_service_runs_the_proxy_module():
    svc = service(PROXY_UNIT)
    (exec_start,) = svc["ExecStart"]
    assert exec_start.startswith("/opt/cronrack-agent/venv/bin/python ")
    assert exec_start.endswith(" -m agent.docker_proxy")
    unit = parse_unit(PROXY_UNIT)["Unit"]
    assert unit["Requires"] == ["cronrack-docker-proxy.socket"]


def test_proxy_socket_is_only_for_the_agent():
    sock = parse_unit(PROXY_SOCKET)["Socket"]
    assert sock["ListenStream"] == [docker_api.DOCKER_SOCKET]
    assert sock["SocketUser"] == ["root"]
    assert sock["SocketGroup"] == ["cronrack"]
    assert sock["SocketMode"] == ["0660"]
    assert sock.get("Accept", ["no"]) == ["no"]
    for key in ("ListenDatagram", "ListenSequentialPacket", "ListenFIFO", "ListenNetlink"):
        assert key not in sock
    assert not any(k.startswith("Listen") and k != "ListenStream" for k in sock)


def test_proxy_talks_to_the_real_docker_socket_only():
    assert docker_proxy.UPSTREAM_SOCKET == "/var/run/docker.sock"
    assert docker_api.DOCKER_SOCKET != docker_proxy.UPSTREAM_SOCKET


def test_every_shipped_unit_is_in_the_package():
    """The package allowlist (server/agents/package.py) carries every unit install.sh uses."""
    from agents import package

    files = set(package.package_files())
    for path in (AGENT_UNIT, PROXY_UNIT, PROXY_SOCKET, DEPLOY / "uninstall.sh"):
        assert f"deploy/{path.name}" in files
