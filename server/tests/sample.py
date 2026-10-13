"""A valid agent report (the shape agent/report.py builds), for tests to modify."""

import copy
from typing import Any

SAMPLE_REPORT: dict[str, Any] = {
    "hostname": "nas",
    "agent_version": "0.1.0",
    "timestamp": "2026-10-12T12:00:00Z",
    "updates": {"total": 0, "security": 0},
    "reboot_required": False,
    "containers": [
        {
            "name": "nginx",
            "state": "running",
            "status": "Up 2 hours (healthy)",
            "health": "healthy",
            "exit_code": None,
        }
    ],
    "failed_units": [],
    "timers": [
        {
            "timer": "apt-daily.timer",
            "activates": "apt-daily.service",
            "last_run": "Sun 2026-10-11 19:42:10 UTC",
            "next_run": "Mon 2026-10-12 06:12:00 UTC",
        }
    ],
    "disks": [{"mount": "/", "percent": 42}],
    "errors": [],
}


def make_report(**changes: Any) -> dict[str, Any]:
    """A deep copy of SAMPLE_REPORT with top-level keys replaced."""
    report = copy.deepcopy(SAMPLE_REPORT)
    report.update(changes)
    return report
