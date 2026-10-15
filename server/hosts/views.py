"""Host pages and their JSON endpoints.

Access rule: every view that takes a host ID loads the host ONLY through
get_owned_host(), which answers 404 for anyone else's host (IDOR protection).
All views require login; POST views are CSRF-protected by CsrfViewMiddleware.
"""

from datetime import UTC

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.defaultfilters import pluralize
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from cronrack.http import wants_json
from hosts.access import get_owned_host
from hosts.forms import DeleteHostForm, HostNameForm
from hosts.humanize import ago
from hosts.models import AgentToken, Host
from hosts.presenters import detail_context, overview_context
from hosts.services import close_agent, request_collect
from rules.engine import effective_status

AGENT_CONFIG_PATH = "/etc/cronrack/agent.toml"
# Where Caddy keeps its own CA's root certificate (`tls internal`), inside the caddy container.
CADDY_ROOT_CA = "/data/caddy/pki/authorities/local/root.crt"
# The file name the instructions use for that certificate on the host.
CA_FILE_NAME = "cronrack-ca.crt"


def agent_setup(request: HttpRequest, host: Host, token: str) -> dict:
    """Context for the "shown once" token block: the install command and the token.

    The token exists only in this response. It is never stored, logged or put in a URL,
    and the response is marked no-store so the browser doesn't cache it either.
    """
    # The server's base URL. In docker compose that's always https://<CRONRACK_DOMAIN>.
    # Without CRONRACK_DOMAIN (local development) it's the URL the browser used;
    # get_host() is checked against ALLOWED_HOSTS, so it can't be a spoofed value.
    if settings.CRONRACK_DOMAIN:
        server_url = f"https://{settings.CRONRACK_DOMAIN}"
    else:
        server_url = request.build_absolute_uri("/").rstrip("/")
    # The token is NOT in the command: install.sh asks for it with hidden input, so it
    # never lands in shell history or the process list.
    install_command = (
        f"curl -fsSL {server_url}/agent/install.sh | sudo bash -s -- --server {server_url}"
    )
    # The same with Caddy's own CA (`tls internal`): curl and the agent trust exactly
    # that root certificate, which the user copies to the host first.
    install_command_ca = (
        f"curl -fsSL --cacert {CA_FILE_NAME} {server_url}/agent/install.sh"
        f" | sudo bash -s -- --server {server_url} --ca-file {CA_FILE_NAME}"
    )
    # Run on the CronRack server, in the folder with docker-compose.yml.
    # -T: no terminal, so the file gets plain \n line endings.
    ca_command = f"docker compose exec -T caddy cat {CADDY_ROOT_CA} > {CA_FILE_NAME}"
    config = f'server_url = "{server_url}"\ntoken = "{token}"\n'
    # Manual fallback (agent already installed, cronrack user exists). The file gets
    # its final owner and mode first; sudoedit then opens it in an editor, so the
    # token is pasted into the file, not typed into the shell.
    commands = (
        "sudo install -d -m 750 -o root -g cronrack /etc/cronrack\n"
        f"sudo install -m 640 -o root -g cronrack /dev/null {AGENT_CONFIG_PATH}\n"
        f"sudoedit {AGENT_CONFIG_PATH}\n"
        "sudo systemctl restart cronrack-agent"
    )
    return {
        "server_url": server_url,
        "install_command": install_command,
        "install_command_ca": install_command_ca,
        "ca_command": ca_command,
        "ca_file_name": CA_FILE_NAME,
        "token": token,
        "agent_config": config,
        "agent_commands": commands,
        "config_path": AGENT_CONFIG_PATH,
    }


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
    now = timezone.now()
    received = host.report_received_at
    return JsonResponse(
        {
            "status": effective_status(host, now),
            "last_seen_human": ago(host.last_seen_at, now),
            # A stable "new report" signal for Check now: it changes only when a report
            # is saved (last_seen_human is relative and changes every second on its own).
            "report_received_at": received.astimezone(UTC).isoformat() if received else None,
        }
    )


OFFLINE_ERROR = "Host is offline, so the agent can't be reached."


@login_required
@require_POST
def host_collect(request: HttpRequest, host_id: int) -> HttpResponse:
    """Check now: ask one host's agent to report.

    fetch (Accept: application/json) gets JSON; a plain form POST is redirected back
    to the host page (a fixed URL, never the Referer) with a flash message.
    """
    host = get_owned_host(request.user, host_id)
    if not host.connected:
        # Nothing to send to: no agent socket is open for this host.
        if wants_json(request):
            return JsonResponse({"ok": False, "error": OFFLINE_ERROR}, status=409)
        messages.error(request, OFFLINE_ERROR)
        return redirect("host-detail", host_id=host.pk)
    request_collect(host)
    if wants_json(request):
        return JsonResponse({"ok": True}, status=202)
    messages.success(request, f"Asked {host.name} to report now.")
    return redirect("host-detail", host_id=host.pk)


@login_required
@require_POST
def host_collect_all(request: HttpRequest) -> HttpResponse:
    """Check all now: ask every connected host the user owns; never anyone else's."""
    asked = offline = 0
    for host in Host.objects.filter(owner=request.user):
        if host.connected:
            request_collect(host)
            asked += 1
        else:
            offline += 1
    if wants_json(request):
        return JsonResponse({"ok": True, "asked": asked, "offline": offline}, status=202)
    messages.success(request, f"Asked {asked} host{pluralize(asked)} · {offline} offline.")
    return redirect("overview")


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
