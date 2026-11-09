"""Read-only Docker API client for container metrics (CPU, memory, network).

The agent already reads container *health* with `docker ps` (collectors.py). Usage numbers
come from the Engine API's stats endpoint instead, because `docker stats` prints rounded,
human-formatted strings ("1.2GiB / 3.8GiB") and can't say whether a memory limit is set.

Only GET requests, only to the local socket at a fixed path (never from the server or the
config), and only these endpoints:

    GET /version                                   which API version the engine speaks
    GET /v<api>/containers/json                    running containers (id + name)
    GET /v<api>/containers/<id>/stats?stream=false&one-shot=true

Reaching the socket needs the same opt-in Docker access the container checks need (the
docker group, see deploy/install.sh). Without it every call fails with PermissionError
and the agent sends `"containers": null`.

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

DOCKER_SOCKET = "/var/run/docker.sock"
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

_API_VERSION = re.compile(r"(\d{1,2})\.(\d{1,3})", re.ASCII)
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}", re.ASCII)
# Docker's own rule for names. Also keeps a name from looking like a --flag in a fix command.
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", re.ASCII)
# cgroup v1 reports "no limit" as a huge number close to 2**63.
UNLIMITED = 2**62


class DockerError(Exception):
    """A call failed (timeout, HTTP error, bad JSON). The message is only the error kind."""


class DockerUnavailable(DockerError):
    """No socket, or no permission to use it: Docker metrics are simply off."""


class _UnixConnection(http.client.HTTPConnection):
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
    conn = _UnixConnection(socket_path, timeout)
    try:
        conn.request("GET", path, headers={"Host": "docker", "Accept": "application/json"})
        response = conn.getresponse()
        if response.status != 200:
            raise DockerError(f"HTTP {response.status}")
        body = response.read(limit + 1)
        if len(body) > limit:
            raise DockerError("response too large")
        return json.loads(body)
    except (FileNotFoundError, PermissionError, ConnectionRefusedError) as exc:
        raise DockerUnavailable(type(exc).__name__) from None
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
            except DockerUnavailable:
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
