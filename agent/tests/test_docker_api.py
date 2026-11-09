"""Container metrics (agent/docker_api.py) against a fake Docker Engine API on a unix socket."""

import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

from agent import docker_api
from agent.docker_api import (
    ContainerStats,
    cpu_share,
    memory_limit,
    parse_api_version,
    parse_container_list,
    parse_stats,
)

GIB = 1024**3
HOST_TOTAL = 8 * GIB


def container(cid: str, name: str) -> dict:
    return {"Id": cid, "Names": [f"/{name}"], "State": "running"}


def stats(
    cpu=1_000_000_000,
    system=100_000_000_000,
    usage=600_000_000,
    inactive=100_000_000,
    limit=HOST_TOTAL,
    rx=1000,
    tx=500,
    v2=True,
    pre_cpu=0,
    pre_system=0,
):
    key = "inactive_file" if v2 else "total_inactive_file"
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": cpu},
            "system_cpu_usage": system,
            "online_cpus": 4,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": pre_cpu}, "system_cpu_usage": pre_system},
        "memory_stats": {"usage": usage, "limit": limit, "stats": {key: inactive}},
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}},
    }


class FakeDocker:
    """Routes: path (without the query) -> (status, JSON body) or a callable returning that."""

    def __init__(self, socket_path: str):
        self.routes: dict = {}
        self.requests: list[tuple[str, str]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # http.server's naming
                fake.requests.append(("GET", self.path))
                route = fake.routes.get(self.path.split("?")[0])
                if callable(route):
                    route = route(self.path)
                status, body = route if route else (404, {"message": "no such route"})
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def address_string(self):
                return "unix"

            def log_message(self, *args):
                pass

        self.server = socketserver.ThreadingUnixStreamServer(socket_path, Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def docker(tmp_path):
    path = str(tmp_path / "docker.sock")
    fake = FakeDocker(path)
    fake.path = path
    fake.routes["/version"] = (200, {"ApiVersion": "1.45", "Version": "27.3.1"})
    yield fake
    fake.close()


class Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


# --- parsers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"ApiVersion": "1.45"}, ("1.45", True)),
        ({"ApiVersion": "1.41"}, ("1.41", True)),
        ({"ApiVersion": "1.40"}, ("1.40", False)),
        ({"ApiVersion": "1.45; rm -rf /"}, ("1.24", False)),
        ({}, ("1.24", False)),
        ([], ("1.24", False)),
    ],
)
def test_api_version(data, expected):
    assert parse_api_version(data) == expected


def test_container_list_sorted_by_name_and_cleaned():
    data = [
        container("b" * 64, "web"),
        container("a" * 64, "db"),
        {"Id": "not-an-id", "Names": ["/bad"]},
        {"Id": "c" * 64, "Names": ["/evil\nname"]},
        {"Id": "f" * 64, "Names": ["/--all"]},
        {"Id": "d" * 64, "Names": []},
        {"Id": "e" * 64, "Names": [f"/{'x' * 300}"]},
        "junk",
    ]
    assert parse_container_list(data) == [("a" * 64, "db"), ("b" * 64, "web")]
    assert parse_container_list({"not": "a list"}) == []


def test_stats_cgroup_v2_and_v1_memory():
    assert parse_stats(stats(v2=True)).mem == 500_000_000
    assert parse_stats(stats(v2=False)).mem == 500_000_000


def test_stats_without_networks_has_no_traffic():
    data = stats()
    del data["networks"]  # network_mode: host
    sample = parse_stats(data)
    assert sample.rx is None and sample.tx is None


def test_stats_with_wrong_types_give_none_fields():
    sample = parse_stats({"cpu_stats": {"cpu_usage": {"total_usage": "10"}}, "memory_stats": []})
    assert sample.cpu_total is None and sample.mem is None
    assert parse_stats("junk") is None


def test_cpu_share_is_of_the_whole_host():
    # 1 s of CPU time in 4 s of host time (1 s on each of 4 cores) = 25 % of the host.
    assert cpu_share(2 * 10**9, 1 * 10**9, 104 * 10**9, 100 * 10**9) == 25.0
    assert cpu_share(1, None, 2, 1) is None
    assert cpu_share(1, 2, 2, 1) is None  # container counter went backwards
    assert cpu_share(1, 0, 5, 5) is None  # no host time passed


def test_memory_limit_only_when_it_is_a_real_limit():
    assert memory_limit(2 * GIB, HOST_TOTAL) == 2 * GIB
    assert memory_limit(HOST_TOTAL, HOST_TOTAL) is None  # no limit: Docker reports the host
    assert memory_limit(9223372036854771712, HOST_TOTAL) is None  # cgroup v1 "unlimited"
    assert memory_limit(2 * GIB, None) is None
    assert memory_limit(0, HOST_TOTAL) is None


# --- against the fake engine ---------------------------------------------------------------


def test_two_reports_give_cpu_and_network_rates(docker):
    cid = "a" * 64
    docker.routes["/v1.45/containers/json"] = (200, [container(cid, "jellyfin")])
    docker.routes[f"/v1.45/containers/{cid}/stats"] = (200, stats(limit=2 * GIB))
    clock = Clock()
    collector = ContainerStats(docker.path, clock=clock)

    first, truncated = collector.collect(HOST_TOTAL)
    assert truncated is False
    assert first == [
        {
            "name": "jellyfin",
            "cpu_pct": None,  # one-shot: no previous sample yet
            "mem": 500_000_000,
            "mem_limit": 2 * GIB,
            "rx_bps": None,
            "tx_bps": None,
        }
    ]

    docker.routes[f"/v1.45/containers/{cid}/stats"] = (
        200,
        stats(cpu=2 * 10**9, system=104 * 10**9, rx=61_000, tx=30_500, limit=2 * GIB),
    )
    clock.now += 60
    second, _ = collector.collect(HOST_TOTAL)
    assert second[0]["cpu_pct"] == 25.0
    assert second[0]["rx_bps"] == 1000
    assert second[0]["tx_bps"] == 500


def test_one_shot_is_asked_for_and_only_gets_are_sent(docker):
    cid = "a" * 64
    docker.routes["/v1.45/containers/json"] = (200, [container(cid, "web")])
    docker.routes[f"/v1.45/containers/{cid}/stats"] = (200, stats())
    ContainerStats(docker.path).collect(HOST_TOTAL)
    assert [m for m, _ in docker.requests] == ["GET", "GET", "GET"]
    assert docker.requests[-1][1] == f"/v1.45/containers/{cid}/stats?stream=false&one-shot=true"


def test_old_engine_uses_its_own_previous_sample(docker):
    docker.routes["/version"] = (200, {"ApiVersion": "1.40"})
    cid = "a" * 64
    docker.routes["/v1.40/containers/json"] = (200, [container(cid, "web")])
    docker.routes[f"/v1.40/containers/{cid}/stats"] = (
        200,
        stats(cpu=2 * 10**9, system=104 * 10**9, pre_cpu=10**9, pre_system=100 * 10**9),
    )
    result, _ = ContainerStats(docker.path).collect(HOST_TOTAL)
    assert result[0]["cpu_pct"] == 25.0
    assert docker.requests[-1][1].endswith("?stream=false")


def test_no_limit_is_reported_as_null(docker):
    cid = "a" * 64
    docker.routes["/v1.45/containers/json"] = (200, [container(cid, "web")])
    docker.routes[f"/v1.45/containers/{cid}/stats"] = (200, stats(limit=HOST_TOTAL))
    result, _ = ContainerStats(docker.path).collect(HOST_TOTAL)
    assert result[0]["mem_limit"] is None


def test_container_count_is_capped(docker, monkeypatch):
    monkeypatch.setattr(docker_api, "MAX_CONTAINERS", 3)
    listed = [container(f"{i:064x}", f"c{i:02d}") for i in range(5)]
    docker.routes["/v1.45/containers/json"] = (200, listed)
    for item in listed:
        docker.routes[f"/v1.45/containers/{item['Id']}/stats"] = (200, stats())
    result, truncated = ContainerStats(docker.path).collect(HOST_TOTAL)
    assert [c["name"] for c in result] == ["c00", "c01", "c02"]
    assert truncated is True
    assert len(docker.requests) == 2 + 3  # never asked about the other two


def test_time_budget_cuts_the_list_short(docker):
    listed = [container(f"{i:064x}", f"c{i:02d}") for i in range(4)]
    docker.routes["/v1.45/containers/json"] = (200, listed)
    clock = Clock()

    def slow(path):
        clock.now += 5  # each stats call "takes" 5 s; the budget is 8 s
        return 200, stats()

    for item in listed:
        docker.routes[f"/v1.45/containers/{item['Id']}/stats"] = slow
    result, truncated = ContainerStats(docker.path, clock=clock).collect(HOST_TOTAL)
    assert len(result) == 2
    assert truncated is True


def test_a_hanging_engine_times_out(docker, monkeypatch):
    monkeypatch.setattr(docker_api, "CALL_TIMEOUT", 0.2)

    def hang(path):
        time.sleep(1)
        return 200, {"ApiVersion": "1.45"}

    docker.routes["/version"] = hang
    started = time.monotonic()
    assert ContainerStats(docker.path).collect(HOST_TOTAL) == (None, False)
    assert time.monotonic() - started < 0.9


def test_a_container_that_vanished_is_skipped(docker):
    a, b = "a" * 64, "b" * 64
    docker.routes["/v1.45/containers/json"] = (200, [container(a, "gone"), container(b, "here")])
    docker.routes[f"/v1.45/containers/{b}/stats"] = (200, stats())
    result, truncated = ContainerStats(docker.path).collect(HOST_TOTAL)
    assert [c["name"] for c in result] == ["here"]
    assert truncated is False


def test_oversized_and_broken_responses(docker, monkeypatch):
    monkeypatch.setattr(docker_api, "MAX_LIST_BYTES", 100)
    docker.routes["/v1.45/containers/json"] = (200, [container("a" * 64, "x" * 50)] * 5)
    assert ContainerStats(docker.path).collect(HOST_TOTAL) == (None, False)
    docker.routes["/version"] = (200, b"not json")
    # A bad /version falls back to the oldest API instead of failing.
    docker.routes["/v1.24/containers/json"] = (200, [])
    monkeypatch.setattr(docker_api, "MAX_LIST_BYTES", 10_000)
    assert ContainerStats(docker.path).collect(HOST_TOTAL) == (None, False)


def test_http_error_from_the_engine(docker):
    docker.routes["/v1.45/containers/json"] = (500, {"message": "boom"})
    assert ContainerStats(docker.path).collect(HOST_TOTAL) == (None, False)


def test_no_socket_means_skipped(tmp_path):
    assert ContainerStats(str(tmp_path / "missing.sock")).collect(HOST_TOTAL) == (None, False)


def test_permission_denied_means_skipped(tmp_path):
    path = tmp_path / "docker.sock"
    path.write_text("")
    path.chmod(0)
    assert ContainerStats(str(path)).collect(HOST_TOTAL) == (None, False)


def test_previous_samples_never_outgrow_the_running_containers(docker):
    docker.routes["/v1.45/containers/json"] = (200, [container("a" * 64, "one")])
    docker.routes[f"/v1.45/containers/{'a' * 64}/stats"] = (200, stats())
    collector = ContainerStats(docker.path)
    collector.collect(HOST_TOTAL)
    docker.routes["/v1.45/containers/json"] = (200, [])
    collector.collect(HOST_TOTAL)
    assert collector._previous == {}
