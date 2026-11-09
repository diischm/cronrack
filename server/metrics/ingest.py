"""Store a report's metrics as raw samples (agents.consumers.ingest_report calls this).

`data` is the report's "metrics" key after MetricsSerializer, or None (an agent before
0.4.0, metrics switched off, or a metrics part that failed validation). None stores nothing.

At most one raw sample per host every MIN_RAW_GAP seconds is kept, however often the agent
reports ("Check now", reconnects), and at most MAX_CONTAINER_NAMES / MAX_MOUNT_NAMES
different names per host in any 24 hours. Together they keep a misbehaving agent (one that
invents new names in every report) from filling the database: the number of rows per host
is bounded by time and by those caps, never by what the agent sends.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from hosts.models import Host
from metrics.models import ContainerSample, HostSample, MountSample, Resolution

MIN_RAW_GAP = timedelta(seconds=50)
# Twice the agent's own per-report caps: room for renames and redeploys within a day.
MAX_CONTAINER_NAMES = 100
MAX_MOUNT_NAMES = 64
NAME_WINDOW = timedelta(hours=24)


def _within_name_cap(model, field: str, host: Host, now: datetime, items: list, cap: int) -> list:
    """The items whose name was already seen in the last 24 h, or that still fit under `cap`."""
    known = set(
        model.objects.filter(host=host, res=Resolution.RAW, at__gte=now - NAME_WINDOW)
        .values_list(field, flat=True)
        .distinct()
    )
    room = cap - len(known)
    kept = []
    for item in items:
        if item[field] in known:
            kept.append(item)
        elif room > 0:
            room -= 1
            kept.append(item)
    return kept


def _pct(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(max(0.0, min(100.0, part * 100.0 / whole)), 1)


def host_values(data: Mapping[str, Any]) -> dict[str, Any]:
    """The host-wide numbers of one report, as HostSample columns."""
    cpu = data.get("cpu") or {}
    memory = data.get("memory")
    disk_io = data.get("disk_io")
    mounts = data.get("mounts") or []
    load = cpu.get("load")
    return {
        "cpu_pct": cpu.get("busy_pct"),
        "iowait_pct": cpu.get("iowait_pct"),
        "load1": load[0] if load else None,
        "mem_pct": _pct(memory["total"] - memory["available"], memory["total"]) if memory else None,
        "swap_pct": _pct(memory["swap_used"], memory["swap_total"]) if memory else None,
        "temp_c": cpu.get("temp_c"),
        "disk_pct": max((m["used_pct"] for m in mounts), default=None),
        "read_bps": disk_io["read_bps"] if disk_io else None,
        "write_bps": disk_io["write_bps"] if disk_io else None,
    }


def save_metrics(host: Host, data: Mapping[str, Any] | None, now: datetime) -> bool:
    """Store one raw sample set. False when there was nothing to store, or it was too soon."""
    if not data:
        return False
    recent = HostSample.objects.filter(
        host=host, res=Resolution.RAW, at__gt=now - MIN_RAW_GAP, at__lte=now
    )
    if recent.exists():
        return False
    at = now.replace(microsecond=0)
    HostSample.objects.create(host=host, res=Resolution.RAW, at=at, **host_values(data))
    MountSample.objects.bulk_create(
        [
            MountSample(
                host=host,
                res=Resolution.RAW,
                at=at,
                mount=m["mount"],
                used_pct=m["used_pct"],
                inodes_pct=m["inodes_pct"],
            )
            for m in _within_name_cap(
                MountSample, "mount", host, now, data.get("mounts") or [], MAX_MOUNT_NAMES
            )
        ]
    )
    ContainerSample.objects.bulk_create(
        [
            ContainerSample(
                host=host,
                res=Resolution.RAW,
                at=at,
                name=c["name"],
                cpu_pct=c["cpu_pct"],
                mem=c["mem"],
                mem_limit=c["mem_limit"],
                rx_bps=c["rx_bps"],
                tx_bps=c["tx_bps"],
            )
            for c in _within_name_cap(
                ContainerSample,
                "name",
                host,
                now,
                data.get("containers") or [],
                MAX_CONTAINER_NAMES,
            )
        ]
    )
    return True
