from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from alerts.discord import is_allowed_webhook, send_discord
from cronrack.http import wants_json
from prefs.forms import PrefsForm
from prefs.models import Prefs


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
        {"prefs_form": prefs_form, "password_form": password_form},
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
    if not send_discord(url, "✅ Test alert from CronRack. Alerts are working."):
        return "Discord didn't accept the message. Check the URL."
    return ""


@login_required
@require_POST
def test_alert(request: HttpRequest) -> HttpResponse:
    """fetch (Accept: application/json) gets {ok, error}; a plain form POST is
    redirected back to the settings page (a fixed URL) with a flash message."""
    error = _send_test_alert(Prefs.objects.filter(owner=request.user).first())
    if wants_json(request):
        return JsonResponse({"ok": not error, "error": error})
    if error:
        messages.error(request, error)
    else:
        messages.success(request, "Test alert sent.")
    return redirect("prefs")
