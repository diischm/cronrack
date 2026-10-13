"""Recording what happened to a host (the "While you were away" list)."""

from collections.abc import Iterable

from django.db import transaction

from activity.models import StatusEvent
from alerts.discord import notify
from hosts.models import Host
from rules.base import Finding

SUMMARY_MAX_LENGTH = StatusEvent._meta.get_field("summary").max_length


def summarize(issues: Iterable[Finding]) -> str:
    """'3 security updates, nginx container restarting', cut to fit the column."""
    summary = ", ".join(issue.text for issue in issues)
    if len(summary) > SUMMARY_MAX_LENGTH:
        summary = summary[: SUMMARY_MAX_LENGTH - 1] + "…"
    return summary


def record_status_change(host: Host, old: str, new: str, issues: Iterable[Finding]) -> StatusEvent:
    """Save a StatusEvent for a status change, then hand it to the alerts.

    The alert is sent only after the database transaction commits: the HTTP call
    to Discord (up to 5s) must not hold the database lock, and if the transaction
    rolls back there was no status change to announce.
    """
    event = StatusEvent.objects.create(
        host=host, old_status=old, new_status=new, summary=summarize(issues)
    )
    transaction.on_commit(lambda: notify(event), robust=True)
    return event
