"""Host pages and their JSON endpoints.

Access rule: every view that takes a host ID loads the host ONLY through
get_owned_host(), which answers 404 for anyone else's host (IDOR protection).
All views require login; POST views are CSRF-protected by CsrfViewMiddleware.
"""

import secrets

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
from hosts.presenters import (
    detail_context,
    host_preview,
    host_status,
    iso_utc,
    overview_context,
)
from hosts.samples import EXAMPLE_DETAIL_CONTEXT
from hosts.services import close_agent, just_reported, request_collect
from rules.engine import effective_status

AGENT_CONFIG_PATH = "/etc/cronrack/agent.toml"
# Session entry holding the unused one-time keys of the Rotate token form: {key: host id}.
ROTATE_KEYS_SESSION = "rotate_keys"
# Each settings page view adds a key; keep only the newest few so the session stays small.
MAX_ROTATE_KEYS = 20
ROTATE_AGAIN = "This token was already rotated. Rotate again?"
# Rotate and revoke cut the agent off, so their POST must say so: a ticked checkbox
# (no JS) or the confirm dialog (JS) sends confirm=yes. Anything else changes nothing.
CONFIRM_VALUE = "yes"
CONFIRM_ERROR = "Tick the box to confirm."

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


@login_required
@require_GET
def host_example(request: HttpRequest) -> HttpResponse:
    """The Welcome example's page: the detail template filled with fixed sample data.

    Only for users without hosts (the overview links here only then). Anyone who already
    has a host is sent to the overview, a fixed URL. Nothing is read from or written to
    the database apart from that check, so the example never counts anywhere.
    """
    if Host.objects.filter(owner=request.user).exists():
        return redirect("overview")
    return render(request, "hosts/detail.html", EXAMPLE_DETAIL_CONTEXT)


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
    return JsonResponse(
        {
            "status": effective_status(host, now),
            "last_seen_human": ago(host.last_seen_at, now),
            # A stable "new report" signal for Check now: it changes only when a report
            # is saved (last_seen_human is relative and changes every second on its own).
            "report_received_at": iso_utc(host.report_received_at),
        }
    )


@login_required
@require_GET
def host_summary_json(request: HttpRequest, host_id: int) -> JsonResponse:
    """The overview's preview window for one host (handoff 5.3). Same data as the detail
    page, cut down to summaries. Another user's host is a 404 like any other view."""
    host = get_owned_host(request.user, host_id)
    return JsonResponse(host_preview(host, timezone.now()))


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
    if just_reported(host):
        # The report on screen is seconds old; asking again would only make the agent
        # send an early report.
        if wants_json(request):
            return JsonResponse({"ok": True, "skipped": "just_reported"})
        messages.success(request, f"{host.name} reported just now.")
        return redirect("host-detail", host_id=host.pk)
    request_collect(host)
    if wants_json(request):
        return JsonResponse({"ok": True}, status=202)
    messages.success(request, f"Asked {host.name} to report now.")
    return redirect("host-detail", host_id=host.pk)


@login_required
@require_POST
def host_collect_all(request: HttpRequest) -> HttpResponse:
    """Check all now: ask every connected host the user owns; never anyone else's.

    Hosts that reported in the last few seconds aren't asked again ("just_reported"),
    but count as asked in the message: their data is current either way.
    """
    asked = offline = skipped = 0
    now = timezone.now()
    for host in Host.objects.filter(owner=request.user):
        if not host.connected:
            offline += 1
            continue
        asked += 1
        if just_reported(host, now):
            skipped += 1
        else:
            request_collect(host)
    if wants_json(request):
        return JsonResponse(
            {"ok": True, "asked": asked, "offline": offline, "just_reported": skipped},
            status=202,
        )
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


def new_rotate_key(request: HttpRequest, host: Host) -> str:
    """A one-time key for the Rotate token form, stored in the session as unused.

    It is a nonce, not a secret: it only proves "this POST comes from a form this
    session was shown and hasn't used yet", so a re-sent POST (Back, reload) can't
    rotate the token a second time.
    """
    key = secrets.token_urlsafe(16)
    keys = dict(request.session.get(ROTATE_KEYS_SESSION, {}))
    keys[key] = host.pk
    request.session[ROTATE_KEYS_SESSION] = dict(list(keys.items())[-MAX_ROTATE_KEYS:])
    return key


def use_rotate_key(request: HttpRequest, host: Host, key) -> bool:
    """Mark the form's one-time key as used. False if it is missing, used, or for another host.

    Call inside the transaction that rotates. The keys are re-read from the session
    store (not the copy loaded at the start of the request), so two simultaneous POSTs
    with the same key can't both see it as unused. The used key is dropped from the
    session and saved before the rotation happens.
    """
    session = request.session
    if not isinstance(key, str) or not key or session.session_key is None:
        return False
    stored = session.__class__(session.session_key)
    keys = dict(stored.get(ROTATE_KEYS_SESSION, {}))
    if keys.get(key) != host.pk:
        return False
    del keys[key]
    stored[ROTATE_KEYS_SESSION] = keys
    stored.save()
    # The end-of-request save writes request.session: keep the key gone there too.
    session[ROTATE_KEYS_SESSION] = keys
    return True


def confirmed(request: HttpRequest) -> bool:
    return request.POST.get("confirm") == CONFIRM_VALUE


def _settings_page(
    request,
    host,
    name_form=None,
    delete_form=None,
    status=200,
    rotate_notice="",
    rotate_error="",
    revoke_error="",
):
    return render(
        request,
        "hosts/settings.html",
        {
            "host": host,
            # Only what the title needs: the status and its words (not the report).
            "title_status": host_status(host, timezone.now()),
            "name_form": name_form or HostNameForm(owner=request.user, instance=host),
            "delete_form": delete_form or DeleteHostForm(host=host),
            "rotate_key": new_rotate_key(request, host),
            "rotate_notice": rotate_notice,
            "rotate_error": rotate_error,
            "revoke_error": revoke_error,
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
    if not confirmed(request):
        # Checked before the one-time key, so the key isn't used up by an unconfirmed POST.
        return _settings_page(request, host, status=400, rotate_error=CONFIRM_ERROR)
    with transaction.atomic():
        # A re-sent POST (Back or reload on the "New token" page) carries a key that is
        # already used: don't rotate again, or the token just installed would stop working.
        if not use_rotate_key(request, host, request.POST.get("rotate_key")):
            return _settings_page(request, host, status=409, rotate_notice=ROTATE_AGAIN)
        # rotate() also closes the agent's open socket once the change is committed.
        token = AgentToken.rotate(host)
    return render(
        request, "hosts/token_rotated.html", {"host": host, **agent_setup(request, host, token)}
    )


@login_required
@require_POST
def host_revoke(request: HttpRequest, host_id: int) -> HttpResponse:
    host = get_owned_host(request.user, host_id)
    if not confirmed(request):
        return _settings_page(request, host, status=400, revoke_error=CONFIRM_ERROR)
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
