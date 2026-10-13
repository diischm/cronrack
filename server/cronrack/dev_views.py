"""Development-only pages. Each view returns 404 unless DEBUG is on."""

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render

# Sample context from FRONTEND-HANDOFF.md section 6.2: one red, one yellow and one
# green host, plus three activity entries. Real data comes with the Phase 6 views.
PREVIEW_CONTEXT = {
    "attention_count": 2,
    "updated_human": "30s ago",
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
            "last_seen_human": "25s ago",
            "url": "/hosts/1/",
        },
        {
            "id": 2,
            "name": "nas",
            "status": "green",
            "status_label": "All good",
            "issues": [],
            "last_seen_human": "12s ago",
            "url": "/hosts/2/",
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
        context.update(attention_count=0, hosts=[], activity=[])
    return render(request, "hosts/overview.html", context)
