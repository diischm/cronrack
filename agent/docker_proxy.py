"""cronrack-docker-proxy: a read-only, allowlisted window onto the Docker API.

Being in the "docker" group is root-equivalent: whoever can talk to /var/run/docker.sock can
start a container that mounts / and change any file. So the agent is NOT in that group.
This small proxy is, and it lets through only what the agent's checks read:

    GET /version                                          -> {"ApiVersion"}
    GET /v1.N/containers/json[?all=1]                     -> [{Id, Names, State, Status}]
    GET /v1.N/containers/<hex id>/stats?stream=false[&one-shot=true] -> CPU/memory/net counters

That's all three. Container inspect (/containers/<id>/json) is NOT allowed: the agent
doesn't use it, and its answer is where environment variables live.

Everything else gets 403 before Docker is even contacted: any other method, path or query,
percent-encoding, "..", doubled slashes, a request body, Transfer-Encoding, Upgrade.
Answers are rebuilt from the few fields above, so container environment variables,
commands, labels, mounts and host config never reach the agent, even if it were
compromised. Client headers are not forwarded.

It takes no network input. systemd creates /run/cronrack/docker.sock (root:cronrack 0660,
deploy/cronrack-docker-proxy.socket) and hands the open socket over (socket activation);
the service itself runs with PrivateNetwork=yes and only AF_UNIX sockets allowed. Without a
socket from systemd it refuses to start. Standard library only.

    systemctl status cronrack-docker-proxy.socket cronrack-docker-proxy.service
"""

import http.client
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, NamedTuple

from agent.docker_api import UnixConnection

logger = logging.getLogger("cronrack-docker-proxy")

UPSTREAM_SOCKET = "/var/run/docker.sock"
SD_LISTEN_FDS_START = 3  # the first file descriptor systemd passes (sd_listen_fds(3))

MAX_HEAD_BYTES = 8 * 1024  # request line + headers; the agent sends about 150 bytes
CLIENT_TIMEOUT = 5.0  # seconds for a client to send its whole request
UPSTREAM_TIMEOUT = 10.0  # seconds per socket operation towards Docker
MAX_LIST_ITEMS = 1000
MAX_NAMES = 4
MAX_TEXT = 255
MAX_NETWORKS = 32

_REQUEST_LINE = re.compile(r"([A-Z]{1,16}) (\S{1,2048}) HTTP/1\.[01]", re.ASCII)
# Header names only; the value is taken with str.strip (a regex with an optional-space
# suffix backtracks badly on long runs of spaces).
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}", re.ASCII)
MAX_HEADER_VALUE = 4096
MAX_WORKERS = 4  # connections answered at the same time; more get 503 at once
_NETWORK_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}", re.ASCII)
_API = r"/v1\.\d{1,3}"
_ID = r"[0-9a-f]{12,64}"
# Headers that would mean a body or a protocol switch: never on a read-only GET.
_REFUSED_HEADERS = frozenset({"content-length", "transfer-encoding", "upgrade", "expect"})


class Route(NamedTuple):
    name: str
    pattern: re.Pattern[str]
    limit: int  # largest answer from Docker we accept, in bytes
    filter: Callable[[Any], Any]


class NotSocketActivated(Exception):
    """Started without exactly one socket from systemd."""


# --- what an answer may contain -----------------------------------------------------------


def _text(value: Any) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _uint(value: Any) -> int | None:
    number = _int(value)
    return number if number is not None and number >= 0 else None


def _pick(source: Any, keys: dict[str, Callable[[Any], Any]]) -> dict[str, Any]:
    """The listed keys of a dict, each passed through its check; bad or missing ones dropped."""
    if not isinstance(source, dict):
        return {}
    picked = {}
    for key, check in keys.items():
        if key in source:
            value = check(source[key])
            if value is not None:
                picked[key] = value
    return picked


def _api_version(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 16 else None


def filter_version(data: Any) -> dict[str, Any]:
    return _pick(data, {"ApiVersion": _api_version})


def _names(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        return None
    return [n[:MAX_TEXT] for n in value[:MAX_NAMES]]


def filter_list(data: Any) -> list[dict[str, Any]]:
    """Container list: Id, Names, State, Status. Command, Labels, Mounts, Ports are dropped."""
    if not isinstance(data, list):
        return []
    keys = {"Id": _text, "Names": _names, "State": _text, "Status": _text}
    return [_pick(item, keys) for item in data[:MAX_LIST_ITEMS] if isinstance(item, dict)]


def _cpu_usage(value: Any) -> dict[str, Any] | None:
    return _pick(value, {"total_usage": _uint}) or None


def _cpu(value: Any) -> dict[str, Any] | None:
    return _pick(value, {"cpu_usage": _cpu_usage, "system_cpu_usage": _uint, "online_cpus": _uint})


def _memory_stats(value: Any) -> dict[str, Any] | None:
    return _pick(value, {"inactive_file": _uint, "total_inactive_file": _uint}) or None


def _memory(value: Any) -> dict[str, Any] | None:
    return _pick(value, {"usage": _uint, "limit": _uint, "stats": _memory_stats})


def _networks(value: Any) -> dict[str, Any] | None:
    """Per-network counters, renamed net0, net1 ...: the agent only adds them up, so the
    network names (which can reveal project names) don't need to leave."""
    if not isinstance(value, dict):
        return None
    networks = {}
    for name, counters in value.items():
        if len(networks) >= MAX_NETWORKS:
            break
        if isinstance(name, str) and _NETWORK_NAME.fullmatch(name):
            counts = _pick(counters, {"rx_bytes": _uint, "tx_bytes": _uint})
            networks[f"net{len(networks)}"] = counts
    return networks


def filter_stats(data: Any) -> dict[str, Any]:
    """Stats: the CPU, memory and network counters agent/docker_api.py computes with."""
    return _pick(
        data,
        {"cpu_stats": _cpu, "precpu_stats": _cpu, "memory_stats": _memory, "networks": _networks},
    )


# --- the allowlist ---------------------------------------------------------------------------

ROUTES = (
    Route("version", re.compile(r"/version", re.ASCII), 64 * 1024, filter_version),
    Route(
        "list",
        re.compile(_API + r"/containers/json(?:\?all=1)?", re.ASCII),
        4 * 1024 * 1024,
        filter_list,
    ),
    Route(
        "stats",
        re.compile(
            _API + r"/containers/" + _ID + r"/stats\?stream=false(?:&one-shot=true)?", re.ASCII
        ),
        256 * 1024,
        filter_stats,
    ),
)


def match_route(method: str, target: str) -> Route | None:
    """The route for an allowed request, or None. Exact GET, exact path and query."""
    if method != "GET":
        return None
    for route in ROUTES:
        if route.pattern.fullmatch(target):
            return route
    return None


# --- one connection ----------------------------------------------------------------------------


class Refused(Exception):
    """The request is answered with this status and nothing is asked of Docker."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    408: "Request Timeout",
    409: "Conflict",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


def read_head(conn: socket.socket) -> bytes:
    """The request line and headers, up to the blank line. Raises Refused."""
    deadline = time.monotonic() + CLIENT_TIMEOUT
    buffer = b""
    while True:
        if b"\r\n\r\n" in buffer:
            head = buffer.split(b"\r\n\r\n", 1)[0]
            if len(head) > MAX_HEAD_BYTES:
                raise Refused(431, "request head too large")
            if b"\n" in head.replace(b"\r\n", b"") or b"\r" in head.replace(b"\r\n", b""):
                raise Refused(400, "bare CR or LF in the request")
            return head
        if b"\n\n" in buffer:
            raise Refused(400, "bare LF in the request")
        if len(buffer) > MAX_HEAD_BYTES:
            raise Refused(431, "request head too large")
        left = deadline - time.monotonic()
        if left <= 0:
            raise Refused(408, "request not finished in time")
        conn.settimeout(left)
        try:
            chunk = conn.recv(4096)
        except TimeoutError:
            raise Refused(408, "request not finished in time") from None
        except OSError:
            raise Refused(400, "connection broken mid-request") from None
        if not chunk:
            raise Refused(400, "connection closed mid-request")
        buffer += chunk


def parse_request(head: bytes) -> tuple[str, str, dict[str, str]]:
    """(method, target, lowercase headers) from a request head. Raises Refused(400)."""
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        raise Refused(400, "not ASCII") from None
    lines = text.split("\r\n")
    match = _REQUEST_LINE.fullmatch(lines[0])
    if not match:
        raise Refused(400, "malformed request line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        value = value.strip(" \t")
        if (
            not sep
            or not _HEADER_NAME.fullmatch(name)
            or len(value) > MAX_HEADER_VALUE
            or not value.isprintable()
        ):
            raise Refused(400, "malformed header")
        headers[name.lower()] = value
    return match[1], match[2], headers


def check_request(method: str, target: str, headers: dict[str, str]) -> Route:
    """The route for an allowed request. Raises Refused(403) for everything else."""
    refused = _REFUSED_HEADERS & set(headers)
    if refused - {"content-length"} or headers.get("content-length", "0") != "0":
        raise Refused(403, "a body or protocol switch is not allowed")
    route = match_route(method, target)
    if route is None:
        raise Refused(403, "not on the allowlist")
    return route


def ask_docker(upstream: str, target: str, route: Route) -> tuple[int, Any]:
    """(status, filtered body) from Docker. Raises Refused(502) if Docker can't answer."""
    conn = UnixConnection(upstream, UPSTREAM_TIMEOUT)
    try:
        # Fixed headers only: nothing the client sent is passed on.
        conn.request("GET", target, headers={"Host": "docker", "Accept": "application/json"})
        response = conn.getresponse()
        body = response.read(route.limit + 1)
        status = response.status
    except (OSError, http.client.HTTPException):
        raise Refused(502, "Docker did not answer") from None
    finally:
        conn.close()
    if status != 200:
        # Docker's error text can quote names or values; pass the status code only.
        return (status if status in REASONS else 502), {"message": f"Docker answered {status}"}
    if len(body) > route.limit:
        raise Refused(502, "Docker's answer is too large")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise Refused(502, "Docker's answer is not JSON") from None
    return 200, route.filter(data)


def send(conn: socket.socket, status: int, body: Any) -> None:
    data = json.dumps(body, separators=(",", ":")).encode()
    head = (
        f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    try:
        conn.settimeout(CLIENT_TIMEOUT)
        conn.sendall(head + data)
    except OSError:
        pass  # the client went away; nothing to do


def handle(conn: socket.socket, upstream: str) -> None:
    """Answer one request on `conn` (one request per connection)."""
    method = "?"
    try:
        method, target, headers = parse_request(read_head(conn))
        route = check_request(method, target, headers)
        status, body = ask_docker(upstream, target, route)
        send(conn, status, body)
    except Refused as refused:
        if refused.status == 403:
            # The method matched [A-Z]{1,16}; the path is attacker-chosen, so it isn't logged.
            logger.warning("Refused a %s request: %s", method, refused.reason)
        send(conn, refused.status, {"message": refused.reason})


def _discard_pending(conn: socket.socket) -> None:
    """Read (and drop) what the client already sent, briefly. Closing a socket with unread
    data makes the kernel reset it, and the client would never see our answer."""
    conn.settimeout(0.2)
    try:
        conn.recv(MAX_HEAD_BYTES)
    except OSError:
        pass


def _answer(conn: socket.socket, upstream: str, slots: threading.BoundedSemaphore) -> None:
    try:
        with conn:
            handle(conn, upstream)
    except Exception:
        logger.exception("Error while answering a request")
    finally:
        slots.release()


def serve(listener: socket.socket, upstream: str, stop: threading.Event | None = None) -> None:
    """Accept connections until `stop` is set; answer up to MAX_WORKERS at once.

    Each connection gets at most CLIENT_TIMEOUT to send its one request, so a client that
    connects and says nothing holds one slot for a few seconds, never the whole proxy. With
    every slot busy, a new connection is told 503 straight away.
    """
    slots = threading.BoundedSemaphore(MAX_WORKERS)
    while stop is None or not stop.is_set():
        try:
            conn, _ = listener.accept()
        except OSError:
            if stop is not None and stop.is_set():
                return
            raise
        if stop is not None and stop.is_set():
            conn.close()
            return
        if not slots.acquire(blocking=False):
            with conn:
                _discard_pending(conn)
                send(conn, 503, {"message": "busy"})
            continue
        threading.Thread(target=_answer, args=(conn, upstream, slots), daemon=True).start()


def listener_from_systemd(environ: Any, pid: int) -> socket.socket:
    """The one stream socket systemd passed us (LISTEN_FDS=1 for our pid), or raise."""
    if environ.get("LISTEN_PID") != str(pid) or environ.get("LISTEN_FDS") != "1":
        raise NotSocketActivated("expected exactly one socket from systemd")
    sock = socket.socket(fileno=SD_LISTEN_FDS_START)
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
        sock.detach()
        raise NotSocketActivated("the socket from systemd is not a unix stream socket")
    return sock


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if argv:
        print("cronrack-docker-proxy takes no arguments", file=sys.stderr)
        return 2
    try:
        listener = listener_from_systemd(os.environ, os.getpid())
    except NotSocketActivated as exc:
        print(
            f"cronrack-docker-proxy: {exc}. It only runs from cronrack-docker-proxy.socket "
            "(systemd socket activation) and never opens a socket of its own.",
            file=sys.stderr,
        )
        return 2
    serve(listener, UPSTREAM_SOCKET)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
