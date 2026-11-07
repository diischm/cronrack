"""Network checks: is the path from this host to the internet working, point by point?

Opt-in ([network] enabled = true in agent.toml). The points come ONLY from agent.toml (or
the default route of this host, for "Router"); nothing the server sends can add a target.

Probing needs no root and no capability:
- ICMP echo through the kernel's unprivileged "ping socket" (SOCK_DGRAM + IPPROTO_ICMP).
  The kernel allows it when the agent's group is inside net.ipv4.ping_group_range, which
  systemd sets to every group by default. The `ping` binary would need the same thing:
  under NoNewPrivileges its file capability is ignored.
- Otherwise a TCP connect to a few well-known ports, per target. "Connection refused"
  counts as an answer: the box replied. Never port 22: repeated SSH connects look like
  login attempts and can get this host banned by fail2ban on the device.

Bounded everywhere: 5 probes per target, 200 ms apart, 1 s timeout each, all targets at
once (a dead point never delays the points behind it; the report's list order is the path
order, not the order answers came in); DNS 2 s, public IP 5 s; one round never takes more than ROUND_DEADLINE seconds, and
rounds are at least MIN_ROUND_GAP apart however often reports are asked for (the result
of the last round is reused in between). Addresses are logged at DEBUG only.
"""

import concurrent.futures
import ipaddress
import logging
import math
import socket
import statistics
import struct
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from agent.config import NetworkConfig, NetworkHop, is_internet_address, is_local_address

logger = logging.getLogger(__name__)

ROUTE_PATH = "/proc/net/route"
PROBES = 5
PROBE_SPACING = 0.2  # seconds between two probes to the same target
PROBE_TIMEOUT = 1.0
DNS_TIMEOUT = 2.0
PUBLIC_IP_TIMEOUT = 5.0
ROUND_DEADLINE = 8.0
MIN_ROUND_GAP = 30.0  # seconds; the most often a round runs, whatever asks for reports
MAX_PUBLIC_IP_BYTES = 64
# TCP fallback: ports a router, ISP box or managed switch usually answers on (DNS forwarder,
# web UI), and public resolvers (DNS, HTTPS). A refusal counts as an answer.
LOCAL_TCP_PORTS = (53, 80, 443)
INTERNET_TCP_PORTS = (53, 443)
RTF_UP = 0x1
RTF_GATEWAY = 0x2
_warned_before_is_router = False  # logged once per run, not once per round


# --- the default gateway -------------------------------------------------------------------


def parse_default_gateway(route_text: str) -> str | None:
    """The IPv4 default gateway from /proc/net/route, or None.

    Columns: Iface Destination Gateway Flags ...; addresses are little-endian hex.
    """
    for line in route_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            gateway = int(fields[2], 16)
        except ValueError:
            continue
        if flags & RTF_UP and flags & RTF_GATEWAY and gateway:
            return socket.inet_ntoa(struct.pack("<L", gateway))
    return None


def read_default_gateway(path: str = ROUTE_PATH) -> str | None:
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return parse_default_gateway(f.read(64 * 1024))
    except OSError:
        return None


# --- probes --------------------------------------------------------------------------------


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def echo_request(seq: int, payload: bytes = b"cronrack-network") -> bytes:
    """An ICMP echo request. With a ping socket the kernel sets the identifier."""
    header = struct.pack("!BBHHH", 8, 0, 0, 0, seq)
    return struct.pack("!BBHHH", 8, 0, _checksum(header + payload), 0, seq) + payload


def icmp_available() -> bool:
    """True if this process may open a ping socket (net.ipv4.ping_group_range)."""
    try:
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP).close()
    except OSError:
        return False
    return True


def icmp_probe(address: str, seq: int, timeout: float = PROBE_TIMEOUT) -> float | None:
    """Milliseconds until the echo reply, or None (no reply in time, or unreachable)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP) as sock:
        start = time.monotonic()
        try:
            sock.sendto(echo_request(seq), (address, 0))
            while True:
                left = timeout - (time.monotonic() - start)
                if left <= 0:
                    return None
                sock.settimeout(left)
                # A ping socket only ever receives replies to its own identifier.
                data, _ = sock.recvfrom(1024)
                if len(data) >= 8 and data[0] == 0 and struct.unpack("!H", data[6:8])[0] == seq:
                    return (time.monotonic() - start) * 1000
        except OSError:  # timeout, host or network unreachable
            return None


def tcp_probe(address: str, ports: tuple[int, ...], timeout: float = PROBE_TIMEOUT) -> float | None:
    """Milliseconds until the first port answered (accepted or refused), or None.

    The budget is shared: each port gets an equal part of what is left, so one filtered
    port can't use it all up before the next port is tried.
    """
    start = time.monotonic()
    for index, port in enumerate(ports):
        left = timeout - (time.monotonic() - start)
        if left <= 0:
            return None
        try:
            with socket.create_connection((address, port), timeout=left / (len(ports) - index)):
                pass
        except ConnectionRefusedError:
            pass  # a RST: the box is there
        except OSError:
            continue  # filtered or unreachable: try the next port
        return (time.monotonic() - start) * 1000
    return None


def summarize(rtts: list[float | None]) -> dict[str, Any]:
    """sent / received and the median and p95 (nearest rank) in ms, 1 decimal."""
    answered = sorted(r for r in rtts if r is not None)
    result: dict[str, Any] = {
        "sent": len(rtts),
        "received": len(answered),
        "median_ms": None,
        "p95_ms": None,
    }
    if answered:
        result["median_ms"] = round(statistics.median(answered), 1)
        result["p95_ms"] = round(answered[math.ceil(0.95 * len(answered)) - 1], 1)
    return result


def probe_target(
    probe: Callable[[int], float | None],
    count: int = PROBES,
    spacing: float = PROBE_SPACING,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """`count` probes, `spacing` seconds apart (start to start): never a flood."""
    rtts: list[float | None] = []
    for seq in range(1, count + 1):
        started = time.monotonic()
        rtts.append(probe(seq))
        if seq < count:
            sleep(max(0.0, spacing - (time.monotonic() - started)))
    return summarize(rtts)


# --- DNS, public IP, "is the server on this machine?" --------------------------------------


def _bounded(fn: Callable[[], Any], timeout: float) -> Any:
    """fn() in a daemon thread; None if it doesn't finish in time (getaddrinfo can't time out)."""
    box: list[Any] = []
    thread = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    thread.start()
    thread.join(timeout)
    return box[0] if box else None


def check_dns(name: str, timeout: float = DNS_TIMEOUT) -> dict[str, Any]:
    """Resolve `name` with the system resolver: {"ok", "ms"}."""

    def resolve() -> float | None:
        start = time.monotonic()
        try:
            socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
        except OSError:
            return None
        return (time.monotonic() - start) * 1000

    ms = _bounded(resolve, timeout)
    return {"ok": ms is not None, "ms": None if ms is None else round(ms, 1)}


def check_public_ip(url: str, timeout: float = PUBLIC_IP_TIMEOUT) -> str | None:
    """Ask `url` (only when public_ip_check is on) for this network's public address."""
    try:
        # The URL comes from agent.toml, where it is checked to be https://.
        with urllib.request.urlopen(url, timeout=timeout) as response:  # nosec B310
            text = response.read(MAX_PUBLIC_IP_BYTES).decode("ascii", "replace").strip()
        ip = ipaddress.ip_address(text)
    except (OSError, ValueError):
        return None
    return str(ip) if ip.is_global else None


def server_is_local(server_url: str | None) -> bool:
    """True if the CronRack server runs on this machine: its host name is a loopback
    address, or an address this machine owns (bind() only succeeds for local addresses).

    Such an agent keeps reporting during a router or internet outage, so the server
    prefers it as the network view.
    """
    host = urlsplit(server_url or "").hostname
    if not host:
        return False

    def addresses() -> list[str]:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError:
            return []
        return [str(info[4][0]) for info in infos]

    for address in _bounded(addresses, DNS_TIMEOUT) or []:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_loopback:
            return True
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.bind((address, 0))
        except OSError:
            continue
        return True
    return False


# --- one round -----------------------------------------------------------------------------


def _before_points(config: NetworkConfig, gateway: str | None) -> list[NetworkHop]:
    """`before`, without a point that turns out to be the detected router (gateway = "auto")."""
    global _warned_before_is_router
    kept = [hop for hop in config.before if hop.address != gateway]
    if len(kept) != len(config.before) and not _warned_before_is_router:
        _warned_before_is_router = True
        names = ", ".join(hop.name for hop in config.before if hop.address == gateway)
        logger.warning("network: ignoring 'before' point(s) that are the router: %s", names)
    return kept


def _points(config: NetworkConfig, gateway: str | None) -> list[dict[str, Any]]:
    """Every point in path order. This list IS the chain order the server reads."""
    points: list[dict[str, Any]] = []
    for hop in _before_points(config, gateway):
        points.append({"name": hop.name, "role": "before", "address": hop.address})
    if gateway:
        points.append({"name": "Router", "role": "gateway", "address": gateway})
    for hop in config.hops:
        points.append({"name": hop.name, "role": "local", "address": hop.address})
    for address in config.internet:
        points.append({"name": address, "role": "internet", "address": address})
    return points


def _done(future: concurrent.futures.Future | None, default: Any) -> Any:
    if future is None or not future.done() or future.exception() is not None:
        return default
    return future.result()


def collect_network(
    config: NetworkConfig,
    server_url: str | None,
    now: datetime | None = None,
    route_path: str = ROUTE_PATH,
) -> dict[str, Any]:
    """One probe round: the "network" key of the report."""
    if now is None:
        now = datetime.now(UTC)
    gateway = read_default_gateway(route_path) if config.gateway == "auto" else config.gateway
    # The server refuses a report with an unusable address (0.0.0.0, multicast, ...): such a
    # route counts as no gateway. A public one is fine (a VPS, some ISP lines).
    if gateway and not (is_local_address(gateway) or is_internet_address(gateway)):
        gateway = None
    method = "icmp" if icmp_available() else "tcp"
    points = _points(config, gateway)

    def prober(point: dict[str, Any]) -> Callable[[int], float | None]:
        address = point["address"]
        if method == "icmp":
            return lambda seq: icmp_probe(address, seq)
        ports = INTERNET_TCP_PORTS if point["role"] == "internet" else LOCAL_TCP_PORTS
        return lambda _seq: tcp_probe(address, ports)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(points) + 3)
    try:
        futures = [pool.submit(probe_target, prober(p)) for p in points]
        dns = pool.submit(check_dns, config.dns_name) if config.dns else None
        public = (
            pool.submit(check_public_ip, config.public_ip_url) if config.public_ip_check else None
        )
        local = pool.submit(server_is_local, server_url)
        waiting = [*futures, *(f for f in (dns, public, local) if f is not None)]
        concurrent.futures.wait(waiting, timeout=ROUND_DEADLINE)
        for point, future in zip(points, futures, strict=True):
            point.update(_done(future, summarize([None] * PROBES)))
        report = {
            "method": method,
            "probed_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "gateway_found": gateway is not None,
            "server_local": bool(_done(local, False)),
            "points": points,
            "dns": _done(dns, {"ok": False, "ms": None}) if config.dns else None,
            "public_ip": _done(public, None),
        }
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    logger.debug("network round: %s", [(p["name"], p["address"], p["received"]) for p in points])
    return report


class RateLimitedNetwork:
    """At most one round per MIN_ROUND_GAP; reports in between get the last result."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        collect: Callable[[NetworkConfig, str | None], dict[str, Any]] = collect_network,
    ) -> None:
        self._clock = clock
        self._collect = collect
        self._last: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def __call__(self, config: NetworkConfig, server_url: str | None) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            if self._last is not None and now - self._last[0] < MIN_ROUND_GAP:
                return self._last[1]
            result = self._collect(config, server_url)
            self._last = (now, result)
            return result


# One per agent process: build_report() runs in a thread for every report.
NETWORK = RateLimitedNetwork()
