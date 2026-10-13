"""Things that change a host and are shared by the agent socket, views and scheduler."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

from hosts.models import Host
from rules.base import Finding
from rules.engine import (
    apply_suppressions,
    compute_status,
    evaluate,
    load_overrides,
    load_suppressions,
)

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


def request_collect(host: Host) -> None:
    """Ask the host's agent for a fresh report now (the "Check now" button).

    Only sends the fixed "collect" message; the agent decides what to run.
    """
    _group_send(host, {"type": "host.collect"})


def close_agent(host: Host, code: int = CLOSE_TOKEN_REVOKED) -> None:
    """Close the host's agent connection (token rotated or revoked, host deleted)."""
    _group_send(host, {"type": "host.close", "code": code})
