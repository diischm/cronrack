"""A user's effective notification settings: the preset's defaults with their overrides on top.

Loaded once per evaluation (load_config) and handed to the rule engine, which turns it into
plain rule settings (rules.engine.resolve_override):
- level "off", or "Applies to: chosen hosts/clusters" that excludes this one -> the rule is
  disabled: no finding, so it is neither shown nor sent;
- "Counts as" different from the rule's default -> the finding's severity (red / yellow);
- a "Limit" choice -> the rule's threshold (the threshold logic itself stays in the rule).
An admin RuleOverride (rules.models) is more specific and still wins, field by field.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from django.db import transaction

from notifications.rules import (
    ALERT,
    COUNTS_AS_SEVERITY,
    DEFAULT_PRESET,
    OFF,
    PRESETS,
    SPECS,
)


@dataclass(frozen=True)
class RuleChoice:
    """One NotificationRuleOverride, without the database."""

    level: str
    counts_as: str = ""
    limit: str = ""
    applies_to: str = "all"
    hosts: frozenset[int] = frozenset()
    clusters: frozenset[int] = frozenset()


@dataclass(frozen=True)
class NotifyConfig:
    preset: str = DEFAULT_PRESET
    choices: Mapping[str, RuleChoice] = field(default_factory=dict)

    def level(self, rule_id: str) -> str:
        choice = self.choices.get(rule_id)
        if choice is not None:
            return choice.level
        spec = SPECS.get(rule_id)
        return spec.defaults[self.preset] if spec else ALERT

    def counts_as(self, rule_id: str) -> str:
        choice, spec = self.choices.get(rule_id), SPECS.get(rule_id)
        if choice is not None and choice.counts_as:
            return choice.counts_as
        return spec.counts_as if spec else ""

    def severity_override(self, rule_id: str) -> str:
        """The severity ("red" / "yellow") when the user changed "Counts as", else "" (keep
        the rule's own, which for disk means yellow from the limit and red from 95%)."""
        choice, spec = self.choices.get(rule_id), SPECS.get(rule_id)
        if choice is None or not choice.counts_as or spec is None:
            return ""
        if choice.counts_as == spec.counts_as:
            return ""
        return COUNTS_AS_SEVERITY.get(choice.counts_as, "")

    def limit_key(self, rule_id: str) -> str:
        spec = SPECS.get(rule_id)
        if spec is None or spec.limit is None:
            return ""
        choice = self.choices.get(rule_id)
        if choice is not None and choice.limit in spec.limit.keys:
            return choice.limit
        return spec.limit.default

    def limit_value(self, rule_id: str) -> float | None:
        """The threshold of the user's limit choice; None = keep the rule's default."""
        spec, choice = SPECS.get(rule_id), self.choices.get(rule_id)
        if spec is None or spec.limit is None or choice is None or not choice.limit:
            return None
        return spec.limit.value(choice.limit)

    def applies(self, rule_id: str, scope: str, subject_id) -> bool:
        """False when the rule is limited to chosen hosts (or clusters) and this isn't one."""
        choice, spec = self.choices.get(rule_id), SPECS.get(rule_id)
        if choice is None or spec is None or choice.applies_to != "selected":
            return True
        if spec.applies_to == "hosts" and scope == "host":
            return subject_id in choice.hosts
        if spec.applies_to == "clusters" and scope == "cluster":
            return subject_id in choice.clusters
        return True

    def enabled(self, rule_id: str, scope: str, subject_id) -> bool:
        return self.level(rule_id) != OFF and self.applies(rule_id, scope, subject_id)

    def is_changed(self, rule_id: str) -> bool:
        return rule_id in self.choices


DEFAULT_CONFIG = NotifyConfig()


def load_config(owner_id: int) -> NotifyConfig:
    """The owner's preset and overrides: three small queries, once per evaluation."""
    from notifications.models import NotificationRuleOverride
    from prefs.models import Prefs

    preset = Prefs.objects.filter(owner_id=owner_id).values_list("notify_preset", flat=True).first()
    if preset not in PRESETS:
        preset = DEFAULT_PRESET
    overrides = NotificationRuleOverride.objects.filter(owner_id=owner_id).prefetch_related(
        "hosts", "clusters"
    )
    choices = {
        o.rule_id: RuleChoice(
            level=o.level,
            counts_as=o.counts_as,
            limit=o.limit,
            applies_to=o.applies_to,
            # Owner-scoped again: a stale link to someone else's host never counts.
            hosts=frozenset(h.pk for h in o.hosts.all() if h.owner_id == owner_id),
            clusters=frozenset(c.pk for c in o.clusters.all() if c.owner_id == owner_id),
        )
        for o in overrides
        if o.rule_id in SPECS
    }
    return NotifyConfig(preset=preset, choices=choices)


def apply_preset(owner, preset: str) -> None:
    """Switch the preset and clear the per-rule notification overrides (admin RuleOverrides
    stay). Raises ValueError for an unknown preset."""
    from notifications.models import NotificationRuleOverride
    from prefs.models import Prefs

    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset!r}")
    with transaction.atomic():
        prefs, _ = Prefs.objects.get_or_create(owner=owner)
        prefs.notify_preset = preset
        prefs.save(update_fields=["notify_preset"])
        NotificationRuleOverride.objects.filter(owner=owner).delete()
