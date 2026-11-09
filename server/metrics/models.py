"""Metric history per host: one table per kind of series, three resolutions each.

    res 0  raw      one row per report, at most one every MIN_RAW_GAP seconds  kept 24 h
    res 1  5 min    the average of the raw rows in each 5-minute bucket         kept 7 days
    res 2  1 hour   the average of the 5-minute rows in each hour               kept 90 days

The scheduler (metrics.rollup) builds the averages and deletes old rows. The current
values shown on the pages come from Host.latest_report, not from these tables.

Owner scoping: every row hangs off a Host, and these tables are only ever read for a host
that was already loaded for its owner (hosts.access.owned_host) or by its agent's token.
"""

from django.db import models

from hosts.models import Host


class Resolution(models.IntegerChoices):
    RAW = 0, "raw"
    FIVE_MIN = 1, "5 minutes"
    HOUR = 2, "1 hour"


class HostSample(models.Model):
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="metric_samples")
    res = models.PositiveSmallIntegerField(choices=Resolution.choices)
    at = models.DateTimeField()  # raw: when the report arrived; rollups: the bucket's start
    cpu_pct = models.FloatField(null=True)
    iowait_pct = models.FloatField(null=True)
    load1 = models.FloatField(null=True)
    mem_pct = models.FloatField(null=True)  # used = (total - available) / total
    swap_pct = models.FloatField(null=True)
    temp_c = models.FloatField(null=True)
    disk_pct = models.FloatField(null=True)  # the fullest measured mount
    read_bps = models.BigIntegerField(null=True)
    write_bps = models.BigIntegerField(null=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=["host", "res", "at"], name="unique_host_sample"),
        )


class MountSample(models.Model):
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="mount_samples")
    mount = models.CharField(max_length=190)
    res = models.PositiveSmallIntegerField(choices=Resolution.choices)
    at = models.DateTimeField()
    used_pct = models.FloatField(null=True)
    inodes_pct = models.FloatField(null=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["host", "res", "at", "mount"], name="unique_mount_sample"
            ),
        )


class ContainerSample(models.Model):
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="container_samples")
    name = models.CharField(max_length=190)
    res = models.PositiveSmallIntegerField(choices=Resolution.choices)
    at = models.DateTimeField()
    cpu_pct = models.FloatField(null=True)  # percent of the whole host
    mem = models.BigIntegerField(null=True)
    mem_limit = models.BigIntegerField(null=True)  # None: the container has no limit
    rx_bps = models.BigIntegerField(null=True)
    tx_bps = models.BigIntegerField(null=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["host", "res", "at", "name"], name="unique_container_sample"
            ),
        )
