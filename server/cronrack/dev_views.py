"""Development-only pages. Each view returns 404 unless DEBUG is on."""

import copy

from django.conf import settings
from django.contrib.staticfiles.views import serve
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render

from hosts.samples import PREVIEW_CONTEXT

XSS_SCRIPT = "<script>alert(1)</script>"
XSS_IMG = '"><img src=x onerror=alert(1)>'


def preview(request: HttpRequest) -> HttpResponse:
    """Render the overview with sample data, so the design can be checked without hosts.

    ?state=empty shows the 0-hosts page, ?state=xss puts hostile text into the data.
    The URL always exists, but answers 404 unless DEBUG is on, so a production
    server shows nothing here.
    """
    if not settings.DEBUG:
        raise Http404
    context = copy.deepcopy(PREVIEW_CONTEXT)
    state = request.GET.get("state")
    if state == "xss":
        # Security self-test (handoff 8.3): hostile text as a host name and as issue
        # text. The page must show both as plain text.
        context["hosts"][0]["name"] = XSS_SCRIPT
        context["hosts"][0]["issues"][0]["text"] = XSS_IMG
        context["hosts"][1]["name"] = XSS_IMG
        context["hosts"][1]["issues"][0]["text"] = XSS_SCRIPT
    elif state == "empty":
        context.update(
            attention_count=0,
            status_counts={"red": 0, "yellow": 0, "green": 0, "offline": 0},
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


def static(request: HttpRequest, path: str) -> HttpResponse:
    """Serve a static file in development, and make the browser re-check it on every load.

    With DEBUG=1 the files keep their plain names (overview.js), so a browser that cached an
    old copy would run old JavaScript against new HTML. "no-cache" doesn't mean "don't
    store": the browser keeps the file but asks first, and gets a quick 304 if it's unchanged.
    Production never reaches this view: Caddy serves the hashed files with its own headers.
    """
    if not settings.DEBUG:
        raise Http404
    response = serve(request, path)
    response["Cache-Control"] = "no-cache"
    return response
