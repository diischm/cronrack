"""The rule engine: report -> findings -> overrides -> suppressions -> host status.

Each step is its own function so it can be tested alone. The functions that need
the user's overrides or suppressions accept them as a list (tests pass plain
objects); when the list is omitted they load them from the database, always
filtered by the host's owner.
"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from django.db.models import Q

from rules.base import (
    COLOUR_SEVERITIES,
    SEVERITY_RANK,
    EffectiveRule,
    Finding,
    RuleContext,
)
from rules.registry import RULES, get_rule

OFFLINE_RULE = "connectivity.offline"

WAITING = "waiting"
MAINTENANCE = "maintenance"
OFFLINE = "offline"
GREEN = "green"

# Overview order: what needs you first, then by name.
STATUS_ORDER = {"red": 0, OFFLINE: 1, "yellow": 2, WAITING: 3, MAINTENANCE: 4, GREEN: 5}


def _owner_id(owner) -> int:
    return getattr(owner, "pk", owner)


def load_overrides(host) -> list:
    """The host owner's overrides that can apply to this host (its own, and global ones)."""
    from rules.models import RuleOverride

    return list(
        RuleOverride.objects.filter(owner_id=host.owner_id).filter(
            Q(host__isnull=True) | Q(host_id=host.pk)
        )
    )


def load_suppressions(host) -> list:
    from rules.models import Suppression

    return list(Suppression.objects.filter(owner_id=host.owner_id, host_id=host.pk))


def resolve_override(owner, rule_id: str, host, subject: str = "", overrides=None) -> EffectiveRule:
    """The rule's settings for this host and subject, with the user's overrides applied.

    Most specific wins, field by field (an empty field means "keep the less specific
    value"):  host + subject -> host -> all hosts + subject -> all hosts -> code default.
    So a host-wide "severity: info" and a global "threshold: 90" both apply.
    """
    rule = get_rule(rule_id)
    owner_id = _owner_id(owner)
    host_id = getattr(host, "pk", host)
    if overrides is None:
        from rules.models import RuleOverride

        overrides = RuleOverride.objects.filter(owner_id=owner_id, rule_id=rule_id).filter(
            Q(host__isnull=True) | Q(host_id=host_id)
        )

    # Lower number = more specific. Overrides for other users, rules, hosts or
    # subjects are skipped, even if the caller passed them in by mistake.
    def rank(override) -> int | None:
        if override.owner_id != owner_id or override.rule_id != rule_id:
            return None
        if override.subject and override.subject != subject:
            return None
        if override.host_id is not None and override.host_id != host_id:
            return None
        on_host = override.host_id is not None
        if on_host:
            return 0 if override.subject else 1
        return 2 if override.subject else 3

    applicable = [(rank(o), o) for o in overrides]
    # sorted() is stable, so overrides of equal rank keep their order.
    chain = [
        o
        for r, o in sorted(
            ((r, o) for r, o in applicable if r is not None), key=lambda item: item[0]
        )
    ]

    def pick(field: str, empty):
        for override in chain:
            value = getattr(override, field)
            if value != empty and value is not None:
                return value
        return None

    enabled = pick("enabled", None)
    severity = pick("severity", "")
    threshold = pick("threshold", None)
    notify = pick("notify", "")
    return EffectiveRule(
        enabled=True if enabled is None else enabled,
        severity=severity or rule.default_severity,
        severity_overridden=bool(severity),
        threshold=rule.default_threshold if threshold is None else threshold,
        notify=notify or "",
    )


def _has_privileged_data(report: Mapping[str, Any]) -> bool:
    security = report.get("security")
    return isinstance(security, Mapping) and security.get("privileged_data") is True


def evaluate(
    host, report: Mapping[str, Any] | None, now: datetime, overrides=None
) -> list[Finding]:
    """Run every rule against the report and return the findings, overrides applied.

    Skips privileged rules without the root helper's data, and deviation rules while
    the host is still learning. Disabled findings are dropped; an override severity
    replaces the rule's own.
    """
    if overrides is None:
        overrides = load_overrides(host)
    report = report or {}

    def settings(rule_id: str, subject: str) -> EffectiveRule:
        return resolve_override(host.owner_id, rule_id, host, subject, overrides=overrides)

    ctx = RuleContext(
        host_id=host.pk,
        now=now,
        connected=host.connected,
        last_seen_at=host.last_seen_at,
        settings=settings,
    )
    learning = host.learning_until is not None and host.learning_until > now
    privileged = _has_privileged_data(report)

    findings: list[Finding] = []
    for rule in RULES.values():
        if rule.deviation and learning:
            continue
        if rule.needs_privileged and not privileged:
            continue
        for finding in rule.evaluate(report, ctx):
            effective = settings(rule.id, finding.subject)
            if not effective.enabled:
                continue
            if effective.severity_overridden:
                finding = replace(finding, severity=effective.severity)
            findings.append(finding)
    return findings


def _suppression_active(suppression, now: datetime) -> bool:
    if suppression.kind == "expected":
        return True
    if suppression.kind == "snooze":
        return suppression.expires_at is not None and suppression.expires_at > now
    return False


def apply_suppressions(
    findings: Iterable[Finding], host, now: datetime, suppressions=None
) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (active, suppressed).

    A finding is suppressed by a matching "This is expected", or by a snooze that
    hasn't expired yet. An expired snooze no longer hides anything.
    """
    if suppressions is None:
        suppressions = load_suppressions(host)
    hidden = {
        s.fingerprint
        for s in suppressions
        if s.host_id == host.pk and s.owner_id == host.owner_id and _suppression_active(s, now)
    }
    active, suppressed = [], []
    for finding in findings:
        (suppressed if finding.fingerprint in hidden else active).append(finding)
    return active, suppressed


def compute_status(active_findings: Iterable[Finding], host, now: datetime) -> str:
    """The host's colour, in this order of precedence:

    waiting (no report yet) -> maintenance -> offline -> red -> yellow -> green.
    Info and ignore findings never change the colour.
    """
    if host.latest_report is None:
        return WAITING
    if host.maintenance_until is not None and host.maintenance_until > now:
        return MAINTENANCE
    colours = [f for f in active_findings if f.severity in COLOUR_SEVERITIES]
    if any(f.rule_id == OFFLINE_RULE for f in colours):
        return OFFLINE
    if not colours:
        return GREEN
    return max(colours, key=lambda f: SEVERITY_RANK[f.severity]).severity


def top_issues(findings: Iterable[Finding], n: int = 3) -> list[Finding]:
    """The n most urgent findings (red first), ignoring severity "ignore".

    The sort is stable, so equal severities keep the registry's order.
    """
    shown = [f for f in findings if f.severity != "ignore"]
    shown.sort(key=lambda f: -SEVERITY_RANK[f.severity])
    return shown[:n]


def sort_hosts(hosts: Iterable) -> list:
    """Order for the overview: red, offline, yellow, waiting, maintenance, green; then name."""
    return sorted(hosts, key=lambda h: (STATUS_ORDER.get(h.status, 99), h.name.lower(), h.pk))
