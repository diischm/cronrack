"""Data collectors for the CronRack agent.

Each check is split into two functions:

- run_*   calls the system command and returns its raw stdout, or None if the
          command is missing, times out, or fails. No logic lives here.
- parse_* turns that raw text into plain dicts/lists. All logic lives here, so it
          can be tested against fixture files on a machine without the real tools.

Parsers never raise: empty or unexpected input gives an empty result, and lines
that don't look right are skipped. Everything they return is still untrusted
(container and unit names come from the host) and must be validated server-side.
"""

import json
import os
import re
import subprocess

COMMAND_TIMEOUT = 15

# Commands run with a fixed locale and timezone so their output looks the same on
# every host: apt translates "Listing...", systemd localizes weekday names, and
# timer timestamps come back in UTC (the report format uses UTC).
_COMMAND_ENV_OVERRIDES = {"LC_ALL": "C", "TZ": "UTC"}

REBOOT_REQUIRED_PATH = "/var/run/reboot-required"

SKIPPED_FS_TYPES = frozenset({"tmpfs", "devtmpfs", "overlay", "squashfs", "efivarfs"})

# Valid systemd unit name: ASCII letters, digits and ":-_.\@", plus a known unit type.
_UNIT_NAME = re.compile(
    r"[A-Za-z0-9:_.\\@-]+\."
    r"(service|socket|target|device|mount|automount|swap|timer|path|slice|scope)",
)

# One package line, e.g.
# curl/noble-updates,noble-security 8.5.0-2ubuntu10.6 amd64 [upgradable from: 8.5.0-2ubuntu10.4]
_APT_LINE = re.compile(
    r"(?P<name>[^\s/]+)/(?P<suites>\S+) \S+ \S+ \[upgradable from: [^\]]+\]",
)

# "Exited (137) 3 hours ago" -> 137
_DOCKER_EXIT_CODE = re.compile(r"Exited \((-?\d+)\)", re.ASCII)

# "Up 2 hours (healthy)", "Up 5 seconds (health: starting)" -> healthcheck state.
_DOCKER_HEALTH = re.compile(r"\((healthy|unhealthy|health: starting)\)", re.ASCII)
_DOCKER_HEALTH_VALUES = {
    "healthy": "healthy",
    "unhealthy": "unhealthy",
    "health: starting": "starting",
}

# A systemd timestamp, e.g. "Sun 2026-10-11 19:42:10 UTC".
_TIMESTAMP = re.compile(r"[A-Za-z]{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \S+", re.ASCII)

_PERCENT = re.compile(r"(\d{1,3})%", re.ASCII)


def _run(args: list[str]) -> str | None:
    """Run a command (no shell) and return stdout, or None on any failure."""
    env = {**os.environ, **_COMMAND_ENV_OVERRIDES}
    try:
        # B603 reviewed: argv is always a literal list from run_* callers; no shell, no external input.
        result = subprocess.run(  # nosec B603
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        # OSError covers FileNotFoundError (tool not installed) and
        # PermissionError (binary not executable for the agent user).
        return None
    if result.returncode != 0:
        return None
    return result.stdout


# --- 1. Package updates ---------------------------------------------------------


def run_apt_upgradable() -> str | None:
    return _run(["apt", "list", "--upgradable"])


def parse_apt_upgradable(text: str) -> dict:
    """Count upgradable packages, and how many come from a security pocket."""
    total = 0
    security = 0
    for line in text.splitlines():
        match = _APT_LINE.fullmatch(line.strip())
        if not match:
            continue  # "Listing..." header, blank lines, anything unexpected
        total += 1
        # Only look at the suite list ("noble-updates,noble-security"), not the
        # whole line, so a package *named* "something-security" isn't counted.
        if "-security" in match["suites"]:
            security += 1
    return {"total": total, "security": security}


# --- 2. Reboot required ---------------------------------------------------------


def check_reboot_required(path: str = REBOOT_REQUIRED_PATH) -> bool:
    """True if Debian/Ubuntu's reboot-required marker file exists."""
    # os.path.exists returns False (instead of raising) on permission errors.
    return os.path.exists(path)


# --- 3. Docker containers -------------------------------------------------------


def run_docker_ps() -> str | None:
    return _run(["docker", "ps", "-a", "--format", "{{json .}}"])


def _container_health(status: str) -> str | None:
    match = _DOCKER_HEALTH.search(status)
    return _DOCKER_HEALTH_VALUES[match[1]] if match else None


def _container_exit_code(status: str) -> int | None:
    match = _DOCKER_EXIT_CODE.match(status)
    return int(match[1]) if match else None


def parse_docker_ps(text: str) -> list[dict]:
    """One dict per container: name, state, status, health, exit_code.

    Facts only. Whether a container is a problem is decided on the server by
    the rule engine, so the user can override it.
    health:    "healthy" | "unhealthy" | "starting", or None without a healthcheck
    exit_code: the N in "Exited (N) ...", or None if the container hasn't exited
    """
    containers = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        name, state, status = data.get("Names"), data.get("State"), data.get("Status")
        if not all(isinstance(value, str) for value in (name, state, status)):
            continue
        containers.append(
            {
                "name": name,
                "state": state,
                "status": status,
                "health": _container_health(status),
                "exit_code": _container_exit_code(status),
            }
        )
    return containers


# --- 4. Failed systemd units ----------------------------------------------------


def run_systemctl_failed() -> str | None:
    return _run(["systemctl", "--failed", "--no-legend", "--plain"])


def parse_systemctl_failed(text: str) -> list[str]:
    """Names of failed units. Columns: UNIT LOAD ACTIVE SUB DESCRIPTION."""
    units = []
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == "●":  # bullet systemctl adds without --plain
            fields = fields[1:]
        if len(fields) < 4:
            continue
        unit, active = fields[0], fields[2]
        if _UNIT_NAME.fullmatch(unit) and active == "failed":
            units.append(unit)
    return units


# --- 5. Systemd timers ----------------------------------------------------------


def run_systemctl_timers() -> str | None:
    return _run(["systemctl", "list-timers", "--all", "--no-legend", "--plain"])


def _is_timer_name(word: str) -> bool:
    return word.endswith(".timer") and _UNIT_NAME.fullmatch(word) is not None


def parse_systemctl_timers(text: str) -> list[dict]:
    """One dict per timer: timer, activates, last_run, next_run.

    activates is None if the ACTIVATES column is missing or isn't a valid unit name.

    Columns are NEXT LEFT LAST PASSED UNIT ACTIVATES, but NEXT/LAST contain
    spaces ("Sun 2026-10-11 19:42:10 UTC"), LEFT/PASSED have a varying number of
    words ("8h", "1h 16min", "6 days ago"), and empty cells are "-" or "n/a".
    So we don't split by column position. Instead:

    1. UNIT and ACTIVATES are the last two words (unit names have no spaces).
       Take them off the end. If ACTIVATES is missing or garbled, take only UNIT.
    2. What's left is NEXT LEFT LAST PASSED. If it *starts* with a timestamp,
       that's NEXT; otherwise NEXT is empty.
    3. Search the rest for the next timestamp: that's LAST. LEFT never looks
       like a timestamp, so it's skipped over naturally. No match = never ran.
    """
    timers = []
    for line in text.splitlines():
        words = line.split()
        if len(words) >= 2 and _is_timer_name(words[-2]):
            timer = words[-2]
            activates = words[-1] if _UNIT_NAME.fullmatch(words[-1]) else None
            rest = words[:-2]
        elif words and _is_timer_name(words[-1]):
            timer, activates, rest = words[-1], None, words[:-1]
        else:
            continue
        # Re-join with single spaces so column padding doesn't break the regex.
        dates = " ".join(rest)
        next_match = _TIMESTAMP.match(dates)
        after_next = dates[next_match.end() :] if next_match else dates
        last_match = _TIMESTAMP.search(after_next)
        timers.append(
            {
                "timer": timer,
                "activates": activates,
                "last_run": last_match[0] if last_match else None,
                "next_run": next_match[0] if next_match else None,
            }
        )
    return timers


# --- 6. Disk usage --------------------------------------------------------------


def run_df() -> str | None:
    # -P: POSIX format, one line per filesystem (long device names don't wrap).
    # -T: add a Type column. Without it we can't reliably tell squashfs (shown as
    #     /dev/loopN) or devtmpfs (shown as "udev") apart from real disks.
    return _run(["df", "-P", "-T"])


def parse_df(text: str) -> list[dict]:
    """Mount point and percent used ("percent") for each real filesystem.

    Columns: Filesystem Type 1024-blocks Used Available Capacity Mounted-on.
    The mount point is last and may contain spaces, so split at most 6 times.
    """
    disks = []
    for line in text.splitlines():
        fields = line.strip().split(None, 6)
        if len(fields) != 7:
            continue
        fs_type, capacity, mount = fields[1], fields[5], fields[6]
        if fs_type in SKIPPED_FS_TYPES:
            continue
        percent = _PERCENT.fullmatch(capacity)
        if not percent:
            continue  # header line ("Capacity") or "-" for pseudo filesystems
        disks.append({"mount": mount, "percent": int(percent[1])})
    return disks
