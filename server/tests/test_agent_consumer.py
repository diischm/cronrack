"""The agent WebSocket endpoint, tested through the real ASGI application.

WebSocketClient (tests/ws.py) plays the agent: it opens /ws/agent/ with a token header,
sends JSON and reads what the server sends back. The consumer talks to the
database from worker threads, so these tests need transaction=True.
"""

import pytest
import pytest_asyncio
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer

from activity.models import StatusEvent
from agents.consumers import ENABLED_CHECKS, AgentConsumer, bearer_token
from cronrack.asgi import application
from hosts.models import AgentToken, Host
from hosts.services import close_agent, request_collect

from .sample import make_report
from .ws import WebSocketClient

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

HELLO = {"type": "hello", "agent_version": "0.1.0", "hostname": "nas", "os": "Ubuntu 24.04"}
WELCOME = {"type": "welcome", "interval_seconds": 60, "enabled_checks": ENABLED_CHECKS}


@pytest_asyncio.fixture(autouse=True)
async def empty_channel_layer():
    """The in-memory layer is shared by the whole process; start every test empty."""
    await get_channel_layer().flush()
    yield
    await get_channel_layer().flush()


@pytest.fixture
def token(host):
    return AgentToken.issue(host)


def agent(token=None, *, header=None):
    headers = []
    if header is not None:
        headers.append((b"authorization", header))
    elif token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return WebSocketClient(application, "/ws/agent/", headers=headers)


async def connected_agent(token):
    """An agent that connected and finished the hello/welcome exchange."""
    comm = agent(token)
    connected, _ = await comm.connect()
    assert connected
    await comm.send_json_to(HELLO)
    assert await comm.receive_json_from() == WELCOME
    return comm


async def expect_close(comm, code):
    assert await comm.receive_output(timeout=2) == {"type": "websocket.close", "code": code}


@database_sync_to_async
def get_host(pk):
    return Host.objects.get(pk=pk)


# --- handshake -------------------------------------------------------------------


async def test_valid_token_connects_and_gets_welcome(host, token):
    comm = await connected_agent(token)

    fresh = await get_host(host.pk)
    assert fresh.connected is True
    assert fresh.last_seen_at is not None
    await comm.disconnect()


@pytest.mark.parametrize(
    "header",
    [
        None,  # no Authorization header at all
        b"Bearer not-a-real-token",
        b"Bearer ",
        b"Basic dXNlcjpwYXNz",
        b"Bearer \xff\xfe",
    ],
)
async def test_bad_token_is_rejected(host, token, header):
    comm = agent(header=header) if header is not None else agent()

    connected, _ = await comm.connect()

    assert connected is False
    assert (await get_host(host.pk)).connected is False


async def test_token_without_bearer_prefix_is_rejected(host, token):
    connected, _ = await agent(header=token.encode()).connect()

    assert connected is False


async def test_revoked_token_is_rejected(host, token):
    await database_sync_to_async(AgentToken.objects.filter(host=host).update)(revoked=True)

    connected, _ = await agent(token).connect()

    assert connected is False


async def test_scheme_is_case_insensitive(host, token):
    comm = agent(header=f"bearer {token}".encode())

    connected, _ = await comm.connect()

    assert connected
    await comm.disconnect()


async def test_bearer_token_parsing():
    assert bearer_token([(b"Authorization", b"Bearer abc")]) == "abc"
    assert bearer_token([(b"cookie", b"sessionid=abc")]) is None
    assert bearer_token([]) is None


async def test_session_cookie_is_not_enough(host, token, alice, client):
    """A logged-in browser's cookie must never open the agent socket."""
    await database_sync_to_async(client.force_login)(alice)
    cookie = f"sessionid={client.cookies['sessionid'].value}".encode()
    comm = WebSocketClient(application, "/ws/agent/", headers=[(b"cookie", cookie)])

    connected, _ = await comm.connect()

    assert connected is False


# --- hello -----------------------------------------------------------------------


async def test_no_hello_in_time_closes_4408(host, token, monkeypatch):
    monkeypatch.setattr(AgentConsumer, "hello_timeout", 0.05)
    comm = agent(token)
    connected, _ = await comm.connect()
    assert connected

    await expect_close(comm, 4408)
    await comm.disconnect()


async def test_hello_in_time_cancels_the_timer(host, token, monkeypatch):
    monkeypatch.setattr(AgentConsumer, "hello_timeout", 0.05)
    comm = await connected_agent(token)

    assert await comm.receive_nothing(timeout=0.2)
    await comm.disconnect()


@pytest.mark.parametrize(
    "message",
    [
        {"type": "hello"},  # fields missing
        {**HELLO, "os": 5},
        {**HELLO, "extra": "x"},
    ],
)
async def test_invalid_hello_closes_4400(host, token, message):
    comm = agent(token)
    await comm.connect()

    await comm.send_json_to(message)

    await expect_close(comm, 4400)
    await comm.disconnect()


async def test_second_hello_closes_4400(host, token):
    comm = await connected_agent(token)

    await comm.send_json_to(HELLO)

    await expect_close(comm, 4400)
    await comm.disconnect()


# --- one connection per host -------------------------------------------------------


async def test_new_connection_replaces_the_old_one(host, token):
    first = await connected_agent(token)

    second = await connected_agent(token)

    await expect_close(first, 4409)
    await first.disconnect()
    # The old connection going away must not mark the host disconnected.
    assert (await get_host(host.pk)).connected is True
    await second.disconnect()
    assert (await get_host(host.pk)).connected is False


# --- reports -----------------------------------------------------------------------


async def test_valid_report_updates_host_and_status(host, token):
    comm = await connected_agent(token)
    report = make_report(updates={"total": 3, "security": 2})

    await comm.send_json_to({"type": "report", "report": report})

    assert await comm.receive_nothing(timeout=0.2)  # no reply to a good report
    fresh = await get_host(host.pk)
    assert fresh.latest_report == report
    assert fresh.status == "red"
    event = await database_sync_to_async(StatusEvent.objects.get)(host=host)
    assert (event.old_status, event.new_status) == ("waiting", "red")
    assert event.summary == "2 security updates, 1 update pending"
    await comm.disconnect()


async def test_same_status_records_no_new_event(host, token):
    comm = await connected_agent(token)

    for _ in range(2):
        await comm.send_json_to({"type": "report", "report": make_report()})
        assert await comm.receive_nothing(timeout=0.2)

    assert await database_sync_to_async(StatusEvent.objects.count)() == 1  # waiting -> green
    await comm.disconnect()


async def test_invalid_report_is_rejected(host, token):
    comm = await connected_agent(token)
    bad = make_report(updates={"total": "3", "security": 0})

    await comm.send_json_to({"type": "report", "report": bad})

    assert await comm.receive_json_from() == {"type": "error", "code": "invalid_report"}
    fresh = await get_host(host.pk)
    assert fresh.latest_report is None
    assert fresh.status == "waiting"
    await comm.disconnect()


async def test_three_invalid_reports_close_4400(host, token):
    comm = await connected_agent(token)
    bad = {"type": "report", "report": make_report(extra_field=1)}

    for _ in range(2):
        await comm.send_json_to(bad)
        assert await comm.receive_json_from() == {"type": "error", "code": "invalid_report"}
    await comm.send_json_to(bad)

    await expect_close(comm, 4400)
    await comm.disconnect()


async def test_valid_report_resets_the_invalid_count(host, token):
    comm = await connected_agent(token)
    bad = {"type": "report", "report": make_report(extra_field=1)}

    for message in (bad, bad, {"type": "report", "report": make_report()}, bad, bad):
        await comm.send_json_to(message)
    for _ in range(4):
        assert await comm.receive_json_from() == {"type": "error", "code": "invalid_report"}

    assert await comm.receive_nothing(timeout=0.2)
    await comm.disconnect()


async def test_report_before_hello_closes_4400(host, token):
    comm = agent(token)
    await comm.connect()

    await comm.send_json_to({"type": "report", "report": make_report()})

    await expect_close(comm, 4400)
    assert (await get_host(host.pk)).latest_report is None
    await comm.disconnect()


@pytest.mark.parametrize(
    "send",
    [
        {"text_data": '{"type": "run", "cmd": "id"}'},
        {"text_data": '{"no_type": true}'},
        {"text_data": '["hello"]'},
        {"text_data": "not json"},
        {"bytes_data": b'{"type": "hello"}'},
    ],
)
async def test_unknown_or_malformed_message_closes_4400(host, token, send):
    comm = await connected_agent(token)

    await comm.send_to(**send)

    await expect_close(comm, 4400)
    await comm.disconnect()


async def test_token_for_host_a_never_updates_host_b(alice, bob, host, token):
    other = await database_sync_to_async(Host.objects.create)(owner=bob, name="victim")
    comm = await connected_agent(token)

    # The report claims to be the other host; the server only trusts the token.
    report = make_report(hostname="victim", failed_units=["x.service"])
    await comm.send_json_to({"type": "report", "report": report})
    assert await comm.receive_nothing(timeout=0.2)

    victim = await get_host(other.pk)
    assert victim.latest_report is None
    assert victim.status == "waiting"
    assert (await get_host(host.pk)).latest_report == report
    await comm.disconnect()


# --- messages from the server ---------------------------------------------------------


async def test_collect_reaches_the_agent(host, token):
    comm = await connected_agent(token)

    await database_sync_to_async(request_collect)(host)

    assert await comm.receive_json_from() == {"type": "collect"}
    await comm.disconnect()


async def test_collect_only_reaches_that_host(alice, host, token):
    other = await database_sync_to_async(Host.objects.create)(owner=alice, name="pi")
    comm = await connected_agent(token)

    await database_sync_to_async(request_collect)(other)

    assert await comm.receive_nothing(timeout=0.2)
    await comm.disconnect()


async def test_close_agent(host, token):
    comm = await connected_agent(token)

    await database_sync_to_async(close_agent)(host)

    await expect_close(comm, 4410)
    await comm.disconnect()
    assert (await get_host(host.pk)).connected is False


async def test_rotating_the_token_closes_the_socket(host, token):
    comm = await connected_agent(token)

    await database_sync_to_async(AgentToken.rotate)(host)

    await expect_close(comm, 4410)
    await comm.disconnect()


async def test_disconnect_marks_host_disconnected(host, token):
    comm = await connected_agent(token)

    await comm.disconnect()

    fresh = await get_host(host.pk)
    assert fresh.connected is False
    assert fresh.last_seen_at is not None


async def test_deleted_host_closes_4410(host, token):
    comm = await connected_agent(token)
    await database_sync_to_async(Host.objects.filter(pk=host.pk).delete)()

    await comm.send_json_to({"type": "report", "report": make_report()})

    await expect_close(comm, 4410)
    await comm.disconnect()
