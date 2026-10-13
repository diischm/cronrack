from django.db import models
from django.utils import timezone

from hosts.models import Host


class StatusEvent(models.Model):
    """One row each time a host's status changes (e.g. green -> red)."""

    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="status_events")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    old_status = models.CharField(max_length=16, choices=Host.Status.choices)
    new_status = models.CharField(max_length=16, choices=Host.Status.choices)
    summary = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = (models.Index(fields=["host", "-created_at"]),)

    def __str__(self) -> str:
        return f"{self.host}: {self.old_status} -> {self.new_status}"

    def describe(self) -> str:
        """One sentence for the activity list and alerts: 'pi-01 went red: 2 security updates'."""
        name = self.host.name
        if self.new_status == "offline":
            text = f"{name} went offline"
        elif self.old_status == "offline":
            text = f"{name} is back online ({self.new_status})"
        elif self.old_status == "waiting":
            text = f"{name} sent its first report ({self.new_status})"
        else:
            text = f"{name} went {self.new_status}"
        return f"{text}: {self.summary}" if self.summary else text
