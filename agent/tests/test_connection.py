"""The agent's connection loop against a tiny local WebSocket server (127.0.0.1 only).

The fake server plays CronRack: it records what the agent sends, answers hello
with welcome, and lets each test send messages or drop the connection.
"""

import asyncio
import json
import logging
import os
import signal
import ssl
import threading
from pathlib import Path

import pytest
import pytest_asyncio
from websockets.asyncio.server import serve
from websockets.sync.server import serve as sync_serve

from agent import connection, report
from agent.__main__ import main
from agent.config import AgentConfig
from agent.connection import MAX_BACKOFF_SECONDS, backoff, build_ssl_context, run_forever

from .test_report import fake_collectors

TOKEN = "test-token-abcdefghijklmnopqrstuvwxyz0123456"
WELCOME = {"type": "welcome", "interval_seconds": 60, "enabled_checks": ["updates", "disks"]}


class FakeServer:
    def __init__(self) -> None:
        self.received: asyncio.Queue = asyncio.Queue()  # (connection number, message)
        self.headers: list = []
        self.connections: list = []
        self.welcome: dict | None = WELCOME

    async def handler(self, ws) -> None:
        self.headers.append(ws.request.headers)
        self.connections.append(ws)
        number = len(self.connections)
        async for raw in ws:
            message = json.loads(raw)
            await self.received.put((number, message))
            if message["type"] == "hello" and self.welcome is not None:
                await ws.send(json.dumps(self.welcome))

    async def next(self, timeout: float = 3) -> tuple[int, dict]:
        return await asyncio.wait_for(self.received.get(), timeout)


@pytest.fixture(autouse=True)
def fast_and_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "DEFAULT_COLLECTORS", fake_collectors())
    monkeypatch.setattr(connection, "MIN_REPORT_GAP_SECONDS", 0)
    monkeypatch.setattr(connection, "backoff", lambda attempt: 0.01)


@pytest_asyncio.fixture
async def server():
    fake = FakeServer()
    async with serve(fake.handler, "127.0.0.1", 0) as ws_server:
        fake.port = ws_server.sockets[0].getsockname()[1]
        yield fake


def config_for(server: FakeServer) -> AgentConfig:
    # http:// to 127.0.0.1 is allowed (local testing) and becomes ws://…/ws/agent/.
    return AgentConfig(server_url=f"http://127.0.0.1:{server.port}", token=TOKEN)


@pytest_asyncio.fixture
async def agent(server):
    task = asyncio.create_task(run_forever(config_for(server)))
    yield task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- backoff ------------------------------------------------------------------------


@pytest.mark.parametrize(("attempt", "base"), [(0, 1), (1, 2), (2, 4), (5, 32), (8, 256)])
def test_backoff_grows_exponentially_with_jitter(attempt: int, base: int) -> None:
    values = {backoff(attempt) for _ in range(50)}

    assert all(base / 2 <= v <= base for v in values)
    assert len(values) > 1  # jitter: not the same number every time


@pytest.mark.parametrize("attempt", [9, 20, 10_000])
def test_backoff_is_capped(attempt: int) -> None:
    assert all(
        MAX_BACKOFF_SECONDS / 2 <= backoff(attempt) <= MAX_BACKOFF_SECONDS for _ in range(50)
    )


# --- TLS -------------------------------------------------------------------------------


def test_ssl_context_always_verifies() -> None:
    context = build_ssl_context(AgentConfig())

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_missing_ca_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        build_ssl_context(AgentConfig(ca_file=str(tmp_path / "missing.pem")))


# --- the session ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_is_sent_in_the_handshake_header(server, agent) -> None:
    _, hello = await server.next()

    assert hello["type"] == "hello"
    assert server.headers[0]["Authorization"] == f"Bearer {TOKEN}"
    assert server.connections[0].request.path == "/ws/agent/"
    # The token is only in the header, never in a message.
    assert TOKEN not in json.dumps(hello)


@pytest.mark.asyncio
async def test_hello_welcome_then_report(server, agent) -> None:
    _, hello = await server.next()
    _, message = await server.next()

    assert set(hello) == {"type", "agent_version", "hostname", "os"}
    assert message["type"] == "report"
    # The welcome's enabled_checks narrowed the local list (all six) to two.
    assert message["report"]["updates"] == {"total": 3, "security": 1}
    assert message["report"]["disks"] == [{"mount": "/", "percent": 42}]
    assert message["report"]["containers"] is None


@pytest.mark.asyncio
async def test_server_cannot_enable_checks_the_local_config_disabled(server) -> None:
    server.welcome = {**WELCOME, "enabled_checks": ["updates", "disks", "containers"]}
    config = config_for(server)
    config.enabled_checks = ["disks"]
    task = asyncio.create_task(run_forever(config))
    try:
        await server.next()  # hello
        _, message = await server.next()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert message["report"]["disks"] is not None
    assert message["report"]["updates"] is None
    assert message["report"]["containers"] is None


@pytest.mark.asyncio
async def test_collect_triggers_a_report(server, agent) -> None:
    await server.next()  # hello
    await server.next()  # first report, sent right after welcome

    await server.connections[0].send(json.dumps({"type": "collect"}))

    _, message = await server.next(timeout=3)  # well before the 60s interval
    assert message["type"] == "report"


@pytest.mark.asyncio
async def test_unknown_messages_are_ignored(server, agent) -> None:
    await server.next()  # hello
    await server.next()  # first report
    ws = server.connections[0]

    for garbage in [
        json.dumps({"type": "exec", "cmd": "touch /tmp/pwned"}),
        json.dumps({"type": "collect", "cmd": "id"}),
        "not json",
    ]:
        await ws.send(garbage)
    await asyncio.sleep(0.2)

    assert server.received.empty()  # nothing triggered a report
    assert len(server.connections) == 1  # and the agent didn't hang up
    await ws.send(json.dumps({"type": "collect"}))
    _, message = await server.next()
    assert message["type"] == "report"


@pytest.mark.asyncio
async def test_reconnects_after_the_server_drops_the_connection(server, agent) -> None:
    await server.next()  # hello on connection 1
    await server.next()  # report on connection 1

    await server.connections[0].close(code=1011)

    number, hello = await server.next()
    assert (number, hello["type"]) == (2, "hello")


@pytest.mark.asyncio
async def test_no_welcome_in_time_reconnects(server, agent, monkeypatch) -> None:
    monkeypatch.setattr(connection, "HELLO_TIMEOUT_SECONDS", 0.1)
    server.welcome = None

    first = await server.next()
    second = await server.next()

    assert [first[0], second[0]] == [1, 2]
    assert [first[1]["type"], second[1]["type"]] == ["hello", "hello"]


@pytest.mark.asyncio
async def test_reconnects_when_the_server_is_down(server, monkeypatch) -> None:
    attempts = []
    monkeypatch.setattr(connection, "backoff", lambda a: attempts.append(a) or 0.01)
    config = AgentConfig(server_url="http://127.0.0.1:9", token=TOKEN)  # nothing listens there

    task = asyncio.create_task(run_forever(config))
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert attempts[:3] == [0, 1, 2]  # backoff keeps growing while it fails


@pytest.mark.asyncio
async def test_healthy_session_resets_the_backoff(server, monkeypatch) -> None:
    attempts = []
    monkeypatch.setattr(connection, "backoff", lambda a: attempts.append(a) or 0.01)
    monkeypatch.setattr(connection, "HEALTHY_SESSION_SECONDS", 0)
    task = asyncio.create_task(run_forever(config_for(server)))
    try:
        for n in range(3):
            await server.next()  # hello
            await server.next()  # report
            await server.connections[n].close()
        await server.next()  # hello on the 4th connection
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert attempts == [0, 0, 0]


@pytest.mark.asyncio
async def test_token_never_logged(server, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    task = asyncio.create_task(run_forever(config_for(server)))
    try:
        await server.next()
        await server.next()
        await server.connections[0].close(code=4410)
        await server.next()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # Only the agent's own loggers: the fake *server* logs the headers it receives.
    agent_log = "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("agent")
    )
    assert "Connected" in agent_log  # something was logged…
    assert TOKEN not in agent_log  # …but never the token, even at DEBUG level
    assert "code 4410" in agent_log


# --- python -m agent: SIGTERM closes cleanly and exits 0 -----------------------------


def test_sigterm_closes_the_socket_and_exits_0(tmp_path: Path) -> None:
    got_report, handler_done, main_done = threading.Event(), threading.Event(), threading.Event()
    close_codes = []

    def handler(ws) -> None:
        for raw in ws:
            message = json.loads(raw)
            if message["type"] == "hello":
                ws.send(json.dumps(WELCOME))
            elif message["type"] == "report":
                got_report.set()
        close_codes.append(ws.close_code)
        handler_done.set()

    def send_sigterm_after_first_report() -> None:
        # Only signal while main() is still running, so pytest itself is never hit.
        if got_report.wait(10) and not main_done.is_set():
            os.kill(os.getpid(), signal.SIGTERM)

    with sync_serve(handler, "127.0.0.1", 0) as ws_server:
        port = ws_server.socket.getsockname()[1]
        threading.Thread(target=ws_server.serve_forever, daemon=True).start()
        path = tmp_path / "agent.toml"
        path.write_text(
            f'server_url = "http://127.0.0.1:{port}"\ntoken = "{TOKEN}"\n', encoding="utf-8"
        )
        os.chmod(path, 0o600)
        threading.Thread(target=send_sigterm_after_first_report, daemon=True).start()

        try:
            code = main(["--config", str(path)])
        finally:
            main_done.set()
        assert handler_done.wait(5)

    assert code == 0
    assert close_codes == [1000]  # a normal, clean close
