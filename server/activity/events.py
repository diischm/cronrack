"""Recording what happened to a host (the "While you were away" list)."""

import logging
from collections.abc import Iterable

from activity.models import StatusEvent
from hosts.models import Host
from rules.base import Finding

logger = logging.getLogger(__name__)

SUMMARY_MAX_LENGTH = StatusEvent._meta.get_field("summary").max_length


def summarize(issues: Iterable[Finding]) -> str:
    """'3 security updates, nginx container restarting', cut to fit the column."""
    summary = ", ".join(issue.text for issue in issues)
    if len(summary) > SUMMARY_MAX_LENGTH:
        summary = summary[: SUMMARY_MAX_LENGTH - 1] + "…"
    return summary


def record_status_change(host: Host, old: str, new: str, issues: Iterable[Finding]) -> StatusEvent:
    """Save a StatusEvent for a status change, then hand it to the alerts."""
    event = StatusEvent.objects.create(
        host=host, old_status=old, new_status=new, summary=summarize(issues)
    )
    notify(event)
    return event


def notify(event: StatusEvent) -> None:
    """Stub: Discord alerts arrive in Phase 6 (alerts/discord.py)."""
    logger.debug(
        "Status change for host %s: %s -> %s", event.host_id, event.old_status, event.new_status
    )
