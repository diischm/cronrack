"""Every rule CronRack knows, with its defaults. Users override them in the database.

Only the MVP rules are registered for now. Later phases add rules here without
touching the engine or the models.

Rule checks read the report defensively (.get, `or []`): any check may be null
when the agent couldn't run it, and a null check simply produces no findings.
"""

from collections.abc import Mapping
from typing import Any

from rules.base import RED, YELLOW, Finding, Rule, RuleContext

# --- connectivity --------------------------------------------------------------


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days"


def check_offline(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    # A host that never connected is "waiting", not offline.
    if ctx.connected or ctx.last_seen_at is None:
        return []
    silent = (ctx.now - ctx.last_seen_at).total_seconds()
    threshold = rule.threshold(ctx)
    if threshold is None or silent <= threshold:
        return []
    return [rule.finding(ctx, f"Last seen {_ago(silent)} ago")]


# --- updates -------------------------------------------------------------------


def check_security_updates(
    rule: Rule, report: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    updates = report.get("updates")
    if not updates or updates["security"] <= 0:
        return []
    count = updates["security"]
    return [rule.finding(ctx, f"{count} security update{'s' if count != 1 else ''}")]


def check_pending_updates(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    # Security updates already have their own (red) rule, so count only the others.
    updates = report.get("updates")
    if not updates:
        return []
    count = updates["total"] - updates["security"]
    if count <= 0:
        return []
    return [rule.finding(ctx, f"{count} update{'s' if count != 1 else ''} pending")]


def check_reboot_required(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    if report.get("reboot_required") is not True:
        return []
    return [rule.finding(ctx, "Reboot required")]


# --- docker --------------------------------------------------------------------


def _container_check(predicate, text: str):
    """Build a check that fires once per container matching `predicate`."""

    def check(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
        findings = []
        for container in report.get("containers") or []:
            if predicate(container):
                name = container["name"]
                findings.append(
                    rule.finding(
                        ctx,
                        text.format(name=name, exit_code=container["exit_code"]),
                        subject=f"container:{name}",
                        name=name,
                    )
                )
        return findings

    return check


# --- systemd -------------------------------------------------------------------


def check_failed_units(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    return [
        rule.finding(ctx, f"{unit} failed", subject=f"unit:{unit}", unit=unit)
        for unit in report.get("failed_units") or []
    ]


# --- disk ----------------------------------------------------------------------

DISK_RED_PERCENT = 95


def check_disk_usage(rule: Rule, report: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    """Yellow from the threshold (default 85%), red from 95%.

    If the user raises the threshold above 95, red starts at the threshold too,
    so a disk below the user's threshold never shows up.
    """
    findings = []
    for disk in report.get("disks") or []:
        mount, percent = disk["mount"], disk["percent"]
        subject = f"mount:{mount}"
        threshold = rule.threshold(ctx, subject)
        if threshold is None or percent < threshold:
            continue
        severity = RED if percent >= max(threshold, DISK_RED_PERCENT) else YELLOW
        findings.append(
            rule.finding(
                ctx, f"Disk {mount} at {percent}%", subject=subject, severity=severity, mount=mount
            )
        )
    return findings


# --- the registry --------------------------------------------------------------

DOCKER_LOGS_FIX = "docker logs --tail 50 {name}"

_ALL_RULES = (
    Rule(
        id="connectivity.offline",
        title="Host stopped checking in",
        category="connectivity",
        default_severity=RED,
        default_threshold=90,  # seconds of silence
        check=check_offline,
        why=(
            "The agent hasn't been in touch for a while. The host may be down, off the "
            "network, or the agent service may have stopped."
        ),
        fix_template="sudo systemctl status cronrack-agent",
    ),
    Rule(
        id="updates.security_pending",
        title="Security updates pending",
        category="updates",
        default_severity=RED,
        check=check_security_updates,
        why=(
            "Security updates fix known holes that attackers already know about. Install them soon."
        ),
        fix_template="sudo apt update && sudo apt upgrade",
    ),
    Rule(
        id="updates.pending",
        title="Updates pending",
        category="updates",
        default_severity=YELLOW,
        check=check_pending_updates,
        why="Regular updates bring bug fixes. Not urgent, but don't let them pile up.",
        fix_template="sudo apt update && sudo apt upgrade",
    ),
    Rule(
        id="updates.reboot_required",
        title="Reboot required",
        category="updates",
        default_severity=YELLOW,
        check=check_reboot_required,
        why=(
            "An update (often the kernel or a core library) only takes effect after a "
            "restart. Until then the host still runs the old code."
        ),
        fix_template="sudo reboot",
    ),
    Rule(
        id="docker.container_unhealthy",
        title="Container unhealthy",
        category="docker",
        default_severity=RED,
        check=_container_check(lambda c: c["health"] == "unhealthy", "{name} container unhealthy"),
        why=(
            "The container is running, but its own health check says it isn't working. "
            "Its logs usually say why."
        ),
        fix_template=DOCKER_LOGS_FIX,
    ),
    Rule(
        id="docker.container_restarting",
        title="Container restarting",
        category="docker",
        default_severity=RED,
        check=_container_check(lambda c: c["state"] == "restarting", "{name} container restarting"),
        why=(
            "The container keeps crashing and Docker keeps restarting it (a crash loop). "
            "Its logs usually show the error."
        ),
        fix_template=DOCKER_LOGS_FIX,
    ),
    Rule(
        id="docker.container_exited_error",
        title="Container exited with an error",
        category="docker",
        default_severity=RED,
        check=_container_check(
            lambda c: c["exit_code"] not in (0, None), "{name} exited with code {exit_code}"
        ),
        why=(
            "The container stopped with a non-zero exit code, which means it failed rather "
            "than finishing normally. Its last log lines usually say why."
        ),
        fix_template=DOCKER_LOGS_FIX,
    ),
    Rule(
        id="systemd.unit_failed",
        title="Systemd unit failed",
        category="systemd",
        default_severity=RED,
        check=check_failed_units,
        why=(
            "A system service or job failed. If it's something like a backup or a "
            "certificate renewal, it silently stops doing its job."
        ),
        fix_template="journalctl -u {unit} -n 50 --no-pager",
    ),
    Rule(
        id="disk.usage",
        title="Disk filling up",
        category="disk",
        default_severity=YELLOW,
        default_threshold=85,  # percent; red from 95
        check=check_disk_usage,
        why=(
            "When a disk fills up, databases corrupt, logs stop and containers fail in "
            "strange ways. Find what's using the space."
        ),
        fix_template="sudo du -xh --max-depth=1 {mount} | sort -h | tail -n 15",
    ),
)

RULES: dict[str, Rule] = {rule.id: rule for rule in _ALL_RULES}


def get_rule(rule_id: str) -> Rule:
    """Return the rule, or raise KeyError for an unknown ID."""
    return RULES[rule_id]
