"""Host pages and their JSON endpoints.

Access rule: every view that takes a host ID loads the host ONLY through
get_owned_host(), which answers 404 for anyone else's host (IDOR protection).
All views require login; POST views are CSRF-protected by CsrfViewMiddleware.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hosts.access import get_owned_host
from hosts.forms import DeleteHostForm, HostNameForm
from hosts.humanize import ago
from hosts.models import AgentToken, Host
from hosts.presenters import detail_context, overview_context
from hosts.services import close_agent, request_collect

AGENT_CONFIG_PATH = "/etc/cronrack/agent.toml"


def agent_setup(request: HttpRequest, host: Host, token: str) -> dict:
    """Context for the "shown once" token block: the agent.toml content and commands.

    The token exists only in this response. It is never stored, logged or put in a URL,
    and the response is marked no-store so the browser doesn't cache it either.
    """
    # get_host() is checked against ALLOWED_HOSTS, so it can't be a spoofed value.
    server_url = f"wss://{request.get_host()}/ws/agent/"
    config = f'server_url = "{server_url}"\ntoken = "{token}"\n'
    commands = (
        "sudo install -d -m 755 /etc/cronrack\n"
        # Create the file as 0600 first, so the token is never readable by others.
        f"sudo install -m 600 /dev/null {AGENT_CONFIG_PATH}\n"
        f"sudo tee {AGENT_CONFIG_PATH} > /dev/null <<'EOF'\n"
        f"{config}"
        "EOF\n"
        f"sudo chmod 600 {AGENT_CONFIG_PATH}"
    )
    return {"agent_config": config, "agent_commands": commands, "config_path": AGENT_CONFIG_PATH}


# --- overview ------------------------------------------------------------------


@login_required
@require_GET
def overview(request: HttpRequest) -> HttpResponse:
    return render(request, "hosts/overview.html", overview_context(request.user, timezone.now()))


@login_required
@require_GET
def overview_json(request: HttpRequest) -> JsonResponse:
    return JsonResponse(overview_context(request.user, timezone.now()))


# --- one host ------------------------------------------------------------------


@login_required
@require_GET
def host_detail(request: HttpRequest, host_id: int) -> HttpResponse:
    host = get_owned_host(request.user, host_id)
    return render(request, "hosts/detail.html", detail_context(host, timezone.now()))


@login_required
@require_GET
def host_status_json(request: HttpRequest, host_id: int) -> JsonResponse:
    host = get_owned_host(request.user, host_id)
    return JsonResponse(
        {"status": host.status, "last_seen_human": ago(host.last_seen_at, timezone.now())}
    )


@login_required
@require_POST
def host_collect(request: HttpRequest, host_id: int) -> JsonResponse:
    host = get_owned_host(request.user, host_id)
    request_collect(host)
    return JsonResponse({"ok": True}, status=202)


@login_required
@require_POST
def host_collect_all(request: HttpRequest) -> JsonResponse:
    for host in Host.objects.filter(owner=request.user, connected=True):
        request_collect(host)
    return JsonResponse({"ok": True}, status=202)


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def host_add(request: HttpRequest) -> HttpResponse:
    form = HostNameForm(request.POST or None, owner=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            host = Host.objects.create(owner=request.user, name=form.cleaned_data["name"])
            token = AgentToken.issue(host)
        context = {"host": host, **agent_setup(request, host, token)}
        return render(request, "hosts/add_done.html", context)
    return render(request, "hosts/add.html", {"form": form})


def _settings_page(request, host, name_form=None, delete_form=None, status=200):
    return render(
        request,
        "hosts/settings.html",
        {
            "host": host,
            "name_form": name_form or HostNameForm(owner=request.user, instance=host),
            "delete_form": delete_form or DeleteHostForm(host=host),
        },
        status=status,
    )


@login_required
@require_http_methods(["GET", "POST"])
def host_settings(request: HttpRequest, host_id: int) -> HttpResponse:
    host = get_owned_host(request.user, host_id)
    if request.method == "GET":
        return _settings_page(request, host)
    form = HostNameForm(request.POST, owner=request.user, instance=host)
    if not form.is_valid():
        return _settings_page(request, host, name_form=form, status=400)
    host.name = form.cleaned_data["name"]
    host.save(update_fields=["name"])
    messages.success(request, "Host renamed.")
    return redirect("host-settings", host_id=host.pk)


@never_cache
@login_required
@require_POST
def host_rotate(request: HttpRequest, host_id: int) -> HttpResponse:
    host = get_owned_host(request.user, host_id)
    # rotate() also closes the agent's open socket once the change is saved.
    token = AgentToken.rotate(host)
    return render(
        request, "hosts/token_rotated.html", {"host": host, **agent_setup(request, host, token)}
    )


@login_required
@require_POST
def host_revoke(request: HttpRequest, host_id: int) -> HttpResponse:
    host = get_owned_host(request.user, host_id)
    AgentToken.objects.filter(host=host).update(revoked=True)
    close_agent(host)
    messages.success(request, "Token revoked. The agent on this host can no longer connect.")
    return redirect("host-settings", host_id=host.pk)


@login_required
@require_POST
def host_delete(request: HttpRequest, host_id: int) -> HttpResponse:
    host = get_owned_host(request.user, host_id)
    form = DeleteHostForm(request.POST, host=host)
    if not form.is_valid():
        return _settings_page(request, host, delete_form=form, status=400)
    name = host.name
    close_agent(host)
    host.delete()  # cascades to its token and activity
    messages.success(request, f"Deleted {name}.")
    return redirect("overview")
