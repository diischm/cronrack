import zoneinfo
from datetime import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Route(models.TextChoices):
    """Where a finding's notification goes."""

    INSTANT = "instant", "Instant alert"
    DIGEST = "digest", "Daily digest"
    DASHBOARD = "dashboard", "Dashboard only"


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
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    digest_enabled = models.BooleanField(default=False)
    digest_time = models.TimeField(default=time(17, 30))
    route_red = models.CharField(max_length=16, choices=Route.choices, default=Route.INSTANT)
    route_yellow = models.CharField(max_length=16, choices=Route.choices, default=Route.DIGEST)
    route_info = models.CharField(max_length=16, choices=Route.choices, default=Route.DASHBOARD)
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
