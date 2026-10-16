"""Add host, steps 1-3 (handoff 5.5): the token is shown exactly once, only as visible text,
in a response the browser must not keep; errors are inline; both pages are CSP-clean."""

import html as html_lib
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.shortcuts import render
from django.test import Client

from hosts.models import Host
from hosts.views import agent_setup

from .rotate import post_rotate
from .test_host_views import token_from
from .test_preview import EVENT_HANDLER_ATTR, scan

pytestmark = pytest.mark.django_db

STATIC_JS = Path(__file__).resolve().parent.parent / "static" / "cronrack" / "js"
XSS_NAME = "<script>alert(1)</script>"


class TokenScanner(HTMLParser):
    """Where a string shows up: in attribute values, or in the text of which elements."""

    def __init__(self, needle: str) -> None:
        super().__init__(convert_charrefs=True)
        self.needle = needle
        self.in_attrs: list[str] = []
        self.in_text: list[tuple[str, dict]] = []  # (tag, attrs) of the element around it
        self.stack: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value and self.needle in value:
                self.in_attrs.append(f"{tag} {name}")
        if tag not in ("input", "meta", "link", "br", "img"):
            self.stack.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        if self.needle in data:
            self.in_text.append(self.stack[-1] if self.stack else ("", {}))


def create(client: Client, name: str = "pi-01"):
    response = client.post("/hosts/add/", {"name": name})
    assert response.status_code == 200
    return response, token_from(response)


# --- step 2: the token ---------------------------------------------------------------


def test_token_appears_exactly_once_in_its_code_well(alice_client: Client):
    response, token = create(alice_client)
    raw = response.content.decode()

    assert raw.count(token) == 1
    found = TokenScanner(token)
    found.feed(raw)
    assert len(found.in_text) == 1
    tag, attrs = found.in_text[0]
    assert tag == "pre"
    assert attrs["id"] == "agent-token"
    assert "code" in attrs["class"].split()
    # Nothing the browser's helpers would pick up.
    assert attrs["autocomplete"] == "off"
    assert attrs["spellcheck"] == "false"
    assert attrs["translate"] == "no"
    # Its own Copy key points at exactly that well.
    assert '<button type="button" class="btn btn--sm js-copy" aria-controls="agent-token">' in raw


def test_token_is_in_no_attribute_title_script_or_url(alice_client: Client):
    response, token = create(alice_client)
    raw = response.content.decode()

    found = TokenScanner(token)
    found.feed(raw)
    assert found.in_attrs == []  # no data-*, value=, href=, title=, aria-* ...
    assert token not in re.search(r"<title>(.*?)</title>", raw, re.DOTALL).group(1)
    assert token not in response.get("Location", "")
    for value in response.headers.values():
        assert token not in value  # no header carries it
    # No hidden input apart from the CSRF one, no inline script that could hold it.
    hidden_inputs = re.findall(r"<input[^>]*type=\"hidden\"[^>]*>", raw)
    assert all('name="csrfmiddlewaretoken"' in i for i in hidden_inputs)
    assert scan(raw).inline_script_text == ""


def test_token_response_is_never_stored_by_the_browser(alice_client: Client):
    response, _token = create(alice_client)
    directives = {d.strip() for d in response["Cache-Control"].split(",")}
    # no-store: no disk cache and no back/forward cache copy of this page.
    assert "no-store" in directives
    assert "private" in directives
    # The step-1 form page gets the same header (same view).
    assert "no-store" in alice_client.get("/hosts/add/")["Cache-Control"]


def test_add_host_js_never_touches_the_token():
    js = (STATIC_JS / "add-host.js").read_text()
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    # The token well is only ever overwritten (the back/forward guard), never read: its
    # id appears exactly twice, in the lookup and in its Copy key's selector, and the only
    # thing done with the element is setting the "Token hidden" text.
    assert code.count("agent-token") == 2
    assert 'const well = document.getElementById("agent-token");' in code
    assert "querySelectorAll('.js-copy[aria-controls=\"agent-token\"]')" in code
    assert re.findall(r"\bwell\.\w+", code) == ["well.textContent"]
    assert "well.textContent = TOKEN_HIDDEN;" in code
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "dataset",
        ".style",
        "console.",
        "localStorage",
        "sessionStorage",
        "document.write",
        "eval(",
    ):
        assert banned not in code, banned
    # Leaving before connect asks first; the prompt is removed again once connected.
    assert 'addEventListener("beforeunload", confirmLeave)' in code
    assert 'removeEventListener("beforeunload", confirmLeave)' in code
    # "Go to host" is a fixed path, checked before use.
    assert r"/^\/hosts\/\d+\/$/" in code


def test_back_forward_guard_hides_the_token_and_never_reloads():
    js = (STATIC_JS / "add-host.js").read_text()
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert 'window.addEventListener("pageshow"' in code
    assert "event.persisted" in code
    assert "Token hidden. If you still need it, rotate the token in Host settings." in code
    # The guard runs before the early return that needs the Connect section, so it also
    # works on the "New token" page.
    assert code.index('addEventListener("pageshow"') < code.index('getElementById("connect")')
    # Both pages are answers to a POST: a reload would create a second host or token.
    assert "reload" not in code
    assert "location" not in code


@pytest.mark.parametrize("page", ["add", "rotate"])
def test_token_pages_load_the_back_forward_guard(alice_client: Client, host, page):
    if page == "add":
        response, _token = create(alice_client)
    else:
        response = post_rotate(alice_client, host.pk)
    html = response.content.decode()
    assert re.search(r'<script src="[^"]*/js/add-host(\.[0-9a-f]{12})?\.js" defer></script>', html)
    assert "no-store" in response["Cache-Control"]


# --- step 2 + 3 markup --------------------------------------------------------------


def test_step_two_page_has_install_caddy_and_connect_parts(alice_client: Client):
    response, _token = create(alice_client)
    raw = response.content.decode()
    text = html_lib.unescape(raw)
    host = Host.objects.get(name="pi-01")

    assert (
        "This token is shown only once. Keep this page open until the installer asks for it."
        in text
    )
    # Command and token: two code wells, each with its own Copy key.
    assert 'aria-controls="install-command">Copy command</button>' in raw
    assert '<pre class="code code--wrap" id="install-command">curl -fsSL ' in raw
    # Caddy variant behind a disclosure key (open without JS, closed by disclosure.js).
    assert (
        'class="btn btn--sm show-more js-disclosure" aria-controls="install-caddy" aria-expanded="true" hidden'
        in raw
    )
    assert 'aria-controls="ca-command">Copy</button>' in raw
    assert 'aria-controls="install-command-ca">Copy</button>' in raw
    assert "--cacert cronrack-ca.crt" in text and "--ca-file cronrack-ca.crt" in text
    assert "disclosure.js" in raw and "add-host.js" in raw
    # Step 3: the loader, the waiting line, the host id (digits only) for add-host.js.
    assert f'data-host-id="{host.pk}"' in raw
    assert '<div class="loader surface" id="connect-loader" aria-hidden="true">' in raw
    assert "Waiting for <strong" in raw and ">pi-01</strong> to check in…" in raw
    assert "Still waiting. Check the agent on the host:" in raw
    assert "sudo systemctl status cronrack-agent\nsudo journalctl -u cronrack-agent" in raw
    # "Go to host" has no href until add-host.js builds it; the no-JS link is fixed.
    assert '<a class="btn btn--primary connect__go" id="connect-go" hidden>Go to host</a>' in raw
    assert f'<noscript><a class="btn" href="/hosts/{host.pk}/">Go to host</a></noscript>' in raw


def test_step_header_says_each_state_in_words(alice_client: Client):
    add = alice_client.get("/hosts/add/").content.decode()
    done = create(alice_client)[0].content.decode()

    def states(page):
        return re.findall(r'<span class="step__state">(\w+)</span>', page)

    assert states(add) == ["Now", "Next", "Next"]
    assert states(done) == ["Done", "Now", "Next"]
    assert 'id="step-1" aria-current="step"' in add
    assert 'id="step-2" aria-current="step"' in done
    assert add.count('aria-current="step"') == done.count('aria-current="step"') == 1


def test_host_name_is_escaped_on_the_token_page(alice_client: Client, alice):
    # The form refuses such names, but hosts made elsewhere (admin, ORM) can have them.
    host = Host.objects.create(owner=alice, name=XSS_NAME)
    request = alice_client.get("/hosts/add/").wsgi_request
    context = {"host": host, **agent_setup(request, host, "t" * 43)}
    raw = render(request, "hosts/add_done.html", context).content.decode()

    assert XSS_NAME not in raw
    assert raw.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 3  # sub, heading, waiting line
    assert scan(raw).inline_script_text == ""


# --- step 1: inline errors ------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("has space", "Use only letters, digits, &#x27;-&#x27; and &#x27;_&#x27;."),
        ("", "This field is required."),
    ],
)
def test_name_errors_are_inline_with_aria_invalid(alice_client: Client, name, message):
    raw = alice_client.post("/hosts/add/", {"name": name}).content.decode()

    assert not Host.objects.exists()
    field = re.search(r"<input type=\"text\" class=\"well\" name=\"name\"[^>]*>", raw).group(0)
    assert 'aria-invalid="true"' in field
    assert 'aria-describedby="id_name_helptext id_name_error"' in field
    assert "autofocus" in field
    errors = re.search(r'<div class="field__errors" id="id_name_error">(.*?)</div>', raw, re.DOTALL)
    assert errors and f'<p class="field__error">{message}</p>' in errors.group(1)
    assert 'role="alert"' not in errors.group(1)
    # The posted value comes back, escaped, so the user can fix it.
    assert f'value="{name}"' in field


def test_name_field_hints(alice_client: Client):
    raw = alice_client.get("/hosts/add/").content.decode()
    field = re.search(r"<input type=\"text\" class=\"well\" name=\"name\"[^>]*>", raw).group(0)
    for attr in (
        'id="id_name"',
        'maxlength="64"',
        'pattern="[A-Za-z0-9_\\-]+"',
        'autocomplete="off"',
        'spellcheck="false"',
        'aria-describedby="id_name_helptext"',
    ):
        assert attr in field, attr
    assert "aria-invalid" not in field
    assert '<label class="field__label" for="id_name">Host name</label>' in raw
    assert '<p class="field__help" id="id_name_helptext">' in raw


def test_posted_xss_name_is_refused_and_echoed_as_text(alice_client: Client):
    raw = alice_client.post("/hosts/add/", {"name": XSS_NAME}).content.decode()
    assert not Host.objects.exists()
    assert XSS_NAME not in raw
    assert 'value="&lt;script&gt;alert(1)&lt;/script&gt;"' in raw


# --- CSP: both pages --------------------------------------------------------------------


@pytest.mark.parametrize("page", ["form", "form-errors", "token"])
def test_add_host_pages_have_no_inline_script_style_or_handlers(alice_client: Client, page):
    if page == "form":
        raw = alice_client.get("/hosts/add/").content.decode()
    elif page == "form-errors":
        raw = alice_client.post("/hosts/add/", {"name": "bad name"}).content.decode()
    else:
        raw = create(alice_client)[0].content.decode()

    found = scan(raw)
    assert found.inline_script_text == ""
    assert all(s.get("src") for s in found.scripts)
    assert found.inline_styles == []
    assert found.event_handlers == []
    assert not EVENT_HANDLER_ATTR.search(raw)
    assert "<style" not in raw
    assert "javascript:" not in raw
