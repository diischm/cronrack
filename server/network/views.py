"""The Network page, its JSON, and the two settings it has. Every query is scoped to
request.user: another user's host id is a 404, the same as one that doesn't exist."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from hosts.models import Host
from network.models import Network
from network.presenters import network_context
from network.services import set_view_host

MIN_CONFIRM, MAX_CONFIRM = 1, 20


@login_required
@require_GET
def network_page(request: HttpRequest) -> HttpResponse:
    return render(request, "network/index.html", network_context(request.user, timezone.now()))


@login_required
@require_GET
def network_json(request: HttpRequest) -> JsonResponse:
    return JsonResponse(network_context(request.user, timezone.now()))


def _back(request: HttpRequest) -> HttpResponse:
    """Back to the page the form was on: same-site paths only."""
    target = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(target, allowed_hosts=None):
        return redirect(target)
    return redirect(reverse("network"))


@login_required
@require_POST
def network_view(request: HttpRequest) -> HttpResponse:
    """Pick the host that measures the network: an id of the user's own reporting host, or
    "auto" (the server's own machine if detected, else the first that reported)."""
    network = Network.objects.filter(owner=request.user).first()
    if network is None:
        raise Http404
    choice = request.POST.get("host", "")
    if choice == "auto":
        set_view_host(network, None)
        messages.success(request, "The network view is automatic again.")
        return _back(request)
    if not choice.isascii() or not choice.isdigit():
        raise Http404
    host = get_object_or_404(
        Host, pk=int(choice), owner=request.user, network_reporter__isnull=False
    )
    set_view_host(network, host)
    messages.success(request, f"{host.name} now measures the network.")
    return _back(request)


@login_required
@require_POST
def network_settings(request: HttpRequest) -> HttpResponse:
    """How many reports in a row a new state must be seen before it counts."""
    network = Network.objects.filter(owner=request.user).first()
    if network is None:
        raise Http404
    raw = request.POST.get("confirm_reports", "")
    if raw.isascii() and raw.isdigit() and MIN_CONFIRM <= int(raw) <= MAX_CONFIRM:
        network.confirm_reports = int(raw)
        network.save(update_fields=["confirm_reports"])
        messages.success(request, "Saved.")
    else:
        messages.error(request, f"Enter a number from {MIN_CONFIRM} to {MAX_CONFIRM}.")
    return redirect(reverse("network"))
