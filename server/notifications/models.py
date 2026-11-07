"""Notification tables.

- NotificationRuleOverride: the user's choice for one rule in Settings → Notifications,
  where it differs from the preset (no row = the preset's default).
- FindingState: one row per problem (its fingerprint), from open to resolved. It is what
  makes "one message per problem" work: the open, a "now red" escalation, the resolved
  message, the quiet-hours bundle and the daily summary all read and update it.
- BaselinePending: the hosts, clusters and the network that existed before this feature.
  Their first evaluation afterwards records what is already open without sending it, so
  upgrading doesn't send a burst of alerts.

The quiet-hours, daily-summary, sound and preset settings live on prefs.Prefs (one row per
user), next to the webhook they belong to.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from clusters.models import Cluster
from hosts.models import Host
from network.models import Network
from notifications.rules import (
    COUNTS_AS_LABELS,
    LEVEL_LABELS,
    SPECS,
)


def validate_rule_id(value: str) -> None:
    if value not in SPECS:
        raise ValidationError("Unknown rule.")


class NotificationRuleOverride(models.Model):
    """One rule's settings where they differ from the preset. Cleared when the preset changes."""

    class AppliesTo(models.TextChoices):
        ALL = "all", "All"
        SELECTED = "selected", "Chosen ones"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_overrides"
    )
    rule_id = models.CharField(max_length=80, validators=[validate_rule_id])
    level = models.CharField(max_length=8, choices=list(LEVEL_LABELS.items()))
    # Blank = the rule's default. Only stored when it differs from it.
    counts_as = models.CharField(max_length=16, choices=list(COUNTS_AS_LABELS.items()), blank=True)
    # Blank = the rule's default limit. Otherwise one of the rule's fixed choice keys.
    limit = models.CharField(max_length=16, blank=True)
    applies_to = models.CharField(max_length=8, choices=AppliesTo.choices, default=AppliesTo.ALL)
    hosts = models.ManyToManyField(Host, blank=True, related_name="+")
    clusters = models.ManyToManyField(Cluster, blank=True, related_name="+")

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=["owner", "rule_id"], name="one_notification_override"),
        )

    def __str__(self) -> str:
        return f"{self.rule_id}: {self.level}"

    def clean(self) -> None:
        spec = SPECS.get(self.rule_id)
        if spec is None:
            return  # validate_rule_id reports it
        if self.limit and (spec.limit is None or self.limit not in spec.limit.keys):
            raise ValidationError({"limit": "Pick one of the listed limits."})
        if self.applies_to == self.AppliesTo.SELECTED and not spec.applies_to:
            raise ValidationError({"applies_to": "This rule applies to the whole network."})


class FindingState(models.Model):
    """One problem, from the report it appeared in until it has been gone for a while."""

    class Announce(models.TextChoices):
        NONE = "", "Nothing to send"
        OPEN = "open", "Opened"
        ESCALATE = "escalate", "Now red"
        RESOLVE = "resolve", "Resolved"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="finding_states"
    )
    # Exactly one of host, cluster and network is set (finding_state_one_subject).
    host = models.ForeignKey(
        Host, on_delete=models.CASCADE, null=True, blank=True, related_name="finding_states"
    )
    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, blank=True, related_name="finding_states"
    )
    network = models.ForeignKey(
        Network, on_delete=models.CASCADE, null=True, blank=True, related_name="finding_states"
    )
    # rules.base.fingerprint: rule ID + scope + subject key (mount, unit, container, node...),
    # never a value or text, so the same problem keeps its row while its numbers change.
    fingerprint = models.CharField(max_length=64)
    rule_id = models.CharField(max_length=80)
    subject = models.CharField(max_length=200, blank=True)
    # The newest text and severity, updated in place on every evaluation.
    text = models.CharField(max_length=200)
    severity = models.CharField(max_length=8)
    level = models.CharField(max_length=8, default="alert")  # alert | show, at the last evaluation
    opened_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    # Evaluations in a row without this finding; resolved at 2 (the flap guard).
    clear_streak = models.PositiveSmallIntegerField(default=0)
    # Severity of the last open / "now red" message that went out; "" = none went out, so
    # no resolved message is owed either.
    announced_severity = models.CharField(max_length=8, blank=True)
    # The message still to send for this row (the outbox).
    announce = models.CharField(max_length=10, choices=Announce.choices, blank=True)
    # Set while quiet hours hold this row back for the morning bundle.
    held_since = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=["fingerprint"], name="one_state_per_fingerprint"),
            models.CheckConstraint(
                condition=(
                    Q(host__isnull=False, cluster__isnull=True, network__isnull=True)
                    | Q(host__isnull=True, cluster__isnull=False, network__isnull=True)
                    | Q(host__isnull=True, cluster__isnull=True, network__isnull=False)
                ),
                name="finding_state_one_subject",
            ),
        )
        indexes = (
            models.Index(fields=["owner", "announce"]),
            models.Index(fields=["resolved_at"]),
        )

    def __str__(self) -> str:
        state = "open" if self.resolved_at is None else "resolved"
        return f"{self.rule_id} {self.subject} ({state})"


class BaselinePending(models.Model):
    """A subject whose first evaluation after the upgrade is recorded without alerts."""

    host = models.OneToOneField(Host, on_delete=models.CASCADE, null=True, blank=True)
    cluster = models.OneToOneField(Cluster, on_delete=models.CASCADE, null=True, blank=True)
    network = models.OneToOneField(Network, on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=(
                    Q(host__isnull=False, cluster__isnull=True, network__isnull=True)
                    | Q(host__isnull=True, cluster__isnull=False, network__isnull=True)
                    | Q(host__isnull=True, cluster__isnull=True, network__isnull=False)
                ),
                name="baseline_one_subject",
            ),
        )
