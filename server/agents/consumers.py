"""The agent's WebSocket endpoint: wss://<server>/ws/agent/.

One agent = one open connection. The conversation (backend guide section 6):

    agent connects with "Authorization: Bearer <token>"  -> rejected unless the token is valid
    agent: hello   -> server: welcome {interval_seconds, enabled_checks}
    agent: report  -> server validates, saves, recomputes the host's status
    server: collect (when the user taps "Check now")      -> agent sends a report

The server only ever sends fixed messages (welcome, collect, and an error notice
for a rejected report). It never sends commands.

Close codes: 4400 invalid message (or too many reports too fast), 4408 no hello in
time, 4409 replaced by a newer connection, 4410 token revoked or host deleted.
"""

import asyncio
import functools
import json
import logging
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.settings import api_settings

from activity.events import record_status_change
from agents.serializers import HelloSerializer, ReportMessageSerializer
from clusters.services import save_cluster_snapshot, split_report
from hosts.models import AgentToken, Host
from hosts.services import (
    CLOSE_HELLO_TIMEOUT,
    CLOSE_INVALID_MESSAGE,
    CLOSE_REPLACED,
    CLOSE_TOKEN_REVOKED,
    MIN_REPORT_INTERVAL_SECONDS,
    REPORT_INTERVAL_SECONDS,
    host_group,
    save_report,
)
from rules.engine import top_issues

logger = logging.getLogger(__name__)

# Check names as in the agent's config (agent/report.py REPORT_KEYS).
# "kubernetes" tells a 0.2.0 agent that it may send the key; an older server never lists it.
ENABLED_CHECKS = ["updates", "reboot", "containers", "units", "timers", "disks", "kubernetes"]


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


MAX_LOGGED_ERRORS = 10


def _field_names(field) -> set[str]:
    """Every field name declared in a serializer, its nested serializers and list items."""
    names: set[str] = set()
    child = getattr(field, "child", None)  # ListField / many=True
    if child is not None:
        names |= _field_names(child)
    if isinstance(field, serializers.Serializer):
        for name, nested in field.fields.items():
            names.add(name)
            names |= _field_names(nested)
    return names


@functools.cache
def declared_field_names() -> frozenset[str]:
    names = _field_names(ReportMessageSerializer()) | {api_settings.NON_FIELD_ERRORS_KEY}
    return frozenset(names)


def error_summary(errors, path: str = "") -> list[str]:
    """'report.updates.total: invalid' lines for a serializer's errors, without the data.

    DRF error messages can repeat what was sent ('"x" is not a valid choice'), and an
    unknown key the agent sent comes back as an error key, so only the error codes and
    the path are kept, and a path shows only field names the serializers declare. List
    positions become [n]; any other key (unknown, or a name from the report) becomes "?".
    """
    lines: list[str] = []
    if isinstance(errors, dict):
        for key, value in errors.items():
            if isinstance(key, int):  # position in a list (many=True)
                lines += error_summary(value, f"{path}[{key}]")
                continue
            name = key if key in declared_field_names() else "?"
            lines += error_summary(value, f"{path}.{name}" if path else name)
    elif isinstance(errors, list) and errors and all(isinstance(e, str) for e in errors):
        codes = sorted({getattr(e, "code", None) or "invalid" for e in errors})
        lines.append(f"{path or 'message'}: {','.join(codes)}")
    elif isinstance(errors, list):
        for index, value in enumerate(errors):
            lines += error_summary(value, f"{path}[{index}]")
    else:
        lines.append(f"{path or 'message'}: {getattr(errors, 'code', None) or 'invalid'}")
    return lines[:MAX_LOGGED_ERRORS]


# --- database work (the ORM is synchronous, so each call runs in a worker thread) ---


@database_sync_to_async
def verify_token(raw: str) -> int | None:
    host = AgentToken.verify(raw)
    return None if host is None else host.pk


@database_sync_to_async
def mark_connected(host_id: int, connection_id: str) -> bool:
    """connected=True after hello, and this socket becomes the host's active one.

    False if the host no longer exists.
    """
    return (
        Host.objects.filter(pk=host_id).update(
            connected=True, connection_id=connection_id, last_seen_at=timezone.now()
        )
        > 0
    )


@database_sync_to_async
def mark_disconnected(host_id: int, connection_id: str) -> None:
    """connected=False, but only if this socket is still the host's active one.

    When an agent reconnects, the old socket may close after the new one already said
    hello; that late close must not mark the live connection as gone.
    """
    Host.objects.filter(pk=host_id, connection_id=connection_id).update(
        connected=False, connection_id="", last_seen_at=timezone.now()
    )


@database_sync_to_async
def ingest_report(host_id: int, report: dict, connection_id: str) -> None:
    """Save the report and, if the status changed, record it. Raises Host.DoesNotExist.

    The host keeps its own part of the report; the Kubernetes cluster view (if any) goes
    to its Cluster, in the same transaction.
    """
    with transaction.atomic():
        host = Host.objects.get(pk=host_id)
        host_report, cluster_view = split_report(report)
        result = save_report(host, host_report)
        if result.old_status != result.new_status:
            record_status_change(
                host, result.old_status, result.new_status, top_issues(result.findings)
            )
        save_cluster_snapshot(host, cluster_view)
        # The scheduler clears `connected` when it marks a host offline. If this
        # (still active) socket reports again after all, it is connected after all.
        Host.objects.filter(pk=host_id, connection_id=connection_id, connected=False).update(
            connected=True
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
    # Agents report every 60 s, or right away when asked to collect. A report sooner
    # than this after the last accepted one is "early" and ignored (no validation, no
    # database work), unless it answers a "collect" we sent. max_early_reports early
    # reports in a row, with no accepted report in between, close the socket (4400).
    min_report_interval = MIN_REPORT_INTERVAL_SECONDS  # tests set 0 to send quickly
    max_early_reports = 10

    # --- connection lifecycle ------------------------------------------------

    async def connect(self):
        self.host_id = None
        self.hello_received = False
        self.closing = False
        self.invalid_reports = 0
        self.early_reports = 0  # early reports since the last accepted one
        self.last_report_at = None  # time.monotonic() of the last accepted report
        # True after we sent "collect" until a report arrives: that report is the
        # answer the user asked for, so it is never early. (The agent merges several
        # collects into one report and never reports more often than every 5 s.)
        self.collect_pending = False
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
        # Only clears `connected` if no newer socket has taken over (see mark_disconnected).
        await mark_disconnected(self.host_id, self.channel_name)

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
        if not await mark_connected(self.host_id, self.channel_name):
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
        now = time.monotonic()
        early = (
            self.last_report_at is not None
            and now - self.last_report_at < self.min_report_interval
            and not self.collect_pending
        )
        if early:
            self.early_reports += 1
            if self.early_reports >= self.max_early_reports:
                logger.warning("Host %s: too many reports too fast, closing", self.host_id)
                await self._close(CLOSE_INVALID_MESSAGE)
            return
        serializer = ReportMessageSerializer(data=message)
        if not serializer.is_valid():
            self.invalid_reports += 1
            # Log where and what kind of error (to help debug a broken agent), never
            # the data: see error_summary.
            logger.warning(
                "Host %s sent an invalid report: %s",
                self.host_id,
                "; ".join(error_summary(serializer.errors)),
            )
            if self.invalid_reports >= self.max_invalid_reports:
                await self._close(CLOSE_INVALID_MESSAGE)
            else:
                await self.send_json({"type": "error", "code": "invalid_report"})
            return
        self.invalid_reports = 0
        try:
            await ingest_report(
                self.host_id, serializer.validated_data["report"], self.channel_name
            )
        except Host.DoesNotExist:
            await self._close(CLOSE_TOKEN_REVOKED)
            return
        self.last_report_at = now
        self.early_reports = 0
        self.collect_pending = False

    # --- messages from the rest of the server (channel layer group) ----------

    async def host_collect(self, event):
        """hosts.services.request_collect(): ask the agent for a report now."""
        if self.hello_received and not self.closing:
            self.collect_pending = True
            await self.send_json({"type": "collect"})

    async def host_close(self, event):
        """hosts.services.close_agent(), or a newer connection replacing this one."""
        if event.get("sender") == self.channel_name:
            return  # our own "replace older connections" message
        code = event.get("code")
        if not isinstance(code, int) or not 4000 <= code <= 4999:
            code = CLOSE_TOKEN_REVOKED
        await self._close(code)
