from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from alerts.discord import is_allowed_webhook, send_discord
from cronrack.http import wants_json
from notifications.text import TEST_MESSAGE
from prefs.forms import PrefsForm
from prefs.models import Prefs

# At most one test alert per user in this many seconds (counted in the cache, like the
# login limit): the key posts to Discord, so it must not be a way to spam the channel.
TEST_ALERT_EVERY_SECONDS = 30
TEST_ALERT_WAIT = f"Wait {TEST_ALERT_EVERY_SECONDS} seconds between test alerts."


def _password_form(user, data=None) -> PasswordChangeForm:
    form = PasswordChangeForm(user, data)
    # Browser autofill hints (the HTML autocomplete attribute), not passwords.
    for name in ("old_password", "new_password1", "new_password2"):
        hint = "current" if name == "old_password" else "new"
        form.fields[name].widget.attrs.update({"class": "well", "autocomplete": f"{hint}-password"})
    return form


@login_required
@require_http_methods(["GET", "POST"])
def prefs_index(request: HttpRequest) -> HttpResponse:
    """Settings: webhook + offline threshold, and the password change, as two forms.

    A hidden "form" field says which one was submitted.
    """
    prefs, _ = Prefs.objects.get_or_create(owner=request.user)
    prefs_form = PrefsForm(instance=prefs)
    password_form = _password_form(request.user)
    status = 200

    if request.method == "POST":
        if request.POST.get("form") == "password":
            password_form = _password_form(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                # Keep this session logged in; other sessions are logged out.
                update_session_auth_hash(request, user)
                messages.success(request, "Password updated.")
                return redirect("prefs")
        else:
            prefs_form = PrefsForm(request.POST, instance=prefs)
            if prefs_form.is_valid():
                prefs_form.save()
                messages.success(request, "Settings saved.")
                return redirect("prefs")
        status = 400

    return render(
        request,
        "prefs/index.html",
        {"prefs_form": prefs_form, "password_form": password_form, "settings_tab": "account"},
        status=status,
    )


def _send_test_alert(prefs: Prefs | None) -> str:
    """Send a test message to the SAVED webhook (never to a URL from the request).

    Returns the error text for the user, or "" when Discord accepted it.
    """
    url = prefs.discord_webhook_url if prefs else ""
    if not url:
        return "Save a Discord webhook URL first."
    if not is_allowed_webhook(url):
        return "Only Discord webhook URLs are allowed."
    if not send_discord(url, TEST_MESSAGE):
        return "Discord didn't accept the message. Check the URL."
    return ""


def _may_send_test_alert(user) -> bool:
    """True at most once per TEST_ALERT_EVERY_SECONDS per user. cache.add() only writes
    a key that doesn't exist yet, so two quick clicks can't both pass."""
    return cache.add(f"test-alert:{user.pk}", 1, TEST_ALERT_EVERY_SECONDS)


@login_required
@require_POST
def test_alert(request: HttpRequest) -> HttpResponse:
    """fetch (Accept: application/json) gets {ok, error}; a plain form POST is
    redirected back to the page it came from (one of two fixed URLs) with a flash message."""
    back = "notifications" if request.POST.get("back") == "notifications" else "prefs"
    status = 200
    prefs = Prefs.objects.filter(owner=request.user).first()
    if not (prefs and prefs.discord_webhook_url):
        error = _send_test_alert(prefs)  # "Save a Discord webhook URL first.": no slot used
    elif _may_send_test_alert(request.user):
        error = _send_test_alert(prefs)
    else:
        error, status = TEST_ALERT_WAIT, 429
    if wants_json(request):
        return JsonResponse({"ok": not error, "error": error}, status=status)
    if error:
        messages.error(request, error)
    else:
        messages.success(request, "Test alert sent.")
    return redirect(back)


@login_required
@require_GET
def appearance(request: HttpRequest) -> HttpResponse:
    """Settings → Appearance: browser-only choices (theme.js, localStorage)."""
    return render(request, "prefs/appearance.html", {"settings_tab": "appearance"})
