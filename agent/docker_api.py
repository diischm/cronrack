"""Read-only Docker API client: container health and container metrics.

The agent never talks to Docker itself. It talks to the read-only proxy
(agent/docker_proxy.py) on /run/cronrack/docker.sock, a path fixed here (never from the
server or the config). The proxy runs in the docker group; the agent doesn't. It allows
only these GET requests, and strips each answer to the fields used below:

    GET /version                                   which API version the engine speaks
    GET /v<api>/containers/json?all=1              every container: name, state, status
    GET /v<api>/containers/json                    running containers (id + name)
    GET /v<api>/containers/<id>/stats?stream=false&one-shot=true

Docker access is opt-in ([docker] enabled = true, written by deploy/install.sh when it sets
up the proxy). Without it the agent never calls this module and sends `"containers": null`.
With it but no proxy answering, health collection raises DockerProxyUnavailable, which
reaches the server as an error entry (rule agent.docker_proxy_unavailable).

Hard limits keep a busy host from slowing the agent down: at most MAX_CONTAINERS
containers (by name), CALL_TIMEOUT seconds per socket operation, TOTAL_BUDGET seconds for
all of them, and a size cap on every response. Containers past a limit are left out and
`containers_truncated` says so.
"""

import http.client
import json
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The read-only proxy (deploy/cronrack-docker-proxy.socket), never /var/run/docker.sock.
DOCKER_SOCKET = "/run/cronrack/docker.sock"
MAX_CONTAINERS = 50
CALL_TIMEOUT = 2.0
TOTAL_BUDGET = 8.0
MAX_LIST_BYTES = 4 * 1024 * 1024
MAX_STATS_BYTES = 256 * 1024
MAX_NAME = 190
# one-shot=true (API 1.41, Docker 20.10) answers at once. Older engines wait for a second
# sample (about 1-2 s per container), so the budget cuts the list short there.
ONE_SHOT_API = (1, 41)
FALLBACK_API = "1.24"  # what Docker 1.12 speaks; only used if /version doesn't say

# "Exited (137) 3 hours ago" -> 137
_EXIT_CODE = re.compile(r"Exited \((-?\d+)\)", re.ASCII)
# "Up 2 hours (healthy)", "Up 5 seconds (health: starting)" -> healthcheck state.
_HEALTH = re.compile(r"\((healthy|unhealthy|health: starting)\)", re.ASCII)
_HEALTH_VALUES = {"healthy": "healthy", "unhealthy": "unhealthy", "health: starting": "starting"}
MAX_STATE = 32
MAX_STATUS = 255

_API_VERSION = re.compile(r"(\d{1,2})\.(\d{1,3})", re.ASCII)
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}", re.ASCII)
# Docker's own rule for names. Also keeps a name from looking like a --flag in a fix command.
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", re.ASCII)
# cgroup v1 reports "no limit" as a huge number close to 2**63.
UNLIMITED = 2**62


class DockerError(Exception):
    """A call failed (timeout, HTTP error, bad JSON). The message is only the error kind."""


class DockerProxyUnavailable(DockerError):
    """Nothing answers on the proxy socket (missing, refused, reset, or no permission)."""


class DockerEngineUnavailable(DockerError):
    """The proxy answers, but Docker behind it doesn't (the proxy says 502)."""


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def get_json(socket_path: str, path: str, timeout: float, limit: int) -> Any:
    """GET `path` on the Docker socket and return the parsed JSON body."""
    conn = UnixConnection(socket_path, timeout)
    try:
        conn.request("GET", path, headers={"Host": "docker", "Accept": "application/json"})
        response = conn.getresponse()
        if response.status == 502:
            raise DockerEngineUnavailable("HTTP 502")
        if response.status != 200:
            raise DockerError(f"HTTP {response.status}")
        body = response.read(limit + 1)
        if len(body) > limit:
            raise DockerError("response too large")
        return json.loads(body)
    except (
        FileNotFoundError,
        PermissionError,
        ConnectionRefusedError,
        ConnectionResetError,
        BrokenPipeError,
    ) as exc:
        raise DockerProxyUnavailable(type(exc).__name__) from None
    except (OSError, http.client.HTTPException, ValueError, RecursionError) as exc:
        raise DockerError(type(exc).__name__) from None
    finally:
        conn.close()


def _uint(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_api_version(data: Any) -> tuple[str, bool]:
    """("1.45", one-shot supported?) from GET /version."""
    if isinstance(data, dict):
        raw = data.get("ApiVersion")
        if isinstance(raw, str):
            match = _API_VERSION.fullmatch(raw)
            if match:
                version = (int(match[1]), int(match[2]))
                return f"{version[0]}.{version[1]}", version >= ONE_SHOT_API
    return FALLBACK_API, False


def parse_container_list(data: Any) -> list[tuple[str, str]]:
    """(id, name) of each running container, sorted by name. Odd entries are skipped."""
    if not isinstance(data, list):
        return []
    found: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        cid, names = item.get("Id"), item.get("Names")
        if not isinstance(cid, str) or not _CONTAINER_ID.fullmatch(cid):
            continue
        if not isinstance(names, list) or not names or not isinstance(names[0], str):
            continue
        name = names[0].lstrip("/")
        if len(name) > MAX_NAME or not _NAME.fullmatch(name):
            continue
        found.setdefault(name, cid)
    return sorted(((cid, name) for name, cid in found.items()), key=lambda pair: pair[1])


def _health(status: str) -> str | None:
    match = _HEALTH.search(status)
    return _HEALTH_VALUES[match[1]] if match else None


def _exit_code(status: str) -> int | None:
    match = _EXIT_CODE.match(status)
    return int(match[1]) if match else None


def parse_container_health(data: Any) -> list[dict[str, Any]]:
    """One dict per container from GET /containers/json?all=1: name, state, status,
    health, exit_code. The same facts `docker ps -a` shows (and the agent sent before 0.5.0).

    Facts only: whether a container is a problem is decided on the server by the rule
    engine, so the user can override it. State "restarting" is what the server's
    docker.container_restarting rule (crash loop) looks for.
    health:    "healthy" | "unhealthy" | "starting", or None without a healthcheck
    exit_code: the N in "Exited (N) ...", or None if the container hasn't exited
    """
    if not isinstance(data, list):
        return []
    containers = []
    for item in data:
        if not isinstance(item, dict):
            continue
        names, state, status = item.get("Names"), item.get("State"), item.get("Status")
        if not isinstance(names, list) or not isinstance(state, str) or not isinstance(status, str):
            continue
        # The first plain name; old-style link aliases look like "/web/db" and are skipped.
        name = next(
            (
                n.lstrip("/")
                for n in names
                if isinstance(n, str)
                and len(n.lstrip("/")) <= MAX_NAME
                and _NAME.fullmatch(n.lstrip("/"))
            ),
            None,
        )
        if name is None:
            continue
        containers.append(
            {
                "name": name,
                "state": state[:MAX_STATE],
                "status": status[:MAX_STATUS],
                "health": _health(status),
                "exit_code": _exit_code(status),
            }
        )
    return containers


def collect_container_health(socket_path: str | None = None) -> list[dict[str, Any]]:
    """Every container's health through the proxy. Raises DockerProxyUnavailable if nothing
    answers on the socket, DockerError if Docker itself fails (the proxy says 502)."""
    path = DOCKER_SOCKET if socket_path is None else socket_path
    api, _ = parse_api_version(get_json(path, "/version", CALL_TIMEOUT, MAX_STATS_BYTES))
    return parse_container_health(
        get_json(path, f"/v{api}/containers/json?all=1", CALL_TIMEOUT, MAX_LIST_BYTES)
    )


@dataclass(frozen=True)
class StatsSample:
    cpu_total: int | None  # container CPU time, ns
    system_total: int | None  # whole-host CPU time, ns (all cores)
    pre_cpu_total: int | None  # the engine's own previous sample (older engines only)
    pre_system_total: int | None
    mem: int | None  # usage minus inactive page cache, like `docker stats`
    mem_limit_raw: int | None
    rx: int | None
    tx: int | None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def parse_stats(data: Any) -> StatsSample | None:
    """The numbers we use from one stats response (cgroup v1 or v2)."""
    if not isinstance(data, dict):
        return None
    cpu, pre = _dict(data.get("cpu_stats")), _dict(data.get("precpu_stats"))
    memory = _dict(data.get("memory_stats"))
    usage = _uint(memory.get("usage"))
    stats = _dict(memory.get("stats"))
    # cgroup v2 calls it inactive_file, v1 total_inactive_file.
    inactive = _uint(stats.get("inactive_file"))
    if inactive is None:
        inactive = _uint(stats.get("total_inactive_file")) or 0
    rx = tx = None
    networks = data.get("networks")
    if isinstance(networks, dict) and networks:
        rx_values = [_uint(_dict(n).get("rx_bytes")) for n in networks.values()]
        tx_values = [_uint(_dict(n).get("tx_bytes")) for n in networks.values()]
        if all(v is not None for v in rx_values + tx_values):
            rx, tx = sum(rx_values), sum(tx_values)  # type: ignore[arg-type]
    return StatsSample(
        cpu_total=_uint(_dict(cpu.get("cpu_usage")).get("total_usage")),
        system_total=_uint(cpu.get("system_cpu_usage")),
        pre_cpu_total=_uint(_dict(pre.get("cpu_usage")).get("total_usage")),
        pre_system_total=_uint(pre.get("system_cpu_usage")),
        mem=None if usage is None else max(0, usage - inactive),
        mem_limit_raw=_uint(memory.get("limit")),
        rx=rx,
        tx=tx,
    )


def cpu_share(cpu: int | None, pre_cpu: int | None, system: int | None, pre_system: int | None):
    """Container CPU as a percent of the WHOLE host (0-100), not `docker stats`' per-core
    sum that can reach 400 % on 4 cores. A container busy on one of 4 cores shows 25 %."""
    if None in (cpu, pre_cpu, system, pre_system):
        return None
    cpu_delta, system_delta = cpu - pre_cpu, system - pre_system  # type: ignore[operator]
    if system_delta <= 0 or cpu_delta < 0:
        return None
    return round(min(100.0, cpu_delta * 100.0 / system_delta), 1)


def memory_limit(raw: int | None, host_total: int | None) -> int | None:
    """The container's memory limit, or None when it has none.

    Without a limit Docker reports the host's whole memory (or, on cgroup v1, a huge
    number), so only a limit below the host's total is a real one.
    """
    if raw is None or raw <= 0 or raw >= UNLIMITED or host_total is None or raw >= host_total:
        return None
    return raw


@dataclass(frozen=True)
class _Previous:
    cpu: int | None
    system: int | None
    rx: int | None
    tx: int | None
    at: float


class ContainerStats:
    """Per-container usage, with the counters of the previous report kept for the rates."""

    def __init__(
        self, socket_path: str = DOCKER_SOCKET, clock: Callable[[], float] = time.monotonic
    ):
        self.socket_path = socket_path
        self.clock = clock
        self._previous: dict[str, _Previous] = {}

    def _get(self, path: str, timeout: float, limit: int) -> Any:
        return get_json(self.socket_path, path, timeout, limit)

    def collect(self, host_total: int | None) -> tuple[list[dict[str, Any]] | None, bool]:
        """(containers, truncated). containers is None when Docker can't be reached."""
        deadline = self.clock() + TOTAL_BUDGET
        try:
            api, one_shot = parse_api_version(self._get("/version", CALL_TIMEOUT, MAX_STATS_BYTES))
            listed = parse_container_list(
                self._get(f"/v{api}/containers/json", CALL_TIMEOUT, MAX_LIST_BYTES)
            )
        except DockerError:
            return None, False
        truncated = len(listed) > MAX_CONTAINERS
        query = "stream=false&one-shot=true" if one_shot else "stream=false"
        results: list[dict[str, Any]] = []
        seen: dict[str, _Previous] = {}
        for cid, name in listed[:MAX_CONTAINERS]:
            remaining = deadline - self.clock()
            if remaining <= 0:
                truncated = True
                break
            try:
                data = self._get(
                    f"/v{api}/containers/{cid}/stats?{query}",
                    min(CALL_TIMEOUT, remaining),
                    MAX_STATS_BYTES,
                )
            except DockerProxyUnavailable:
                return None, False
            except DockerError:
                continue  # it stopped between the list and now, or the engine hiccuped
            sample = parse_stats(data)
            if sample is None:
                continue
            now = self.clock()
            prev = self._previous.get(cid)
            if sample.pre_system_total:  # an older engine measured the interval itself
                cpu = cpu_share(
                    sample.cpu_total,
                    sample.pre_cpu_total,
                    sample.system_total,
                    sample.pre_system_total,
                )
            elif prev is not None:
                cpu = cpu_share(sample.cpu_total, prev.cpu, sample.system_total, prev.system)
            else:
                cpu = None
            seconds = None if prev is None else now - prev.at
            results.append(
                {
                    "name": name,
                    "cpu_pct": cpu,
                    "mem": sample.mem,
                    "mem_limit": memory_limit(sample.mem_limit_raw, host_total),
                    "rx_bps": _rate(prev.rx if prev else None, sample.rx, seconds),
                    "tx_bps": _rate(prev.tx if prev else None, sample.tx, seconds),
                }
            )
            seen[cid] = _Previous(sample.cpu_total, sample.system_total, sample.rx, sample.tx, now)
        # Only containers still running are remembered, so this never grows past the cap.
        self._previous = seen
        return results, truncated


def _rate(prev: int | None, cur: int | None, seconds: float | None) -> int | None:
    if prev is None or cur is None or not seconds or seconds <= 0 or cur < prev:
        return None  # first sample, or the counter restarted with the container
    return round((cur - prev) / seconds)
