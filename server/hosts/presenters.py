"""Turn database rows into the plain dicts the templates and JSON endpoints use.

The shapes follow FRONTEND-HANDOFF.md section 6.2. The overview page and
/api/v1/overview.json share overview_context(), so they can't drift apart.

Every value here may contain text from an agent (container names, unit names...).
It is only ever output with {{ }} (auto-escaped) or as JSON, never marked safe.
"""

from datetime import UTC, datetime
from typing import Any

from django.urls import reverse

from activity.models import StatusEvent
from hosts.humanize import ago, clock, day, user_zone
from hosts.models import Host
from prefs.models import Prefs
from rules.base import Finding
from rules.engine import (
    OFFLINE_RULE,
    apply_suppressions,
    effective_status,
    evaluate,
    sort_hosts,
    top_issues,
)
from rules.models import RuleOverride, Suppression
from rules.registry import get_rule

STATUS_LABELS = {
    "red": "Needs you",
    "yellow": "Heads up",
    "green": "All good",
    "offline": "Offline",
    "waiting": "Waiting",
    "maintenance": "Maintenance",
}
NEEDS_ATTENTION = frozenset({"red", "yellow", "offline"})
OVERVIEW_ACTIVITY = 3


class OwnerData:
    """The owner's prefs, overrides and suppressions, loaded once per request."""

    def __init__(self, owner) -> None:
        self.prefs = Prefs.objects.filter(owner=owner).first() or Prefs(owner=owner)
        self.tz = user_zone(self.prefs.timezone)
        self.overrides = list(RuleOverride.objects.filter(owner=owner))
        self.suppressions = list(Suppression.objects.filter(owner=owner))

    def status(self, host: Host, now: datetime) -> str:
        """What the UI shows: offline is worked out now, not only by the scheduler."""
        return effective_status(host, now, self.prefs.silent_after_seconds)


def active_findings(host: Host, now: datetime, data: OwnerData) -> list[Finding]:
    """The host's current problems, after overrides and suppressions.

    The connectivity.offline finding is never listed: whether a host is offline comes
    from OwnerData.status, and the UI says it in the host's "Offline · last report …"
    line, so an issue line would only repeat it.
    """
    if host.latest_report is None:
        return []
    findings = evaluate(host, host.latest_report, now, overrides=data.overrides)
    active, _ = apply_suppressions(findings, host, now, suppressions=data.suppressions)
    return [f for f in active if f.rule_id != OFFLINE_RULE]


def issue_dict(finding: Finding) -> dict[str, Any]:
    return {
        "fingerprint": finding.fingerprint,
        "rule_id": finding.rule_id,
        "subject": finding.subject,
        "text": finding.text,
        "severity": finding.severity,
        "why": finding.why,
        "fix": finding.fix,
        "category": get_rule(finding.rule_id).category,
    }


def _problem_containers(findings: list[Finding]) -> set[str]:
    """Names of containers that have an active red or yellow docker.* finding."""
    return {
        f.subject.removeprefix("container:")
        for f in findings
        if f.rule_id.startswith("docker.") and f.severity in ("red", "yellow")
    }


def iso_utc(when: datetime | None) -> str | None:
    """A stable machine-readable time ("2026-10-15T08:00:00+00:00"), or None."""
    return when.astimezone(UTC).isoformat() if when else None


def _running(containers: list[dict]) -> int:
    return sum(1 for c in containers if c["state"] == "running")


def _card_numbers(report: dict | None, findings: list[Finding]) -> dict[str, Any]:
    """The numbers in a host card's detail strip. A check the host didn't report stays None."""
    numbers: dict[str, Any] = {"updates": None, "containers": None, "disk_max_percent": None}
    if report is None:
        return numbers
    if report.get("updates") is not None:
        numbers["updates"] = {
            "total": report["updates"]["total"],
            "security": report["updates"]["security"],
        }
    containers = report.get("containers")
    if containers is not None:
        failing = _problem_containers(findings)
        numbers["containers"] = {
            "total": len(containers),
            "running": _running(containers),
            "failing": sum(1 for c in containers if c["name"] in failing),
        }
    if report.get("disks"):
        numbers["disk_max_percent"] = max(d["percent"] for d in report["disks"])
    return numbers


def host_summary(host: Host, now: datetime, data: OwnerData) -> dict[str, Any]:
    """One host card on the overview."""
    status = data.status(host, now)
    findings = active_findings(host, now, data)
    issues = top_issues(findings)
    return {
        "id": host.pk,
        "name": host.name,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "issues": [{"text": f.text, "severity": f.severity} for f in issues],
        "last_seen_human": ago(host.last_seen_at, now),
        "url": reverse("host-detail", args=[host.pk]),
        **_card_numbers(host.latest_report, findings),
    }


def activity_entry(event: StatusEvent, now: datetime, data: OwnerData, with_day=False):
    entry = {
        "text": event.describe(),
        "time_human": clock(event.created_at, data.tz),
        "status": event.new_status,
    }
    if with_day:
        entry["day"] = day(event.created_at, now, data.tz)
    return entry


def overview_context(user, now: datetime) -> dict[str, Any]:
    data = OwnerData(user)
    owned = list(Host.objects.filter(owner=user))
    status_of = {h.pk: data.status(h, now) for h in owned}
    hosts = sort_hosts(owned, status_of=lambda h: status_of[h.pk])
    newest = max((h.last_seen_at for h in hosts if h.last_seen_at), default=None)
    events = StatusEvent.objects.filter(host__owner=user).select_related("host")
    statuses = list(status_of.values())
    return {
        "attention_count": sum(1 for s in statuses if s in NEEDS_ATTENTION),
        "updated_human": ago(newest, now),
        # The desktop summary tiles. An offline host has an unlit lamp, so it isn't
        # counted as red; the "Hosts online" tile shows it instead.
        "status_counts": {
            "red": statuses.count("red"),
            "yellow": statuses.count("yellow"),
            "green": statuses.count("green"),
            "offline": statuses.count("offline"),
        },
        "hosts_online": sum(1 for h in hosts if h.connected),
        "hosts": [host_summary(h, now, data) for h in hosts],
        "activity": [activity_entry(e, now, data) for e in events[:OVERVIEW_ACTIVITY]],
    }


def _container_sections(report: dict, findings: list[Finding]):
    containers = report.get("containers")
    if containers is None:
        return None
    problem_names = _problem_containers(findings)
    problems, healthy = [], []
    for c in containers:
        if c["name"] in problem_names:
            problems.append({"name": c["name"], "state": c["state"], "status": c["status"]})
        else:
            healthy.append({"name": c["name"], "status": c["status"]})
    return {"problems": problems, "healthy": healthy}


def _disk_rows(report: dict, findings: list[Finding]):
    disks = report.get("disks")
    if disks is None:
        return None
    levels = {f.subject: f.severity for f in findings if f.rule_id == "disk.usage"}
    return [
        {
            "mount": d["mount"],
            "percent": d["percent"],
            "level": levels.get(f"mount:{d['mount']}", "green"),
        }
        for d in disks
    ]


def detail_context(host: Host, now: datetime) -> dict[str, Any]:
    data = OwnerData(host.owner)
    status = data.status(host, now)
    findings = active_findings(host, now, data)
    report = host.latest_report
    context: dict[str, Any] = {
        "host": {
            "id": host.pk,
            "name": host.name,
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "last_seen_human": ago(host.last_seen_at, now),
            "has_report": report is not None,
            # From the agent's last report (validated, at most 32 characters); None
            # until the first one arrives. The template escapes it like any agent data.
            "agent_version": report.get("agent_version") if report else None,
        },
        "issues": [issue_dict(f) for f in top_issues(findings, n=len(findings))],
        "updates": None,
        "containers": None,
        "failed_units": None,
        "timers": None,
        "disks": None,
    }
    if report is None:
        return context
    if report.get("updates") is not None:
        context["updates"] = {
            "total": report["updates"]["total"],
            "security": report["updates"]["security"],
            "reboot_required": report.get("reboot_required") is True,
        }
    context["containers"] = _container_sections(report, findings)
    context["failed_units"] = report.get("failed_units")
    if report.get("timers") is not None:
        context["timers"] = [
            {
                "name": t["timer"],
                "last_run": t["last_run"] or "—",
                "next_run": t["next_run"] or "—",
            }
            for t in report["timers"]
        ]
    context["disks"] = _disk_rows(report, findings)
    return context


def host_preview(host: Host, now: datetime) -> dict[str, Any]:
    """The overview's host preview window (/api/v1/hosts/<id>/summary.json).

    A smaller cut of detail_context(), so the preview and the host page can never
    disagree: same status (offline worked out now), same issue filtering (no offline
    issue line), same disk levels. Only short summaries: no raw logs, no tokens, no
    container status strings. A check the host didn't report stays None, and a host
    that never reported has every section None.
    """
    detail = detail_context(host, now)
    head = detail["host"]
    report = host.latest_report
    containers = detail["containers"]
    return {
        "id": head["id"],
        "name": head["name"],
        "status": head["status"],
        "status_label": head["status_label"],
        "last_seen_human": head["last_seen_human"],
        "report_received_at": iso_utc(host.report_received_at),
        "url": reverse("host-detail", args=[host.pk]),
        "issues": [{"text": i["text"], "severity": i["severity"]} for i in detail["issues"]],
        "updates": detail["updates"],
        "containers": None
        if containers is None
        else {
            "running": _running(report["containers"]),
            "problems": [{"name": c["name"], "state": c["state"]} for c in containers["problems"]],
        },
        "failed_units": detail["failed_units"],
        "disks": detail["disks"],
    }
