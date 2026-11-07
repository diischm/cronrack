"""The Glass theme (default) next to Legacy: wiring, rules and the unchanged CSP.

Glass is browser-only like the old theme choice: theme-init.js sets <html data-skin
data-theme data-motion data-labels> before first paint, glass.css scopes every rule under
[data-skin="glass"], and glass.js does the motion. No backend state, no migrations.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client

from cronrack.middleware import CONTENT_SECURITY_POLICY

pytestmark = pytest.mark.django_db

SERVER = Path(settings.BASE_DIR)
REPO = SERVER.parent
TEMPLATES = SERVER / "templates"
STATIC = SERVER / "static" / "cronrack"
GLASS_JS = ["glass.js", "glass-tabs.js", "power-slide.js"]

# The exact policy from before the Glass theme. Glass must not loosen it in any way.
CSP_BEFORE_GLASS = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


class HeadAssets(HTMLParser):
    """The <script> and <link rel=stylesheet> tags of a page, in document order."""

    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" or (tag == "link" and values.get("rel") == "stylesheet"):
            self.tags.append((tag, values))


def assets(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = HeadAssets()
    parser.feed(html)
    return parser.tags


def src(tag: tuple[str, dict[str, str | None]]) -> str:
    return tag[1].get("src") or tag[1].get("href") or ""


# --- base.html ---------------------------------------------------------------------------


@pytest.mark.parametrize("logged_in", [False, True])
def test_theme_init_is_still_the_first_script_and_not_deferred(client: Client, alice, logged_in):
    if logged_in:
        client.force_login(alice)
    html = client.get("/settings/" if logged_in else "/login/").content.decode()
    tags = assets(html)
    first = tags[0]
    assert first[0] == "script"
    assert src(first).endswith("cronrack/js/theme-init.js")
    assert "defer" not in first[1]
    assert "async" not in first[1]


def test_base_loads_glass_css_last_and_glass_js_deferred(client: Client, alice):
    client.force_login(alice)
    tags = assets(client.get("/settings/").content.decode())
    sheets = [src(t) for t in tags if t[0] == "link"]
    assert sheets[:4] == [
        "/static/cronrack/css/tokens.css",
        "/static/cronrack/css/components.css",
        "/static/cronrack/css/pages.css",
        "/static/cronrack/css/glass.css",
    ]
    glass = [t for t in tags if src(t).endswith("cronrack/js/glass.js")]
    assert len(glass) == 1
    assert "defer" in glass[0][1]


def test_logged_out_pages_load_glass_too(client: Client, alice):
    # alice exists, so /login/ renders instead of redirecting to first-run setup.
    html = client.get("/login/").content.decode()
    assert "/static/cronrack/css/glass.css" in html
    assert "/static/cronrack/js/glass.js" in html


def test_500_page_gets_theme_init_first_and_glass_css():
    page = (TEMPLATES / "500.html").read_text(encoding="utf-8")
    tags = assets(page)
    assert src(tags[0]) == "/static/cronrack/js/theme-init.js"
    assert "defer" not in tags[0][1]
    assert "/static/cronrack/css/glass.css" in [src(t) for t in tags]


# --- templates: no inline styles ----------------------------------------------------------

STYLE_ATTR = re.compile(r"""\sstyle\s*=""", re.IGNORECASE)


def test_no_template_has_a_style_attribute_or_element():
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        if STYLE_ATTR.search(text) or "<style" in text.lower():
            offenders.append(str(path.relative_to(SERVER)))
    assert offenders == []


def test_only_the_styleguide_hard_codes_a_skin():
    # The skin comes from theme-init.js (localStorage). The styleguide's Glass island is the
    # one place that sets it in markup, so Glass can be compared with Legacy on one page.
    # Anywhere in the text (a ">" inside a tag or a Django tag can't hide it).
    attribute = re.compile(r"\sdata-skin\s*=")
    found = [
        str(path.relative_to(TEMPLATES))
        for path in sorted(TEMPLATES.rglob("*.html"))
        if attribute.search(path.read_text(encoding="utf-8"))
    ]
    assert found == ["dev/_styleguide_glass.html"]


def test_only_500_and_the_styleguide_override_the_skin_or_theme():
    # theme-init.js lets a <meta> win over the visitor's choice; only these two pages may.
    found = [
        str(path.relative_to(TEMPLATES))
        for path in sorted(TEMPLATES.rglob("*.html"))
        if re.search(
            r'name="cronrack-(skin|legacy-theme)-override"', path.read_text(encoding="utf-8")
        )
    ]
    assert found == ["500.html", "dev/styleguide.html"]


# --- static files -----------------------------------------------------------------------


def test_third_party_notices_cover_smoothui_and_inter():
    notices = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "SmoothUI" in notices
    assert "Copyright (c) 2024 Eduardo Calvo" in notices
    assert "Permission is hereby granted, free of charge" in notices  # the MIT text itself
    assert "Inter" in notices
    assert "SIL Open Font License" in notices


def test_inter_ships_with_its_licence():
    assert (STATIC / "fonts" / "InterVariable.woff2").is_file()
    licence = (STATIC / "fonts" / "OFL-Inter.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE" in licence.upper()


@pytest.mark.parametrize("name", GLASS_JS)
def test_ported_js_credits_smoothui_at_the_top(name):
    head = "\n".join((STATIC / "js" / name).read_text(encoding="utf-8").splitlines()[:3])
    assert "SmoothUI" in head
    assert "MIT" in head


def test_glass_css_credits_smoothui_at_the_top():
    head = "\n".join((STATIC / "css" / "glass.css").read_text(encoding="utf-8").splitlines()[:3])
    assert "SmoothUI" in head
    assert "MIT" in head


@pytest.mark.parametrize("name", [*GLASS_JS, "theme-init.js", "theme.js"])
def test_glass_js_follows_the_rules(name):
    source = (STATIC / "js" / name).read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert banned not in source, banned
    assert not re.search(r"\beval\(", source)
    assert "console." not in source
    assert 'setAttribute("style"' not in source
    assert "cssText" not in source
    # CSSOM custom properties only (tilt angles, beam, digits): element.style.setProperty("--…").
    for match in re.finditer(r"\.style\b(.{0,16})", source):
        assert match.group(1).startswith('.setProperty("--'), match.group(0)


SCOPES = ('[data-skin="glass"]', ':root:not([data-skin="glass"])', '[data-skin="legacy"]')


def unscoped_selectors(css: str) -> list[str]:
    """Every selector in `css` that doesn't start at one of SCOPES.

    A small scanner for glass.css (plain CSS, no nesting): comments removed, each text before
    a "{" is a prelude; at-rules (@media, @keyframes, …) and keyframe steps are skipped, and
    a selector list is split on its top-level commas only.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    preludes = []
    buffer = ""
    for char in css:
        if char == "{":
            text = buffer.strip()
            if text and not text.startswith("@") and not re.fullmatch(r"[\d.%,\s]+|from|to", text):
                preludes.append(text)
            buffer = ""
        elif char in "};":
            buffer = ""
        else:
            buffer += char
    return [
        selector
        for group in preludes
        for selector in top_level_parts(group)
        if not selector.startswith(SCOPES)
    ]


def test_scope_checker_catches_unscoped_rules():
    # The checker itself must not pass everything.
    assert unscoped_selectors(".foo { color: red; }") == [".foo"]
    assert unscoped_selectors("@media (min-width: 1px) { .x { color: red; } }") == [".x"]
    assert unscoped_selectors('[data-skin="glass"] :is(a, b), .y { color: red; }') == [".y"]
    assert unscoped_selectors("@keyframes k { from { opacity: 0; } 50% { opacity: 1; } }") == []


def test_glass_css_is_scoped_to_the_glass_skin():
    # Every selector either starts at [data-skin="glass"] (Glass looks) or is one of the few
    # rules for Glass-only or new elements. Legacy is untouched by anything else.
    css = (STATIC / "css" / "glass.css").read_text(encoding="utf-8")
    assert "{" in css
    assert unscoped_selectors(css) == []


def top_level_parts(group: str) -> list[str]:
    """Split a selector list on its top-level commas (not those inside :is() or :has())."""
    parts, depth, current = [], 0, ""
    for char in group:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return [part for part in parts if part]


# --- Settings > Appearance ------------------------------------------------------------------


def test_settings_page_has_the_appearance_controls(client: Client, alice):
    client.force_login(alice)
    html = client.get("/settings/appearance/").content.decode()  # its own tab now
    for value in ("glass", "legacy"):
        assert f'type="radio" name="skin" value="{value}"' in html
    for value in ("light", "dark", "system"):
        assert f'type="radio" name="theme" value="{value}"' in html
    for name in ("pref-reduce-motion", "pref-labels"):
        tag = re.search(rf'<input[^>]*id="{name}"[^>]*>', html)
        assert tag, name
        assert 'type="checkbox"' in tag.group(0)
        assert 'role="switch"' in tag.group(0)
    assert ">Reduce animations<" in html
    assert "Moving background" not in html
    assert ">Show status labels<" in html
    # "Send test alert" (on the Account tab) still posts to the real server action.
    account = client.get("/settings/").content.decode()
    assert 'form="test-alert-form">Send test alert</button>' in account
    assert 'action="/settings/test-alert/"' in account


# --- CSP --------------------------------------------------------------------------------------


def test_csp_is_unchanged_by_the_glass_theme(client: Client, alice):
    assert CONTENT_SECURITY_POLICY == CSP_BEFORE_GLASS
    client.force_login(alice)
    for url in ("/", "/settings/", "/hosts/add/"):
        assert client.get(url)["Content-Security-Policy"] == CSP_BEFORE_GLASS, url
