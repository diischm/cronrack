from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from hosts.models import Host
from prefs.models import Route
from rules.base import fingerprint

# Rule IDs look like "disk.usage" or "docker.container_restarting".
# (Checking that the ID exists in rules.registry comes with the rule views.)
rule_id_validator = RegexValidator(
    r"^[a-z0-9_]+(\.[a-z0-9_]+)+\Z",
    "Rule IDs look like 'disk.usage': lowercase words joined by dots.",
)


class Severity(models.TextChoices):
    RED = "red", "Red"
    YELLOW = "yellow", "Yellow"
    INFO = "info", "Info"
    IGNORE = "ignore", "Ignore"


def fingerprint_for(rule_id: str, host_id: int, subject: str) -> str:
    """Stable ID of one finding: SHA-256 of 'rule_id|host_id|subject'."""
    return fingerprint(rule_id, host_id, subject)


def _check_host_owner(obj) -> None:
    if obj.host_id is not None and obj.host.owner_id != obj.owner_id:
        raise ValidationError({"host": "You can only use your own hosts."})


class RuleOverride(models.Model):
    """The user's changes to one rule. Blank/null fields mean 'keep the default'."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rule_overrides"
    )
    rule_id = models.CharField(max_length=80, validators=[rule_id_validator])
    host = models.ForeignKey(
        Host,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="rule_overrides",
        help_text="Empty = applies to all hosts.",
    )
    subject = models.CharField(
        max_length=200, blank=True, help_text="e.g. container:nginx. Empty = whole rule."
    )
    enabled = models.BooleanField(null=True, blank=True)
    severity = models.CharField(max_length=8, choices=Severity.choices, blank=True)
    threshold = models.FloatField(null=True, blank=True)
    notify = models.CharField(max_length=16, choices=Route.choices, blank=True)

    class Meta:
        constraints = (
            # Two constraints because SQL treats NULL hosts as all different, so a
            # single unique constraint would allow duplicate "all hosts" overrides.
            models.UniqueConstraint(
                fields=["owner", "rule_id", "host", "subject"],
                condition=models.Q(host__isnull=False),
                name="unique_override_per_host",
            ),
            models.UniqueConstraint(
                fields=["owner", "rule_id", "subject"],
                condition=models.Q(host__isnull=True),
                name="unique_override_global",
            ),
        )

    def __str__(self) -> str:
        scope = self.host or "all hosts"
        return f"{self.rule_id} ({scope}{' / ' + self.subject if self.subject else ''})"

    def clean(self) -> None:
        _check_host_owner(self)


class Suppression(models.Model):
    """'This is expected' (permanent) or a snooze (until expires_at) for one finding."""

    class Kind(models.TextChoices):
        EXPECTED = "expected", "This is expected"
        SNOOZE = "snooze", "Snooze"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="suppressions"
    )
    # Derived from rule_id + host + subject on save; never typed by hand.
    fingerprint = models.CharField(max_length=64, db_index=True, blank=True, editable=False)
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="suppressions")
    rule_id = models.CharField(max_length=80, validators=[rule_id_validator])
    subject = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    expires_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=(
                    models.Q(kind="snooze", expires_at__isnull=False)
                    | models.Q(kind="expected", expires_at__isnull=True)
                ),
                name="snooze_needs_expiry",
                violation_error_message=(
                    "A snooze needs an expiry time; 'This is expected' must not have one."
                ),
            ),
        )

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.rule_id} on {self.host}"

    def clean(self) -> None:
        _check_host_owner(self)
        if self.host_id is not None:
            self.fingerprint = fingerprint_for(self.rule_id, self.host_id, self.subject)

    def save(self, *args, **kwargs):
        self.fingerprint = fingerprint_for(self.rule_id, self.host_id, self.subject)
        super().save(*args, **kwargs)
