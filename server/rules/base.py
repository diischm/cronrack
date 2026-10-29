"""Building blocks of the rule engine: Finding, Rule, EffectiveRule, RuleContext.

Plain Python on purpose (no database, no Django), so every rule can be tested
with a dict that looks like an agent report.
"""

import hashlib
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

RED = "red"
YELLOW = "yellow"
INFO = "info"
IGNORE = "ignore"

# Higher number = more urgent. Used to pick the host colour and sort issues.
SEVERITY_RANK = {RED: 3, YELLOW: 2, INFO: 1, IGNORE: 0}

# Only these severities change a host's colour. Info and ignore are shown, never counted.
COLOUR_SEVERITIES = frozenset({RED, YELLOW})


def ago(seconds: float) -> str:
    """'45 min', '3 h', '2 days': how long something has been quiet."""
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days"


def fingerprint(rule_id: str, host_id: int | str, subject: str) -> str:
    """Stable ID of one problem on one host: SHA-256 hex of 'rule_id|host_id|subject'.

    For a cluster, host_id is "c<cluster ID>" (RuleContext.scope_prefix), so a cluster
    and a host with the same number never share a fingerprint.

    Exceptions and snoozes attach to this, so it must never change for the same problem.
    """
    return hashlib.sha256(f"{rule_id}|{host_id}|{subject}".encode()).hexdigest()


@dataclass(frozen=True)
class Finding:
    """One problem a rule found, e.g. 'nginx container restarting'."""

    rule_id: str
    subject: str  # "" for host-wide findings, "container:nginx", "mount:/", "unit:x.service"
    severity: str  # red | yellow | info | ignore
    text: str  # short sentence for the host card
    why: str  # plain-language explanation
    fix: str  # command the user may copy; "" when there is nothing sensible to suggest
    fingerprint: str


@dataclass(frozen=True)
class EffectiveRule:
    """A rule's settings after the user's overrides are applied."""

    enabled: bool
    severity: str  # the rule's default severity, or the override's
    severity_overridden: bool  # True if the user picked the severity explicitly
    threshold: float | None
    notify: str  # "" = route by severity (Phase 12)


@dataclass(frozen=True)
class RuleContext:
    """What a rule may know besides the report."""

    host_id: int
    now: datetime
    connected: bool
    last_seen_at: datetime | None
    # (rule_id, subject) -> that rule's effective settings for that subject.
    settings: Callable[[str, str], EffectiveRule]
    # "" for a host; "c" for a cluster, where host_id is the cluster's ID. Part of the
    # fingerprint, so host fingerprints are exactly what they always were.
    scope_prefix: str = ""
    # Newest released versions from clusters.updates ("k3s", "argocd", "helm"); empty
    # unless the owner turned online update checks on.
    latest: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


Check = Callable[["Rule", Mapping[str, Any], RuleContext], list[Finding]]


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    # updates | docker | systemd | disk | security | changes | backups | connectivity | kubernetes
    category: str
    default_severity: str
    check: Check
    why: str
    fix_template: str = ""  # str.format() template; values are shell-quoted first
    default_threshold: float | None = None
    needs_privileged: bool = False  # needs the root helper's data (Phase 9)
    deviation: bool = False  # compares with the learned baseline; quiet while learning

    def evaluate(self, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
        return self.check(self, report, ctx)

    def threshold(self, ctx: RuleContext, subject: str = "") -> float | None:
        return ctx.settings(self.id, subject).threshold

    def finding(
        self,
        ctx: RuleContext,
        text: str,
        subject: str = "",
        severity: str | None = None,
        **fix_values: str,
    ) -> Finding:
        """Build a Finding. fix_values fill fix_template, each one shell-quoted.

        Container, unit and mount names come from the host and are untrusted. Quoting
        them means a container named "x; rm -rf ~" can't turn a copied fix command
        into a second command. (CronRack never runs fix commands; the user copies them.)
        """
        fix = ""
        if self.fix_template:
            quoted = {key: shlex.quote(value) for key, value in fix_values.items()}
            fix = self.fix_template.format(**quoted)
        return Finding(
            rule_id=self.id,
            subject=subject,
            severity=severity or self.default_severity,
            text=text,
            why=self.why,
            fix=fix,
            fingerprint=fingerprint(self.id, f"{ctx.scope_prefix}{ctx.host_id}", subject),
        )
