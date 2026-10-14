"""Rule engine tests. Plain objects stand in for hosts, overrides and suppressions,
so most of these need no database."""

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from rules.base import fingerprint
from rules.engine import (
    apply_suppressions,
    compute_status,
    effective_status,
    evaluate,
    resolve_override,
    sort_hosts,
    top_issues,
)
from rules.models import RuleOverride, fingerprint_for
from rules.registry import RULES

from .sample import make_report

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
OWNER = 10
HOST_ID = 1

MVP_RULES = {
    "connectivity.offline",
    "updates.security_pending",
    "updates.pending",
    "updates.reboot_required",
    "docker.container_unhealthy",
    "docker.container_restarting",
    "docker.container_exited_error",
    "systemd.unit_failed",
    "disk.usage",
}


def fake_host(**changes):
    values = {
        "pk": HOST_ID,
        "owner_id": OWNER,
        "name": "nas",
        "status": "green",
        "connected": True,
        "last_seen_at": NOW,
        "learning_until": None,
        "maintenance_until": None,
        "latest_report": make_report(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def override(rule_id, host_id=None, subject="", owner_id=OWNER, **fields):
    values = {"enabled": None, "severity": "", "threshold": None, "notify": ""}
    values.update(fields)
    return SimpleNamespace(
        owner_id=owner_id, rule_id=rule_id, host_id=host_id, subject=subject, **values
    )


def suppression(fp, kind="expected", expires_at=None, host_id=HOST_ID, owner_id=OWNER):
    return SimpleNamespace(
        fingerprint=fp, kind=kind, expires_at=expires_at, host_id=host_id, owner_id=owner_id
    )


def run(report=None, host=None, overrides=()):
    host = host or fake_host()
    return evaluate(host, make_report() if report is None else report, NOW, overrides=overrides)


def ids(findings):
    return [f.rule_id for f in findings]


def container(**changes):
    values = {
        "name": "nginx",
        "state": "running",
        "status": "Up 2 hours",
        "health": None,
        "exit_code": None,
    }
    values.update(changes)
    return values


# --- registry ------------------------------------------------------------------


def test_only_the_mvp_rules_are_registered():
    assert set(RULES) == MVP_RULES


@pytest.mark.parametrize("rule", RULES.values(), ids=lambda r: r.id)
def test_every_rule_explains_itself(rule):
    assert rule.title
    assert len(rule.why) > 20
    assert rule.default_severity in {"red", "yellow", "info"}


def test_healthy_report_has_no_findings():
    assert run() == []


# --- each MVP rule fires, and doesn't fire ------------------------------------


def test_offline_fires_after_the_threshold():
    host = fake_host(connected=False, last_seen_at=NOW - timedelta(seconds=91))

    findings = run(host=host)

    assert ids(findings) == ["connectivity.offline"]
    assert findings[0].severity == "red"
    assert findings[0].text == "Last seen 1 min ago"


@pytest.mark.parametrize(
    "changes",
    [
        {"connected": False, "last_seen_at": NOW - timedelta(seconds=90)},  # not past it yet
        {"connected": True, "last_seen_at": NOW - timedelta(hours=5)},  # socket still open
        {"connected": False, "last_seen_at": None},  # never connected: waiting, not offline
    ],
)
def test_offline_does_not_fire(changes):
    assert run(host=fake_host(**changes)) == []


def test_security_updates():
    findings = run(make_report(updates={"total": 5, "security": 2}))

    assert ids(findings) == ["updates.security_pending", "updates.pending"]
    assert findings[0].text == "2 security updates"
    assert findings[0].severity == "red"
    # Only the 3 non-security updates count as "pending" (yellow).
    assert findings[1].text == "3 updates pending"
    assert findings[1].severity == "yellow"


def test_only_security_updates_is_not_also_pending():
    assert ids(run(make_report(updates={"total": 1, "security": 1}))) == [
        "updates.security_pending"
    ]


@pytest.mark.parametrize("updates", [None, {"total": 0, "security": 0}])
def test_no_update_findings(updates):
    assert run(make_report(updates=updates)) == []


def test_reboot_required():
    findings = run(make_report(reboot_required=True))

    assert ids(findings) == ["updates.reboot_required"]
    assert findings[0].severity == "yellow"


@pytest.mark.parametrize("value", [False, None])
def test_reboot_not_required(value):
    assert run(make_report(reboot_required=value)) == []


@pytest.mark.parametrize(
    ("changes", "rule_id"),
    [
        ({"health": "unhealthy"}, "docker.container_unhealthy"),
        ({"state": "restarting"}, "docker.container_restarting"),
        ({"state": "exited", "exit_code": 137}, "docker.container_exited_error"),
        ({"state": "exited", "exit_code": -1}, "docker.container_exited_error"),
    ],
)
def test_container_rules_fire(changes, rule_id):
    findings = run(make_report(containers=[container(**changes)]))

    assert ids(findings) == [rule_id]
    assert findings[0].subject == "container:nginx"
    assert findings[0].severity == "red"
    assert findings[0].fix == "docker logs --tail 50 nginx"


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"health": "healthy"},
        {"health": "starting"},
        {"state": "exited", "exit_code": 0},  # finished normally
        {"state": "created", "exit_code": None},
    ],
)
def test_container_rules_do_not_fire(changes):
    assert run(make_report(containers=[container(**changes)])) == []


def test_exit_code_in_text():
    findings = run(make_report(containers=[container(state="exited", exit_code=137)]))

    assert findings[0].text == "nginx exited with code 137"


def test_one_finding_per_container():
    containers = [container(name="a", state="restarting"), container(name="b", state="restarting")]

    findings = run(make_report(containers=containers))

    assert [f.subject for f in findings] == ["container:a", "container:b"]


def test_failed_unit():
    findings = run(make_report(failed_units=["backup.service"]))

    assert ids(findings) == ["systemd.unit_failed"]
    assert findings[0].subject == "unit:backup.service"
    assert findings[0].text == "backup.service failed"
    assert findings[0].fix == "journalctl -u backup.service -n 50 --no-pager"


@pytest.mark.parametrize("units", [[], None])
def test_no_failed_units(units):
    assert run(make_report(failed_units=units)) == []


@pytest.mark.parametrize(
    ("percent", "severity"),
    [(84, None), (85, "yellow"), (94, "yellow"), (95, "red"), (100, "red")],
)
def test_disk_usage_levels(percent, severity):
    findings = run(make_report(disks=[{"mount": "/", "percent": percent}]))

    if severity is None:
        assert findings == []
    else:
        assert ids(findings) == ["disk.usage"]
        assert findings[0].severity == severity
        assert findings[0].subject == "mount:/"
        assert findings[0].text == f"Disk / at {percent}%"


def test_disk_usage_per_mount():
    disks = [{"mount": "/", "percent": 10}, {"mount": "/mnt/backup", "percent": 90}]

    findings = run(make_report(disks=disks))

    assert [f.subject for f in findings] == ["mount:/mnt/backup"]


def test_null_checks_give_no_findings():
    report = make_report(
        updates=None, reboot_required=None, containers=None, failed_units=None, disks=None
    )

    assert run(report) == []


def test_fix_commands_quote_untrusted_names():
    hostile = "x; rm -rf ~"

    findings = run(make_report(containers=[container(name=hostile, state="restarting")]))

    assert findings[0].fix == "docker logs --tail 50 'x; rm -rf ~'"


def test_disk_fix_quotes_the_mount():
    findings = run(make_report(disks=[{"mount": "/mnt/my disk", "percent": 99}]))

    assert findings[0].fix == "sudo du -xh --max-depth=1 '/mnt/my disk' | sort -h | tail -n 15"


# --- fingerprints ----------------------------------------------------------------


def test_fingerprint_is_stable():
    value = fingerprint("disk.usage", 7, "mount:/")

    # SHA-256 of "rule_id|host_id|subject": the same inputs always give the same ID.
    assert value == hashlib.sha256(b"disk.usage|7|mount:/").hexdigest()
    assert value == fingerprint("disk.usage", 7, "mount:/")
    # Same formula as the Suppression model, so suppressions match findings.
    assert value == fingerprint_for("disk.usage", 7, "mount:/")


@pytest.mark.parametrize(
    "other",
    [("disk.usage", 2, "mount:/"), ("disk.usage", 1, "mount:/srv"), ("updates.pending", 1, "")],
)
def test_fingerprint_differs_per_rule_host_and_subject(other):
    assert fingerprint("disk.usage", 1, "mount:/") != fingerprint(*other)


def test_finding_fingerprint_survives_new_reports():
    first = run(make_report(disks=[{"mount": "/", "percent": 90}]))
    second = run(
        make_report(disks=[{"mount": "/", "percent": 97}], timestamp="2026-10-12T12:01:00Z")
    )

    assert first[0].fingerprint == second[0].fingerprint == fingerprint("disk.usage", 1, "mount:/")


# --- overrides -------------------------------------------------------------------


def resolve(overrides, rule_id="disk.usage", subject="mount:/"):
    return resolve_override(OWNER, rule_id, HOST_ID, subject, overrides=overrides)


def test_no_override_gives_code_defaults():
    effective = resolve([])

    assert effective.enabled is True
    assert effective.severity == "yellow"
    assert effective.severity_overridden is False
    assert effective.threshold == 85
    assert effective.notify == ""


def test_override_precedence_most_specific_wins():
    overrides = [
        override("disk.usage", severity="info"),  # all hosts
        override("disk.usage", subject="mount:/", severity="yellow"),  # all hosts, this mount
        override("disk.usage", host_id=HOST_ID, severity="red"),  # this host
        override("disk.usage", host_id=HOST_ID, subject="mount:/", severity="ignore"),
    ]

    assert resolve(overrides).severity == "ignore"
    assert resolve(overrides[:3]).severity == "red"
    assert resolve(overrides[:2]).severity == "yellow"
    assert resolve(overrides[:1]).severity == "info"
    # Order in the list doesn't matter.
    assert resolve(list(reversed(overrides))).severity == "ignore"


def test_override_fields_merge():
    overrides = [
        override("disk.usage", threshold=70),
        override("disk.usage", host_id=HOST_ID, severity="red"),
    ]

    effective = resolve(overrides)

    assert effective.threshold == 70
    assert effective.severity == "red"
    assert effective.severity_overridden is True


def test_subject_override_only_applies_to_its_subject():
    overrides = [override("disk.usage", host_id=HOST_ID, subject="mount:/srv", enabled=False)]

    assert resolve(overrides, subject="mount:/").enabled is True
    assert resolve(overrides, subject="mount:/srv").enabled is False


def test_overrides_of_other_owners_hosts_and_rules_are_ignored():
    overrides = [
        override("disk.usage", owner_id=OWNER + 1, severity="red"),
        override("disk.usage", host_id=HOST_ID + 1, severity="red"),
        override("updates.pending", severity="red"),
    ]

    assert resolve(overrides).severity == "yellow"


def test_unknown_rule_is_refused():
    with pytest.raises(KeyError):
        resolve([], rule_id="no.such_rule")


def test_disabled_rule_produces_no_findings():
    overrides = [override("updates.reboot_required", enabled=False)]

    assert run(make_report(reboot_required=True), overrides=overrides) == []


def test_disabled_globally_but_enabled_for_one_container():
    overrides = [
        override("docker.container_restarting", enabled=False),
        override(
            "docker.container_restarting", host_id=HOST_ID, subject="container:db", enabled=True
        ),
    ]
    containers = [
        container(name="web", state="restarting"),
        container(name="db", state="restarting"),
    ]

    findings = run(make_report(containers=containers), overrides=overrides)

    assert [f.subject for f in findings] == ["container:db"]


def test_severity_override_changes_the_finding():
    overrides = [override("updates.reboot_required", host_id=HOST_ID, severity="red")]

    findings = run(make_report(reboot_required=True), overrides=overrides)

    assert findings[0].severity == "red"


def test_disk_threshold_override():
    overrides = [override("disk.usage", host_id=HOST_ID, subject="mount:/", threshold=50)]

    findings = run(make_report(disks=[{"mount": "/", "percent": 60}]), overrides=overrides)

    assert findings[0].severity == "yellow"


def test_disk_threshold_above_red_level():
    overrides = [override("disk.usage", threshold=98)]

    assert run(make_report(disks=[{"mount": "/", "percent": 96}]), overrides=overrides) == []
    findings = run(make_report(disks=[{"mount": "/", "percent": 98}]), overrides=overrides)
    assert findings[0].severity == "red"


def test_offline_threshold_override():
    host = fake_host(connected=False, last_seen_at=NOW - timedelta(minutes=5))
    overrides = [override("connectivity.offline", host_id=HOST_ID, threshold=600)]

    assert run(host=host, overrides=overrides) == []


# --- suppressions ----------------------------------------------------------------


def reboot_finding():
    return run(make_report(reboot_required=True))


def test_expected_hides_the_finding():
    findings = reboot_finding()
    rules = [suppression(findings[0].fingerprint)]

    active, suppressed = apply_suppressions(findings, fake_host(), NOW, suppressions=rules)

    assert active == []
    assert suppressed == findings


def test_unexpired_snooze_hides_the_finding():
    findings = reboot_finding()
    snooze = suppression(findings[0].fingerprint, "snooze", expires_at=NOW + timedelta(days=1))

    active, _ = apply_suppressions(findings, fake_host(), NOW, suppressions=[snooze])

    assert active == []


def test_expired_snooze_brings_the_finding_back():
    findings = reboot_finding()
    snooze = suppression(findings[0].fingerprint, "snooze", expires_at=NOW - timedelta(seconds=1))

    active, suppressed = apply_suppressions(findings, fake_host(), NOW, suppressions=[snooze])

    assert active == findings
    assert suppressed == []


def test_suppression_for_another_host_or_owner_does_nothing():
    findings = reboot_finding()
    rules = [
        suppression(findings[0].fingerprint, host_id=HOST_ID + 1),
        suppression(findings[0].fingerprint, owner_id=OWNER + 1),
    ]

    active, _ = apply_suppressions(findings, fake_host(), NOW, suppressions=rules)

    assert active == findings


def test_suppression_only_hides_its_own_finding():
    findings = run(make_report(reboot_required=True, failed_units=["x.service"]))
    rules = [suppression(fingerprint("updates.reboot_required", HOST_ID, ""))]

    active, _ = apply_suppressions(findings, fake_host(), NOW, suppressions=rules)

    assert ids(active) == ["systemd.unit_failed"]


# --- status ----------------------------------------------------------------------


def status_for(report=None, host=None, overrides=(), suppressions=()):
    host = host or fake_host()
    findings = run(report, host=host, overrides=overrides)
    active, _ = apply_suppressions(findings, host, NOW, suppressions=suppressions)
    return compute_status(active, host, NOW)


def test_status_waiting_without_a_report():
    assert status_for(host=fake_host(latest_report=None)) == "waiting"


def test_status_green():
    assert status_for() == "green"


def test_status_yellow():
    assert status_for(make_report(reboot_required=True)) == "yellow"


def test_status_red_beats_yellow():
    assert status_for(make_report(reboot_required=True, failed_units=["x.service"])) == "red"


def test_status_offline():
    host = fake_host(connected=False, last_seen_at=NOW - timedelta(minutes=10))

    assert status_for(make_report(reboot_required=True), host=host) == "offline"


def test_status_maintenance_beats_everything_but_waiting():
    host = fake_host(
        maintenance_until=NOW + timedelta(hours=1),
        connected=False,
        last_seen_at=NOW - timedelta(minutes=10),
    )

    assert status_for(make_report(failed_units=["x.service"]), host=host) == "maintenance"
    assert (
        status_for(host=fake_host(latest_report=None, maintenance_until=NOW + timedelta(hours=1)))
        == "waiting"
    )


def test_status_after_maintenance_ends():
    host = fake_host(maintenance_until=NOW - timedelta(seconds=1))

    assert status_for(make_report(failed_units=["x.service"]), host=host) == "red"


@pytest.mark.parametrize("severity", ["info", "ignore"])
def test_info_and_ignore_never_change_the_colour(severity):
    overrides = [override("systemd.unit_failed", severity=severity)]

    assert status_for(make_report(failed_units=["x.service"]), overrides=overrides) == "green"


def test_offline_with_severity_ignore_is_not_offline():
    host = fake_host(connected=False, last_seen_at=NOW - timedelta(minutes=10))
    overrides = [override("connectivity.offline", severity="ignore")]

    assert status_for(host=host, overrides=overrides) == "green"


def test_suppressed_finding_turns_status_green():
    report = make_report(failed_units=["x.service"])
    rules = [suppression(fingerprint("systemd.unit_failed", HOST_ID, "unit:x.service"))]

    assert status_for(report, suppressions=rules) == "green"


# --- effective_status: offline worked out at read time --------------------------

SILENT_AFTER = 90


def test_effective_status_silent_past_threshold_is_offline():
    host = fake_host(connected=False, last_seen_at=NOW - timedelta(hours=28))

    assert effective_status(host, NOW, SILENT_AFTER) == "offline"


def test_effective_status_connected_keeps_the_stored_status():
    host = fake_host(status="yellow", connected=True, last_seen_at=NOW - timedelta(hours=28))

    assert effective_status(host, NOW, SILENT_AFTER) == "yellow"


def test_effective_status_silent_under_threshold_keeps_the_stored_status():
    host = fake_host(status="red", connected=False, last_seen_at=NOW - timedelta(seconds=60))

    assert effective_status(host, NOW, SILENT_AFTER) == "red"


def test_effective_status_exactly_at_threshold_is_not_offline_yet():
    host = fake_host(connected=False, last_seen_at=NOW - timedelta(seconds=SILENT_AFTER))

    assert effective_status(host, NOW, SILENT_AFTER) == "green"


def test_effective_status_never_reported_is_waiting():
    host = fake_host(status="waiting", latest_report=None, connected=False, last_seen_at=None)

    assert effective_status(host, NOW, SILENT_AFTER) == "waiting"


def test_effective_status_waiting_wins_even_if_silent():
    # A host that connected once but never sent a report is still "waiting".
    host = fake_host(latest_report=None, connected=False, last_seen_at=NOW - timedelta(days=2))

    assert effective_status(host, NOW, SILENT_AFTER) == "waiting"


def test_effective_status_maintenance_wins_over_offline():
    host = fake_host(
        status="maintenance",
        maintenance_until=NOW + timedelta(hours=1),
        connected=False,
        last_seen_at=NOW - timedelta(hours=28),
    )

    assert effective_status(host, NOW, SILENT_AFTER) == "maintenance"


def test_effective_status_after_maintenance_ends_can_be_offline():
    host = fake_host(
        maintenance_until=NOW - timedelta(seconds=1),
        connected=False,
        last_seen_at=NOW - timedelta(hours=28),
    )

    assert effective_status(host, NOW, SILENT_AFTER) == "offline"


def test_effective_status_matches_compute_status_order():
    """Same precedence as compute_status: waiting -> maintenance -> offline."""
    silent = {"connected": False, "last_seen_at": NOW - timedelta(minutes=10)}
    for host in (
        fake_host(**silent),
        fake_host(latest_report=None, **silent),
        fake_host(maintenance_until=NOW + timedelta(hours=1), **silent),
    ):
        assert effective_status(host, NOW, SILENT_AFTER) == status_for(host=host)


@pytest.mark.django_db
def test_effective_status_loads_the_owners_threshold(alice, bob):
    from hosts.models import Host
    from prefs.models import Prefs

    Prefs.objects.create(owner=alice, silent_after_seconds=3600)
    Prefs.objects.create(owner=bob, silent_after_seconds=60)  # must not be used
    host = Host.objects.create(
        owner=alice,
        name="pi",
        status="green",
        latest_report=make_report(),
        connected=False,
        last_seen_at=NOW - timedelta(minutes=30),
    )

    assert effective_status(host, NOW) == "green"
    assert effective_status(host, NOW + timedelta(minutes=31)) == "offline"


@pytest.mark.django_db
def test_effective_status_uses_the_default_without_prefs(alice):
    from hosts.models import Host

    host = Host.objects.create(
        owner=alice,
        name="pi",
        status="green",
        latest_report=make_report(),
        connected=False,
        last_seen_at=NOW - timedelta(seconds=91),
    )

    assert effective_status(host, NOW) == "offline"  # default silent_after_seconds is 90


# --- top_issues and sort_hosts -------------------------------------------------


def test_top_issues_red_first_and_limited():
    report = make_report(
        reboot_required=True,
        updates={"total": 4, "security": 1},
        failed_units=["a.service", "b.service"],
    )

    top = top_issues(run(report))

    assert [f.severity for f in top] == ["red", "red", "red"]
    assert len(top_issues(run(report), n=10)) == 5


def test_top_issues_skips_ignored():
    overrides = [override("updates.reboot_required", severity="ignore")]

    assert top_issues(run(make_report(reboot_required=True), overrides=overrides)) == []


def test_sort_hosts():
    hosts = [
        SimpleNamespace(pk=i, name=name, status=status)
        for i, (name, status) in enumerate(
            [
                ("b-green", "green"),
                ("yellow", "yellow"),
                ("a-green", "green"),
                ("off", "offline"),
                ("red", "red"),
                ("new", "waiting"),
                ("mnt", "maintenance"),
            ]
        )
    ]

    assert [h.name for h in sort_hosts(hosts)] == [
        "red",
        "off",
        "yellow",
        "new",
        "mnt",
        "a-green",
        "b-green",
    ]


# --- with the database: overrides are loaded per owner ----------------------------


@pytest.mark.django_db
def test_database_overrides_are_scoped_to_the_owner(alice, bob, host):
    host.latest_report = make_report(reboot_required=True)
    RuleOverride.objects.create(owner=bob, rule_id="updates.reboot_required", enabled=False)

    findings = evaluate(host, host.latest_report, NOW)

    assert ids(findings) == ["updates.reboot_required"]

    RuleOverride.objects.create(owner=alice, rule_id="updates.reboot_required", enabled=False)
    assert evaluate(host, host.latest_report, NOW) == []
    assert resolve_override(alice, "updates.reboot_required", host).enabled is False
