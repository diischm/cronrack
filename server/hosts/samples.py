"""Fixed sample data for pages that must render without real hosts.

Shared by the dev-only preview (cronrack.dev_views) and the /hosts/example/ page a new
user sees before adding a host (hosts.views.host_example). Nothing here comes from the
database or an agent, and none of it is ever counted anywhere.
"""

# Sample context in the shape of hosts.presenters.overview_context(), with the
# hosts from design-ref/cronrack-dashboard-desktop.html: one red, one yellow and
# two green hosts, plus three activity entries.
PREVIEW_CONTEXT = {
    "attention_count": 2,
    "updated_human": "30s ago",
    "status_counts": {"red": 1, "yellow": 1, "green": 2, "offline": 0},
    "hosts_online": 4,
    # Normally set by hosts.context_processors.shell from the database.
    "shell_hosts": {"connected": 4, "total": 4},
    "hosts": [
        {
            "id": 3,
            "name": "homeserver",
            "status": "red",
            "status_label": "Needs you",
            "issues": [
                {"text": "3 security updates", "severity": "red"},
                {"text": "nginx container restarting", "severity": "red"},
            ],
            "last_seen_human": "40s ago",
            "url": "/hosts/3/",
            "updates": {"total": 12, "security": 3},
            "containers": {"total": 9, "running": 8, "failing": 1},
            "disk_max_percent": 62,
        },
        {
            "id": 1,
            "name": "pi-01",
            "status": "yellow",
            "status_label": "Heads up",
            "issues": [
                {"text": "Reboot required", "severity": "yellow"},
                {"text": "Disk / at 87%", "severity": "yellow"},
            ],
            "last_seen_human": "22s ago",
            "url": "/hosts/1/",
            "updates": {"total": 4, "security": 0},
            "containers": {"total": 3, "running": 3, "failing": 0},
            "disk_max_percent": 87,
        },
        {
            "id": 2,
            "name": "nas",
            "status": "green",
            "status_label": "All good",
            "issues": [],
            "last_seen_human": "15s ago",
            "url": "/hosts/2/",
            "updates": {"total": 0, "security": 0},
            "containers": {"total": 6, "running": 6, "failing": 0},
            "disk_max_percent": 48,
        },
        {
            "id": 4,
            "name": "media-box",
            "status": "green",
            "status_label": "All good",
            "issues": [],
            "last_seen_human": "31s ago",
            "url": "/hosts/4/",
            "updates": {"total": 1, "security": 0},
            "containers": {"total": 4, "running": 4, "failing": 0},
            "disk_max_percent": 71,
        },
    ],
    "activity": [
        {"text": "homeserver went red", "time_human": "14:32", "status": "red"},
        {"text": "nas back online", "time_human": "11:05", "status": "green"},
        {"text": "Nightly backup timer ran", "time_human": "03:00", "status": "green"},
    ],
}

# The host detail page (/hosts/example/) in the shape of hosts.presenters.detail_context():
# two yellow issues ("Heads up", matching the Welcome unit's amber lamp on the overview),
# and the Docker, Systemd and Disk sections filled in.
EXAMPLE_DETAIL_CONTEXT = {
    "example": True,
    "host": {
        "id": 0,
        "name": "Welcome to CronRack!",
        "status": "yellow",
        "status_label": "Heads up",
        "last_seen_human": "40s ago",
        "has_report": True,
        "agent_version": "0.1.0",
    },
    "issues": [
        {
            "fingerprint": "",
            "rule_id": "updates.reboot_required",
            "subject": "",
            "text": "Reboot required",
            "severity": "yellow",
            "why": "",
            "fix": "",
            "category": "updates",
        },
        {
            "fingerprint": "",
            "rule_id": "disk.usage",
            "subject": "mount:/",
            "text": "Disk / at 87%",
            "severity": "yellow",
            "why": "",
            "fix": "",
            "category": "disk",
        },
    ],
    "updates": {"total": 4, "security": 0, "reboot_required": True},
    "containers": {
        "problems": [],
        "healthy": [
            {"name": "nginx", "status": "Up 3 days"},
            {"name": "postgres", "status": "Up 3 days"},
            {"name": "jellyfin", "status": "Up 3 days"},
        ],
    },
    "failed_units": [],
    "timers": [
        {"name": "apt-daily.timer", "last_run": "Today 06:12", "next_run": "Tomorrow 06:10"},
        {"name": "backup.timer", "last_run": "Today 03:00", "next_run": "Tomorrow 03:00"},
    ],
    "disks": [
        {"mount": "/", "percent": 87, "level": "yellow"},
        {"mount": "/srv/media", "percent": 48, "level": "green"},
    ],
}
