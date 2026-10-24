"""Discord webhook alerts.

Three rules keep this safe:
- SSRF guard: the server only ever POSTs to a Discord webhook URL. Without it, a user
  could save "http://192.168.1.1/admin" as their "webhook" and make the CronRack
  server send requests into the home network. The URL is checked when it is saved
  AND again right before sending (the database could hold an old or edited value).
- Never raise: an alert that fails is logged and dropped. A Discord outage must
  never break report ingestion or the scheduler.
- Cooldown: at most one alert per host per ALERT_COOLDOWN_SECONDS. A host flapping
  between green and red (or a misbehaving agent) can't flood the channel. Changes
  during the cooldown are coalesced, not lost: Host.last_alert_status remembers what
  the last alert said, and when the cooldown is over the scheduler sends one alert
  with the CURRENT status if it differs (send_pending_alerts). So the last alert
  always matches reality. Every change is also in the activity list.
"""

import http.client
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from activity.models import StatusEvent
from hosts.models import Host
from prefs.models import Prefs

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5
# At most one alert per host in this many seconds.
ALERT_COOLDOWN_SECONDS = 60
# Discord rejects messages longer than this.
MAX_CONTENT_LENGTH = 2000

# Exactly https://discord.com/api/webhooks/<id>/<token> (or discordapp.com, the old
# domain). Anchored at both ends, so lookalikes such as "discord.com.evil.net",
# "discord.com@evil.net", "http://", extra ports or query strings never match.
WEBHOOK_URL_RE = re.compile(
    r"^https://(?:discord\.com|discordapp\.com)/api/webhooks/[0-9]{1,30}/[A-Za-z0-9_-]{1,200}\Z"
)

STATUS_EMOJI = {"red": "🔴", "offline": "🔴", "yellow": "🟡", "green": "🟢"}


def is_allowed_webhook(url) -> bool:
    """True only for a Discord webhook URL (the SSRF allowlist)."""
    return isinstance(url, str) and WEBHOOK_URL_RE.match(url) is not None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, so a reply can't send the request on to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def send_discord(url: str, text: str) -> bool:
    """POST one message to a Discord webhook. Returns True if Discord accepted it.

    Never raises; failures are logged without the URL (the URL contains the
    webhook's secret token).
    """
    if not is_allowed_webhook(url):
        logger.warning("Discord alert not sent: the webhook URL is not a Discord webhook URL")
        return False
    payload = {
        "content": text[:MAX_CONTENT_LENGTH],
        # Host data can contain "@everyone"; never let a message ping anyone.
        "allowed_mentions": {"parse": []},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "CronRack"},
        method="POST",
    )
    try:
        # B310 reviewed: the URL matched WEBHOOK_URL_RE just above (https, Discord only),
        # so file:// and other schemes can't reach this call; redirects are refused.
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            ok = 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        logger.warning("Discord alert not sent: HTTP %s", exc.code)
        return False
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # Timeouts, DNS, TLS, connection resets: an alert must never crash the caller.
        logger.warning("Discord alert not sent: %s", type(exc).__name__)
        return False
    if not ok:
        logger.warning("Discord alert not sent: HTTP %s", response.status)
    return ok


def format_alert(event: StatusEvent) -> str:
    """'🔴 pi-01 went red: 2 security updates'"""
    emoji = STATUS_EMOJI.get(event.new_status, "⚪")
    return f"{emoji} {event.describe()}"


def _cooldown_cutoff(now):
    return now - timedelta(seconds=ALERT_COOLDOWN_SECONDS)


def _claim_alert(host_id: int, status: str, now) -> bool:
    """True if an alert saying `status` may go out now; then it starts the cooldown.

    False while the host cools down (the change stays pending: last_alert_status
    differs from the host's status), or if the last alert already said `status`.
    One UPDATE that only matches when allowed, so the web app and the scheduler
    (separate processes) can't both send within the cooldown.
    """
    return (
        Host.objects.filter(pk=host_id)
        .filter(Q(last_alert_at__isnull=True) | Q(last_alert_at__lte=_cooldown_cutoff(now)))
        .exclude(last_alert_status=status)
        .update(last_alert_at=now, last_alert_status=status)
        > 0
    )


def notify(event: StatusEvent) -> bool:
    """Send the alert for one status change, if the owner set a webhook.

    Called once per StatusEvent, and a StatusEvent only exists when the status
    really changed, so an alert never repeats. Within the host's cooldown nothing is
    sent now; send_pending_alerts() sends the then-current status afterwards.
    Returns True if a message was sent.
    """
    if event.old_status == event.new_status:
        return False
    prefs = Prefs.objects.filter(owner_id=event.host.owner_id).first()
    if prefs is None or not prefs.discord_webhook_url:
        return False
    if not _claim_alert(event.host_id, event.new_status, timezone.now()):
        logger.info("Discord alert for host %s held back: cooldown", event.host_id)
        return False
    return send_discord(prefs.discord_webhook_url, format_alert(event))


def format_current(host: Host) -> str:
    """'🟢 pi-01 is green now (after several quick changes)'"""
    emoji = STATUS_EMOJI.get(host.status, "⚪")
    return f"{emoji} {host.name} is {host.status} now (after several quick changes)"


def send_pending_alerts(now=None) -> list[Host]:
    """Run by the scheduler: catch up on changes that happened during a cooldown.

    For every host whose cooldown is over and whose status differs from what its last
    alert said, send one alert with the current status. Hosts that never alerted are
    left alone (nothing was held back for them). Returns the hosts alerted.
    """
    if now is None:
        now = timezone.now()
    webhooks = dict(
        Prefs.objects.exclude(discord_webhook_url="").values_list("owner_id", "discord_webhook_url")
    )
    candidates = (
        Host.objects.filter(owner_id__in=webhooks, last_alert_at__lte=_cooldown_cutoff(now))
        .exclude(status=F("last_alert_status"))
        .values_list("pk", flat=True)
    )
    sent = []
    for host_id in candidates:
        # Re-read and claim under the write lock: a report or notify() may have
        # changed the host since the query above.
        with transaction.atomic():
            host = Host.objects.select_for_update().filter(pk=host_id).first()
            if (
                host is None
                or host.last_alert_at is None
                or host.last_alert_at > _cooldown_cutoff(now)
                or host.status == host.last_alert_status
            ):
                continue
            host.last_alert_at = now
            host.last_alert_status = host.status
            host.save(update_fields=["last_alert_at", "last_alert_status"])
        # The HTTP call happens after the transaction, never while holding the lock.
        send_discord(webhooks[host.owner_id], format_current(host))
        sent.append(host)
    return sent
