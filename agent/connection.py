"""The agent's one WebSocket connection to the server, kept open and re-opened forever.

    run_forever ── connect (token in the handshake header) ── session ──┐
         ▲                                                              │
         └──────── wait backoff(attempt): 1s, 2s, 4s … 300s ◄───────────┘

The agent only ever connects *out*. It never listens on a port, and nothing the
server sends is executed: protocol.parse_server_message() lets through only
"welcome" (how often to report) and "collect" (report now).
"""

import asyncio
import json
import logging
import secrets
import ssl
import time
from dataclasses import replace
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from agent import AGENT_VERSION
from agent.config import AgentConfig, websocket_url
from agent.protocol import (
    MAX_MESSAGE_BYTES,
    MAX_REPORT_BYTES,
    hello_message,
    parse_server_message,
    report_message,
)
from agent.report import build_report, fit_report

logger = logging.getLogger(__name__)

# websockets logs every handshake header (including Authorization) at DEBUG level.
# Give it its own logger that never goes below INFO, whatever the root level is.
_ws_logger = logging.getLogger("agent.websockets")
_ws_logger.setLevel(logging.INFO)

HELLO_TIMEOUT_SECONDS = 10
OPEN_TIMEOUT_SECONDS = 20
PING_SECONDS = 20  # heartbeat; the server side (uvicorn) pings every 20s too
MAX_BACKOFF_SECONDS = 300
# A connection that stayed up this long counts as healthy: the next
# failure starts the backoff again from 1s. Shorter sessions keep growing it, so a
# server that accepts and immediately drops us isn't hammered every second.
HEALTHY_SESSION_SECONDS = 60
# Never build reports more often than this, however many "collect" messages arrive.
MIN_REPORT_GAP_SECONDS = 5

_random = secrets.SystemRandom()


class SessionError(Exception):
    """The server broke the protocol (e.g. no welcome in time)."""


def backoff(attempt: int) -> float:
    """Seconds to wait before reconnect attempt number `attempt` (0, 1, 2, …).

    Exponential (1, 2, 4, … capped at 300) with "equal jitter": a random value
    between half and all of that. The randomness spreads out many agents that
    lost the server at the same moment, so they don't all reconnect at once.
    """
    base = min(MAX_BACKOFF_SECONDS, 2 ** min(attempt, 16))
    return _random.uniform(base / 2, base)


def build_ssl_context(config: AgentConfig) -> ssl.SSLContext:
    """TLS settings for wss://. Certificate checking is always on; there is no off switch.

    ca_file is for a private CA (e.g. Caddy's internal CA): it is trusted *in
    addition* to the system's CAs, never instead of verification.
    """
    context = ssl.create_default_context(cafile=config.ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # create_default_context() already sets both; stated so a refactor can't drop them.
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


async def _send(ws: ClientConnection, message: dict[str, Any]) -> None:
    await ws.send(json.dumps(message))


async def _wait_for_welcome(ws: ClientConnection) -> dict[str, Any]:
    while True:
        message = parse_server_message(await ws.recv())
        if message is not None and message["type"] == "welcome":
            return message


async def report_loop(
    ws: ClientConnection, config: AgentConfig, interval: int, collect: asyncio.Event
) -> None:
    """Send a report now, then every `interval` seconds or as soon as `collect` is set."""
    while True:
        started = time.monotonic()
        collect.clear()
        # build_report runs commands (apt, docker, df …) that block for seconds.
        # In a thread, the event loop keeps answering heartbeats meanwhile.
        report = await asyncio.to_thread(build_report, config)
        # One oversized message would make the server drop us, again and again.
        report = fit_report(report, MAX_REPORT_BYTES)
        await _send(ws, report_message(report))
        logger.debug("Report sent")
        try:
            await asyncio.wait_for(collect.wait(), timeout=interval)
        except TimeoutError:
            continue
        # Asked to collect early: still respect the minimum gap.
        gap = MIN_REPORT_GAP_SECONDS - (time.monotonic() - started)
        if gap > 0:
            await asyncio.sleep(gap)


async def receive_loop(ws: ClientConnection, collect: asyncio.Event) -> None:
    """Read server messages until the connection closes. Only "collect" does anything."""
    async for raw in ws:
        message = parse_server_message(raw)
        if message is None:
            continue
        if message["type"] == "collect":
            logger.info("Server asked for a report")
            collect.set()
        # A second "welcome" mid-session is valid but ignored.


async def session(ws: ClientConnection, config: AgentConfig) -> None:
    """One connection's lifetime: hello → welcome → report and receive side by side.

    Returns or raises when the connection ends.
    """
    await _send(ws, hello_message(config))
    try:
        welcome = await asyncio.wait_for(_wait_for_welcome(ws), HELLO_TIMEOUT_SECONDS)
    except TimeoutError:
        raise SessionError("no welcome from the server within 10s") from None

    # The local config decides what may run. The server can only narrow it down.
    # An old server doesn't announce "kubernetes" and would reject that part of a report,
    # so it only goes to a server that does.
    allowed = set(welcome["enabled_checks"])
    kubernetes = replace(
        config.kubernetes, enabled=config.kubernetes.enabled and "kubernetes" in allowed
    )
    effective = replace(
        config,
        enabled_checks=[c for c in config.enabled_checks if c in allowed],
        kubernetes=kubernetes,
    )
    interval = welcome["interval_seconds"]
    checks = effective.enabled_checks + (["kubernetes"] if kubernetes.enabled else [])
    logger.info("Connected; reporting every %ss (%s)", interval, ", ".join(checks))

    collect = asyncio.Event()
    tasks = {
        asyncio.create_task(report_loop(ws, effective, interval, collect)),
        asyncio.create_task(receive_loop(ws, collect)),
    }
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in done:
        task.result()  # re-raise ConnectionClosed etc.


def _describe(exc: BaseException) -> str:
    """A short, token-free description of why a connection ended."""
    if isinstance(exc, InvalidStatus):
        return f"server refused the connection (HTTP {exc.response.status_code})"
    if isinstance(exc, ConnectionClosed):
        code = exc.rcvd.code if exc.rcvd is not None else None
        return f"connection closed (code {code})"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "TLS certificate check failed (wrong host name or untrusted CA; see ca_file)"
    if isinstance(exc, SessionError):
        return str(exc)
    return type(exc).__name__


def _connect(config: AgentConfig):
    url = websocket_url(config.server_url)
    return connect(
        url,
        # websockets >= 14: "additional_headers" (older versions called it "extra_headers").
        additional_headers={"Authorization": f"Bearer {config.token}"},
        ssl=build_ssl_context(config) if url.startswith("wss://") else None,
        user_agent_header=f"cronrack-agent/{AGENT_VERSION}",
        open_timeout=OPEN_TIMEOUT_SECONDS,
        ping_interval=PING_SECONDS,
        ping_timeout=PING_SECONDS,
        max_size=MAX_MESSAGE_BYTES,
        logger=_ws_logger,
    )


async def run_forever(config: AgentConfig) -> None:
    """Connect, run a session, and on any failure wait and try again. Never returns.

    Stop it by cancelling the task: leaving `async with connect(...)` closes the
    socket cleanly (close code 1000).
    """
    if not config.token or not config.server_url:
        raise ValueError("run_forever needs server_url and token")
    attempt = 0
    while True:
        started = None
        try:
            async with _connect(config) as ws:
                started = time.monotonic()
                try:
                    await session(ws, config)
                except asyncio.CancelledError:
                    # Shutting down (SIGTERM): say goodbye with a normal close (1000).
                    # Without this, websockets would close with 1011 "internal error".
                    await ws.close()
                    raise
            reason = "connection closed"
        except (OSError, WebSocketException, SessionError, TimeoutError) as exc:
            reason = _describe(exc)
        except Exception as exc:  # noqa: BLE001 - a bug must not stop the agent for good
            reason = f"unexpected error {type(exc).__name__}"
        if started is not None and time.monotonic() - started >= HEALTHY_SESSION_SECONDS:
            attempt = 0
        delay = backoff(attempt)
        attempt += 1
        logger.warning("Disconnected: %s. Reconnecting in %.1fs", reason, delay)
        await asyncio.sleep(delay)
