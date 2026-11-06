"""Recording what happened to a host, a cluster or the network (the "While you were away"
list)."""

from collections.abc import Iterable
from typing import Any

from django.db import transaction

from activity.models import StatusEvent
from alerts.discord import notify
from clusters.models import Cluster
from hosts.models import Host
from network.models import Network
from rules.base import Finding

SUMMARY_MAX_LENGTH = StatusEvent._meta.get_field("summary").max_length


def summarize(issues: Iterable[Finding]) -> str:
    """'3 security updates, nginx container restarting', cut to fit the column."""
    summary = ", ".join(issue.text for issue in issues)
    if len(summary) > SUMMARY_MAX_LENGTH:
        summary = summary[: SUMMARY_MAX_LENGTH - 1] + "…"
    return summary


def record_note(network: Network, summary: str) -> StatusEvent:
    """An activity entry about the network that isn't a status change ("Router rebooted",
    "Public IP changed"). Old and new status are the same, so notify() never alerts on it."""
    return StatusEvent.objects.create(
        network=network,
        old_status=network.status,
        new_status=network.status,
        summary=summary[:SUMMARY_MAX_LENGTH],
    )


def record_status_change(
    subject: Host | Cluster | Network, old: str, new: str, issues: Iterable[Finding]
) -> StatusEvent:
    """Save a StatusEvent for a host's, a cluster's or the network's status change, then hand
    it to the alerts.

    The alert is sent only after the database transaction commits: the HTTP call
    to Discord (up to 5s) must not hold the database lock, and if the transaction
    rolls back there was no status change to announce.
    """
    if isinstance(subject, Network):
        owner: dict[str, Any] = {"network": subject}
    else:
        owner = {"cluster": subject} if isinstance(subject, Cluster) else {"host": subject}
    event = StatusEvent.objects.create(
        **owner, old_status=old, new_status=new, summary=summarize(issues)
    )
    transaction.on_commit(lambda: notify(event), robust=True)
    return event
