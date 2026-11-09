"""The metrics UI: overview meters, chart building, the host page panel, security rules."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from hosts.models import Host
from hosts.presenters import meters
from metrics.charts import build_chart, downsample
from metrics.ingest import save_metrics
from metrics.presenters import DEFAULT_RANGE, clean_range, human_bytes
from rules.base import Finding

from .sample import GIB, SAMPLE_METRICS, make_metrics, make_metrics_report, make_report

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
STATIC = Path(__file__).resolve().parents[1] / "static" / "cronrack"


def finding(rule_id, severity="yellow", subject=""):
    return Finding(rule_id, subject, severity, "text", "why", "", "fp")


def by_key(rows):
    return {row["key"]: row for row in rows}


# --- overview meters ---------------------------------------------------------------------


def test_meters_values():
    rows = by_key(meters(make_metrics_report(disks=[{"mount": "/", "percent": 62}]), []))
    assert rows["cpu"]["percent"] == 23
    assert rows["ram"]["percent"] == 38  # 3 of 8 GiB used
    assert rows["disk"]["percent"] == 62
    assert all(row["level"] == "" for row in rows.values())


def test_a_high_number_alone_stays_grey():
    cpu = {**SAMPLE_METRICS["cpu"], "busy_pct": 99.0}
    rows = by_key(meters(make_metrics_report(make_metrics(cpu=cpu)), []))
    assert rows["cpu"]["percent"] == 99 and rows["cpu"]["level"] == ""


def test_colour_comes_from_the_rule_finding():
    rows = by_key(
        meters(
            make_metrics_report(),
            [finding("host.cpu_high"), finding("disk.usage", "red", "mount:/")],
        )
    )
    assert rows["cpu"]["level"] == "yellow"
    assert rows["ram"]["level"] == ""
    assert rows["disk"]["level"] == "red"


def test_an_old_agent_has_only_the_disk_meter():
    assert [row["key"] for row in meters(make_report(), [])] == ["disk"]
    assert meters(None, []) == []


# --- charts ------------------------------------------------------------------------------


def points(values, step=60):
    start = NOW - timedelta(seconds=step * (len(values) - 1))
    return [(start + timedelta(seconds=step * i), v) for i, v in enumerate(values)]


def chart(values, **kwargs):
    defaults = {
        "title": "CPU",
        "unit": "%",
        "range_words": "last hour",
        "gap": timedelta(minutes=3),
    }
    return build_chart(points(values), NOW - timedelta(hours=1), NOW, **{**defaults, **kwargs})


def test_chart_numbers_and_summary():
    built = chart([10, 20, 60], limit=90)
    assert (built["min"], built["avg"], built["max"], built["now"]) == ("10%", "30%", "60%", "60%")
    assert built["summary"] == (
        "CPU, last hour: lowest 10%, average 30%, highest 60%, now 60%. Limit 90%."
    )
    assert built["over"] is False


def test_chart_only_marks_over_when_it_crossed_the_limit():
    assert chart([10, 95], limit=90)["over"] is True
    assert chart([10, 95])["over"] is False


def test_chart_paths_are_only_numbers():
    built = chart([10, 20, 60], limit=90)
    assert re.fullmatch(r"[ML0-9., Z-]+", built["line"])
    assert re.fullmatch(r"[ML0-9., Z-]+", built["area"])


def test_a_gap_breaks_the_line():
    start = NOW - timedelta(minutes=30)
    data = [(start, 10.0), (start + timedelta(minutes=1), 12.0), (NOW, 14.0)]
    built = build_chart(
        data,
        NOW - timedelta(hours=1),
        NOW,
        title="CPU",
        unit="%",
        range_words="last hour",
        gap=timedelta(minutes=3),
    )
    assert built["line"].count("M") == 2


def test_no_points_is_an_honest_empty_chart():
    built = chart([], limit=90)
    assert built["empty"] is True and "no data yet" in built["summary"]


def test_values_above_the_scale_stay_inside_the_chart():
    built = chart([250.0])
    assert built["line"].split(",")[1].split(" ")[0] == "0"  # y = 0 is the top edge


def test_downsample():
    data = points(list(range(1000)))
    small = downsample(data, 96)
    assert len(small) == 96
    assert small[-1][0] == data[-1][0]
    assert downsample(data[:10], 96) == data[:10]


def test_clean_range():
    assert clean_range("7d") == "7d"
    assert clean_range("../../etc") == DEFAULT_RANGE
    assert clean_range(None) == DEFAULT_RANGE


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, "n/a"),
        (512, "512 B"),
        (2048, "2.0 KiB"),
        (3 * GIB, "3.0 GiB"),
        (150 * GIB, "150 GiB"),
    ],
)
def test_human_bytes(value, text):
    assert human_bytes(value) == text


# --- the host page -----------------------------------------------------------------------


@pytest.fixture
def reported(alice):
    """A host with a report at 'now' and half an hour of samples behind it."""
    host = Host.objects.create(owner=alice, name="edge-pi")
    now = timezone.now()
    for i in range(30, -1, -1):
        save_metrics(host, SAMPLE_METRICS, now - timedelta(minutes=i))
    host.latest_report = make_metrics_report()
    host.report_received_at = host.last_seen_at = now
    host.status = "green"
    host.save()
    return host


@pytest.mark.django_db
def test_host_page_has_the_panel_charts_and_containers(alice_client, reported):
    html = alice_client.get(f"/hosts/{reported.pk}/?range=1h").content.decode()
    assert 'id="panel-metrics"' in html
    assert 'class="mchart__svg"' in html
    for title in ("CPU", "Memory", "Disk", "Temperature"):
        assert f'<span class="mchart__title">{title}</span>' in html
    assert "jellyfin" in html and "immich-server" in html
    assert 'aria-controls="mct-more-0"' in html
    assert 'aria-label="CPU, last hour: lowest' in html
    assert 'href="?range=24h#panel-metrics"' in html
    assert re.search(r'href="\?range=1h#panel-metrics" aria-current="true"', html)


@pytest.mark.django_db
def test_a_bad_range_falls_back_and_is_never_echoed(alice_client, reported):
    html = alice_client.get(
        f"/hosts/{reported.pk}/", {"range": '"><script>alert(1)</script>'}
    ).content.decode()
    assert "alert(1)" not in html
    assert re.search(r'href="\?range=24h#panel-metrics" aria-current="true"', html)


@pytest.mark.django_db
def test_no_temperature_chart_without_a_sensor(alice_client, alice):
    host = Host.objects.create(owner=alice, name="node-a")
    cpu = {**SAMPLE_METRICS["cpu"], "temp_c": None}
    host.latest_report = make_metrics_report(make_metrics(cpu=cpu))
    host.last_seen_at = host.report_received_at = timezone.now()
    host.save()
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    assert 'id="panel-metrics"' in html
    assert "Temperature" not in html


@pytest.mark.django_db
def test_old_agent_hosts_have_no_metrics_panel(alice_client, alice):
    host = Host.objects.create(owner=alice, name="node-a")
    host.latest_report = make_report()
    host.last_seen_at = host.report_received_at = timezone.now()
    host.save()
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    assert 'id="panel-metrics"' not in html


@pytest.mark.django_db
def test_another_users_host_is_404(client, bob, reported):
    client.force_login(bob)
    assert client.get(f"/hosts/{reported.pk}/?range=7d").status_code == 404


@pytest.mark.django_db
def test_hostile_container_names_are_escaped(alice_client, alice):
    host = Host.objects.create(owner=alice, name="node-a")
    evil = '<img src=x onerror="alert(1)">'
    containers = [{**SAMPLE_METRICS["containers"][0], "name": evil}]
    host.latest_report = make_metrics_report(make_metrics(containers=containers))
    host.last_seen_at = host.report_received_at = timezone.now()
    host.save()
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    assert evil not in html
    assert "&lt;img src=x" in html


@pytest.mark.django_db
def test_overview_renders_meters_and_the_json_has_them(alice_client, reported):
    html = alice_client.get("/").content.decode()
    assert 'class="meter mmeter__bar"' in html
    blank = html.split('<template id="rack-unit-template">')[1].split("</template>")[0]
    assert "style=" not in blank
    data = alice_client.get("/api/v1/overview.json").json()
    assert [m["key"] for m in data["hosts"][0]["meters"]] == ["cpu", "ram", "disk"]


def test_the_static_files_build_charts_without_inner_html():
    for name in ("js/overview.js", "js/glass-tabs.js"):
        assert "innerHTML" not in (STATIC / name).read_text(encoding="utf-8")
