import zoneinfo
from datetime import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models

PRESET_CHOICES = (
    ("quiet", "Quiet"),
    ("balanced", "Balanced"),
    ("everything", "Everything"),
)


def validate_timezone(value: str) -> None:
    if value not in zoneinfo.available_timezones():
        raise ValidationError(f"Unknown time zone: {value}")


class Prefs(models.Model):
    """Per-user settings. One row per user."""

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="prefs"
    )
    discord_webhook_url = models.URLField(max_length=500, blank=True)
    silent_after_seconds = models.PositiveIntegerField(default=90)
    timezone = models.CharField(
        max_length=64, default="Europe/Skopje", validators=[validate_timezone]
    )
    # Settings → Notifications. Times are in `timezone` above, the user's own.
    notify_preset = models.CharField(max_length=16, choices=PRESET_CHOICES, default="balanced")
    # On for new users. Users who had CronRack before this setting existed start with it
    # off (prefs migration 0003), so nothing they relied on changes by itself.
    quiet_hours_enabled = models.BooleanField(default=True)
    quiet_hours_start = models.TimeField(default=time(23, 0))
    quiet_hours_end = models.TimeField(default=time(8, 0))
    digest_enabled = models.BooleanField(default=False)
    digest_time = models.TimeField(default=time(9, 0))
    # The user's local date of the last daily summary, so a pass sends it once a day.
    digest_sent_on = models.DateField(null=True, blank=True, editable=False)
    sounds_enabled = models.BooleanField(default=False)
    sound_volume = models.PositiveSmallIntegerField(default=40, validators=[MaxValueValidator(100)])
    # Off by default: CronRack contacts the internet only if the user asks for it.
    check_updates_online = models.BooleanField(
        default=False,
        help_text="Once a day, ask update.k3s.io and api.github.com for the newest "
        "k3s, Argo CD and Helm versions.",
    )

    class Meta:
        verbose_name_plural = "prefs"

    def __str__(self) -> str:
        return f"Prefs for {self.owner}"
