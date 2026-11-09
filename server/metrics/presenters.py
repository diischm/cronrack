"""The host page's Metrics panel: charts from the history, numbers from the latest report.

`host` always comes from hosts.access.owned_host (or the page's own host), so every query
here is already scoped to the owner through that one host.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from hosts.models import Host
from metrics.charts import MAX_SMALL_POINTS, SMALL_HEIGHT, build_chart, downsample
from metrics.models import ContainerSample, HostSample, Resolution
from rules.base import Finding
from rules.engine import resolve_override

DEFAULT_RANGE = "24h"


# key -> (length, main resolution, finer resolution for the newest part, words, gap)
RANGES = {
    "1h": (timedelta(hours=1), Resolution.RAW, None, "last hour", timedelta(minutes=3)),
    "24h": (
        timedelta(hours=24),
        Resolution.FIVE_MIN,
        Resolution.RAW,
        "last 24 hours",
        timedelta(minutes=15),
    ),
    "7d": (
        timedelta(days=7),
        Resolution.HOUR,
        Resolution.FIVE_MIN,
        "last 7 days",
        timedelta(hours=3),
    ),
}
RANGE_CHIPS = (("1h", "1 h"), ("24h", "24 h"), ("7d", "7 d"))


def clean_range(value: str | None) -> str:
    """A range key from the query string; anything else is the default."""
    return value if value in RANGES else DEFAULT_RANGE


def human_bytes(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if amount < 1024 or unit == "PiB":
            text = f"{amount:.0f}" if unit == "B" or amount >= 100 else f"{amount:.1f}"
            return f"{text} {unit}{suffix}"
        amount /= 1024
    return "n/a"  # not reached: the loop always returns at PiB


def _rows(model, host: Host, range_key: str, now: datetime, columns: Sequence[str], **filters):
    """Rows of the range, oldest first: the main resolution, then finer rows for the part
    the scheduler hasn't averaged yet (so a 24-hour chart is current to the minute)."""
    length, res, finer, _words, _gap = RANGES[range_key]
    start = now - length
    query = model.objects.filter(host=host, at__gte=start, at__lte=now, **filters)
    rows = list(query.filter(res=res).order_by("at").values("at", *columns))
    if finer is not None:
        tail = query.filter(res=finer)
        if rows:
            tail = tail.filter(at__gt=rows[-1]["at"] + _bucket(res))
        rows += list(tail.order_by("at").values("at", *columns))
    return rows


def _bucket(res: int) -> timedelta:
    return timedelta(minutes=5) if res == Resolution.FIVE_MIN else timedelta(hours=1)


def _series(rows: list[dict], column: str) -> list[tuple[datetime, float]]:
    return [(r["at"], r[column]) for r in rows if r[column] is not None]


def _level(findings: list[Finding], rule_id: str, subject: str = "") -> str:
    severities = {f.severity for f in findings if f.rule_id == rule_id and f.subject == subject}
    if "red" in severities:
        return "red"
    return "yellow" if "yellow" in severities else ""


def _limit(rule_id: str, host: Host, overrides, notify, subject: str = "") -> float | None:
    """The limit in force for this host, or None when the rule is off for it."""
    effective = resolve_override(
        host.owner_id, rule_id, host, subject, overrides=overrides, notify=notify
    )
    return effective.threshold if effective.enabled else None


def _host_charts(host, report_metrics, findings, range_key, now, overrides, notify) -> list[dict]:
    length, _res, _finer, words, gap = RANGES[range_key]
    start = now - length
    rows = _rows(
        HostSample,
        host,
        range_key,
        now,
        ("cpu_pct", "mem_pct", "swap_pct", "disk_pct", "temp_c"),
    )
    common = {"range_words": words, "gap": gap}
    cpu = report_metrics.get("cpu") or {}
    memory = report_metrics.get("memory")
    disk_io = report_metrics.get("disk_io")
    mounts = report_metrics.get("mounts") or []

    extra_cpu = []
    if cpu.get("load"):
        extra_cpu.append(f"load {cpu['load'][0]:.2f}")
    if cpu.get("cores"):
        extra_cpu.append(f"{cpu['cores']} cores")

    free_limit = _limit("host.memory_high", host, overrides, notify)
    extra_memory = []
    if memory:
        extra_memory.append(f"{human_bytes(memory['available'])} free")
        if memory["swap_total"]:
            swap = memory["swap_used"] * 100.0 / memory["swap_total"]
            extra_memory.append(f"swap {swap:.0f}%")

    extra_disk = []
    if mounts:
        fullest = max(mounts, key=lambda m: m["used_pct"])
        extra_disk.append(f"fullest: {fullest['mount']}")
    if disk_io:
        extra_disk.append(f"read {human_bytes(disk_io['read_bps'], '/s')}")
        extra_disk.append(f"write {human_bytes(disk_io['write_bps'], '/s')}")

    charts = [
        build_chart(
            _series(rows, "cpu_pct"),
            start,
            now,
            title="CPU",
            unit="%",
            limit=_limit("host.cpu_high", host, overrides, notify),
            extra=" · ".join(extra_cpu),
            level=_level(findings, "host.cpu_high"),
            **common,
        ),
        build_chart(
            _series(rows, "mem_pct"),
            start,
            now,
            title="Memory",
            unit="%",
            limit=None if free_limit is None else 100 - free_limit,
            extra=" · ".join(extra_memory),
            level=_level(findings, "host.memory_high"),
            **common,
        ),
        build_chart(
            _series(rows, "disk_pct"),
            start,
            now,
            title="Disk",
            unit="%",
            limit=_limit("disk.usage", host, overrides, notify),
            extra=" · ".join(extra_disk),
            **common,
        ),
    ]
    temperatures = _series(rows, "temp_c")
    if cpu.get("temp_c") is not None or temperatures:  # only hosts with a sensor
        charts.append(
            build_chart(
                temperatures,
                start,
                now,
                title="Temperature",
                unit=" °C",
                limit=_limit("host.cpu_temp_high", host, overrides, notify),
                level=_level(findings, "host.cpu_temp_high"),
                **common,
            )
        )
    for index, chart in enumerate(charts):
        chart["clip_id"] = f"mchart-clip-{index}"
    return charts


def _container_rows(host, data, findings, range_key, now, overrides, notify) -> list[dict] | None:
    containers = data.get("containers")
    if containers is None:
        return None
    length, _res, _finer, words, gap = RANGES[range_key]
    start = now - length
    history: dict[str, list[dict]] = {}
    # Only the containers in the latest report: old names never cost this page anything.
    for row in _rows(
        ContainerSample,
        host,
        range_key,
        now,
        ("name", "cpu_pct", "mem", "mem_limit"),
        name__in=[c["name"] for c in containers],
    ):
        history.setdefault(row["name"], []).append(row)

    # Downsampling widens the spacing between points, so the "this is an outage" gap widens too.
    common = {
        "range_words": words,
        "gap": max(gap, length / MAX_SMALL_POINTS * 2),
        "height": SMALL_HEIGHT,
    }
    rows = []
    for index, container in enumerate(sorted(containers, key=lambda c: c["name"])):
        name, limit_bytes = container["name"], container["mem_limit"]
        subject = f"container:{name}"
        past = history.get(name, [])
        charts = [
            build_chart(
                downsample(_series(past, "cpu_pct"), MAX_SMALL_POINTS),
                start,
                now,
                title="CPU",
                unit="%",
                limit=_limit("docker.container_cpu_high", host, overrides, notify, subject),
                level=_level(findings, "docker.container_cpu_high", subject),
                **common,
            )
        ]
        level = _level(findings, "docker.container_memory_near_limit", subject)
        percent = None
        if limit_bytes:
            percent = None if container["mem"] is None else container["mem"] * 100.0 / limit_bytes
            of_limit = [
                (r["at"], r["mem"] * 100.0 / r["mem_limit"])
                for r in past
                if r["mem"] is not None and r["mem_limit"]
            ]
            charts.append(
                build_chart(
                    downsample(of_limit, MAX_SMALL_POINTS),
                    start,
                    now,
                    title="Memory of its limit",
                    unit="%",
                    limit=_limit(
                        "docker.container_memory_near_limit", host, overrides, notify, subject
                    ),
                    level=level,
                    **common,
                )
            )
        for chart_index, chart in enumerate(charts):
            chart["clip_id"] = f"mchart-clip-c{index}-{chart_index}"
        cpu = container["cpu_pct"]
        rows.append(
            {
                "index": index,
                "name": name,
                "cpu": "n/a" if cpu is None else f"{cpu:.1f}%",
                "mem": human_bytes(container["mem"]),
                "limit": human_bytes(limit_bytes) if limit_bytes else "",
                "percent": None if percent is None else max(0, min(100, round(percent))),
                "level": level,
                "charts": charts,
            }
        )
    return rows


def metrics_panel(
    host: Host,
    report: dict | None,
    findings: list[Finding],
    now: datetime,
    range_key: str,
    overrides,
    notify,
) -> dict[str, Any] | None:
    """The Metrics panel, or None for a host that has never sent metrics (no empty panel)."""
    data = (report or {}).get("metrics")
    data = data if isinstance(data, dict) else {}
    if not data and not HostSample.objects.filter(host=host).exists():
        return None
    return {
        "range": range_key,
        "chips": [
            {"key": key, "label": label, "current": key == range_key} for key, label in RANGE_CHIPS
        ],
        "charts": _host_charts(host, data, findings, range_key, now, overrides, notify),
        "containers": _container_rows(host, data, findings, range_key, now, overrides, notify),
        "truncated": data.get("containers_truncated") is True,
    }
