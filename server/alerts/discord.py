"""Discord webhook transport: the only code that talks to Discord.

What to send and when is decided in notifications.dispatch (one message per problem).
This module keeps the three safety rules:
- SSRF guard: the server only ever POSTs to a Discord webhook URL. Without it, a user
  could save "http://192.168.1.1/admin" as their "webhook" and make the CronRack
  server send requests into the home network. The URL is checked when it is saved
  AND again right before sending (the database could hold an old or edited value).
- Never raise: an alert that fails is logged and dropped. A Discord outage must
  never break report ingestion or the scheduler.
- Cooldown: at most one message per host, cluster or network per ALERT_COOLDOWN_SECONDS
  (claim_cooldown). A flapping host or a misbehaving agent can't flood the channel.
  Nothing is lost: what is held back stays in the outbox (notifications.FindingState) and
  the scheduler sends it, in one message, once the cooldown is over.
"""

import http.client
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import timedelta

from django.db.models import Q

from cronrack.outbound import opener as _opener

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5
# At most one message per host (cluster, network) in this many seconds.
ALERT_COOLDOWN_SECONDS = 60
# Discord rejects messages longer than this.
MAX_CONTENT_LENGTH = 2000

# Exactly https://discord.com/api/webhooks/<id>/<token> (or discordapp.com, the old
# domain). Anchored at both ends, so lookalikes such as "discord.com.evil.net",
# "discord.com@evil.net", "http://", extra ports or query strings never match.
WEBHOOK_URL_RE = re.compile(
    r"^https://(?:discord\.com|discordapp\.com)/api/webhooks/[0-9]{1,30}/[A-Za-z0-9_-]{1,200}\Z"
)


def is_allowed_webhook(url) -> bool:
    """True only for a Discord webhook URL (the SSRF allowlist)."""
    return isinstance(url, str) and WEBHOOK_URL_RE.match(url) is not None


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


def claim_cooldown(model, pk: int, now) -> bool:
    """True if a message about this host, cluster or network may go out now; then the
    cooldown starts.

    One UPDATE that only matches when allowed, so the web app and the scheduler
    (separate processes) can't both send within the cooldown.
    """
    cutoff = now - timedelta(seconds=ALERT_COOLDOWN_SECONDS)
    return (
        model.objects.filter(pk=pk)
        .filter(Q(last_alert_at__isnull=True) | Q(last_alert_at__lte=cutoff))
        .update(last_alert_at=now)
        > 0
    )
