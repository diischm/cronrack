from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from hosts.models import Host, host_name_validator

uid_validator = RegexValidator(
    r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z",
    "A cluster ID is a lowercase UUID.",
)


class Cluster(models.Model):
    """One Kubernetes cluster, as read through the API by one host's agent.

    The cluster's identity is the UID of its kube-system namespace. It is unique per
    owner: two users who monitor the same cluster get two separate rows.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="clusters"
    )
    uid = models.CharField(max_length=36, validators=[uid_validator], editable=False)
    name = models.CharField(max_length=64, validators=[host_name_validator])
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    # Same choices as Host.Status, so a StatusEvent can hold either.
    status = models.CharField(
        max_length=16, choices=Host.Status.choices, default=Host.Status.WAITING
    )
    # The validated `cluster` part of the last report (the agent's cluster view).
    latest_snapshot = models.JSONField(null=True, blank=True)
    snapshot_received_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    # Whose agent sends the cluster view. Cleared if that host is deleted; another
    # host's agent can take over (clusters.services.save_cluster_snapshot).
    reporter = models.ForeignKey(
        Host, on_delete=models.SET_NULL, null=True, blank=True, related_name="reported_clusters"
    )
    # Same meaning as on Host (alerts.discord: cooldown, and catching up on changes).
    last_alert_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_alert_status = models.CharField(max_length=16, blank=True, default="", editable=False)

    class Meta:
        ordering = ("name",)
        constraints = (
            models.UniqueConstraint(fields=["owner", "uid"], name="unique_cluster_uid_per_owner"),
            models.UniqueConstraint(fields=["owner", "name"], name="unique_cluster_name_per_owner"),
        )

    def __str__(self) -> str:
        return self.name

    @property
    def latest_report(self):
        """The rule engine asks every subject for its latest data under this name."""
        return self.latest_snapshot


class UpdateCheck(models.Model):
    """The newest released versions, fetched from the internet (clusters.updates).

    Public data and the same for everyone, so there is no owner. It lives in the
    database because the web server and the scheduler are separate processes.
    """

    class Source(models.TextChoices):
        K3S = "k3s", "k3s"
        ARGOCD = "argocd", "Argo CD"
        HELM = "helm", "Helm"

    source = models.CharField(max_length=16, choices=Source.choices, unique=True)
    # k3s: {"stable": "v1.31.5+k3s1", "v1.30": "...", ...}; others: {"latest": "v2.13.1"}.
    data = models.JSONField(default=dict)
    checked_at = models.DateTimeField(null=True, blank=True)  # last success
    attempted_at = models.DateTimeField(null=True, blank=True)  # last try (drives the retry wait)
    last_error = models.CharField(max_length=64, blank=True, default="")

    def __str__(self) -> str:
        return self.source
