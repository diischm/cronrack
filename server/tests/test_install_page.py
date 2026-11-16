"""GET /install (pwa/views.py): the public page a phone opens after scanning the QR code."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.templatetags.static import static
from django.test import Client, override_settings

from cronrack import SERVER_VERSION
from cronrack.middleware import CONTENT_SECURITY_POLICY
from pwa.views import PRECACHE_STATIC

STATIC = Path(settings.BASE_DIR) / "static" / "cronrack"
DOCS_LINK = "https://github.com/diischm/cronrack/blob/main/docs/install-app.md"
HTTPS_NOTE = "Open CronRack over HTTPS to install the app"
LAN = {"HTTP_HOST": "cronrack.home.lan"}


@pytest.fixture
def lan():
    with override_settings(ALLOWED_HOSTS=["cronrack.home.lan", "localhost"]):
        yield


def page(client: Client, **extra) -> str:
    response = client.get("/install", **extra)
    assert response.status_code == 200
    return response.content.decode()


# --- Reachability -------------------------------------------------------------------------


@pytest.mark.django_db
def test_reachable_before_the_first_account_exists(client: Client):
    response = client.get("/install", secure=True)
    assert response.status_code == 200  # not a redirect to /setup/


def test_reachable_with_an_account(client: Client, alice):
    assert client.get("/install", secure=True).status_code == 200


def test_trailing_slash_is_not_a_route(client: Client, alice):
    assert client.get("/install/", secure=True).status_code == 404


def test_post_is_refused(client: Client, alice):
    assert client.post("/install", secure=True).status_code == 405


def test_head_is_answered(client: Client, alice):
    # Uptime checkers and link previews often ask with HEAD.
    assert client.head("/install", secure=True).status_code == 200


def test_sets_no_cookie_and_no_csrf_token(client: Client, alice):
    client.force_login(alice)
    response = client.get("/install", secure=True)
    assert not response.cookies
    assert "csrfmiddlewaretoken" not in response.content.decode()


# --- Content: nothing personal, nothing inline ---------------------------------------------


def test_leaks_nothing(client: Client, alice, monkeypatch):
    client.force_login(alice)
    monkeypatch.setenv("POD_NAME", "leaky-pod-7f3a")
    with override_settings(CRONRACK_DOMAIN="leaky-domain-7f3a.example"):
        body = page(client, secure=True)
    assert "alice" not in body
    assert "leaky-" not in body
    assert not re.search(rf"(?<![\w.]){re.escape(SERVER_VERSION)}(?![\w.])", body)
    assert "testserver" not in body


def test_no_inline_script_style_or_handlers(client: Client, alice):
    for kwargs in ({"secure": True}, {}):
        body = page(client, **kwargs)
        scripts = re.findall(r"<script\b[^>]*>", body)
        assert scripts
        assert all("src=" in tag for tag in scripts)
        assert not re.search(r"<script\b[^>]*>\s*[^<\s]", body)
        assert not re.search(r"\sstyle\s*=", body, re.IGNORECASE)
        assert "<style" not in body
        assert not re.search(r"\son\w+\s*=", body, re.IGNORECASE)
        assert "javascript:" not in body


def test_head_has_the_pwa_tags_and_scripts(client: Client, alice):
    body = page(client, secure=True)
    for tag in (
        '<link rel="manifest" href="/manifest.webmanifest">',
        '<meta name="theme-color" content="#0b0c0f">',
        '<meta name="mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">',
        f'<link rel="apple-touch-icon" href="{static("cronrack/icons/apple-touch-icon.png")}">',
        f'<link rel="stylesheet" href="{static("cronrack/css/install.css")}">',
        f'<script src="{static("cronrack/js/install.js")}" defer></script>',
        f'<script src="{static("cronrack/js/sw-register.js")}" defer></script>',
        "<title>Install · CronRack</title>",
    ):
        assert tag in body, tag


def test_first_line_is_the_placeholder_note():
    template = Path(settings.BASE_DIR) / "templates" / "pwa" / "install.html"
    assert template.read_text().startswith("{# PLACEHOLDER — Apo owns the final version #}")


# --- Secure and not secure ------------------------------------------------------------------


def test_https_shows_the_install_markup(client: Client, alice):
    body = page(client, secure=True)
    assert 'id="install-button"' in body
    assert re.search(r"<h1[^>]*>Get CronRack on your phone</h1>", body)
    assert HTTPS_NOTE not in body
    assert "Add to Home screen" in body
    assert 'href="/"' in body


def test_https_has_all_four_states(client: Client, alice):
    body = page(client, secure=True)
    assert "Tap Share" in body and "Add to Home Screen" in body
    assert "You're all set" in body and "Open dashboard" in body
    assert "<ol" in body and body.count("<svg") >= 2
    assert body.count('aria-hidden="true" focusable="false"') >= 2
    assert 'role="status"' in body and 'aria-live="polite"' in body
    # Everything but the always-visible fallback starts hidden.
    assert re.search(r'id="install-button"[^>]*\shidden', body)


def test_loopback_http_counts_as_secure(client: Client, alice, lan):
    assert 'id="install-button"' in page(client, HTTP_HOST="localhost")


def test_plain_http_on_a_real_name_shows_only_the_https_note(client: Client, alice, lan):
    body = page(client, **LAN)
    assert HTTPS_NOTE in body
    assert f'href="{DOCS_LINK}"' in body
    assert 'rel="noopener noreferrer"' in body
    assert "How to set up HTTPS" in body
    for absent in ("install-button", "Tap Share", "<ol", "<svg", "Open dashboard"):
        assert absent not in body


# --- Host header and CSP -----------------------------------------------------------------


def test_forged_host_is_refused(client: Client, alice):
    assert client.get("/install", secure=True, HTTP_HOST="evil.example").status_code == 400
    client.force_login(alice)
    assert client.get("/settings/", secure=True, HTTP_HOST="evil.example").status_code == 400


def test_csp_is_the_fixed_policy(client: Client, alice):
    client.force_login(alice)
    for path in ("/install", "/settings/"):
        assert client.get(path, secure=True)["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


def test_csp_string_is_pinned():
    assert CONTENT_SECURITY_POLICY == (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self'; "
        "script-src 'self'; "
        "manifest-src 'self'; "
        "worker-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    )
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "http" not in CONTENT_SECURITY_POLICY
    assert "img-src 'self' data:" in CONTENT_SECURITY_POLICY


# --- Static files ------------------------------------------------------------------------


def test_install_js_follows_the_rules():
    js = (STATIC / "js" / "install.js").read_text(encoding="utf-8")
    assert js.startswith("// PLACEHOLDER — Apo owns the final version.")
    for needed in (
        "beforeinstallprompt",
        "preventDefault",
        "prompt(",
        "display-mode: standalone",
        "appinstalled",
        "isSecureContext",
    ):
        assert needed in js, needed
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "console.",
        "eval(",
        "document.write",
    ):
        assert banned not in js, banned


def test_install_css_has_the_hidden_rule_and_no_new_effects():
    css = (STATIC / "css" / "install.css").read_text(encoding="utf-8")
    assert "[hidden] { display: none !important; }" in css
    assert "env(safe-area-inset-top" in css
    for banned in ("box-shadow", "text-shadow", "@keyframes", "animation"):
        assert banned not in css, banned


def test_the_service_worker_does_not_precache_install(client: Client, alice):
    assert not [name for name in PRECACHE_STATIC if "install" in name]
    source = client.get("/sw.js").content.decode()
    precache = re.search(r"^const PRECACHE = (\[.*\]);$", source, re.MULTILINE)
    assert precache
    assert "/install" not in precache.group(1)


# --- Review fixes (accessibility) --------------------------------------------------------


def test_dismissed_prompt_falls_back_and_keeps_focus():
    js = (STATIC / "js" / "install.js").read_text(encoding="utf-8")
    # Waits for the user's answer; a "no" brings back the plain instructions, and focus moves
    # to a visible link instead of vanishing with the hidden button.
    assert "userChoice" in js
    assert '"accepted"' in js
    assert ".focus(" in js
    # Newer iPads call themselves a Mac; navigator.platform is deprecated.
    assert "navigator.platform" not in js
    # Chrome, Firefox and Edge on iPhone keep the generic instructions.
    assert "CriOS|FxiOS|EdgiOS" in js


def test_steps_keep_list_semantics_and_the_docs_link_says_where_it_goes(client: Client, alice, lan):
    assert '<ol class="install__steps" role="list">' in page(client, secure=True, **LAN)
    assert "How to set up HTTPS (on GitHub)" in page(client, **LAN)


def test_status_line_is_for_screen_readers_only():
    css = (STATIC / "css" / "install.css").read_text(encoding="utf-8")
    rule = css.split(".install__status", 1)[1].split("}", 1)[0]
    assert "clip-path: inset(50%)" in rule
    assert "position: absolute" in rule
