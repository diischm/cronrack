"""What happens when a report with network data arrives. Called from agents.consumers in a
savepoint of its own (like the cluster view): if this fails, the host's report still counts.

Every reporting host gets its sample stored (bounded). Only the network-view host drives
the Network: classify the report, hold it (flapping protection), run the network rules on
the held state, and record a StatusEvent (with its alert) when the status changes. The
host itself never changes colour because of any of this.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from activity.events import record_note, record_status_change
from hosts.models import Host
from network import decide
from network.models import (
    MAX_SAMPLES_PER_HOST,
    SAMPLE_RETENTION_HOURS,
    Network,
    NetworkReporter,
    NetworkSample,
)
from rules.base import SEVERITY_RANK
from rules.engine import evaluate_network, load_network_overrides, resolve_override

# Status per the highest severity among the rules' findings; info never colours.
_STATUS_OF = {"red": Host.Status.RED, "yellow": Host.Status.YELLOW}
AUTO_VIEW_FRESH = timedelta(hours=1)


def compact(data: Mapping[str, Any]) -> dict[str, Any]:
    """A report's network data as one history row: only what the trend needs."""
    dns = data.get("dns")
    return {
        "p": [
            [p["name"], p["role"], p["received"], p["sent"], p["median_ms"], p["p95_ms"]]
            for p in data.get("points") or []
        ],
        "d": [dns["ok"], dns["ms"]] if isinstance(dns, Mapping) else None,
    }


def save_sample(host: Host, data: Mapping[str, Any], now: datetime) -> None:
    """Store one row and prune: older than the retention, then the hard cap per host."""
    NetworkSample.objects.create(host=host, at=now, data=compact(data))
    samples = NetworkSample.objects.filter(host=host)
    samples.filter(at__lt=now - timedelta(hours=SAMPLE_RETENTION_HOURS)).delete()
    beyond = samples.order_by("-at", "-pk").values_list("pk", flat=True)[MAX_SAMPLES_PER_HOST:]
    extra = list(beyond[:100])
    if extra:
        NetworkSample.objects.filter(pk__in=extra).delete()


def view_reporter(network: Network) -> NetworkReporter | None:
    """The network view: the owner's pick, else the host on the server's own machine (it
    can still report during an outage), else the first one that reported. Automatic picks
    prefer a host that reported lately, so an agent whose [network] was turned off (or that
    has been gone for AUTO_VIEW_FRESH) doesn't leave the network unknown for good."""
    reporters = NetworkReporter.objects.filter(host__owner_id=network.owner_id).select_related(
        "host"
    )
    if network.view_host_id is not None:
        picked = reporters.filter(host_id=network.view_host_id).first()
        if picked is not None:
            return picked
    ordered = reporters.order_by("-server_local", "first_reported_at", "pk")
    fresh = ordered.filter(last_reported_at__gte=timezone.now() - AUTO_VIEW_FRESH).first()
    return fresh or ordered.first()


def _forget_held(network: Network) -> None:
    """The held state belonged to another host: the next report sets it at once."""
    network.state = decide.UNKNOWN
    network.state_point = ""
    network.state_detail = {}
    network.state_host = None
    network.candidate_state = ""
    network.candidate_point = ""
    network.candidate_count = 0
    network.router_down_since = None


def set_view_host(network: Network, host: Host | None) -> None:
    """Pick the network view (None = automatic). The next report sets the state at once:
    there is nothing to flap from, because the old state belonged to another host."""
    network.view_host = host
    _forget_held(network)
    network.save(
        update_fields=[
            "view_host", "state", "state_point", "state_detail", "candidate_state",
            "candidate_point", "candidate_count", "router_down_since", "state_host",
        ]
    )  # fmt: skip


def _threshold(network: Network, rule_id: str, overrides, default: float, notify=None) -> float:
    value = resolve_override(
        network.owner_id, rule_id, network, overrides=overrides, scope="network", notify=notify
    ).threshold
    return default if value is None else float(value)


def _address_of(data: Mapping[str, Any], name: str) -> str:
    for point in data.get("points") or []:
        if point["name"] == name:
            return point["address"]
    return ""


def _duration(delta: timedelta) -> str:
    minutes = max(1, round(delta.total_seconds() / 60))
    return f"{minutes} min"


def _apply(network: Network, data: Mapping[str, Any], now: datetime) -> None:
    from notifications import dispatch
    from notifications.config import load_config

    overrides = load_network_overrides(network)
    notify = load_config(network.owner_id)
    verdict = decide.classify(
        data,
        _threshold(network, "network.internet_slow", overrides, decide.DEFAULT_LATENCY_MS, notify),
        _threshold(network, "network.packet_loss", overrides, decide.DEFAULT_LOSS_PERCENT, notify),
    )

    # A short router outage that ended: an activity entry, never an alert. With a point before
    # the router down, this report can't say whether the router came back: keep counting.
    if not verdict.router_untested:
        router_down = verdict.state == decide.ROUTER_DOWN
        if decide.router_rebooted(network.router_down_since, now, router_down):
            took = _duration(now - network.router_down_since)
            network.last_router_reboot_at = now
            record_note(network, f"Router rebooted (unreachable for {took})")
        if router_down:
            network.router_down_since = network.router_down_since or now
        else:
            network.router_down_since = None

    ip = data.get("public_ip")
    ip_changed = bool(ip and network.public_ip and ip != network.public_ip)
    if ip:
        network.public_ip = ip

    held = decide.hold(
        decide.Held(
            network.state,
            network.state_point,
            network.candidate_state,
            network.candidate_point,
            network.candidate_count,
        ),
        verdict,
        network.confirm_reports,
    )
    network.candidate_state = held.candidate_state
    network.candidate_point = held.candidate_point
    network.candidate_count = held.candidate_count
    if held.changed:
        network.state = held.state
        network.state_point = held.point
        network.state_since = now
    if (network.state, network.state_point) == verdict.key():
        network.state_detail = dict(verdict.detail)  # the newest numbers for the held state

    findings = evaluate_network(
        network,
        {
            "state": network.state,
            "point": network.state_point,
            "address": _address_of(data, network.state_point),
            "detail": network.state_detail,
            "public_ip_changed": ip_changed,
        },
        now,
        overrides,
        notify=notify,
    )
    if any(f.rule_id == "network.public_ip_changed" for f in findings):
        record_note(network, "Public IP changed")

    colour = sorted(
        (f for f in findings if f.severity in _STATUS_OF),
        key=lambda f: -SEVERITY_RANK[f.severity],
    )
    new_status = _STATUS_OF[colour[0].severity] if colour else Host.Status.GREEN
    old_status = network.status
    network.status = new_status
    network.save()
    # The first report of a healthy network is no news.
    first_ok = old_status == Host.Status.WAITING and new_status == Host.Status.GREEN
    if new_status != old_status and not first_ok:
        record_status_change(network, old_status, new_status, colour)
    dispatch.sync(network, findings, now, config=notify)
    dispatch.after_commit(network, now)


def save_network(host: Host, data: Any, now: datetime | None = None) -> None:
    """One report's "network" key. Absent or null (collector crashed): nothing happens."""
    if not isinstance(data, Mapping):
        return
    if now is None:
        now = timezone.now()
    probed_at = str(data.get("probed_at") or "")
    with transaction.atomic():
        known = NetworkReporter.objects.filter(host=host).first()
        repeated = known is not None and known.last_probed_at == probed_at
        NetworkReporter.objects.update_or_create(
            host=host,
            defaults={
                "last_reported_at": now,
                "last_probed_at": probed_at,
                "server_local": bool(data.get("server_local")),
            },
            create_defaults={
                "first_reported_at": now,
                "last_reported_at": now,
                "last_probed_at": probed_at,
                "server_local": bool(data.get("server_local")),
            },
        )
        if repeated:
            return  # the same round again (Check now, a short interval): it counts once
        save_sample(host, data, now)
        network, _ = Network.objects.select_for_update().get_or_create(owner_id=host.owner_id)
        view = view_reporter(network)
        if view is not None and view.host_id == host.pk:
            if network.state_host_id != host.pk:
                _forget_held(network)  # the automatic pick moved to this host
                network.state_host = host
            _apply(network, data, now)
