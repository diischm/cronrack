"""The resource rules (rules/resources.py): sustained windows, limits, subjects, no metrics.

A fake history stands in for metrics.windows.HostHistory, so these need no database.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from notifications.config import NotifyConfig, RuleChoice
from rules.base import RED, YELLOW, fingerprint
from rules.engine import evaluate
from rules.resources import MAX_GAP, sustained

from .sample import GIB, make_metrics, make_metrics_report, make_report
from .test_rules_engine import NOW, fake_host, override


class FakeHistory:
    def __init__(self, host=None, containers=None, mem_of_limit=None):
        self._host = host or {}
        self._containers = containers or {}
        self._mem = mem_of_limit or {}

    def host(self, field):
        return self._host.get(field, [])

    def container_cpu(self, name):
        return self._containers.get(name, [])

    def container_mem_of_limit(self, name):
        return self._mem.get(name, [])


def minutes(values, end=NOW):
    """One point a minute, the last one at `end`."""
    count = len(values)
    return [(end - timedelta(minutes=count - 1 - i), v) for i, v in enumerate(values)]


def run(history, report=None, overrides=(), notify=None):
    return evaluate(
        fake_host(),
        make_metrics_report() if report is None else report,
        NOW,
        overrides=list(overrides),
        notify=notify,
        history=history,
    )


def by_rule(findings, rule_id):
    return [f for f in findings if f.rule_id == rule_id]


# --- sustained() -------------------------------------------------------------------------


def over90(value):
    return value >= 90


def test_sustained_needs_the_whole_window():
    window = timedelta(minutes=10)
    assert sustained(minutes([95] * 11), NOW, window, over90)
    assert not sustained(minutes([95] * 5), NOW, window, over90)  # only 4 minutes covered


def test_one_quiet_minute_does_not_end_it_and_one_spike_does_not_start_it():
    window = timedelta(minutes=10)
    assert sustained(minutes([95] * 5 + [40] + [95] * 5), NOW, window, over90)
    assert not sustained(minutes([40] * 10 + [99]), NOW, window, over90)
    assert not sustained(minutes([95] * 6 + [40] * 5), NOW, window, over90)


def test_a_gap_means_no_judgement():
    window = timedelta(minutes=10)
    points = minutes([95] * 11)
    del points[3:7]  # 5 minutes missing: the host was offline
    assert not sustained(points, NOW, window, over90)
    stale = minutes([95] * 11, end=NOW - MAX_GAP - timedelta(seconds=1))
    assert not sustained(stale, NOW, window, over90)  # nothing recent


@pytest.mark.parametrize("seconds_since_last", [0, 15, 45, 59])
@pytest.mark.parametrize("spacing", [60, 61, 65])
def test_long_sustained_load_is_steady_whatever_the_report_phase(spacing, seconds_since_last):
    # Regression: the same busy host must not flicker with the page's render time or
    # with reports that aren't exactly 60 s apart.
    window = timedelta(minutes=10)
    now = NOW + timedelta(seconds=seconds_since_last)
    points = [(NOW - timedelta(seconds=spacing * i), 95.0) for i in range(14, -1, -1)]
    assert sustained(points, now, window, over90)


def test_points_outside_the_window_are_ignored():
    old_busy_then_calm = minutes([99] * 10 + [10] * 11)
    assert not sustained(old_busy_then_calm, NOW, timedelta(minutes=10), over90)


# --- host.cpu_high -----------------------------------------------------------------------


def test_cpu_high_fires_after_ten_minutes_over_the_limit():
    findings = by_rule(run(FakeHistory({"cpu_pct": minutes([96] * 11)})), "host.cpu_high")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.text == "CPU at 96% for 10+ min"
    assert finding.severity == YELLOW
    assert finding.subject == ""
    assert finding.fingerprint == fingerprint("host.cpu_high", 1, "")
    assert finding.fix.startswith("ps -eo")


def test_cpu_high_is_quiet_below_the_limit_and_for_a_short_burst():
    assert not by_rule(run(FakeHistory({"cpu_pct": minutes([85] * 11)})), "host.cpu_high")
    assert not by_rule(run(FakeHistory({"cpu_pct": minutes([99] * 4)})), "host.cpu_high")


def test_cpu_high_uses_the_notification_limit():
    notify = NotifyConfig(choices={"host.cpu_high": RuleChoice(level="alert", limit="80")})
    history = FakeHistory({"cpu_pct": minutes([85] * 11)})
    assert by_rule(run(history, notify=notify), "host.cpu_high")


def test_cpu_high_uses_an_override_threshold():
    history = FakeHistory({"cpu_pct": minutes([96] * 11)})
    findings = run(history, overrides=[override("host.cpu_high", threshold=97)])
    assert not by_rule(findings, "host.cpu_high")


def test_the_fingerprint_stays_the_same_while_the_value_moves():
    a = by_rule(run(FakeHistory({"cpu_pct": minutes([92] * 11)})), "host.cpu_high")[0]
    b = by_rule(run(FakeHistory({"cpu_pct": minutes([99] * 11)})), "host.cpu_high")[0]
    assert a.fingerprint == b.fingerprint and a.text != b.text


# --- memory and swap ---------------------------------------------------------------------


def test_memory_high_is_about_available_memory():
    history = FakeHistory({"mem_pct": minutes([93] * 6)})  # 7 % available, for 5 min
    findings = by_rule(run(history), "host.memory_high")
    assert [f.text for f in findings] == ["Memory low: 7% available"]
    assert not by_rule(run(FakeHistory({"mem_pct": minutes([85] * 6)})), "host.memory_high")


def test_memory_limit_choices_are_free_percentages():
    notify = NotifyConfig(choices={"host.memory_high": RuleChoice(level="alert", limit="5")})
    history = FakeHistory({"mem_pct": minutes([93] * 6)})  # 7 % free is fine with a 5 % limit
    assert not by_rule(run(history, notify=notify), "host.memory_high")


def test_swap_high_is_per_report():
    memory = {"total": 8 * GIB, "available": 4 * GIB, "swap_total": 4 * GIB, "swap_used": 3 * GIB}
    findings = by_rule(
        run(FakeHistory(), make_metrics_report(make_metrics(memory=memory))), "host.swap_high"
    )
    assert [f.text for f in findings] == ["Swap 75% used"]


def test_no_swap_means_no_swap_finding():
    memory = {"total": 8 * GIB, "available": 4 * GIB, "swap_total": 0, "swap_used": 0}
    report = make_metrics_report(make_metrics(memory=memory))
    assert not by_rule(run(FakeHistory(), report), "host.swap_high")


# --- temperature -------------------------------------------------------------------------


def test_temperature_is_yellow_from_the_limit_and_red_from_85():
    yellow = by_rule(run(FakeHistory({"temp_c": minutes([81] * 6)})), "host.cpu_temp_high")
    assert [(f.text, f.severity) for f in yellow] == [("CPU temperature 81 °C", YELLOW)]
    red = by_rule(run(FakeHistory({"temp_c": minutes([88] * 6)})), "host.cpu_temp_high")
    assert red[0].severity == RED


def test_no_sensor_means_the_rule_does_not_apply():
    cpu = {**make_metrics()["cpu"], "temp_c": None}
    report = make_metrics_report(make_metrics(cpu=cpu))
    history = FakeHistory({"temp_c": minutes([95] * 6)})
    assert not by_rule(run(history, report), "host.cpu_temp_high")


# --- inodes ------------------------------------------------------------------------------


def test_inodes_low_per_mount_with_a_stable_subject():
    mounts = [
        {
            "mount": "/",
            "fs": "ext4",
            "size": GIB,
            "free": GIB // 2,
            "used_pct": 50.0,
            "inodes_pct": 92.0,
        },
        {
            "mount": "/srv",
            "fs": "ext4",
            "size": GIB,
            "free": GIB // 2,
            "used_pct": 50.0,
            "inodes_pct": 97.0,
        },
        {
            "mount": "/data",
            "fs": "btrfs",
            "size": GIB,
            "free": GIB // 2,
            "used_pct": 50.0,
            "inodes_pct": None,
        },
    ]
    findings = by_rule(
        run(FakeHistory(), make_metrics_report(make_metrics(mounts=mounts))), "disk.inodes_low"
    )
    assert [(f.subject, f.severity) for f in findings] == [("mount:/", YELLOW), ("mount:/srv", RED)]
    assert findings[0].fix == "df -i /"


def test_inode_fix_quotes_a_hostile_mount_name():
    mounts = [
        {
            "mount": "/x; rm -rf ~",
            "fs": "ext4",
            "size": GIB,
            "free": 0,
            "used_pct": 100.0,
            "inodes_pct": 99.0,
        }
    ]
    finding = by_rule(
        run(FakeHistory(), make_metrics_report(make_metrics(mounts=mounts))), "disk.inodes_low"
    )[0]
    assert finding.fix == "df -i '/x; rm -rf ~'"


# --- containers --------------------------------------------------------------------------


def test_container_cpu_high_per_container():
    history = FakeHistory(
        containers={"jellyfin": minutes([95] * 11), "immich-server": minutes([5] * 11)}
    )
    findings = by_rule(run(history), "docker.container_cpu_high")
    assert [(f.subject, f.text) for f in findings] == [
        ("container:jellyfin", "jellyfin container CPU at 95% of the host")
    ]
    assert findings[0].fix == "docker stats --no-stream jellyfin"


def test_container_memory_only_for_containers_with_a_limit():
    # jellyfin has no limit in the sample report: even a "100 %" history never fires.
    history = FakeHistory(
        mem_of_limit={"immich-server": minutes([93] * 6), "jellyfin": minutes([100] * 6)}
    )
    findings = by_rule(run(history), "docker.container_memory_near_limit")
    assert [f.subject for f in findings] == ["container:immich-server"]
    assert findings[0].text == "immich-server container memory at 93% of its limit"


def test_a_container_override_only_changes_that_container():
    history = FakeHistory(containers={"jellyfin": minutes([95] * 11)})
    quiet = override("docker.container_cpu_high", subject="container:jellyfin", enabled=False)
    assert not by_rule(run(history, overrides=[quiet]), "docker.container_cpu_high")


# --- reports without metrics change nothing ----------------------------------------------


@pytest.mark.parametrize("report", [make_report(), make_metrics_report(None)], ids=["old", "null"])
def test_no_metrics_no_resource_findings_and_no_history_read(report):
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError("history must not be read")

    findings = evaluate(fake_host(), report, NOW, overrides=[], history=Exploding())
    assert not [
        f
        for f in findings
        if f.rule_id.startswith(
            ("host.", "docker.container_cpu", "docker.container_memory", "disk.inodes")
        )
    ]


def test_off_in_notification_settings_means_no_finding():
    notify = NotifyConfig(choices={"host.cpu_high": RuleChoice(level="off")})
    history = FakeHistory({"cpu_pct": minutes([99] * 11)})
    assert not by_rule(run(history, notify=notify), "host.cpu_high")


def test_balanced_defaults_alert_and_show_as_planned():
    cfg = NotifyConfig()
    assert cfg.level("host.cpu_high") == "alert"
    assert cfg.level("host.swap_high") == "show"
    assert cfg.level("docker.container_cpu_high") == "show"
    assert NotifyConfig(preset="quiet").level("host.swap_high") == "off"
    assert NotifyConfig(preset="everything").level("docker.container_cpu_high") == "alert"


def test_fake_history_is_never_shared_between_hosts():
    # The engine builds one history per evaluation; a SimpleNamespace host works too.
    host = SimpleNamespace(**{**vars(fake_host()), "pk": 2})
    findings = evaluate(
        host,
        make_metrics_report(),
        NOW,
        overrides=[],
        history=FakeHistory({"cpu_pct": minutes([99] * 11)}),
    )
    assert by_rule(findings, "host.cpu_high")[0].fingerprint == fingerprint("host.cpu_high", 2, "")
