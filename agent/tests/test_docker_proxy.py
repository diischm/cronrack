"""The read-only Docker API proxy (agent/docker_proxy.py).

The proxy runs in the docker group, so it is the one piece that could do real damage if it
let the wrong request through. These tests pin down three things:

1. The allowlist: only four GET routes pass; every other method, path, query, encoding
   or trick gets 403, and the real Docker socket is never even contacted.
2. The filters: what comes back holds only the fields the agent uses. Env, Config,
   Mounts, Labels and HostConfig never leave the proxy (tested with a secret env value).
3. The wiring: the agent's own client (agent/docker_api.py) works through it.
"""

import json
import socket
import threading
from pathlib import Path

import pytest

from agent import docker_api, docker_proxy
from agent.docker_proxy import (
    MAX_HEAD_BYTES,
    filter_list,
    filter_stats,
    filter_version,
    match_route,
)

from .test_docker_api import FakeDocker, stats

CID = "3f9c2a1b7d4e" + "0" * 52
SECRET = "hunter2-db-password-never-leaves"


def inspect_body(state: str = "restarting", restart_count: int = 7) -> dict:
    """A realistic GET /containers/<id>/json answer, including the parts that must not leak."""
    return {
        "Id": CID,
        "Name": "/worker",
        "Image": "sha256:" + "a" * 64,
        "RestartCount": restart_count,
        "State": {
            "Status": state,
            "Running": state == "running",
            "Paused": False,
            "Restarting": state == "restarting",
            "OOMKilled": False,
            "Dead": False,
            "Pid": 4242,
            "ExitCode": 1,
            "Error": "",
            "StartedAt": "2026-11-17T10:00:00Z",
            "FinishedAt": "2026-11-17T10:00:05Z",
            "Health": {
                "Status": "unhealthy",
                "FailingStreak": 3,
                "Log": [{"Output": f"curl: password={SECRET}"}],
            },
        },
        "Config": {
            "Env": [f"DB_PASSWORD={SECRET}", "PATH=/usr/bin"],
            "Cmd": ["python", "worker.py", f"--token={SECRET}"],
            "Labels": {"traefik.http.middlewares.auth.basicauth.users": f"admin:{SECRET}"},
        },
        "HostConfig": {"Binds": ["/srv/secrets:/run/secrets:ro"], "Privileged": True},
        "Mounts": [{"Source": "/srv/secrets", "Destination": "/run/secrets"}],
        "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "192.0.2.10"}}},
        "LogPath": "/var/lib/docker/containers/x/x-json.log",
    }


def list_body() -> list:
    return [
        {
            "Id": CID,
            "Names": ["/worker"],
            "Image": "example/worker:latest",
            "Command": f"python worker.py --token={SECRET}",
            "Labels": {"secret": SECRET},
            "State": "restarting",
            "Status": "Restarting (1) 12 seconds ago",
            "Mounts": [{"Source": "/srv/secrets"}],
            "Ports": [{"PublicPort": 8080}],
        }
    ]


# --- 1. the allowlist ----------------------------------------------------------------------

ALLOWED = [
    "/version",
    "/v1.45/containers/json",
    "/v1.45/containers/json?all=1",
    "/v1.24/containers/json?all=1",
    f"/v1.45/containers/{CID}/stats?stream=false",
    f"/v1.45/containers/{CID[:12]}/stats?stream=false",
    f"/v1.45/containers/{CID}/stats?stream=false&one-shot=true",
]


def test_the_allowlist_is_exactly_three_routes():
    assert [route.name for route in docker_proxy.ROUTES] == ["version", "list", "stats"]


@pytest.mark.parametrize("target", ALLOWED)
def test_allowed_routes(target):
    assert match_route("GET", target) is not None


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"])
@pytest.mark.parametrize("target", ALLOWED)
def test_every_other_method_is_refused(method, target):
    assert match_route(method, target) is None


FORBIDDEN_TARGETS = [
    # Container inspect: not used by the agent, and where environment variables live.
    f"/v1.45/containers/{CID}/json",
    f"/v1.45/containers/{CID[:12]}/json",
    f"/v1.24/containers/{CID}/json",
    # Writes and remote control, even as GET.
    "/v1.45/containers/create",
    f"/v1.45/containers/{CID}/start",
    f"/v1.45/containers/{CID}/stop",
    f"/v1.45/containers/{CID}/kill",
    f"/v1.45/containers/{CID}/exec",
    "/v1.45/exec/abc/start",
    f"/v1.45/containers/{CID}/attach",
    f"/v1.45/containers/{CID}/attach/ws",
    # Reads that leak data: logs, files, the whole filesystem, processes.
    f"/v1.45/containers/{CID}/logs?stdout=1",
    f"/v1.45/containers/{CID}/export",
    f"/v1.45/containers/{CID}/archive?path=/etc/shadow",
    f"/v1.45/containers/{CID}/top",
    f"/v1.45/containers/{CID}/changes",
    # Other resources.
    "/v1.45/images/json",
    "/v1.45/images/create?fromImage=evil",
    "/v1.45/build",
    "/v1.45/volumes",
    "/v1.45/secrets",
    "/v1.45/configs",
    "/v1.45/networks",
    "/v1.45/info",
    "/v1.45/events",
    "/v1.45/system/df",
    "/v1.45/swarm",
    "/v1.45/plugins",
    "/v1.45/session",
    "/v1.45/grpc",
    "/_ping",
    "/v1.45/version",
    # Unversioned or oddly versioned container routes.
    "/containers/json",
    f"/containers/{CID}/json",
    "/v2.0/containers/json",
    "/v1/containers/json",
    "/v1.4500/containers/json",
    # Queries other than the exact ones the agent sends.
    "/v1.45/containers/json?all=true",
    "/v1.45/containers/json?all=1&filters=%7B%7D",
    "/v1.45/containers/json?size=1",
    "/v1.45/containers/json?",
    f"/v1.45/containers/{CID}/stats",
    f"/v1.45/containers/{CID}/stats?stream=true",
    f"/v1.45/containers/{CID}/stats?stream=false&stream=true",
    f"/v1.45/containers/{CID}/json?size=1",
    "/version?x=1",
    # Names instead of ids, short or uppercase ids.
    "/v1.45/containers/worker/json",
    "/v1.45/containers/abc/json",
    f"/v1.45/containers/{CID.upper()}/json",
    # Path tricks: traversal, encoding, doubled slashes, trailing slash, absolute form.
    f"/v1.45/containers/{CID}/../../images/json",
    f"/v1.45/containers/{CID}/json/../exec",
    "/v1.45/containers/%2e%2e/images/json",
    f"/v1.45/containers/{CID}%2Fjson",
    f"/v1.45/containers/{CID}/json%00",
    "/v1.45/containers/json%3Fall=1",
    "//v1.45/containers/json",
    "/v1.45//containers/json",
    "/v1.45/containers/json/",
    "/version/",
    "/./version",
    "/VERSION",
    "http://docker/version",
    "http://localhost/v1.45/containers/json",
    "*",
    "",
    "version",
    "/version#x",
    "/version\x00",
    "/version\r\nX-Injected: 1",
    "/v1.45/containers/json\t",
]


@pytest.mark.parametrize("target", FORBIDDEN_TARGETS)
def test_forbidden_targets(target):
    assert match_route("GET", target) is None


def test_method_must_be_exactly_get():
    assert match_route("get", "/version") is None
    assert match_route("GET ", "/version") is None


# --- 2. the filters ------------------------------------------------------------------------


def test_list_keeps_only_id_names_state_status():
    assert filter_list(list_body()) == [
        {
            "Id": CID,
            "Names": ["/worker"],
            "State": "restarting",
            "Status": "Restarting (1) 12 seconds ago",
        }
    ]
    assert SECRET not in json.dumps(filter_list(list_body()))
    assert filter_list({"not": "a list"}) == []
    assert filter_list(["junk", {"Id": 5, "Names": "x"}]) == [{}]


def test_stats_keeps_only_the_counters_the_agent_uses():
    body = stats()
    body["name"] = f"/{SECRET}"
    body["id"] = CID
    body["pids_stats"] = {"current": 4}
    body["blkio_stats"] = {"io_service_bytes_recursive": []}
    body["memory_stats"]["stats"]["anon"] = 5
    body["networks"]["eth0"]["rx_errors"] = 0
    filtered = filter_stats(body)
    assert filtered == {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 1_000_000_000},
            "system_cpu_usage": 100_000_000_000,
            "online_cpus": 4,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
        "memory_stats": {
            "usage": 600_000_000,
            "limit": 8 * 1024**3,
            "stats": {"inactive_file": 100_000_000},
        },
        "networks": {"net0": {"rx_bytes": 1000, "tx_bytes": 500}},
    }
    assert SECRET not in json.dumps(filtered)
    # What the agent computes from the filtered answer is what it computed before.
    assert docker_api.parse_stats(filtered) == docker_api.parse_stats(stats())


def test_stats_network_names_are_bounded():
    body = stats()
    body["networks"] = {f"net{i}": {"rx_bytes": 1, "tx_bytes": 1} for i in range(100)}
    body["networks"]["bad\nname"] = {"rx_bytes": 1, "tx_bytes": 1}
    filtered = filter_stats(body)
    assert len(filtered["networks"]) == docker_proxy.MAX_NETWORKS
    # Network names never leave: the agent only sums the counters.
    assert set(filtered["networks"]) == {f"net{i}" for i in range(docker_proxy.MAX_NETWORKS)}


def test_version_keeps_only_api_version():
    assert filter_version({"ApiVersion": "1.45", "Version": "27.3.1", "KernelVersion": "x"}) == {
        "ApiVersion": "1.45"
    }
    assert filter_version({"ApiVersion": 5}) == {}
    assert filter_version([]) == {}


# --- 3. the proxy end to end ---------------------------------------------------------------


class Proxy:
    """The proxy serving on a unix socket in a thread, in front of a fake Docker."""

    def __init__(self, path: str, upstream: str):
        self.path = path
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(8)
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=docker_proxy.serve,
            args=(self.listener, upstream),
            kwargs={"stop": self.stop},
            daemon=True,
        )
        self.thread.start()

    def close(self):
        self.stop.set()
        # Wake the accept() so the loop sees the stop flag.
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(self.path)
        except OSError:
            pass
        self.thread.join(timeout=5)
        self.listener.close()


@pytest.fixture
def upstream(tmp_path):
    path = str(tmp_path / "docker.sock")
    fake = FakeDocker(path)
    fake.path = path
    fake.routes["/version"] = (200, {"ApiVersion": "1.45", "Version": "27.3.1"})
    fake.routes["/v1.45/containers/json"] = (200, list_body())
    fake.routes[f"/v1.45/containers/{CID}/json"] = (200, inspect_body())
    fake.routes[f"/v1.45/containers/{CID}/stats"] = (200, stats())
    yield fake
    fake.close()


@pytest.fixture
def proxy(tmp_path, upstream):
    p = Proxy(str(tmp_path / "proxy.sock"), upstream.path)
    yield p
    p.close()


def raw(path: str, request: bytes) -> bytes:
    """Send raw bytes to the proxy, return everything it answers."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(path)
        s.sendall(request)
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def get(path: str, target: str, extra: bytes = b"") -> bytes:
    return raw(path, f"GET {target} HTTP/1.1\r\nHost: docker\r\n".encode() + extra + b"\r\n")


def status_of(answer: bytes) -> int:
    return int(answer.split(b" ", 2)[1])


def body_of(answer: bytes):
    return json.loads(answer.split(b"\r\n\r\n", 1)[1])


def test_inspect_is_refused_and_docker_is_never_asked(proxy, upstream):
    """GET /containers/<id>/json is where Env lives: 403, and Docker never sees the request,
    even though the fake Docker would answer it (with a planted secret)."""
    answer = get(proxy.path, f"/v1.45/containers/{CID}/json")
    assert status_of(answer) == 403
    assert SECRET.encode() not in answer
    assert upstream.requests == []


def test_list_through_the_proxy_never_shows_labels_or_command(proxy):
    answer = get(proxy.path, "/v1.45/containers/json?all=1")
    assert status_of(answer) == 200
    assert SECRET.encode() not in answer
    assert body_of(answer) == filter_list(list_body())


@pytest.mark.parametrize(
    "target",
    [
        "/v1.45/containers/create",
        f"/v1.45/containers/{CID}/exec",
        f"/v1.45/containers/{CID}/archive?path=/etc/shadow",
        f"/v1.45/containers/{CID}/logs?stdout=1",
        f"/v1.45/containers/{CID}/export",
        f"/v1.45/containers/{CID}/attach/ws",
        f"/v1.45/containers/{CID}/../../images/json",
        "/v1.45/containers/%2e%2e/images/json",
        "/v1.45/images/json",
        "/v1.45/secrets",
        "/_ping",
    ],
)
def test_forbidden_request_gets_403_and_docker_is_never_asked(proxy, upstream, target):
    answer = get(proxy.path, target)
    assert status_of(answer) == 403
    assert upstream.requests == []


def test_post_gets_403_and_docker_is_never_asked(proxy, upstream):
    answer = raw(
        proxy.path,
        b"POST /v1.45/containers/create HTTP/1.1\r\nHost: docker\r\n"
        b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
    )
    assert status_of(answer) == 403
    assert upstream.requests == []


@pytest.mark.parametrize(
    "header",
    [
        b"Content-Length: 5\r\n",
        b"Transfer-Encoding: chunked\r\n",
        b"Upgrade: tcp\r\nConnection: Upgrade\r\n",
        b"Expect: 100-continue\r\n",
    ],
)
def test_allowed_path_with_a_body_or_upgrade_gets_403(proxy, upstream, header):
    answer = get(proxy.path, "/version", header)
    assert status_of(answer) == 403
    assert upstream.requests == []


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"garbage\r\n\r\n",
        b"GET /version\r\n\r\n",
        b"GET /version HTTP/2\r\n\r\n",
        b"GET  /version HTTP/1.1\r\n\r\n",
        b"GET /version HTTP/1.1\nHost: docker\n\n",
        b"\xff\xfe /version HTTP/1.1\r\n\r\n",
    ],
)
def test_malformed_requests_get_400(proxy, upstream, request_bytes):
    assert status_of(raw(proxy.path, request_bytes)) == 400
    assert upstream.requests == []


def test_oversized_head_is_refused(proxy, upstream):
    big = b"X-Pad: " + b"a" * (MAX_HEAD_BYTES + 10) + b"\r\n"
    assert status_of(get(proxy.path, "/version", big)) == 431
    assert upstream.requests == []


def test_head_just_over_the_limit_in_one_write_is_refused(proxy, upstream):
    """The limit holds even when the whole head, terminator included, arrives at once."""
    pad = b"X-Pad: " + b"a" * (MAX_HEAD_BYTES - 40) + b"\r\n"
    assert status_of(get(proxy.path, "/version", pad)) == 431
    assert upstream.requests == []


def test_long_runs_of_spaces_are_cheap(proxy, upstream):
    import time

    start = time.monotonic()
    get(proxy.path, "/version", b"X-Pad: " + b" " * 4000 + b"x\r\n")
    assert time.monotonic() - start < 1.0


def test_bare_cr_is_refused(proxy, upstream):
    assert status_of(get(proxy.path, "/version", b"X-A: b\rX-B: c\r\n")) == 400
    assert upstream.requests == []


def test_a_pipelined_second_request_is_never_forwarded(proxy, upstream):
    answer = raw(
        proxy.path,
        b"GET /version HTTP/1.1\r\nHost: d\r\n\r\n"
        b"POST /v1.45/containers/create HTTP/1.1\r\nHost: d\r\n\r\n",
    )
    assert status_of(answer) == 200
    assert upstream.requests == [("GET", "/version")]


def test_client_headers_are_not_forwarded(proxy, upstream):
    get(proxy.path, "/version", b"X-Registry-Auth: c2VjcmV0\r\nAuthorization: Bearer x\r\n")
    assert upstream.requests == [("GET", "/version")]
    assert upstream.headers[-1].get("X-Registry-Auth") is None
    assert upstream.headers[-1].get("Authorization") is None


def test_docker_errors_pass_through_as_status_only(proxy, upstream):
    upstream.routes[f"/v1.45/containers/{CID}/stats"] = (404, {"message": f"no such {SECRET}"})
    answer = get(proxy.path, f"/v1.45/containers/{CID}/stats?stream=false")
    assert status_of(answer) == 404
    assert SECRET.encode() not in answer


def test_docker_down_gives_502(tmp_path):
    p = Proxy(str(tmp_path / "proxy.sock"), str(tmp_path / "no-docker.sock"))
    try:
        assert status_of(get(p.path, "/version")) == 502
        # ...which the agent tells apart from "the proxy itself is down".
        with pytest.raises(docker_api.DockerEngineUnavailable):
            docker_api.collect_container_health(socket_path=p.path)
    finally:
        p.close()


def test_oversized_docker_answer_gives_502(proxy, upstream):
    upstream.routes["/version"] = (200, b'{"ApiVersion": "1.45", "x": "' + b"a" * 300_000 + b'"}')
    assert status_of(get(proxy.path, "/version")) == 502


def test_agent_client_works_through_the_proxy(proxy, upstream):
    """agent/docker_api.py, pointed at the proxy, gets the same numbers as before."""
    upstream.routes["/v1.45/containers/json"] = (
        200,
        [{"Id": CID, "Names": ["/worker"], "State": "running", "Status": "Up 1 hour"}],
    )
    sampler = docker_api.ContainerStats(socket_path=proxy.path)
    containers, truncated = sampler.collect(host_total=16 * 1024**3)
    assert truncated is False
    assert [c["name"] for c in containers] == ["worker"]
    assert containers[0]["mem"] == 500_000_000


def test_container_health_works_through_the_proxy(proxy, upstream):
    """Restart-loop detection (state "restarting") survives the proxy and its filter."""
    result = docker_api.collect_container_health(socket_path=proxy.path)
    assert result == [
        {
            "name": "worker",
            "state": "restarting",
            "status": "Restarting (1) 12 seconds ago",
            "health": None,
            "exit_code": None,
        }
    ]
    assert ("GET", "/v1.45/containers/json?all=1") in upstream.requests


def test_one_slow_client_does_not_hold_up_the_others(proxy):
    """An idle client takes one slot; the next request is answered at once, not after
    CLIENT_TIMEOUT (5 s)."""
    import time

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as idle:
        idle.connect(proxy.path)
        idle.sendall(b"GET /vers")  # never finishes the request
        start = time.monotonic()
        assert status_of(get(proxy.path, "/version")) == 200
        assert time.monotonic() - start < 1.0


def test_all_slots_busy_gives_503_at_once(tmp_path, upstream, monkeypatch):
    import time

    monkeypatch.setattr(docker_proxy, "MAX_WORKERS", 1)
    p = Proxy(str(tmp_path / "proxy.sock"), upstream.path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as idle:
            idle.connect(p.path)
            idle.sendall(b"GET /vers")
            time.sleep(0.2)  # let the proxy hand the idle one its slot
            start = time.monotonic()
            assert status_of(get(p.path, "/version")) == 503
            assert time.monotonic() - start < 1.0
    finally:
        p.close()


def test_a_client_that_hangs_up_is_not_an_error(proxy, caplog):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(proxy.path)
        s.sendall(b"GET /vers")
    assert status_of(get(proxy.path, "/version")) == 200
    assert "Traceback" not in caplog.text


# --- 4. systemd socket activation: the proxy never opens a socket of its own ----------------


def test_listener_only_from_systemd(monkeypatch):
    with pytest.raises(docker_proxy.NotSocketActivated):
        docker_proxy.listener_from_systemd({}, pid=1234)
    with pytest.raises(docker_proxy.NotSocketActivated):
        docker_proxy.listener_from_systemd({"LISTEN_FDS": "1", "LISTEN_PID": "999"}, pid=1234)
    with pytest.raises(docker_proxy.NotSocketActivated):
        docker_proxy.listener_from_systemd({"LISTEN_FDS": "2", "LISTEN_PID": "1234"}, pid=1234)


def test_main_refuses_to_start_without_systemd(monkeypatch, capsys):
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    assert docker_proxy.main([]) == 2
    assert "socket" in capsys.readouterr().err


def test_proxy_module_has_no_bind_or_listen():
    """The proxy takes its one socket from systemd; it never creates a listening one."""
    source = Path(docker_proxy.__file__).read_text(encoding="utf-8")
    assert ".bind(" not in source
    assert ".listen(" not in source
    assert "AF_INET" not in source
