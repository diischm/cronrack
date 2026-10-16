"""Status icons in rendered HTML (components/status_icon.html)."""

import re

# base.html's <template id="status-icons"> holds one of each icon for the JS to clone; it is
# inert, so checks about what the page SHOWS leave it out.
ICON_TEMPLATE_RE = re.compile(r'<template id="status-icons">.*?</template>', re.DOTALL)


def status_icon(kind: str, words: str) -> str:
    """Regex: the icon of `kind` (good, warn, bad, info, waiting) with its words, visually
    hidden right next to the decorative SVG, and as the hover title."""
    w = re.escape(words)
    return (
        rf'<span class="status-icon status-icon--{kind}" title="{w}">'
        r'<svg [^>]*aria-hidden="true"[^>]*>.*?</svg>'
        rf'<span class="visually-hidden">{w}</span></span>'
    )


def shown(html: str) -> str:
    """The page without the inert icon template."""
    return ICON_TEMPLATE_RE.sub("", html)
