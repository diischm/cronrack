"""Development-only pages. Each view returns 404 unless DEBUG is on."""

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render

# Sample context in the shape of hosts.presenters.overview_context(), with the
# hosts from design-ref/cronrack-dashboard-desktop.html: one red, one yellow and
# two green hosts, plus three activity entries.
PREVIEW_CONTEXT = {
    "attention_count": 2,
    "updated_human": "30s ago",
    "status_counts": {"red": 1, "yellow": 1, "green": 2},
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


def preview(request: HttpRequest) -> HttpResponse:
    """Render the overview with sample data, so the design can be checked without hosts.

    The URL always exists, but answers 404 unless DEBUG is on, so a production
    server shows nothing here.
    """
    if not settings.DEBUG:
        raise Http404
    context = dict(PREVIEW_CONTEXT)
    if request.GET.get("state") == "empty":
        context.update(
            attention_count=0,
            status_counts={"red": 0, "yellow": 0, "green": 0},
            hosts_online=0,
            shell_hosts={"connected": 0, "total": 0},
            hosts=[],
            activity=[],
        )
    return render(request, "hosts/overview.html", context)


def styleguide(request: HttpRequest) -> HttpResponse:
    """Every design-system component in every state, in both themes.

    Same guard as preview(): the URL always exists, but answers 404 unless DEBUG is on.
    """
    if not settings.DEBUG:
        raise Http404
    return render(request, "dev/styleguide.html")
