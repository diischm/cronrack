"""The network as its own subject: strict validation, the decision table, flapping, storage,
StatusEvents and alerts, rule overrides, which host is the network view. The host measuring
the network never changes colour because of it. Documentation addresses only (RFC 5737)."""

import itertools
import re
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.test import override_settings
from django.utils import timezone

from activity.models import StatusEvent
from agents.consumers import ingest_report
from agents.serializers import NetworkSerializer, ReportSerializer
from alerts import discord
from hosts.models import Host
from hosts.presenters import overview_context
from network import decide
from network.models import MAX_SAMPLES_PER_HOST, Network, NetworkReporter, NetworkSample
from network.presenters import network_summary
from network.services import save_network, save_sample, set_view_host
from prefs.models import Prefs
from rules.models import RuleOverride
from rules.registry import get_rule

from .sample import make_report
from .test_host_views import make_host

SERVER = Path(__file__).resolve().parent.parent
ROUTER_IP = "192.0.2.1"
BOX_IP = "192.0.2.254"


def point(name, role, address, received=5, median=2.0, p95=3.0, sent=5):
    if received == 0:
        median = p95 = None
    return {"name": name, "role": role, "address": address, "sent": sent, "received": received,
            "median_ms": median, "p95_ms": p95}  # fmt: skip


def net(router=5, box=5, internet=5, dns=True, median=18.0, p95=22.0, **extra):
    """A report's network section: Router, ISP box, 1.1.1.1 with these answers out of 5."""
    points = [point("Router", "gateway", ROUTER_IP, router)]
    if box is not None:
        points.append(point("ISP box", "local", BOX_IP, box))
    points.append(point("1.1.1.1", "internet", "1.1.1.1", internet, median, p95))
    data = {
        "method": "icmp",
        "probed_at": "2026-11-04T12:00:00Z",
        "gateway_found": True,
        "server_local": False,
        "points": points,
        "dns": {"ok": dns, "ms": 12.0 if dns else None},
        "public_ip": None,
    }
    data.update(extra)
    return data


# --- 1. Strict validation -------------------------------------------------------------------


def valid(data) -> bool:
    return NetworkSerializer(data=data).is_valid()


def test_a_good_section_is_valid():
    assert valid(net())
    assert valid(net(box=None, dns=False))


def test_a_public_default_gateway_is_valid():
    data = net()
    data["points"][0]["address"] = "8.8.4.4"  # a VPS: refusing it would refuse the whole report
    assert valid(data)


def test_old_agents_without_the_section_keep_working():
    report = make_report()
    assert "network" not in report
    assert ReportSerializer(data=report).is_valid(), ReportSerializer(data=report).errors


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(surprise=1),
        lambda d: d.update(method="raw"),
        lambda d: d.update(probed_at="yesterday"),
        lambda d: d.update(gateway_found="yes"),
        lambda d: d.update(public_ip="192.0.2.5"),  # not public
        lambda d: d.update(public_ip="not-an-ip"),
        lambda d: d["points"][1].update(address="8.8.8.8"),  # an extra point is local
        lambda d: d["points"][0].update(address="224.0.0.1"),  # no router here
        lambda d: d["points"][0].update(address="router.lan"),
        lambda d: d["points"][-1].update(address="192.0.2.9"),  # internet targets are public
        lambda d: d["points"][0].update(name="evil\nname"),
        lambda d: d["points"][0].update(name="x" * 33),
        lambda d: d["points"][0].update(name=""),
        lambda d: d["points"][0].update(sent=11),
        lambda d: d["points"][0].update(received=6),
        lambda d: d["points"][0].update(median_ms=-1),
        lambda d: d["points"][0].update(median_ms=60001),
        lambda d: d["points"][0].update(median_ms="2"),
        lambda d: d["points"][0].update(median_ms=None),  # answered, but no number
        lambda d: d["points"][0].update(p95_ms=1.0),  # below the median
        lambda d: d["points"][0].update(extra=1),
        lambda d: d["points"].reverse(),  # not in path order
        lambda d: d["points"].insert(0, dict(d["points"][0])),  # two gateways
        lambda d: d["points"].extend([point(f"p{i}", "internet", "1.1.1.1") for i in range(8)]),
        lambda d: d["points"].insert(1, point("Router", "local", BOX_IP)),  # same name
        lambda d: d.update(gateway_found=False),  # but there is a gateway point
        lambda d: d["dns"].update(ok=False),  # failed, but with a time
    ],
)
def test_anything_odd_is_refused(change):
    data = net()
    change(data)
    assert not valid(data)


# --- 2. The decision table -----------------------------------------------------------------

U, O, D, S = decide.UNTESTED, decide.OK, decide.DOWN, decide.SLOW


@pytest.mark.parametrize(
    ("data", "state", "chain", "text"),
    [
        (net(), decide.STATE_OK, (O, O, O), "Network OK"),
        (net(box=None), decide.STATE_OK, (O, O), "Network OK"),
        (net(router=0, box=0, internet=0), decide.ROUTER_DOWN, (D, U, U), "Router down"),
        (net(router=0), decide.ROUTER_DOWN, (D, U, U), "Router down"),  # the rest can't count
        (net(box=0, internet=0), decide.LOCAL_DOWN, (O, D, U), "ISP box down"),
        (net(internet=0), decide.INTERNET_DOWN, (O, O, D), "Internet down"),
        (net(box=None, internet=0), decide.INTERNET_DOWN, (O, D), "Internet down"),
        (net(dns=False), decide.DNS_FAILING, (O, O, O), "DNS failing"),
        (net(internet=0, dns=False), decide.INTERNET_DOWN, (O, O, D), "Internet down"),
        (net(p95=151), decide.INTERNET_SLOW, (O, O, S), "Internet slow"),
        (net(p95=150), decide.STATE_OK, (O, O, O), "Network OK"),  # at the threshold is fine
        (net(internet=4), decide.INTERNET_SLOW, (O, O, S), "Internet slow"),  # 20% loss
        (net(dns=False, p95=500), decide.DNS_FAILING, (O, O, O), "DNS failing"),
        (net(gateway_found=False, points=[point("1.1.1.1", "internet", "1.1.1.1", 0)]),
         decide.ROUTER_DOWN, (D, U), "Router down"),
    ],
)  # fmt: skip
def test_classify(data, state, chain, text):
    verdict = decide.classify(data)
    assert verdict.state == state
    assert verdict.chain == chain
    assert decide.state_text(verdict.state, verdict.point) == text


def test_one_bad_internet_target_never_makes_the_internet_look_down():
    data = net()
    data["points"].append(point("8.8.8.8", "internet", "8.8.8.8", 0))
    assert decide.classify(data).state == decide.STATE_OK


def test_slow_is_judged_on_the_best_target():
    data = net(p95=500)
    data["points"].append(point("8.8.8.8", "internet", "8.8.8.8", 5, 20, 30))
    assert decide.classify(data).state == decide.STATE_OK


def test_a_down_point_uses_the_users_name_for_it():
    data = net(box=0)
    data["points"][1]["name"] = "Modem"
    verdict = decide.classify(data)
    assert decide.state_text(verdict.state, verdict.point) == "Modem down"


@pytest.mark.parametrize(
    ("latency", "loss", "reason"),
    [(100, 50, "latency"), (500, 1, "loss"), (100, 1, "both")],
)
def test_thresholds_and_the_reason(latency, loss, reason):
    verdict = decide.classify(net(internet=4, median=120, p95=140), latency, loss)
    assert (verdict.state, verdict.detail["reason"]) == (decide.INTERNET_SLOW, reason)


# --- 3. Flapping -----------------------------------------------------------------------------


def run_hold(start, verdicts, needed=3):
    held = decide.Held(start)
    seen = []
    for state in verdicts:
        held = decide.hold(held, decide.Verdict(state), needed)
        seen.append(held.state)
    return seen


def test_a_state_must_hold_n_reports_in_a_row():
    r, o = decide.ROUTER_DOWN, decide.STATE_OK
    assert run_hold(o, [r, r, r]) == [o, o, r]
    assert run_hold(o, [r, r, o, r, r]) == [o, o, o, o, o]  # a blip restarts the count
    assert run_hold(r, [o, o, o]) == [r, r, o]  # back to OK needs it too
    assert run_hold(o, [r], needed=1) == [r]


def test_the_first_data_is_taken_at_once():
    assert run_hold(decide.UNKNOWN, [decide.ROUTER_DOWN]) == [decide.ROUTER_DOWN]


def test_router_rebooted_window():
    now = timezone.now()
    five = now - timedelta(minutes=5)
    assert decide.router_rebooted(five, now, router_down_now=False)
    assert not decide.router_rebooted(five, now, router_down_now=True)
    assert not decide.router_rebooted(None, now, router_down_now=False)
    assert not decide.router_rebooted(now - timedelta(minutes=11), now, router_down_now=False)


# --- 4. Own subject: status, events, alerts; the host never changes -------------------------


@pytest.fixture
def host(alice):
    return make_host(alice, "nas", "green", make_report(network=net()))


ROUNDS = itertools.count()


def send(host, reports, start=None):
    start = start or timezone.now()
    for i, data in enumerate(reports):
        at = start + timedelta(minutes=i)
        stamp = f"2026-11-04T12:{next(ROUNDS) // 60 % 60:02d}:{next(ROUNDS) % 60:02d}Z"
        save_network(host, {**data, "probed_at": stamp}, at)  # each its own round


def network_of(user):
    return Network.objects.get(owner=user)


pytestmark = pytest.mark.django_db


def test_the_first_report_creates_the_network_quietly(alice, host):
    send(host, [net()])
    network = network_of(alice)
    assert (network.state, network.status) == ("ok", "green")
    assert not StatusEvent.objects.filter(network=network).exists()  # no news
    reporter = NetworkReporter.objects.get(host=host)
    assert reporter.server_local is False


def test_a_problem_changes_the_network_not_the_host(
    alice, host, django_capture_on_commit_callbacks
):
    send(host, [net()])
    with django_capture_on_commit_callbacks(execute=False):
        send(host, [net(internet=0)] * 3)
    network = network_of(alice)
    assert (network.state, network.status) == ("internet_down", "red")
    assert Host.objects.get(pk=host.pk).status == "green"
    event = StatusEvent.objects.get(network=network)
    assert (event.old_status, event.new_status) == ("green", "red")
    assert event.describe() == "Network: Internet down"
    assert event.owner_id == alice.pk


def test_no_attention_card_or_host_issue_for_network_problems(alice, alice_client, host):
    send(host, [net(), net(internet=0), net(internet=0), net(internet=0)])
    ingest = make_report(network=net(internet=0))
    Host.objects.filter(pk=host.pk).update(latest_report=ingest)
    data = overview_context(alice, timezone.now())
    assert data["attention_count"] == 0
    assert data["hosts"][0]["issues"] == []
    assert data["network"]["text"] == "Internet down"


def test_recovery_is_an_event_too(alice, host, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False):
        send(host, [net(), *[net(internet=0)] * 3, *[net()] * 3])
    events = StatusEvent.objects.filter(network=network_of(alice)).order_by("created_at", "pk")
    assert [e.new_status for e in events] == ["red", "green"]
    assert events[1].describe() == "Network OK again"


def test_alerts_go_through_the_existing_path(alice, host, django_capture_on_commit_callbacks):
    Prefs.objects.create(owner=alice, discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    send(host, [net()])
    with (
        mock.patch.object(discord, "send_discord", return_value=True) as sender,
        django_capture_on_commit_callbacks(execute=True),
    ):
        send(host, [net(box=0)] * 3)
    sender.assert_called_once()
    text = sender.call_args.args[1]
    assert text.endswith("Network: ISP box down")
    assert BOX_IP not in text  # the point's name is enough
    network = network_of(alice)
    assert network.last_alert_status == "red"


def test_alert_cooldown_and_coalescing_cover_the_network(alice, host):
    Prefs.objects.create(owner=alice, discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    send(host, [net()])
    now = timezone.now()
    Network.objects.filter(owner=alice).update(
        status="red", last_alert_status="green", last_alert_at=now - timedelta(minutes=5)
    )
    with mock.patch.object(discord, "send_discord", return_value=True) as sender:
        sent = discord.send_pending_alerts(now)
    assert [type(s) for s in sent] == [Network]
    assert sender.call_args.args[1].endswith("Network is red now (after several quick changes)")


def test_a_router_reboot_is_an_activity_entry_not_an_alert(
    alice, host, django_capture_on_commit_callbacks
):
    Prefs.objects.create(owner=alice, discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    send(host, [net()])
    with (
        mock.patch.object(discord, "send_discord", return_value=True) as sender,
        django_capture_on_commit_callbacks(execute=True),
    ):
        send(host, [net(router=0), net(router=0), net()])  # 2 reports: never held
    network = network_of(alice)
    assert network.state == "ok" and network.status == "green"
    assert network.last_router_reboot_at is not None
    event = StatusEvent.objects.get(network=network)
    assert event.describe() == "Network: Router rebooted (unreachable for 2 min)"
    assert event.old_status == event.new_status
    sender.assert_not_called()


def test_public_ip_change_is_info_only(alice, host, django_capture_on_commit_callbacks):
    Prefs.objects.create(owner=alice, discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    send(host, [net(public_ip="8.8.4.4")])
    with (
        mock.patch.object(discord, "send_discord", return_value=True) as sender,
        django_capture_on_commit_callbacks(execute=True),
    ):
        send(host, [net(public_ip="8.8.4.4"), net(public_ip="9.9.9.9")])
    network = network_of(alice)
    assert network.status == "green" and network.state == "ok"
    assert network.public_ip == "9.9.9.9"
    event = StatusEvent.objects.get(network=network)
    assert event.describe() == "Network: Public IP changed"
    assert "9.9.9.9" not in event.describe()
    sender.assert_not_called()


def test_a_broken_section_never_breaks_the_host_report(alice, host, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr("agents.consumers.save_network", boom)
    report = make_report(network=net(), hostname="nas")
    ingest_report.func(host.pk, report, "c")
    assert Host.objects.get(pk=host.pk).latest_report["network"] == report["network"]


def test_a_report_without_network_data_does_nothing(alice, host):
    save_network(host, None)
    assert not Network.objects.filter(owner=alice).exists()


def test_one_round_counts_once(alice, host):
    # Reports in between reuse the agent's last round (same probed_at): never "3 in a row".
    send(host, [net()])
    round_ = net(internet=0, probed_at="2030-01-01T00:00:00Z")
    now = timezone.now()
    for i in range(5):
        save_network(host, round_, now + timedelta(minutes=10 + i))
    network = network_of(alice)
    assert network.state == "ok" and network.candidate_count == 1
    assert NetworkSample.objects.filter(host=host).count() == 2


# --- 5. Overrides -----------------------------------------------------------------------------


def override(user, rule, **fields):
    return RuleOverride.objects.create(owner=user, rule_id=rule, **fields)


def test_every_network_rule_explains_itself():
    for rule_id in ("router_down", "local_down", "internet_down", "internet_slow", "packet_loss",
                    "dns_failing", "public_ip_changed"):  # fmt: skip
        rule = get_rule(f"network.{rule_id}")
        assert rule.category == "network" and rule.why
    assert "ping -c 3" in get_rule("network.router_down").fix_template


def test_a_global_override_changes_severity(alice, host, django_capture_on_commit_callbacks):
    override(alice, "network.internet_down", severity="yellow")
    with django_capture_on_commit_callbacks(execute=False):
        send(host, [net(), *[net(internet=0)] * 3])
    assert network_of(alice).status == "yellow"


def test_a_disabled_rule_keeps_the_strip_honest_but_doesnt_colour(alice, host):
    override(alice, "network.internet_down", enabled=False)
    send(host, [net(), *[net(internet=0)] * 3])
    network = network_of(alice)
    assert network.state == "internet_down"  # the strip still tells the truth
    assert network.status == "green"
    assert not StatusEvent.objects.filter(network=network).exists()


def test_a_threshold_override_moves_the_slow_limit(alice, host):
    override(alice, "network.internet_slow", threshold=20)
    send(host, [net(p95=22), *[net(p95=22)] * 3])
    assert network_of(alice).state == "internet_slow"


def test_a_hosts_override_never_applies_to_the_network(alice, host):
    RuleOverride.objects.create(
        owner=alice, rule_id="network.internet_down", host=host, enabled=False
    )
    send(host, [net(), *[net(internet=0)] * 3])
    assert network_of(alice).status == "red"


def test_another_users_override_never_applies(alice, bob, host):
    override(bob, "network.internet_down", enabled=False)
    send(host, [net(), *[net(internet=0)] * 3])
    assert network_of(alice).status == "red"


# --- 6. Which host is the network view ---------------------------------------------------------


def test_the_first_reporter_is_the_default_view(alice, host):
    other = make_host(alice, "pi", "green", make_report())
    send(host, [net()])
    send(other, [net(internet=0)] * 4)  # not the view: stored, but changes nothing
    assert network_of(alice).state == "ok"
    assert NetworkSample.objects.filter(host=other).count() == 4


def test_the_servers_own_machine_is_preferred(alice, host):
    local = make_host(alice, "server", "green", make_report())
    send(host, [net()])
    send(local, [net(server_local=True, internet=0)] * 3)
    assert network_of(alice).state == "internet_down"


def test_the_users_pick_wins(alice, host):
    local = make_host(alice, "server", "green", make_report())
    send(local, [net(server_local=True)])
    send(host, [net()])
    set_view_host(network_of(alice), host)
    send(host, [net(box=0)])  # taken at once: the old state belonged to another host
    assert network_of(alice).state == "local_down"
    send(local, [net(internet=0)] * 3)
    assert network_of(alice).state == "local_down"


def test_automatic_view_moves_on_from_a_host_gone_for_long(alice, host):
    other = make_host(alice, "pi", "green", make_report())
    send(host, [net()], start=timezone.now() - timedelta(hours=3))
    send(other, [net(internet=0)] * 3)  # the first reporter hasn't reported for 3 h
    assert network_of(alice).state == "internet_down"


def test_a_view_host_that_is_deleted_falls_back(alice, host):
    other = make_host(alice, "pi", "green", make_report())
    send(host, [net()])
    send(other, [net()])
    set_view_host(network_of(alice), host)
    host.delete()
    assert network_of(alice).view_host_id is None
    send(other, [net()])
    assert network_of(alice).state == "ok"


def test_networks_are_per_owner(alice, bob, host):
    theirs = make_host(bob, "their-box", "green", make_report())
    send(host, [net()])
    send(theirs, [net(internet=0)])
    assert network_of(alice).state == "ok"
    assert network_of(bob).state == "internet_down"


# --- 7. "Network unknown": never a stale OK ------------------------------------------------------


def test_no_data_means_no_strip(alice):
    assert network_summary(alice, timezone.now()) is None


def test_an_offline_view_host_is_unknown(alice, host):
    send(host, [net()])
    assert network_summary(alice, timezone.now())["text"] == "Network OK"
    Host.objects.filter(pk=host.pk).update(
        connected=False, last_seen_at=timezone.now() - timedelta(hours=2)
    )
    summary = network_summary(alice, timezone.now())
    assert summary["state"] == "unknown"
    assert summary["text"] == "Network unknown"
    assert {p["status"] for p in summary["points"]} == {"untested"}
    assert network_of(alice).status == "green"  # shown, not alerted: the host's own alert says it


# --- 8. Storage -----------------------------------------------------------------------------


def test_samples_are_compact_and_pruned_by_age(alice, host):
    now = timezone.now()
    save_sample(host, net(), now - timedelta(hours=49))
    save_sample(host, net(), now)
    rows = list(NetworkSample.objects.filter(host=host))
    assert len(rows) == 1
    assert rows[0].data["p"][0] == ["Router", "gateway", 5, 5, 2.0, 3.0]
    assert rows[0].data["d"] == [True, 12.0]


def test_samples_have_a_hard_cap(alice, host, monkeypatch):
    monkeypatch.setattr("network.services.MAX_SAMPLES_PER_HOST", 3)
    now = timezone.now()
    for i in range(6):
        save_sample(host, net(), now + timedelta(seconds=i))
    ats = sorted(NetworkSample.objects.filter(host=host).values_list("at", flat=True))
    assert len(ats) == 3
    assert ats[-1] == now + timedelta(seconds=5)  # the newest stay
    assert MAX_SAMPLES_PER_HOST >= 2880  # 48 h of 60 s reports


def test_samples_go_with_their_host(alice, host):
    send(host, [net()])
    host.delete()
    assert not NetworkSample.objects.exists() and not NetworkReporter.objects.exists()


def test_events_of_other_users_stay_hidden(alice, bob, host, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False):
        send(host, [net(), *[net(internet=0)] * 3])
    assert StatusEvent.objects.owned_by(alice).count() == 1
    assert StatusEvent.objects.owned_by(bob).count() == 0


# --- 9. The strip, the page, the settings -----------------------------------------------------


def shown_html(client, url="/"):
    response = client.get(url)
    assert response.status_code == 200, url
    html = response.content.decode()
    return re.sub(r"<template\b.*?</template>", "", html, flags=re.DOTALL)


def test_no_network_data_renders_no_strip(alice_client, alice, host):
    html = shown_html(alice_client)
    assert 'id="net-strip"' not in html
    assert alice_client.get("/api/v1/overview.json").json()["network"] is None


def test_the_strip_is_the_first_thing_on_the_overview(alice_client, alice, host):
    send(host, [net()])
    html = shown_html(alice_client)
    main = html[html.index("<main") :]
    assert main.index('id="net-strip"') < main.index('class="page-head overview-head legacy-only"')
    assert main.index('id="net-strip"') < main.index('id="rack"')
    strip = main[main.index('<section class="net-strip"') : main.index("</section>")]
    assert 'aria-label="Network"' in strip
    assert '<span class="net-strip__status" id="net-status">Network OK</span>' in strip
    assert '<a class="net-strip__more" id="net-more" href="/network/">More info →</a>' in strip
    # Accessible names with the status; no numbers, no title, no dots in the strip.
    for name in ("You", "Router", "ISP box", "Internet"):
        assert f'<span class="net-pt__label">{name}</span>' in strip
    assert '<span class="visually-hidden net-pt__word">: Working</span>' in strip
    assert not re.search(r"\d+ ?ms|%", strip)
    assert "<h2" not in strip and "lamp" not in strip


def test_overview_json_has_the_summary_for_the_refresh(alice_client, alice, host):
    send(host, [net(), *[net(box=0)] * 3])
    data = alice_client.get("/api/v1/overview.json").json()["network"]
    assert data["text"] == "ISP box down"
    assert [(p["name"], p["status"], p["line"]) for p in data["points"]] == [
        ("You", "ok", ""),
        ("Router", "ok", "ok"),
        ("ISP box", "down", "off"),
        ("Internet", "untested", "off"),
    ]


def test_network_js_and_overview_js_never_parse_html():
    for name in ("network.js", "overview.js"):
        js = (SERVER / "static/cronrack/js" / name).read_text()
        assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    overview = (SERVER / "static/cronrack/js/overview.js").read_text()
    assert 'setText("net-announce", now); // once' in overview
    assert 'setAttribute("href", "/network/")' in overview  # a fixed path, never from data


def test_the_page_without_data_says_how_to_turn_it_on(alice_client):
    html = shown_html(alice_client, "/network/")
    assert "No agent reports network data yet." in html and "--network" in html
    assert alice_client.get("/api/v1/network.json").json() == {"network": None, "reporters": []}


def test_the_page_shows_points_in_words(alice_client, alice, host):
    send(host, [net(), *[net(box=0, internet=0)] * 3])
    Host.objects.filter(pk=host.pk).update(
        latest_report=make_report(network=net(box=0, internet=0))
    )
    html = shown_html(alice_client, "/network/")
    assert '<h1 class="page-title" id="net-title">ISP box down</h1>' in html
    assert "Your router is fine; ISP box isn&#x27;t answering (down or rebooting)." in html
    assert '<span class="net-word net-word--ok">Working</span>' in html
    assert '<span class="net-word net-word--down">Down</span>' in html
    assert '<span class="net-word net-word--untested">Not tested</span>' in html
    assert ROUTER_IP in html and BOX_IP in html
    assert '<td class="mono net-cell--latency">no reply</td>' in html
    assert '<td class="mono net-cell--loss">100%</td>' in html
    assert "ping -c 3 192.0.2.254" in html  # the read-only check command
    assert "<polyline" in html  # the trend
    assert 'style="' not in html


def test_a_hostile_point_name_stays_text(alice_client, alice, host):
    evil = net()
    evil["points"][1]["name"] = "<img src=x>"
    assert valid(evil)  # printable: allowed, and escaped wherever it is shown
    send(host, [evil, *[{**evil, "points": [evil["points"][0],
                                            {**evil["points"][1], "received": 0, "median_ms": None, "p95_ms": None},
                                            evil["points"][2]]}] * 3])  # fmt: skip
    Host.objects.filter(pk=host.pk).update(latest_report=make_report(network=evil))
    for url in ("/", "/network/"):
        html = shown_html(alice_client, url)
        assert "<img src=x>" not in html
        assert "&lt;img src=x&gt;" in html


def test_a_silent_view_host_shows_unknown_on_the_page(alice_client, alice, host):
    send(host, [net()])
    Host.objects.filter(pk=host.pk).update(
        latest_report=make_report(network=net()),
        connected=False,
        last_seen_at=timezone.now() - timedelta(hours=2),
    )
    html = shown_html(alice_client, "/network/")
    assert '<h1 class="page-title" id="net-title">Network unknown</h1>' in html
    assert html.count('net-word--untested">Not tested') == 3
    strip = shown_html(alice_client)
    assert '<span class="net-strip__status" id="net-status">Network unknown</span>' in strip
    assert "Network OK" not in strip


def test_picking_the_view_is_owner_scoped(alice_client, alice, bob, host):
    theirs = make_host(bob, "their-box", "green", make_report())
    mine = make_host(alice, "pi", "green", make_report())
    send(host, [net()])
    send(theirs, [net()])
    # Another user's host, a host without network data, junk: 404, nothing changes.
    for choice in (str(theirs.pk), str(mine.pk), "x", "", "١"):
        assert alice_client.post("/network/view/", {"host": choice}).status_code == 404
    assert network_of(alice).view_host_id is None
    send(mine, [net()])
    assert alice_client.post("/network/view/", {"host": str(mine.pk)}).status_code == 302
    assert network_of(alice).view_host_id == mine.pk
    assert alice_client.post("/network/view/", {"host": "auto"}).status_code == 302
    assert network_of(alice).view_host_id is None


def test_view_redirect_stays_on_this_site(alice_client, alice, host):
    send(host, [net()])
    response = alice_client.post(
        "/network/view/", {"host": "auto", "next": "https://evil.example/"}
    )
    assert response["Location"] == "/network/"
    response = alice_client.post(
        "/network/view/", {"host": "auto", "next": f"/hosts/{host.pk}/settings/"}
    )
    assert response["Location"] == f"/hosts/{host.pk}/settings/"


def test_the_host_settings_page_offers_the_network_view(alice_client, alice, host):
    html = shown_html(alice_client, f"/hosts/{host.pk}/settings/")
    assert "Use this host for the network view" not in html  # no network data yet
    send(host, [net()])
    html = shown_html(alice_client, f"/hosts/{host.pk}/settings/")
    assert "Use this host for the network view" in html
    assert "it can still report during a router or internet outage" in html


def test_confirm_reports_setting(alice_client, alice, host):
    send(host, [net()])
    alice_client.post("/network/settings/", {"confirm_reports": "5"})
    assert network_of(alice).confirm_reports == 5
    for bad in ("0", "21", "x", ""):
        alice_client.post("/network/settings/", {"confirm_reports": bad})
        assert network_of(alice).confirm_reports == 5


@pytest.mark.parametrize("url", ["/network/", "/api/v1/network.json"])
def test_the_page_needs_a_login(client, url):
    assert client.get(url).status_code == 302


def test_gets_dont_change_settings(alice_client, alice, host):
    send(host, [net()])
    assert alice_client.get("/network/view/").status_code == 405
    assert alice_client.get("/network/settings/").status_code == 405


@override_settings(DEBUG=True)
@pytest.mark.usefixtures("alice")
@pytest.mark.parametrize(
    ("name", "text"),
    [("ok", "Network OK"), ("router", "Router down"), ("isp", "ISP box down"),
     ("internet", "Internet down"), ("slow", "Internet slow"), ("unknown", "Network unknown")],
)  # fmt: skip
def test_preview_states(client, name, text):
    overview = shown_html(client, f"/dev/preview/?state=small&net={name}")
    assert f'id="net-status">{text}</span>' in overview
    assert f'id="net-title">{text}</h1>' in shown_html(client, f"/dev/preview/network/?net={name}")


@override_settings(DEBUG=True)
@pytest.mark.usefixtures("alice")
def test_preview_without_network_data(client):
    assert 'id="net-strip"' not in shown_html(client, "/dev/preview/?state=small")
    assert "No agent reports network data yet." in shown_html(client, "/dev/preview/network/")


@pytest.mark.usefixtures("alice")
def test_previews_are_debug_only(client):
    assert client.get("/dev/preview/network/?net=ok").status_code == 404


def test_the_page_announces_a_change_and_its_table_can_be_scrolled_by_keyboard(
    alice_client, alice, host
):
    send(host, [net()])
    Host.objects.filter(pk=host.pk).update(latest_report=make_report(network=net()))
    html = shown_html(alice_client, "/network/")
    assert '<p class="visually-hidden" id="net-page-announce" role="status"></p>' in html
    assert '<div class="table-scroll" tabindex="0" role="region"' in html
    js = (SERVER / "static/cronrack/js/network.js").read_text()
    assert 'setText("net-page-announce", network.text); // once' in js


def test_a_held_state_never_carries_over_to_another_host(alice, host):
    # The preferred host is away for hours, another takes over and sees its router down;
    # when the preferred one returns, that state isn't applied to its first report.
    local = make_host(alice, "server", "green", make_report())
    send(local, [net(server_local=True)], start=timezone.now() - timedelta(hours=3))
    send(host, [net(router=0)] * 3)
    network = network_of(alice)
    assert (network.state, network.state_host_id) == ("router_down", host.pk)
    send(local, [net(server_local=True)])
    network = network_of(alice)
    assert (network.state, network.state_host_id) == ("ok", local.pk)
    assert network.router_down_since is None
    assert not StatusEvent.objects.filter(network=network, summary__contains="rebooted").exists()
