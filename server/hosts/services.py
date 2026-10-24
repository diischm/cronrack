"""Things that change a host and are shared by the agent socket, views and scheduler."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

from hosts.humanize import duration
from hosts.models import Host
from rules.base import Finding, fingerprint
from rules.engine import (
    OFFLINE_RULE,
    apply_suppressions,
    compute_status,
    evaluate,
    load_overrides,
    load_suppressions,
)
from rules.registry import get_rule

# How often a connected agent reports (sent to it in the "welcome" message).
REPORT_INTERVAL_SECONDS = 60
# A report sooner than this after the previous one is "early": the agent socket
# ignores it (unless it answers a collect), and "Check now" doesn't ask for one.
MIN_REPORT_INTERVAL_SECONDS = 4

# WebSocket close codes the server uses (backend guide section 6).
CLOSE_INVALID_MESSAGE = 4400
CLOSE_HELLO_TIMEOUT = 4408
CLOSE_REPLACED = 4409
CLOSE_TOKEN_REVOKED = 4410  # token revoked or rotated, or host deleted


def host_group(host_id: int) -> str:
    """Channels group that holds the host's open agent connection(s)."""
    return f"host-{host_id}"


@dataclass(frozen=True)
class ReportResult:
    old_status: str
    new_status: str
    new_findings: list[Finding]  # active now, but not after the previous report
    findings: list[Finding]  # every active (not suppressed) finding


def _active_findings(host: Host, report: dict | None, now: datetime, overrides, suppressions):
    findings = evaluate(host, report, now, overrides=overrides)
    active, _suppressed = apply_suppressions(findings, host, now, suppressions=suppressions)
    return active


def save_report(host: Host, data: dict[str, Any], now: datetime | None = None) -> ReportResult:
    """Store a validated report, run the rule engine and store the new status.

    Everything happens in one transaction, so the report and the status it
    produced are always saved together. `data` must already have passed
    ReportSerializer; `host` must come from the agent's token, never from the report.
    """
    if now is None:
        now = timezone.now()
    with transaction.atomic():
        host = Host.objects.select_for_update().get(pk=host.pk)
        overrides = load_overrides(host)
        suppressions = load_suppressions(host)
        old_status = host.status

        # The previous report is re-evaluated (not stored as findings), so "new"
        # means: a problem that the last report didn't have.
        previous = set()
        if host.latest_report is not None:
            previous = {
                f.fingerprint
                for f in _active_findings(host, host.latest_report, now, overrides, suppressions)
            }

        host.latest_report = data
        host.report_received_at = now
        host.last_seen_at = now
        active = _active_findings(host, data, now, overrides, suppressions)
        host.status = compute_status(active, host, now)
        host.save(update_fields=["latest_report", "report_received_at", "last_seen_at", "status"])

    return ReportResult(
        old_status=old_status,
        new_status=host.status,
        new_findings=[f for f in active if f.fingerprint not in previous],
        findings=active,
    )


def _group_send(host: Host, message: dict[str, Any]) -> None:
    async_to_sync(get_channel_layer().group_send)(host_group(host.pk), message)


def just_reported(host: Host, now: datetime | None = None) -> bool:
    """True if the host's last report is fresher than MIN_REPORT_INTERVAL_SECONDS.

    "Check now" then skips the collect: the data on screen is already current.
    """
    if host.report_received_at is None:
        return False
    if now is None:
        now = timezone.now()
    return (now - host.report_received_at).total_seconds() < MIN_REPORT_INTERVAL_SECONDS


def request_collect(host: Host) -> None:
    """Ask the host's agent for a fresh report now (the "Check now" button).

    Only sends the fixed "collect" message; the agent decides what to run.
    """
    _group_send(host, {"type": "host.collect"})


def close_agent(host: Host, code: int = CLOSE_TOKEN_REVOKED) -> None:
    """Close the host's agent connection (token rotated or revoked, host deleted)."""
    _group_send(host, {"type": "host.close", "code": code})


def reset_connections() -> int:
    """On app start: no agent socket can be open yet, so no host is connected.

    Without this, a host whose socket was open when the server stopped would show
    "connected" until it reconnects (agents do that within seconds). Returns how many
    hosts were changed.
    """
    return Host.objects.filter(connected=True).update(connected=False, connection_id="")


# --- dead-man's switch (run by the check_hosts scheduler) -----------------------------

# A connected agent that sends nothing for this many report intervals is stuck.
STUCK_AFTER_INTERVALS = 3


def _offline_finding(host: Host, text: str) -> Finding:
    rule = get_rule(OFFLINE_RULE)
    return Finding(
        rule_id=rule.id,
        subject="",
        severity=rule.default_severity,
        text=text,
        why=rule.why,
        fix="",
        fingerprint=fingerprint(rule.id, host.pk, ""),
    )


def silence_reason(host: Host, now: datetime, silent_after_seconds: int) -> str | None:
    """Why this host counts as offline right now, or None if it doesn't.

    - not connected, and silent for longer than the user's threshold, or
    - connected, but no report for more than 3 report intervals (a stuck agent,
      or a "connected" flag left over from a server restart).
    A host that never reported is "waiting", never offline.
    """
    if host.last_seen_at is None or host.report_received_at is None:
        return None
    if not host.connected:
        silent = (now - host.last_seen_at).total_seconds()
        if silent > silent_after_seconds:
            return f"Last seen {duration(silent)} ago"
        return None
    quiet = (now - host.report_received_at).total_seconds()
    if quiet > STUCK_AFTER_INTERVALS * REPORT_INTERVAL_SECONDS:
        return f"Connected, but no report for {duration(quiet)}"
    return None


def mark_silent_hosts_offline(now: datetime | None = None) -> list[Host]:
    """Mark every host that stopped checking in as offline; record and alert each change.

    Hosts already offline, still waiting for their first report, or in maintenance
    are skipped, so each outage produces exactly one event and one alert. A host
    comes back from offline by itself when its next report is saved.
    """
    # Imported here: activity.events -> alerts -> prefs would otherwise load during
    # hosts app setup.
    from activity.events import record_status_change
    from prefs.models import Prefs

    if now is None:
        now = timezone.now()
    default_silent = Prefs._meta.get_field("silent_after_seconds").default
    silent_after = dict(Prefs.objects.values_list("owner_id", "silent_after_seconds"))

    candidates = Host.objects.exclude(
        status__in=[Host.Status.WAITING, Host.Status.OFFLINE, Host.Status.MAINTENANCE]
    ).values_list("pk", "owner_id")

    marked = []
    for host_id, owner_id in candidates:
        threshold = silent_after.get(owner_id, default_silent)
        with transaction.atomic():
            # Re-read under a lock: a report may have arrived since the query above.
            host = Host.objects.select_for_update().filter(pk=host_id).first()
            if host is None or host.status in (Host.Status.OFFLINE, Host.Status.WAITING):
                continue
            reason = silence_reason(host, now, threshold)
            if reason is None:
                continue
            old_status = host.status
            host.status = Host.Status.OFFLINE
            # An offline host is not connected, whatever the flag said (a stuck socket,
            # or a flag left over from a crash). If that socket is somehow still alive,
            # its next report sets the flag again (agents.consumers.ingest_report).
            host.connected = False
            host.save(update_fields=["status", "connected"])
            record_status_change(host, old_status, host.status, [_offline_finding(host, reason)])
        marked.append(host)
    return marked
