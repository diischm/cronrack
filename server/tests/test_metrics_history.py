"""Metric history: ingest (rate-limited), rollup (idempotent), retention, rule windows."""

from datetime import UTC, datetime, timedelta

import pytest

from hosts.models import Host
from metrics import ingest, rollup
from metrics.ingest import MIN_RAW_GAP, host_values, save_metrics
from metrics.models import ContainerSample, HostSample, MountSample, Resolution
from metrics.windows import HostHistory

from .sample import GIB, SAMPLE_METRICS, make_metrics

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
RAW, FIVE, HOUR = Resolution.RAW, Resolution.FIVE_MIN, Resolution.HOUR


def every_minute(host, start, minutes, **changes):
    for i in range(minutes):
        assert save_metrics(host, make_metrics(**changes), start + timedelta(minutes=i))


# --- ingest ------------------------------------------------------------------------------


def test_host_values_from_one_report():
    assert host_values(SAMPLE_METRICS) == {
        "cpu_pct": 23.4,
        "iowait_pct": 1.2,
        "load1": 0.42,
        "mem_pct": 37.5,  # 3 of 8 GiB used (5 available)
        "swap_pct": 12.5,
        "temp_c": 52.1,
        "disk_pct": 61.6,  # the fullest mount
        "read_bps": 184320,
        "write_bps": 921600,
    }


def test_nulls_stay_null():
    values = host_values(make_metrics(memory=None, disk_io=None, mounts=[]))
    assert values["mem_pct"] is None and values["swap_pct"] is None
    assert values["disk_pct"] is None and values["read_bps"] is None
    no_swap = {**SAMPLE_METRICS["memory"], "swap_total": 0, "swap_used": 0}
    assert host_values(make_metrics(memory=no_swap))["swap_pct"] is None


def test_one_report_writes_host_mount_and_container_rows(host):
    assert save_metrics(host, SAMPLE_METRICS, T0)
    assert HostSample.objects.get(host=host).res == RAW
    assert set(MountSample.objects.values_list("mount", flat=True)) == {"/", "/srv/media"}
    assert set(ContainerSample.objects.values_list("name", flat=True)) == {
        "jellyfin",
        "immich-server",
    }


def test_no_metrics_writes_nothing(host):
    assert save_metrics(host, None, T0) is False
    assert not HostSample.objects.exists()


def test_reports_closer_than_the_minimum_gap_are_not_stored(host):
    assert save_metrics(host, SAMPLE_METRICS, T0)
    for seconds in (5, 20, 49):
        assert not save_metrics(host, SAMPLE_METRICS, T0 + timedelta(seconds=seconds))
    assert save_metrics(host, SAMPLE_METRICS, T0 + MIN_RAW_GAP + timedelta(seconds=1))
    assert HostSample.objects.filter(host=host).count() == 2


def test_rows_per_host_are_bounded_by_time_not_by_reports(host):
    # A hostile agent reporting every 5 s for an hour still writes at most one row per
    # MIN_RAW_GAP (50 s): 72 an hour, not 720.
    for i in range(720):
        save_metrics(host, SAMPLE_METRICS, T0 + timedelta(seconds=5 * i))
    assert HostSample.objects.filter(host=host).count() == 3600 // 50


def test_new_container_names_stop_at_the_cap(host, monkeypatch):
    # A stolen token inventing names in every report can't multiply the rows.
    monkeypatch.setattr(ingest, "MAX_CONTAINER_NAMES", 3)
    base = SAMPLE_METRICS["containers"][0]
    for i in range(5):
        data = make_metrics(containers=[{**base, "name": f"c{i}"}])
        save_metrics(host, data, T0 + timedelta(minutes=i))
    assert ContainerSample.objects.values("name").distinct().count() == 3


def test_known_names_still_get_samples_at_the_cap(host, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_CONTAINER_NAMES", 1)
    base = SAMPLE_METRICS["containers"][0]
    for i in range(3):
        save_metrics(host, make_metrics(containers=[base]), T0 + timedelta(minutes=i))
    assert ContainerSample.objects.filter(name=base["name"]).count() == 3


def test_the_name_cap_forgets_after_a_day(host, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_CONTAINER_NAMES", 1)
    base = SAMPLE_METRICS["containers"][0]
    save_metrics(host, make_metrics(containers=[{**base, "name": "old"}]), T0)
    later = T0 + timedelta(hours=25)
    save_metrics(host, make_metrics(containers=[{**base, "name": "new"}]), later)
    assert ContainerSample.objects.filter(name="new").exists()


def test_mount_names_are_capped_too(host, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_MOUNT_NAMES", 1)
    save_metrics(host, SAMPLE_METRICS, T0)  # two mounts in the sample
    assert MountSample.objects.count() == 1


# --- rollup ------------------------------------------------------------------------------


def test_one_hosts_error_does_not_stop_the_others(host, alice, monkeypatch, caplog):
    other = Host.objects.create(owner=alice, name="edge-pi")
    every_minute(host, T0, 5)
    every_minute(other, T0, 5)
    real = rollup._rollup_host

    def fails_for_the_first(model, key, fields, host_id, source, target, now):
        if host_id == host.pk:
            raise RuntimeError("locked")
        return real(model, key, fields, host_id, source, target, now)

    monkeypatch.setattr(rollup, "_rollup_host", fails_for_the_first)
    rollup.rollup(T0 + timedelta(minutes=6))
    assert HostSample.objects.filter(host=other, res=FIVE).count() == 1
    assert "RuntimeError" in caplog.text


def test_closed_five_minute_buckets_are_averaged(host):
    every_minute(host, T0, 10, cpu={**SAMPLE_METRICS["cpu"], "busy_pct": 10.0})
    HostSample.objects.filter(host=host, at__gte=T0 + timedelta(minutes=5)).update(cpu_pct=30.0)

    written = rollup.rollup(T0 + timedelta(minutes=10, seconds=30))

    fives = list(HostSample.objects.filter(host=host, res=FIVE).order_by("at"))
    assert [f.at for f in fives] == [T0, T0 + timedelta(minutes=5)]
    assert [f.cpu_pct for f in fives] == [10.0, 30.0]
    assert written >= 2 + 2 * 2 + 2 * 2  # host + 2 mounts + 2 containers, per bucket


def test_an_open_bucket_waits(host):
    every_minute(host, T0, 3)
    assert rollup.rollup(T0 + timedelta(minutes=3)) == 0
    assert not HostSample.objects.filter(res=FIVE).exists()


def test_rollup_is_idempotent(host):
    every_minute(host, T0, 30)
    now = T0 + timedelta(minutes=31)
    rollup.rollup(now)
    counts = {res: HostSample.objects.filter(res=res).count() for res in (FIVE, HOUR)}
    assert rollup.rollup(now) == 0
    assert {res: HostSample.objects.filter(res=res).count() for res in (FIVE, HOUR)} == counts
    assert ContainerSample.objects.filter(res=FIVE).count() == 2 * 6


def test_hours_are_built_from_five_minute_rows(host):
    every_minute(host, T0, 65)
    rollup.rollup(T0 + timedelta(minutes=66))  # 5-min buckets, and the first closed hour
    hours = HostSample.objects.filter(host=host, res=HOUR)
    assert list(hours.values_list("at", flat=True)) == [T0]
    assert hours.get().cpu_pct == pytest.approx(23.4)


def test_integer_columns_stay_integers_and_nulls_are_skipped(host):
    save_metrics(host, make_metrics(disk_io={"read_bps": 100, "write_bps": 1}), T0)
    save_metrics(host, make_metrics(disk_io=None), T0 + timedelta(minutes=1))
    save_metrics(
        host, make_metrics(disk_io={"read_bps": 201, "write_bps": 2}), T0 + timedelta(minutes=2)
    )
    rollup.rollup(T0 + timedelta(minutes=6))
    five = HostSample.objects.get(host=host, res=FIVE)
    assert five.read_bps == 150 and isinstance(five.read_bps, int)
    assert five.write_bps == 2  # round(1.5) -> 2 (banker's rounding keeps it an int)


def test_each_host_is_rolled_up_separately(host, alice):
    other = Host.objects.create(owner=alice, name="edge-pi")
    every_minute(host, T0, 5)
    every_minute(other, T0, 5, cpu={**SAMPLE_METRICS["cpu"], "busy_pct": 80.0})
    rollup.rollup(T0 + timedelta(minutes=6))
    assert HostSample.objects.get(host=host, res=FIVE).cpu_pct == 23.4
    assert HostSample.objects.get(host=other, res=FIVE).cpu_pct == 80.0


# --- retention ---------------------------------------------------------------------------


def test_prune_keeps_each_resolution_for_its_own_time(host):
    now = T0 + timedelta(days=100)
    for res, age in (
        (RAW, timedelta(hours=23)),
        (RAW, timedelta(hours=25)),
        (FIVE, timedelta(days=6)),
        (FIVE, timedelta(days=8)),
        (HOUR, timedelta(days=89)),
        (HOUR, timedelta(days=91)),
    ):
        HostSample.objects.create(host=host, res=res, at=now - age)
        ContainerSample.objects.create(host=host, res=res, at=now - age, name="web")
    deleted = rollup.prune(now)
    assert deleted == 6
    assert HostSample.objects.count() == 3
    assert ContainerSample.objects.count() == 3
    assert rollup.prune(now) == 0  # idempotent


def test_deleting_a_host_deletes_its_history(host):
    save_metrics(host, SAMPLE_METRICS, T0)
    host.delete()
    assert not HostSample.objects.exists()
    assert not ContainerSample.objects.exists()
    assert not MountSample.objects.exists()


# --- windows for the rules ----------------------------------------------------------------


def test_history_returns_recent_raw_points_oldest_first(host):
    every_minute(host, T0, 20)
    history = HostHistory(host.pk, T0 + timedelta(minutes=20))
    points = history.host("cpu_pct")
    assert len(points) == 15  # LOOKBACK is 15 minutes
    assert points[0][0] < points[-1][0]
    assert history.container_cpu("jellyfin")[-1][1] == 4.1
    assert history.container_mem_of_limit("immich-server")[-1][1] == pytest.approx(50.0)
    assert history.container_mem_of_limit("jellyfin") == []  # no limit, nothing to be near


def test_history_only_reads_the_database_once(host, django_assert_num_queries):
    every_minute(host, T0, 5)
    history = HostHistory(host.pk, T0 + timedelta(minutes=5))
    with django_assert_num_queries(2):  # one for the host, one for its containers
        for _ in range(3):
            history.host("cpu_pct")
            history.host("mem_pct")
            history.container_cpu("jellyfin")


def test_history_of_another_host_is_separate(host, alice):
    other = Host.objects.create(owner=alice, name="edge-pi")
    every_minute(other, T0, 5)
    assert HostHistory(host.pk, T0 + timedelta(minutes=5)).host("cpu_pct") == []


def test_memory_numbers_in_gib_are_right():
    assert (
        host_values(
            make_metrics(
                memory={"total": 4 * GIB, "available": GIB, "swap_total": 0, "swap_used": 0}
            )
        )["mem_pct"]
        == 75.0
    )
