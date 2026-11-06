"""The network rules: one per thing that can be wrong with the path to the internet.

They run on the Network subject (network/services.py), never on a host, against the HELD
state: a dict with state, point, address, detail and flags, so flapping protection applies
before any rule sees anything. Overrides work like for hosts, but only global ones exist
here (one network per owner): rule_id + enabled / severity / threshold in RuleOverride,
with no host and no cluster (scope="network" in rules.engine.resolve_override).

The thresholds of internet_slow (p95 ms) and packet_loss (%) are read by the classifier
in network/services.py; these checks only say which of the two it was.
Fix commands are read-only, and quoted by Rule.finding like every other rule's.
"""

from collections.abc import Mapping
from typing import Any

from rules.base import INFO, RED, YELLOW, Finding, Rule, RuleContext


def _when(state: str, text: str):
    def check(rule: Rule, held: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
        if held.get("state") != state:
            return []
        return [rule.finding(ctx, text, address=str(held.get("address") or ""))]

    return check


def check_local_down(rule: Rule, held: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    if held.get("state") != "local_down":
        return []
    point = str(held.get("point") or "A point")
    return [
        rule.finding(
            ctx, f"{point} down", subject=f"point:{point}", address=str(held.get("address") or "")
        )
    ]


def check_slow(rule: Rule, held: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    detail = held.get("detail") or {}
    if held.get("state") != "internet_slow" or detail.get("reason") not in ("latency", "both"):
        return []
    return [rule.finding(ctx, f"Internet slow: {detail.get('p95'):.0f} ms")]


def check_loss(rule: Rule, held: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    detail = held.get("detail") or {}
    if held.get("state") != "internet_slow" or detail.get("reason") not in ("loss", "both"):
        return []
    return [rule.finding(ctx, f"Internet slow: {detail.get('loss'):.0f}% packet loss")]


def check_public_ip(rule: Rule, held: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    return [rule.finding(ctx, "Public IP changed")] if held.get("public_ip_changed") else []


_NETWORK_RULES = (
    Rule(
        id="network.router_down",
        title="Router down",
        category="network",
        default_severity=RED,
        check=_when("router_down", "Router down"),
        why=(
            "Your router isn't answering, so nothing after it can be reached or tested. It may "
            "be off, rebooting, or the cable to it may be out."
        ),
        fix_template="ping -c 3 {address}",
    ),
    Rule(
        id="network.local_down",
        title="A local network point is down",
        category="network",
        default_severity=RED,
        check=check_local_down,
        why=(
            "Your router answers, but the next box on the way out (such as the ISP's box) "
            "doesn't. It may be down or rebooting."
        ),
        fix_template="ping -c 3 {address}",
    ),
    Rule(
        id="network.internet_down",
        title="Internet down",
        category="network",
        default_severity=RED,
        check=_when("internet_down", "Internet down"),
        why=(
            "All your own devices answer, but no internet address does. It's most likely your "
            "ISP. Local services keep working."
        ),
        fix_template="ip route show default",
    ),
    Rule(
        id="network.internet_slow",
        title="Internet slow (latency)",
        category="network",
        default_severity=YELLOW,
        default_threshold=150,  # p95 in ms, of the best internet target
        check=check_slow,
        why=(
            "The internet answers, but slowly, over several reports in a row. Often a busy "
            "line, or something on your network using all of it."
        ),
        fix_template="ip route show default",
    ),
    Rule(
        id="network.packet_loss",
        title="Internet slow (packet loss)",
        category="network",
        default_severity=YELLOW,
        default_threshold=5,  # percent, of the best internet target
        check=check_loss,
        why=(
            "Part of what is sent to the internet gets lost, over several reports in a row. "
            "It makes everything slow; often the line, or Wi-Fi on the way."
        ),
        fix_template="ip route show default",
    ),
    Rule(
        id="network.dns_failing",
        title="DNS failing",
        category="network",
        default_severity=YELLOW,
        check=_when("dns_failing", "DNS failing"),
        why=(
            "The internet answers, but names can't be looked up, so most things look broken. "
            "Usually the router's or the ISP's DNS server."
        ),
        fix_template="getent hosts example.com",
    ),
    Rule(
        id="network.public_ip_changed",
        title="Public IP changed",
        category="network",
        default_severity=INFO,
        check=check_public_ip,
        why=(
            "Your ISP gave you a new public address. Normal for many home lines, but anything "
            "that points at the old one (DNS, VPN) needs updating."
        ),
    ),
)

NETWORK_RULES: dict[str, Rule] = {rule.id: rule for rule in _NETWORK_RULES}
