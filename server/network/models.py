"""The home network as a subject of its own, like a cluster.

One Network per owner: its status (green / yellow / red, from the network rules), its
StatusEvents in the activity log and its Discord alerts (same cooldown and coalescing as
hosts). The host whose agent measures it never changes colour because of the network.

Only one host is the "network view" (the overview strip, the rules, the alerts): the one
the owner picked, else the one on the CronRack server's own machine (it can still report
during a router or internet outage), else the first one that reported. Every reporting host
keeps its own samples; the Network page lists them all.
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from hosts.models import Host

# 48 h of 60 s reports is 2 880 rows; the cap holds even if an agent reports faster.
SAMPLE_RETENTION_HOURS = 48
MAX_SAMPLES_PER_HOST = 3000
DEFAULT_CONFIRM_REPORTS = 3


class Network(models.Model):
    class State(models.TextChoices):
        OK = "ok", "Network OK"
        ROUTER_DOWN = "router_down", "Router down"
        LOCAL_DOWN = "local_down", "Point down"
        INTERNET_DOWN = "internet_down", "Internet down"
        INTERNET_SLOW = "internet_slow", "Internet slow"
        DNS_FAILING = "dns_failing", "DNS failing"
        UNKNOWN = "unknown", "Network unknown"

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="network"
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    # Same choices as Host.Status, so a StatusEvent and the alert cooldown can hold it.
    status = models.CharField(
        max_length=16, choices=Host.Status.choices, default=Host.Status.WAITING
    )
    # The held state: what the strip shows, after flapping protection.
    state = models.CharField(max_length=16, choices=State.choices, default=State.UNKNOWN)
    state_point = models.CharField(max_length=32, blank=True)  # the point that is down
    state_detail = models.JSONField(default=dict, blank=True)  # numbers for the rule texts
    state_since = models.DateTimeField(null=True, blank=True)
    # A new state must be seen in confirm_reports reports in a row before it is held.
    candidate_state = models.CharField(max_length=16, blank=True)
    candidate_point = models.CharField(max_length=32, blank=True)
    candidate_count = models.PositiveSmallIntegerField(default=0)
    confirm_reports = models.PositiveSmallIntegerField(
        default=DEFAULT_CONFIRM_REPORTS, validators=[MinValueValidator(1), MaxValueValidator(20)]
    )
    # The owner's pick; empty = automatic (see the module docstring).
    view_host = models.ForeignKey(
        Host, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # The host the held state came from: it never carries over to another host.
    state_host = models.ForeignKey(
        Host, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    public_ip = models.CharField(max_length=45, blank=True)
    router_down_since = models.DateTimeField(null=True, blank=True)
    last_router_reboot_at = models.DateTimeField(null=True, blank=True)
    # Alert cooldown, exactly like Host and Cluster (alerts/discord.py).
    last_alert_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_alert_status = models.CharField(max_length=16, blank=True, default="", editable=False)

    name = "Network"  # what the activity list and alerts call it

    def __str__(self) -> str:
        return f"Network of user {self.owner_id}: {self.state}"


class NetworkReporter(models.Model):
    """A host whose agent sends network data."""

    host = models.OneToOneField(Host, on_delete=models.CASCADE, related_name="network_reporter")
    first_reported_at = models.DateTimeField(default=timezone.now)
    last_reported_at = models.DateTimeField(default=timezone.now)
    # The agent's own time of the round. Reports in between reuse the round (at most one
    # per 30 s), and one measurement must not count as several "in a row".
    last_probed_at = models.CharField(max_length=20, blank=True)
    # The agent found the CronRack server's address on its own machine.
    server_local = models.BooleanField(default=False)


class NetworkSample(models.Model):
    """One report's network numbers, compact: kept SAMPLE_RETENTION_HOURS, at most
    MAX_SAMPLES_PER_HOST per host (network/services.py prunes on every insert).

    data = {"p": [[name, role, received, sent, median_ms, p95_ms], ...] in path order,
            "d": [dns_ok, dns_ms] or None}
    """

    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="network_samples")
    at = models.DateTimeField()
    data = models.JSONField()

    class Meta:
        indexes = (models.Index(fields=["host", "-at"]),)
