from django.db import models
from django.db.models import Q
from django.utils import timezone

from clusters.models import Cluster
from hosts.models import Host


class StatusEventQuerySet(models.QuerySet):
    def owned_by(self, user):
        """Events of the user's hosts and clusters. Every view goes through this, so
        none can forget the cluster half."""
        return self.filter(Q(host__owner=user) | Q(cluster__owner=user))


class StatusEvent(models.Model):
    """One row each time a host's or a cluster's status changes (e.g. green -> red)."""

    # Exactly one of host and cluster is set (status_event_one_subject).
    host = models.ForeignKey(
        Host, on_delete=models.CASCADE, null=True, blank=True, related_name="status_events"
    )
    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, blank=True, related_name="status_events"
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    old_status = models.CharField(max_length=16, choices=Host.Status.choices)
    new_status = models.CharField(max_length=16, choices=Host.Status.choices)
    summary = models.CharField(max_length=200, blank=True)

    objects = StatusEventQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=["host", "-created_at"]),
            models.Index(fields=["cluster", "-created_at"]),
        )
        constraints = (
            models.CheckConstraint(
                condition=(
                    Q(host__isnull=False, cluster__isnull=True)
                    | Q(host__isnull=True, cluster__isnull=False)
                ),
                name="status_event_one_subject",
            ),
        )

    @property
    def subject(self):
        """The host or cluster this event is about."""
        return self.host if self.host_id is not None else self.cluster

    @property
    def subject_name(self) -> str:
        return self.subject.name

    @property
    def owner_id(self) -> int:
        return self.subject.owner_id

    def __str__(self) -> str:
        return f"{self.subject_name}: {self.old_status} -> {self.new_status}"

    def describe(self) -> str:
        """One sentence for the activity list and alerts: 'pi-01 went red: 2 security updates'."""
        name = self.subject_name
        if self.new_status == "offline":
            text = f"{name} went offline"
        elif self.old_status == "offline":
            text = f"{name} is back online ({self.new_status})"
        elif self.old_status == "waiting":
            text = f"{name} sent its first report ({self.new_status})"
        else:
            text = f"{name} went {self.new_status}"
        return f"{text}: {self.summary}" if self.summary else text
