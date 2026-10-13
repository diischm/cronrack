from datetime import timedelta

import pytest
from django.utils import timezone

from activity.events import record_status_change, summarize
from activity.models import StatusEvent
from hosts.models import Host
from hosts.services import save_report
from rules.models import Suppression

from .sample import make_report

pytestmark = pytest.mark.django_db


def test_first_report_stores_report_and_status(host):
    report = make_report(updates={"total": 3, "security": 2})

    result = save_report(host, report)

    host.refresh_from_db()
    assert host.latest_report == report
    assert host.report_received_at is not None
    assert host.last_seen_at == host.report_received_at
    assert host.status == "red"
    assert (result.old_status, result.new_status) == ("waiting", "red")
    assert [f.rule_id for f in result.new_findings] == [
        "updates.security_pending",
        "updates.pending",
    ]


def test_only_new_findings_are_returned(host):
    save_report(host, make_report(reboot_required=True))

    same = save_report(host, make_report(reboot_required=True))
    worse = save_report(host, make_report(reboot_required=True, failed_units=["x.service"]))

    assert (same.old_status, same.new_status, same.new_findings) == ("yellow", "yellow", [])
    assert [f.rule_id for f in worse.new_findings] == ["systemd.unit_failed"]
    assert [f.rule_id for f in worse.findings] == [
        "updates.reboot_required",
        "systemd.unit_failed",
    ]
    assert worse.new_status == "red"


def test_problem_fixed_turns_green(host):
    save_report(host, make_report(failed_units=["x.service"]))

    result = save_report(host, make_report())

    assert (result.old_status, result.new_status) == ("red", "green")
    assert result.new_findings == []


def test_expected_suppression_from_the_database(alice, host):
    Suppression.objects.create(
        owner=alice,
        host=host,
        rule_id="systemd.unit_failed",
        subject="unit:x.service",
        kind="expected",
    )

    result = save_report(host, make_report(failed_units=["x.service"]))

    assert result.new_status == "green"
    assert result.findings == []


def test_maintenance_status(host):
    Host.objects.filter(pk=host.pk).update(maintenance_until=timezone.now() + timedelta(hours=1))

    result = save_report(host, make_report(failed_units=["x.service"]))

    # Findings are still there, only the status (and later the alerts) is muted.
    assert result.new_status == "maintenance"
    assert [f.rule_id for f in result.findings] == ["systemd.unit_failed"]


def test_record_status_change(host):
    result = save_report(host, make_report(reboot_required=True, failed_units=["x.service"]))

    event = record_status_change(host, "waiting", "red", result.findings)

    assert StatusEvent.objects.get() == event
    assert (event.old_status, event.new_status) == ("waiting", "red")
    assert event.summary == "Reboot required, x.service failed"


def test_summary_is_cut_to_fit(host):
    result = save_report(
        host, make_report(failed_units=[f"unit-{i:03}-{'x' * 40}.service" for i in range(10)])
    )

    summary = summarize(result.findings)

    assert len(summary) == 200
    assert summary.endswith("…")
