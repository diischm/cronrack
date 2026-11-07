"""The daily summary: one Discord message a day with the "show only" findings.

Run by the scheduler every pass; sends at most once per user per local day, at or after
the user's digest_time (in their own time zone). Skipped when there is nothing to list.
"""

from datetime import datetime, timedelta

from django.utils import timezone

from alerts import discord
from hosts.humanize import user_zone
from notifications import text as words
from notifications.models import FindingState
from notifications.rules import GROUPS, SHOW, SPECS

WINDOW = timedelta(hours=24)


def _items(owner_id: int, now: datetime) -> list[tuple[str, list[tuple[str, str, bool]]]]:
    rows = (
        FindingState.objects.filter(owner_id=owner_id, level=SHOW, last_seen_at__gte=now - WINDOW)
        .select_related("host", "cluster", "network")
        .order_by("pk")
    )
    grouped: dict[str, list[tuple[str, str, bool]]] = {g: [] for g in GROUPS}
    for row in rows:
        spec = SPECS.get(row.rule_id)
        if spec is None:
            continue
        subject = row.host or row.cluster or row.network
        grouped[spec.group].append((subject.name, row.text, row.resolved_at is not None))
    return [(group, items) for group, items in grouped.items() if items]


def send_daily_summaries(now: datetime | None = None) -> int:
    """Returns how many summaries went out."""
    from prefs.models import Prefs

    if now is None:
        now = timezone.now()
    sent = 0
    for prefs in Prefs.objects.filter(digest_enabled=True).exclude(discord_webhook_url=""):
        local = now.astimezone(user_zone(prefs.timezone))
        if local.time() < prefs.digest_time or prefs.digest_sent_on == local.date():
            continue
        # Claim today's summary with one UPDATE, so two processes can't both send it.
        claimed = (
            Prefs.objects.filter(pk=prefs.pk)
            .exclude(digest_sent_on=local.date())
            .update(digest_sent_on=local.date())
        )
        if not claimed:
            continue
        groups = _items(prefs.owner_id, now)
        if not groups:
            continue
        discord.send_discord(prefs.discord_webhook_url, words.summary_message(groups))
        sent += 1
    return sent
