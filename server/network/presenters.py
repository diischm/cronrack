"""The network as plain dicts: the overview strip (overview.json "network") and the Network
page (/network/ and /api/v1/network.json share network_context()).

Addresses and point names come from the host's own agent.toml through a validated report;
they are only ever output with {{ }} (auto-escaped) or as JSON, never marked safe.
"""

import statistics
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from django.urls import reverse

from hosts.humanize import ago
from hosts.models import Host
from hosts.presenters import OwnerData
from network import decide
from network.models import Network, NetworkReporter, NetworkSample
from network.services import view_reporter
from rules.engine import effective_status, evaluate_network
from rules.registry import get_rule

STATUS_WORDS = {
    decide.OK: "Working",
    decide.DOWN: "Down",
    decide.SLOW: "Slow",
    decide.UNTESTED: "Not tested",
}
# What the Network page says about each state (network-minimal.html).
SENTENCES = {
    decide.STATE_OK: "Everything is reachable.",
    decide.ROUTER_DOWN: "Your router isn't answering, so nothing after it can be tested.",
    decide.INTERNET_DOWN: "Your own devices are fine. It's most likely your ISP.",
    decide.DNS_FAILING: "The internet answers, but names can't be looked up.",
    decide.UNKNOWN: "The agent that measures your network isn't reporting, so nothing is known.",
}
TREND_BUCKETS = 96  # 24 h in 15-minute steps
TREND_HOURS = 24
TREND_W = 96
TREND_H = 24


def with_words(points: list[dict[str, str]]) -> list[dict[str, str]]:
    """Each strip point's status word, and the line into it."""
    previous = None
    for point in points:
        point["word"] = STATUS_WORDS[point["status"]]
        point["line"] = decide.line_before(previous, point["status"]) if previous else ""
        previous = point["status"]
    return points


def _report_network(host: Host) -> Mapping[str, Any] | None:
    report = host.latest_report if isinstance(host.latest_report, dict) else {}
    data = report.get("network")
    return data if isinstance(data, Mapping) else None


def _resolve(user, now: datetime, data: OwnerData | None):
    """(network, reporter, its latest network data, known?), or None without any reporter."""
    network = Network.objects.filter(owner=user).first()
    if network is None:
        return None
    reporter = view_reporter(network)
    if reporter is None:
        return None
    host = reporter.host
    latest = _report_network(host)
    silent = (data or OwnerData(user)).prefs.silent_after_seconds
    # Never a stale "Network OK": an offline view host means nothing is known.
    known = latest is not None and effective_status(host, now, silent) != "offline"
    return network, reporter, latest, known


def _state(network: Network, known: bool) -> tuple[str, str]:
    return (network.state, network.state_point) if known else (decide.UNKNOWN, "")


def _strip_points(network: Network, latest, known: bool) -> list[dict[str, str]]:
    state, point = _state(network, known)
    points = decide.points_for_display(latest, decide.chain_for_held(state, point, latest))
    return with_words(points)


def network_summary(user, now: datetime, data: OwnerData | None = None) -> dict[str, Any] | None:
    """The overview strip; None (nothing rendered) until some agent reports network data."""
    resolved = _resolve(user, now, data)
    if resolved is None:
        return None
    network, _, latest, known = resolved
    state, point = _state(network, known)
    return {
        "state": state,
        "text": decide.state_text(state, point),
        "points": _strip_points(network, latest, known),
        "url": reverse("network"),
    }


# --- the Network page ---------------------------------------------------------------------


def _sentence(state: str, point: str, detail: Mapping[str, Any]) -> str:
    if state == decide.LOCAL_DOWN:
        return f"Your router is fine; {point} isn't answering (down or rebooting)."
    if state == decide.INTERNET_SLOW:
        p95, loss = detail.get("p95"), detail.get("loss")
        reason = detail.get("reason")
        if reason == "loss":
            return f"{loss:.0f}% packet loss on the way to the internet."
        if reason == "both":
            return f"High latency ({p95:.0f} ms) and {loss:.0f}% packet loss."
        return f"High latency: {p95:.0f} ms to the internet."
    return SENTENCES.get(state, SENTENCES[decide.UNKNOWN])


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} ms" if value >= 10 else f"{value:.1f} ms"


def _row(name: str, address: str, status: str, point: Mapping[str, Any] | None) -> dict[str, Any]:
    tested = status != decide.UNTESTED and point is not None
    answered = tested and point.get("received")
    latency = "—"
    if tested:
        latency = "no reply"
    if answered:
        latency = _ms(point.get("median_ms"))
    return {
        "name": name,
        "address": address,
        "status": status,
        "word": STATUS_WORDS[status],
        "latency": latency,
        "p95": _ms(point.get("p95_ms")) if answered else "—",
        "loss": f"{decide.loss_percent(point):.0f}%" if tested else "—",
    }


def _best_internet(points: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    targets = [p for p in points if p["role"] == "internet"]
    answering = [p for p in targets if p.get("received")]
    if answering:
        return min(answering, key=lambda p: (decide.loss_percent(p), p.get("p95_ms") or 0))
    return targets[0] if targets else None


def _rows(latest: Mapping[str, Any] | None, shown: list[dict[str, str]]) -> list[dict[str, Any]]:
    """One row per strip point but "You": the local points by name, then the internet."""
    points = list((latest or {}).get("points") or [])
    by_name = {p["name"]: p for p in points}
    rows = []
    for item in shown[1:]:
        if item["kind"] == "internet":
            addresses = ", ".join(p["address"] for p in points if p["role"] == "internet")
            rows.append(_row("Internet", addresses, item["status"], _best_internet(points)))
        else:
            point = by_name.get(item["name"])
            rows.append(
                _row(item["name"], point["address"] if point else "", item["status"], point)
            )
    return rows


def _trend(samples: list[NetworkSample], name: str, now: datetime) -> dict[str, Any]:
    """Latency line and loss bars for one point: SVG attribute values, 24 h in 96 buckets."""
    start = now - timedelta(hours=TREND_HOURS)
    step = TREND_HOURS * 3600 / TREND_BUCKETS
    buckets: list[list[tuple[float | None, float]]] = [[] for _ in range(TREND_BUCKETS)]
    for sample in samples:
        i = int((sample.at - start).total_seconds() // step)
        if not 0 <= i < TREND_BUCKETS:
            continue
        rows = sample.data.get("p") or []
        if name == "Internet":
            rows = [r for r in rows if r[1] == "internet"]
            rows = sorted(rows, key=lambda r: (r[3] - r[2], r[5] if r[5] is not None else 1e9))[:1]
        else:
            rows = [r for r in rows if r[0] == name]
        for row in rows:
            _, _, received, sent, median, _ = row
            buckets[i].append((median, 100.0 * (sent - received) / sent if sent else 100.0))
    medians = [
        statistics.median(values) if (values := [m for m, _ in b if m is not None]) else None
        for b in buckets
    ]
    top = max((m for m in medians if m is not None), default=0.0) or 1.0
    line = [
        f"{i},{TREND_H - (m / top) * (TREND_H - 2):.1f}"
        for i, m in enumerate(medians)
        if m is not None
    ]
    bars = []
    for i, bucket in enumerate(buckets):
        if bucket:
            loss = statistics.mean(l for _, l in bucket)
            if loss > 0:
                height = max(1.0, loss / 100 * TREND_H)
                bars.append({"x": i, "y": f"{TREND_H - height:.1f}", "h": f"{height:.1f}"})
    return {
        "has_data": any(buckets),
        "line": " ".join(line),
        "bars": bars,
        "top": _ms(top) if line else "—",
        "w": TREND_W,
        "h": TREND_H,
    }


def _facts(network: Network, reporter, latest, now: datetime) -> dict[str, Any]:
    dns = (latest or {}).get("dns")
    if not isinstance(dns, Mapping):
        dns_text = "off"
    elif dns.get("ok"):
        dns_text = _ms(dns.get("ms"))
    else:
        dns_text = "failing"
    public_ip = (latest or {}).get("public_ip")
    return {
        "dns": dns_text,
        "public_ip": public_ip or None,
        "last_reboot": ago(network.last_router_reboot_at, now)
        if network.last_router_reboot_at
        else "not seen",
        "method": "TCP" if (latest or {}).get("method") == "tcp" else "ping",
        "tcp": (latest or {}).get("method") == "tcp",
        "checked": ago(reporter.last_reported_at, now),
    }


def _advice(network: Network, state: str, latest, now: datetime) -> list[dict[str, str]]:
    """Why + the read-only check command of the active rule(s). Copy only."""
    if state == decide.UNKNOWN:
        return []
    address = ""
    for point in (latest or {}).get("points") or []:
        if point["name"] == network.state_point:
            address = point["address"]
    findings = evaluate_network(
        network,
        {
            "state": network.state,
            "point": network.state_point,
            "address": address,
            "detail": network.state_detail,
        },
        now,
    )
    return [{"why": get_rule(f.rule_id).why, "fix": f.fix} for f in findings]


def network_context(user, now: datetime, data: OwnerData | None = None) -> dict[str, Any]:
    """Everything on the Network page. No reporter yet: {"network": None}."""
    resolved = _resolve(user, now, data)
    if resolved is None:
        return {"network": None, "reporters": []}
    network, view, latest, known = resolved
    state, point = _state(network, known)
    shown = _strip_points(network, latest, known)
    rows = _rows(latest, shown)
    samples = list(
        NetworkSample.objects.filter(host=view.host, at__gte=now - timedelta(hours=TREND_HOURS))
    )
    for row in rows:
        row["trend"] = _trend(samples, row["name"], now)
    reporters = NetworkReporter.objects.filter(host__owner=user).select_related("host")
    return {
        "network": {
            "state": state,
            "text": decide.state_text(state, point),
            "sentence": _sentence(state, point, network.state_detail),
            "points": shown,
            "rows": rows,
            "facts": _facts(network, view, latest, now),
            "advice": _advice(network, state, latest, now),
            "confirm_reports": network.confirm_reports,
            "view_host": {"id": view.host_id, "name": view.host.name},
            "view_is_auto": network.view_host_id is None,
        },
        "reporters": [
            {
                "id": r.host_id,
                "name": r.host.name,
                "server_local": r.server_local,
                "is_view": r.host_id == view.host_id,
                "last_report": ago(r.last_reported_at, now),
            }
            for r in sorted(reporters, key=lambda r: (r.first_reported_at, r.pk))
        ],
    }
