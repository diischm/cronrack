"""Resource rules: CPU, memory, swap, temperature, inodes and per-container usage.

They read the report's "metrics" key (agent 0.4.0+). A report without it (an older agent,
or metrics switched off on the host) produces no findings, so nothing changes for them.

"Sustained" rules never fire on one report. They look at the host's raw samples of the
last few minutes (ctx.history, metrics.windows.HostHistory) and fire only when
- the samples cover the whole window, less COVERAGE_SLACK for a report that came a few
  seconds late: ten minutes means ten minutes, not the first reports after a restart;
- there is no gap longer than MAX_GAP (a host that was offline isn't judged on old data);
- at least SHARE_OVER of the samples are over the limit, so one quiet minute in a busy
  stretch doesn't end it, and one spike never starts it.
Resolving then goes through the usual flap guard (notifications.dispatch).

Subjects are stable keys ("" host-wide, "mount:/", "container:nginx"); the numbers are in
the text, so one problem keeps one notification row while its value moves.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from rules.base import RED, YELLOW, Finding, Rule, RuleContext

COVERAGE_SLACK = timedelta(seconds=30)
SHARE_OVER = 0.8
MAX_GAP = timedelta(minutes=3)
MIN_SAMPLES = 3

CPU_WINDOW = timedelta(minutes=10)
MEMORY_WINDOW = timedelta(minutes=5)
TEMP_WINDOW = timedelta(minutes=5)
CONTAINER_CPU_WINDOW = timedelta(minutes=10)
CONTAINER_MEMORY_WINDOW = timedelta(minutes=5)

TEMP_RED_C = 85
INODES_RED_PCT = 95


def sustained(
    points: Sequence[tuple[datetime, float]],
    now: datetime,
    window: timedelta,
    over: Callable[[float], bool],
) -> bool:
    """True if `over` held for (nearly) all of the last `window`. See the module docstring."""
    # Start from the newest sample at or before the window's start: it proves the window
    # is covered, whatever the phase of the reports (61 s apart, or a page rendered 45 s
    # after the last one). Judging only the points inside would flicker on and off.
    points = [point for point in points if point[0] <= now]
    latest_start = now - window + COVERAGE_SLACK
    anchors = [index for index, (at, _) in enumerate(points) if at <= latest_start]
    if not anchors:
        return False
    recent = points[anchors[-1] :]
    if len(recent) < MIN_SAMPLES:
        return False
    times = [at for at, _ in recent]
    edges = [*times, now]
    if any(later - earlier > MAX_GAP for earlier, later in pairwise(edges)):
        return False
    hits = sum(1 for _, value in recent if over(value))
    return hits >= SHARE_OVER * len(recent)


def _metrics(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    data = report.get("metrics")
    return data if isinstance(data, Mapping) else None


def _minutes(window: timedelta) -> int:
    return int(window.total_seconds() // 60)


# --- host-wide ---------------------------------------------------------------------------


def check_cpu_high(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    limit = rule.threshold(ctx)
    if _metrics(report) is None or limit is None or ctx.history is None:
        return []
    points = ctx.history.host("cpu_pct")
    if not sustained(points, ctx.now, CPU_WINDOW, lambda v: v >= limit):
        return []
    minutes = _minutes(CPU_WINDOW)
    return [rule.finding(ctx, f"CPU at {points[-1][1]:.0f}% for {minutes}+ min")]


def check_memory_high(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    """The limit is the share of memory still AVAILABLE (MemAvailable): under it fires."""
    limit = rule.threshold(ctx)
    if _metrics(report) is None or limit is None or ctx.history is None:
        return []
    points = ctx.history.host("mem_pct")  # used %
    if not sustained(points, ctx.now, MEMORY_WINDOW, lambda used: 100 - used < limit):
        return []
    return [rule.finding(ctx, f"Memory low: {100 - points[-1][1]:.0f}% available")]


def check_swap_high(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    data, limit = _metrics(report), rule.threshold(ctx)
    memory = data.get("memory") if data else None
    if not memory or limit is None or memory["swap_total"] <= 0:
        return []
    used = memory["swap_used"] * 100.0 / memory["swap_total"]
    if used < limit:
        return []
    return [rule.finding(ctx, f"Swap {used:.0f}% used")]


def check_cpu_temp_high(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    """Yellow from the limit, red from 85 °C (or the limit, if that is higher)."""
    data, limit = _metrics(report), rule.threshold(ctx)
    cpu = data.get("cpu") if data else None
    if not cpu or cpu.get("temp_c") is None or limit is None or ctx.history is None:
        return []  # no sensor on this host: the rule doesn't apply
    points = ctx.history.host("temp_c")
    if not sustained(points, ctx.now, TEMP_WINDOW, lambda v: v >= limit):
        return []
    red_from = max(limit, TEMP_RED_C)
    red = sustained(points, ctx.now, TEMP_WINDOW, lambda v: v >= red_from)
    text = f"CPU temperature {points[-1][1]:.0f} °C"
    return [rule.finding(ctx, text, severity=RED if red else YELLOW)]


# --- disks -------------------------------------------------------------------------------


def check_inodes_low(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    """Yellow from the limit, red from 95 % (or the limit, if that is higher)."""
    data = _metrics(report)
    findings = []
    for mount in (data.get("mounts") or []) if data else []:
        used = mount.get("inodes_pct")
        subject = f"mount:{mount['mount']}"
        limit = rule.threshold(ctx, subject)
        if used is None or limit is None or used < limit:
            continue
        severity = RED if used >= max(limit, INODES_RED_PCT) else YELLOW
        findings.append(
            rule.finding(
                ctx,
                f"Inodes on {mount['mount']} {used:.0f}% used",
                subject=subject,
                severity=severity,
                mount=mount["mount"],
            )
        )
    return findings


# --- containers --------------------------------------------------------------------------


def _containers(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = _metrics(report)
    return list((data.get("containers") or []) if data else [])


def check_container_cpu_high(
    rule: Rule, report: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    if ctx.history is None:
        return []
    findings = []
    for container in _containers(report):
        name = container["name"]
        subject = f"container:{name}"
        limit = rule.threshold(ctx, subject)
        if limit is None:
            continue
        points = ctx.history.container_cpu(name)
        if sustained(points, ctx.now, CONTAINER_CPU_WINDOW, lambda v, lim=limit: v >= lim):
            text = f"{name} container CPU at {points[-1][1]:.0f}% of the host"
            findings.append(rule.finding(ctx, text, subject=subject, name=name))
    return findings


def check_container_memory_near_limit(
    rule: Rule, report: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    """Only containers that HAVE a memory limit: without one there is nothing to be near."""
    if ctx.history is None:
        return []
    findings = []
    for container in _containers(report):
        if not container.get("mem_limit"):
            continue
        name = container["name"]
        subject = f"container:{name}"
        limit = rule.threshold(ctx, subject)
        if limit is None:
            continue
        points = ctx.history.container_mem_of_limit(name)
        if sustained(points, ctx.now, CONTAINER_MEMORY_WINDOW, lambda v, lim=limit: v >= lim):
            text = f"{name} container memory at {points[-1][1]:.0f}% of its limit"
            findings.append(rule.finding(ctx, text, subject=subject, name=name))
    return findings


TOP_CPU_FIX = "ps -eo pid,comm,%cpu --sort=-%cpu | head -n 10"
TOP_MEM_FIX = "ps -eo pid,comm,%mem,rss --sort=-%mem | head -n 10"
DOCKER_STATS_FIX = "docker stats --no-stream {name}"

RESOURCE_RULES = (
    Rule(
        id="host.cpu_high",
        title="CPU busy for a long time",
        category="resources",
        default_severity=YELLOW,
        default_threshold=90,  # percent busy, for 10 minutes
        check=check_cpu_high,
        why=(
            "The processor has been almost fully busy for ten minutes or more. Everything on "
            "the host gets slower. A runaway process or a stuck job is the usual cause."
        ),
        fix_template=TOP_CPU_FIX,
    ),
    Rule(
        id="host.memory_high",
        title="Memory running out",
        category="resources",
        default_severity=YELLOW,
        default_threshold=10,  # percent still available, for 5 minutes
        check=check_memory_high,
        why=(
            "Almost no memory is left for new work. When it runs out, Linux starts killing "
            "processes (the OOM killer), often the biggest one: a database or a container."
        ),
        fix_template=TOP_MEM_FIX,
    ),
    Rule(
        id="host.swap_high",
        title="Swap filling up",
        category="resources",
        default_severity=YELLOW,
        default_threshold=75,  # percent of swap used
        check=check_swap_high,
        why=(
            "The host is moving memory to disk. A little is normal; a lot means it doesn't "
            "have enough RAM for what runs on it, and it gets slow."
        ),
        fix_template=TOP_MEM_FIX,
    ),
    Rule(
        id="host.cpu_temp_high",
        title="CPU running hot",
        category="resources",
        default_severity=YELLOW,
        default_threshold=80,  # °C for 5 minutes; red from 85
        check=check_cpu_temp_high,
        why=(
            "The processor is hot. Around 80 °C most boards (a Raspberry Pi from 80 °C) slow "
            "themselves down to cool off. Check the fan, the case and the dust."
        ),
        fix_template=TOP_CPU_FIX,
    ),
    Rule(
        id="disk.inodes_low",
        title="Disk running out of inodes",
        category="disk",
        default_severity=YELLOW,
        default_threshold=90,  # percent of inodes used; red from 95
        check=check_inodes_low,
        why=(
            "Every file needs an inode. When they run out, no new file can be created even "
            "if there is free space. Millions of tiny files (caches, mail, thumbnails) do it."
        ),
        fix_template="df -i {mount}",
    ),
    Rule(
        id="docker.container_cpu_high",
        title="Container CPU busy",
        category="docker",
        default_severity=YELLOW,
        default_threshold=90,  # percent of the whole host, for 10 minutes
        check=check_container_cpu_high,
        why=(
            "One container has been using most of the host's processor for ten minutes or "
            "more. It may be stuck in a loop, or just doing a big job such as transcoding."
        ),
        fix_template=DOCKER_STATS_FIX,
    ),
    Rule(
        id="docker.container_memory_near_limit",
        title="Container near its memory limit",
        category="docker",
        default_severity=YELLOW,
        default_threshold=90,  # percent of its own memory limit, for 5 minutes
        check=check_container_memory_near_limit,
        why=(
            "The container is close to the memory limit set for it. At the limit the kernel "
            "kills it, and Docker restarts it if it should. Raise the limit or find the leak."
        ),
        fix_template=DOCKER_STATS_FIX,
    ),
)
