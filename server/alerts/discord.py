"""Discord webhook alerts.

Two rules keep this safe:
- SSRF guard: the server only ever POSTs to a Discord webhook URL. Without it, a user
  could save "http://192.168.1.1/admin" as their "webhook" and make the CronRack
  server send requests into the home network. The URL is checked when it is saved
  AND again right before sending (the database could hold an old or edited value).
- Never raise: an alert that fails is logged and dropped. A Discord outage must
  never break report ingestion or the scheduler.
"""

import http.client
import json
import logging
import re
import urllib.error
import urllib.request

from activity.models import StatusEvent
from prefs.models import Prefs

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5
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


def notify(event: StatusEvent) -> bool:
    """Send the alert for one status change, if the owner set a webhook.

    Called once per StatusEvent, and a StatusEvent only exists when the status
    really changed, so an alert never repeats. Returns True if a message was sent.
    """
    if event.old_status == event.new_status:
        return False
    prefs = Prefs.objects.filter(owner_id=event.host.owner_id).first()
    if prefs is None or not prefs.discord_webhook_url:
        return False
    return send_discord(prefs.discord_webhook_url, format_alert(event))
