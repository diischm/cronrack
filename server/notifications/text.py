"""The words of every notification message. Pure functions, so tests can check them exactly.

Host, cluster, container, unit and mount names come from agents and are untrusted: they
are Discord-markdown-escaped before they go into a message, so a container named
"[click](https://evil.example)" or "**URGENT**" stays plain text. Mentions are blocked
separately (alerts.discord sends allowed_mentions: none).
"""

import re
from collections.abc import Iterable, Sequence

from alerts.discord import MAX_CONTENT_LENGTH

# Everything Discord treats as markdown: emphasis, code, strike, spoiler, quotes, headings,
# masked links, and the backslash itself.
# "<" too: Discord renders <@123>, <#123> and <:emoji:1> (no ping: mentions are blocked).
_MARKDOWN = re.compile(r"([\\*_`~|<>#\[\]()])")
# One line never takes the whole message: longer bodies are cut (with "…").
LINE_MAX = 300

EMOJI = {"red": "🔴", "yellow": "🟡", "info": "⚪"}
RESOLVED_EMOJI = "🟢"

OFFLINE_WORDS = {
    # rule ID: (went-away words, came-back words)
    "connectivity.offline": ("went offline", "is back online"),
    "k8s.cluster_offline": ("stopped reporting", "is reporting again"),
}

QUIET_TITLE = "🌙 While you were asleep"
SUMMARY_TITLE = "📋 Daily summary"
TEST_MESSAGE = (
    "🧪 Test message from CronRack: alerts reach this channel. "
    "This is only a test; nothing is wrong."
)


_WHITESPACE = re.compile(r"\s+")
# A zero-width space after ":" keeps Discord from turning agent text into a link.
_LINK = re.compile(r"://")
ZERO_WIDTH_SPACE = chr(0x200B)
_UNLINKED = ":" + ZERO_WIDTH_SPACE + "//"


def escape_markdown(value: str) -> str:
    """Plain text for Discord: markdown escaped, newlines and tabs collapsed to one space (so
    a name can't forge extra message lines), and no clickable links."""
    text = _WHITESPACE.sub(" ", str(value)).strip()
    text = _LINK.sub(_UNLINKED, text)
    return _MARKDOWN.sub(r"\\\1", text)


Line = tuple[str, str, str]  # (emoji, separator after the name, body)


def line(kind: str, rule_id: str, text: str, severity: str) -> Line:
    """One message line. kind: open | escalate | resolve."""
    body = escape_markdown(text)
    offline = OFFLINE_WORDS.get(rule_id)
    if kind == "resolve":
        if offline:
            return (RESOLVED_EMOJI, " ", offline[1])
        return (RESOLVED_EMOJI, ": ", f"Resolved: {body}")
    if offline:
        return (EMOJI.get(severity, "⚪"), " ", f"{offline[0]}: {body}")
    if kind == "escalate":
        return (EMOJI["red"], ": ", f"{body} (now red)")
    return (EMOJI.get(severity, "⚪"), ": ", body)


def _fit(lines: list[str], limit: int = MAX_CONTENT_LENGTH) -> str:
    """Join lines; if that is too long for Discord, cut whole lines and say how many.
    Every line is at most LINE_MAX characters, so the first one always fits."""
    lines = [ln if len(ln) <= LINE_MAX else ln[: LINE_MAX - 1] + "…" for ln in lines]
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    kept: list[str] = []
    for i, current in enumerate(lines):
        rest = len(lines) - i
        tail = f"…and {rest} more"
        if len("\n".join([*kept, current, tail])) > limit:
            return "\n".join([*kept, f"…and {rest} more"])
        kept.append(current)
    return "\n".join(kept)


def _block(name: str, lines: Sequence[Line]) -> list[str]:
    return [f"{name}:", *(f"{emoji} {body}" for emoji, _sep, body in lines)]


def subject_message(name: str, lines: Sequence[Line]) -> str:
    """'🟡 nas: Disk / at 91%' for one line; 'nas:' and one line each for more."""
    name = escape_markdown(name)
    if len(lines) == 1:
        emoji, sep, body = lines[0]
        return _fit([f"{emoji} {name}{sep}{body}"])
    return _fit(_block(name, lines))


def bundle_message(blocks: Iterable[tuple[str, Sequence[Line]]]) -> str:
    """The one 'while you were asleep' message: every subject's held lines."""
    out = [QUIET_TITLE]
    for name, lines in blocks:
        out.extend(_block(escape_markdown(name), lines))
    return _fit(out)


def summary_message(groups: Iterable[tuple[str, Sequence[tuple[str, str, bool]]]]) -> str:
    """The daily summary: per group, '• name: text' (plus '(resolved)' for gone ones)."""
    out = [SUMMARY_TITLE]
    for group, items in groups:
        out.append(group)
        for name, text, resolved in items:
            suffix = " (resolved)" if resolved else ""
            out.append(f"• {escape_markdown(name)}: {escape_markdown(text)}{suffix}")
    return _fit(out)
