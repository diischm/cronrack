"""The agent's WebSocket endpoint: wss://<server>/ws/agent/.

One agent = one open connection. The conversation (backend guide section 6):

    agent connects with "Authorization: Bearer <token>"  -> rejected unless the token is valid
    agent: hello   -> server: welcome {interval_seconds, enabled_checks}
    agent: report  -> server validates, saves, recomputes the host's status
    server: collect (when the user taps "Check now")      -> agent sends a report

The server only ever sends fixed messages (welcome, collect, and an error notice
for a rejected report). It never sends commands.

Close codes: 4400 invalid message, 4408 no hello in time, 4409 replaced by a newer
connection, 4410 token revoked or host deleted.
"""

import asyncio
import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db import transaction
from django.utils import timezone

from activity.events import record_status_change
from agents.serializers import HelloSerializer, ReportMessageSerializer
from hosts.models import AgentToken, Host
from hosts.services import (
    CLOSE_HELLO_TIMEOUT,
    CLOSE_INVALID_MESSAGE,
    CLOSE_REPLACED,
    CLOSE_TOKEN_REVOKED,
    host_group,
    save_report,
)
from rules.engine import top_issues

logger = logging.getLogger(__name__)

REPORT_INTERVAL_SECONDS = 60
# Check names as in the agent's config (agent/report.py REPORT_KEYS).
ENABLED_CHECKS = ["updates", "reboot", "containers", "units", "timers", "disks"]


def bearer_token(headers) -> str | None:
    """The token from an "Authorization: Bearer <token>" handshake header, or None."""
    for name, value in headers:
        if name.lower() != b"authorization":
            continue
        try:
            text = value.decode("ascii")
        except UnicodeDecodeError:
            return None
        scheme, _, token = text.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        return token
    return None


# --- database work (the ORM is synchronous, so each call runs in a worker thread) ---


@database_sync_to_async
def verify_token(raw: str) -> int | None:
    host = AgentToken.verify(raw)
    return None if host is None else host.pk


@database_sync_to_async
def mark_connected(host_id: int) -> bool:
    """connected=True after hello. False if the host no longer exists."""
    return Host.objects.filter(pk=host_id).update(connected=True, last_seen_at=timezone.now()) > 0


@database_sync_to_async
def mark_disconnected(host_id: int) -> None:
    Host.objects.filter(pk=host_id).update(connected=False, last_seen_at=timezone.now())


@database_sync_to_async
def ingest_report(host_id: int, report: dict) -> None:
    """Save the report and, if the status changed, record it. Raises Host.DoesNotExist."""
    with transaction.atomic():
        host = Host.objects.get(pk=host_id)
        result = save_report(host, report)
        if result.old_status != result.new_status:
            record_status_change(
                host, result.old_status, result.new_status, top_issues(result.findings)
            )


class AgentConsumer(AsyncJsonWebsocketConsumer):
    """One instance per agent connection.

    Authentication is the token ONLY. This route is deliberately not wrapped in
    AuthMiddlewareStack (or any session/cookie auth): browsers attach cookies to
    WebSocket handshakes automatically, even when a *different* website opens the
    socket. If this endpoint trusted the login cookie, any web page the user visits
    could open /ws/agent/ in their name and send fake reports. That attack is
    called cross-site WebSocket hijacking. A bearer token is never sent
    automatically by a browser, so it can't be hijacked that way.
    """

    hello_timeout = 10  # seconds; tests make this shorter
    max_invalid_reports = 3

    # --- connection lifecycle ------------------------------------------------

    async def connect(self):
        self.host_id = None
        self.hello_received = False
        self.replaced = False
        self.closing = False
        self.invalid_reports = 0
        self.hello_timer = None

        token = bearer_token(self.scope.get("headers", []))
        host_id = await verify_token(token) if token else None
        if host_id is None:
            # close() before accept() rejects the handshake: the client gets
            # HTTP 403 and no socket is ever opened.
            await self.close()
            return

        self.host_id = host_id
        self.group = host_group(host_id)
        await self.accept()
        await self.channel_layer.group_add(self.group, self.channel_name)
        # Only one connection per host: tell any older one to go away.
        await self.channel_layer.group_send(
            self.group,
            {"type": "host.close", "code": CLOSE_REPLACED, "sender": self.channel_name},
        )
        self.hello_timer = asyncio.create_task(self._close_without_hello())

    async def _close_without_hello(self):
        await asyncio.sleep(self.hello_timeout)
        if not self.hello_received:
            logger.info("Host %s: no hello within %ss", self.host_id, self.hello_timeout)
            await self._close(CLOSE_HELLO_TIMEOUT)

    async def disconnect(self, code):
        if self.hello_timer is not None:
            self.hello_timer.cancel()
        if self.host_id is None:
            return  # rejected handshake: nothing to clean up
        await self.channel_layer.group_discard(self.group, self.channel_name)
        # A replaced connection leaves the host alone: the new one is live.
        if not self.replaced:
            await mark_disconnected(self.host_id)

    async def _close(self, code: int):
        self.closing = True
        await self.close(code=code)

    # --- messages from the agent ---------------------------------------------

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        """Parse JSON ourselves so bad input closes with 4400 instead of crashing."""
        if self.closing:
            return
        if text_data is None:
            await self._close(CLOSE_INVALID_MESSAGE)
            return
        try:
            content = json.loads(text_data)
        except (ValueError, RecursionError):
            await self._close(CLOSE_INVALID_MESSAGE)
            return
        await self.receive_json(content)

    async def receive_json(self, content, **kwargs):
        message_type = content.get("type") if isinstance(content, dict) else None
        if message_type == "hello":
            await self.handle_hello(content)
        elif message_type == "report":
            await self.handle_report(content)
        else:
            await self._close(CLOSE_INVALID_MESSAGE)

    async def handle_hello(self, message):
        serializer = HelloSerializer(data=message)
        if self.hello_received or not serializer.is_valid():
            await self._close(CLOSE_INVALID_MESSAGE)
            return
        self.hello_received = True
        if self.hello_timer is not None:
            self.hello_timer.cancel()
        if not await mark_connected(self.host_id):
            await self._close(CLOSE_TOKEN_REVOKED)
            return
        await self.send_json(
            {
                "type": "welcome",
                "interval_seconds": REPORT_INTERVAL_SECONDS,
                "enabled_checks": ENABLED_CHECKS,
            }
        )

    async def handle_report(self, message):
        if not self.hello_received:
            await self._close(CLOSE_INVALID_MESSAGE)
            return
        serializer = ReportMessageSerializer(data=message)
        if not serializer.is_valid():
            self.invalid_reports += 1
            # Log the field errors (not the data) to help debug a broken agent.
            logger.warning(
                "Host %s sent an invalid report: %s", self.host_id, str(serializer.errors)[:500]
            )
            if self.invalid_reports >= self.max_invalid_reports:
                await self._close(CLOSE_INVALID_MESSAGE)
            else:
                await self.send_json({"type": "error", "code": "invalid_report"})
            return
        self.invalid_reports = 0
        try:
            await ingest_report(self.host_id, serializer.validated_data["report"])
        except Host.DoesNotExist:
            await self._close(CLOSE_TOKEN_REVOKED)

    # --- messages from the rest of the server (channel layer group) ----------

    async def host_collect(self, event):
        """hosts.services.request_collect(): ask the agent for a report now."""
        if self.hello_received and not self.closing:
            await self.send_json({"type": "collect"})

    async def host_close(self, event):
        """hosts.services.close_agent(), or a newer connection replacing this one."""
        if event.get("sender") == self.channel_name:
            return  # our own "replace older connections" message
        code = event.get("code")
        if not isinstance(code, int) or not 4000 <= code <= 4999:
            code = CLOSE_TOKEN_REVOKED
        if code == CLOSE_REPLACED:
            self.replaced = True
        await self._close(code)
