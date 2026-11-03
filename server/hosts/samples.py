"""Fixed sample data for pages that must render without real hosts.

Shared by the dev-only preview (cronrack.dev_views) and the /hosts/example/ page a new
user sees before adding a host (hosts.views.host_example). Nothing here comes from the
database or an agent, and none of it is ever counted anywhere.
"""

# Sample context in the shape of hosts.presenters.overview_context(), with the
# hosts from design-ref/cronrack-dashboard-desktop.html: one red, one yellow and
# two green hosts, plus three activity entries. homeserver and pi-01 are the k3s server and
# worker of one sample cluster (the role chips and the Clusters rack).
PREVIEW_CONTEXT = {
    "attention_count": 3,  # 2 hosts + the yellow cluster: the same set as the cards
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
            "k8s_role": "server",
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
            "k8s_role": "agent",
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
            "k8s_role": None,
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
            "k8s_role": None,
            "updates": {"total": 1, "security": 0},
            "containers": {"total": 4, "running": 4, "failing": 0},
            "disk_max_percent": 71,
        },
    ],
    # In the shape of clusters.presenters.cluster_summary().
    "clusters": [
        {
            "id": 1,
            "name": "homelab",
            "status": "yellow",
            "status_label": "Heads up",
            "issues": [{"text": "apps/web can't pull its image", "severity": "yellow"}],
            "last_seen_human": "25s ago",
            "url": "/clusters/1/",
            "nodes": {"ready": 2, "total": 2},
            "problem_pods": 1,
            "version": "v1.31.2+k3s1",
        }
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


# --- More preview states for the dev preview (cronrack.dev_views.preview, ?state=…) ----------
# Generic names only. attention_count and status_counts are worked out from the rows, the
# same way hosts.presenters.overview_context() does, so the headline and the cards agree.


def _host(pk, name, status, issues=(), disk=50, role=None, containers=4, failing=0, seen="20s ago"):
    labels = {"red": "Needs you", "yellow": "Heads up", "green": "All good", "offline": "Offline"}
    severity = "red" if status == "red" else "yellow"
    return {
        "id": pk,
        "name": name,
        "status": status,
        "status_label": labels[status],
        "issues": [{"text": text, "severity": severity} for text in issues],
        "last_seen_human": seen,
        "url": f"/hosts/{pk}/",
        "k8s_role": role,
        "updates": {"total": 2, "security": 1 if status == "yellow" else 0},
        "containers": {"total": containers, "running": containers - failing, "failing": failing},
        "disk_max_percent": disk,
    }


def _cluster(pk, name, status, issues=(), ready=3, total=3, problems=0):
    labels = {"red": "Needs you", "yellow": "Heads up", "green": "All good", "offline": "Offline"}
    return {
        "id": pk,
        "name": name,
        "status": status,
        "status_label": labels[status],
        "issues": [{"text": text, "severity": status} for text in issues],
        "last_seen_human": "30s ago",
        "url": f"/clusters/{pk}/",
        "nodes": {"ready": ready, "total": total},
        "problem_pods": problems,
        "version": "v1.31.2+k3s1",
    }


def _context(hosts, clusters, activity):
    statuses = [h["status"] for h in hosts]
    needs = {"red", "yellow", "offline"}
    return {
        "attention_count": sum(1 for h in hosts if h["status"] in needs)
        + sum(1 for c in clusters if c["status"] in needs),
        "updated_human": "30s ago",
        "status_counts": {s: statuses.count(s) for s in ("red", "yellow", "green", "offline")},
        "hosts_online": sum(1 for s in statuses if s != "offline"),
        "shell_hosts": {
            "connected": sum(1 for s in statuses if s != "offline"),
            "total": len(hosts),
        },
        "hosts": hosts,
        "clusters": clusters,
        "activity": activity,
    }


def _events(n):
    names = ["host-a", "host-b", "host-c", "host-d"]
    statuses = ["red", "green", "yellow", "green", "offline"]
    return [
        {
            "text": f"{names[i % 4]} went {statuses[i % 5]}",
            "time_human": f"{(23 - i) % 24:02d}:{(i * 7) % 60:02d}",
            "status": statuses[i % 5],
        }
        for i in range(n)
    ]


PREVIEW_STATES = {
    # 1 host only: no attention, no activity, no clusters.
    "one": lambda: _context([_host(1, "host-a", "green")], [], []),
    # 3 hosts + 1 cluster + 2 findings.
    "small": lambda: _context(
        [
            _host(1, "host-a", "red", ["web container exited (137)"], disk=91, failing=1),
            _host(2, "host-b", "yellow", ["2 security updates"], role="server"),
            _host(3, "host-c", "green", role="agent"),
        ],
        [_cluster(1, "cluster-a", "green")],
        _events(3),
    ),
    # 7 hosts + 3 clusters + 4 things needing you + the longest activity list.
    "busy": lambda: _context(
        [
            _host(1, "host-a", "red", ["backup.service failed", "disk / at 94%"], disk=94),
            _host(2, "host-b", "yellow", ["Reboot required"], role="server"),
            _host(3, "host-c", "offline", seen="3 days ago"),
            _host(4, "host-d", "green", role="agent"),
            _host(5, "host-e", "green", containers=12),
            _host(6, "host-f", "green", disk=67),
            _host(7, "host-g", "green", containers=0),
        ],
        [
            _cluster(1, "cluster-a", "red", ["2 pods crash-looping"], problems=2),
            _cluster(2, "cluster-b", "green"),
            _cluster(3, "cluster-c", "green", ready=1, total=1),
        ],
        _events(10),  # the most overview_context() sends (OVERVIEW_ACTIVITY)
    ),
    # Everything healthy: no attention, no activity.
    "healthy": lambda: _context(
        [_host(i, f"host-{c}", "green") for i, c in enumerate("abcd", start=1)],
        [_cluster(1, "cluster-a", "green")],
        [],
    ),
}
