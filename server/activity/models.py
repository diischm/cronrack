from django.db import models
from django.db.models import Q
from django.utils import timezone

from clusters.models import Cluster
from hosts.models import Host
from network.models import Network


class StatusEventQuerySet(models.QuerySet):
    def owned_by(self, user):
        """Events of the user's hosts, clusters and network. Every view goes through this,
        so none can forget a kind of subject."""
        return self.filter(Q(host__owner=user) | Q(cluster__owner=user) | Q(network__owner=user))


class StatusEvent(models.Model):
    """One row each time a host's, a cluster's or the network's status changes (e.g.
    green -> red). The network also gets rows that don't change its status (old == new,
    "Router rebooted", "Public IP changed"): they are shown, never alerted.
    """

    # Exactly one of host, cluster and network is set (status_event_one_subject).
    host = models.ForeignKey(
        Host, on_delete=models.CASCADE, null=True, blank=True, related_name="status_events"
    )
    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, blank=True, related_name="status_events"
    )
    network = models.ForeignKey(
        Network, on_delete=models.CASCADE, null=True, blank=True, related_name="status_events"
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
            models.Index(fields=["network", "-created_at"]),
        )
        constraints = (
            models.CheckConstraint(
                condition=(
                    Q(host__isnull=False, cluster__isnull=True, network__isnull=True)
                    | Q(host__isnull=True, cluster__isnull=False, network__isnull=True)
                    | Q(host__isnull=True, cluster__isnull=True, network__isnull=False)
                ),
                name="status_event_one_subject",
            ),
        )

    @property
    def subject(self) -> Host | Cluster | Network:
        """The host, cluster or network this event is about."""
        if self.host_id is not None:
            return self.host
        return self.cluster if self.cluster_id is not None else self.network

    @property
    def subject_name(self) -> str:
        return self.subject.name

    @property
    def owner_id(self) -> int:
        return self.subject.owner_id

    def __str__(self) -> str:
        return f"{self.subject_name}: {self.old_status} -> {self.new_status}"

    def _subject_ref(self) -> tuple[str, int]:
        """('host', 4): the kind of subject and its id, from the foreign keys alone."""
        if self.host_id is not None:
            return "host", self.host_id
        if self.cluster_id is not None:
            return "cluster", self.cluster_id
        return "network", self.network_id

    @property
    def subject_kind(self) -> str:
        """'host', 'cluster' or 'network': which kind of subject this event is about."""
        return self._subject_ref()[0]

    @property
    def subject_key(self) -> str:
        """A stable id for grouping ('host-4'). Survives renames; built from the ids only."""
        kind, pk = self._subject_ref()
        return f"{kind}-{pk}"

    def _describe_parts(self) -> tuple[str, str]:
        """(joiner, short): the sentence is the subject's name + joiner + short."""
        if self.network_id is not None:
            # "Network: Internet down", "Network: Router rebooted …", "Network OK again".
            if self.summary:
                return ": ", self.summary
            return " ", "OK again" if self.new_status == "green" else f"went {self.new_status}"
        if self.new_status == "offline":
            short = "went offline"
        elif self.old_status == "offline":
            short = f"is back online ({self.new_status})"
        elif self.old_status == "waiting":
            short = f"sent its first report ({self.new_status})"
        else:
            short = f"went {self.new_status}"
        if self.summary:
            short = f"{short}: {self.summary}"
        return " ", short

    def describe(self) -> str:
        """One sentence for the activity list and alerts: 'pi-01 went red: 2 security updates'."""
        joiner, short = self._describe_parts()
        return f"{self.subject_name}{joiner}{short}"

    def describe_short(self) -> str:
        """The sentence without the subject's name: 'went red: 2 security updates'."""
        return self._describe_parts()[1]
