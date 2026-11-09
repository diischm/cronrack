"""Scheduler jobs: average raw samples into 5-minute and 1-hour rows, and delete old rows.

Both are idempotent, so a pass that runs twice, crashes half way or is skipped changes
nothing in the result:
- only CLOSED buckets are averaged (a bucket that is still filling waits for later), and
  a host's buckets start after the newest one it already has;
- rows are inserted with ignore_conflicts, and (host, res, at[, name]) is unique.

Retention (raw 24 h, 5 min 7 days, 1 h 90 days) is a plain DELETE by age.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from django.db import transaction
from django.db.models import Max

from metrics.models import ContainerSample, HostSample, MountSample, Resolution

logger = logging.getLogger(__name__)

RAW, FIVE_MIN, HOUR = Resolution.RAW, Resolution.FIVE_MIN, Resolution.HOUR
BUCKET_SECONDS = {FIVE_MIN: 300, HOUR: 3600}
RETENTION = {
    RAW: timedelta(hours=24),
    FIVE_MIN: timedelta(days=7),
    HOUR: timedelta(days=90),
}
# Integer columns are averaged and rounded back to integers.
_INT_FIELDS = {"read_bps", "write_bps", "mem", "mem_limit", "rx_bps", "tx_bps"}
SERIES = (
    (
        HostSample,
        None,
        (
            "cpu_pct",
            "iowait_pct",
            "load1",
            "mem_pct",
            "swap_pct",
            "temp_c",
            "disk_pct",
            "read_bps",
            "write_bps",
        ),
    ),
    (MountSample, "mount", ("used_pct", "inodes_pct")),
    (ContainerSample, "name", ("cpu_pct", "mem", "mem_limit", "rx_bps", "tx_bps")),
)


def bucket_start(at: datetime, seconds: int) -> datetime:
    epoch = int(at.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, UTC)


def _average(values: Iterable, integer: bool):
    present = [v for v in values if v is not None]
    if not present:
        return None
    mean = sum(present) / len(present)
    return round(mean) if integer else round(mean, 2)


def _rollup_host(model, key, fields, host_id: int, source, target, now: datetime) -> int:
    seconds = BUCKET_SECONDS[target]
    end = bucket_start(now, seconds)  # buckets starting before this one are closed
    newest = model.objects.filter(host_id=host_id, res=target).aggregate(m=Max("at"))["m"]
    rows = model.objects.filter(host_id=host_id, res=source, at__lt=end)
    if newest is not None:
        rows = rows.filter(at__gte=newest + timedelta(seconds=seconds))
    columns = ["at", *fields] + ([key] if key else [])
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows.values(*columns).iterator():
        groups[(bucket_start(row["at"], seconds), row[key] if key else None)].append(row)
    if not groups:
        return 0
    objects = []
    for (at, name), members in groups.items():
        values = {f: _average((m[f] for m in members), f in _INT_FIELDS) for f in fields}
        if key:
            values[key] = name
        objects.append(model(host_id=host_id, res=target, at=at, **values))
    model.objects.bulk_create(objects, ignore_conflicts=True, batch_size=500)
    return len(objects)


def rollup(now: datetime) -> int:
    """Average closed buckets: raw -> 5 min, then 5 min -> 1 h. Returns rows written."""
    written = 0
    for model, key, fields in SERIES:
        for source, target in ((RAW, FIVE_MIN), (FIVE_MIN, HOUR)):
            host_ids = model.objects.filter(res=source).values_list("host_id", flat=True).distinct()
            for host_id in list(host_ids):
                # One host at a time, each in its own transaction: short write locks, and a
                # host that fails is simply retried on the next pass, without stopping the rest.
                try:
                    with transaction.atomic():
                        written += _rollup_host(model, key, fields, host_id, source, target, now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Host %s: rollup failed (%s)", host_id, type(exc).__name__)
    return written


def prune(now: datetime) -> int:
    """Delete rows older than their resolution keeps. Returns rows deleted."""
    deleted = 0
    for model, _key, _fields in SERIES:
        for res, keep in RETENTION.items():
            count, _ = model.objects.filter(res=res, at__lt=now - keep).delete()
            deleted += count
    return deleted
