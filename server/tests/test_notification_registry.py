"""The notification rule registry (notifications/rules.py): one RuleSpec per check.

The guard test below FAILS when a check exists without a RuleSpec, or a RuleSpec names a
check that doesn't exist. See docs/adding-a-check.md.
"""

import pytest

from notifications.rules import (
    ALERT,
    GROUPS,
    HEADS_UP,
    LEVELS,
    NEEDS_YOU,
    OFF,
    PRESETS,
    SHOW,
    SPECS,
    spec_for,
    specs_by_group,
)
from rules.base import INFO, RED, YELLOW
from rules.k8s import CLUSTER_RULES
from rules.network import NETWORK_RULES
from rules.registry import RULES, get_rule


def every_rule_id() -> set[str]:
    """Every rule the server can produce a finding for: host, cluster and network."""
    return set(RULES) | set(CLUSTER_RULES) | set(NETWORK_RULES)


def test_guard_every_check_has_a_rule_spec():
    missing = sorted(every_rule_id() - set(SPECS))
    assert not missing, (
        f"These checks have no RuleSpec in notifications/rules.py: {missing}. "
        "Add one (see docs/adding-a-check.md)."
    )


def test_guard_no_rule_spec_without_a_real_check():
    invented = sorted(set(SPECS) - every_rule_id())
    assert not invented, f"RuleSpecs without a check: {invented}"


def test_there_are_43_rules_today():
    # Not a rule for the future, just a check that nothing was lost or doubled.
    assert len(SPECS) == 43


@pytest.mark.parametrize("rule_id", sorted(SPECS))
def test_each_spec_is_complete(rule_id):
    spec = SPECS[rule_id]
    assert spec.id == rule_id
    assert spec.group in GROUPS
    assert spec.label and spec.description
    assert set(spec.defaults) == set(PRESETS)
    assert all(level in LEVELS for level in spec.defaults.values())
    assert spec.counts_as in (NEEDS_YOU, HEADS_UP)


@pytest.mark.parametrize("rule_id", sorted(SPECS))
def test_balanced_keeps_todays_behaviour(rule_id):
    """Upgrading must not change what alerts: red and yellow alerted, info never did."""
    spec, rule = SPECS[rule_id], get_rule(rule_id)
    expected = SHOW if rule.default_severity == INFO else ALERT
    assert spec.defaults["balanced"] == expected
    assert spec.counts_as == (NEEDS_YOU if rule.default_severity == RED else HEADS_UP)


@pytest.mark.parametrize("rule_id", sorted(SPECS))
def test_presets_are_ordered_quiet_to_everything(rule_id):
    rank = {OFF: 0, SHOW: 1, ALERT: 2}
    d = SPECS[rule_id].defaults
    assert rank[d["quiet"]] <= rank[d["balanced"]] <= rank[d["everything"]]
    assert d["everything"] == ALERT


def test_quiet_keeps_broken_things_and_drops_the_noise():
    assert SPECS["systemd.unit_failed"].defaults["quiet"] == ALERT
    assert SPECS["docker.container_exited_error"].defaults["quiet"] == ALERT
    assert SPECS["updates.pending"].defaults["quiet"] == OFF
    assert SPECS["updates.reboot_required"].defaults["quiet"] == OFF
    assert SPECS["updates.security_pending"].defaults["quiet"] == SHOW
    assert SPECS["network.internet_slow"].defaults["quiet"] == OFF
    assert SPECS["k8s.argo_out_of_sync"].defaults["quiet"] == OFF


@pytest.mark.parametrize("rule_id", sorted(r for r in SPECS if SPECS[r].limit))
def test_limits_are_fixed_choices_whose_default_is_todays_threshold(rule_id):
    spec, rule = SPECS[rule_id], get_rule(rule_id)
    keys = [c.key for c in spec.limit.choices]
    assert len(keys) == len(set(keys)) >= 2
    assert spec.limit.default in keys
    assert spec.limit.value(spec.limit.default) == rule.default_threshold
    assert spec.limit.value("nonsense") is None


def test_the_limits_from_the_mockup_exist_with_todays_defaults():
    disk = SPECS["disk.usage"].limit
    assert [c.value for c in disk.choices] == [80, 85, 90, 95]
    assert disk.value(disk.default) == 85
    cert = SPECS["k8s.api_cert_expiring"].limit
    assert cert.value(cert.default) == 30
    slow = SPECS["network.internet_slow"].limit
    assert slow.value(slow.default) == 150
    loss = SPECS["network.packet_loss"].limit
    assert loss.value(loss.default) == 5


def test_offline_and_confirm_reports_have_no_second_control_here():
    # Host offline is set on Account; "N reports in a row" on the Network page.
    assert SPECS["connectivity.offline"].limit is None
    assert SPECS["network.router_down"].limit is None


def test_security_updates_stay_red_and_disk_stays_yellow():
    assert get_rule("updates.security_pending").default_severity == RED
    assert SPECS["updates.security_pending"].counts_as == NEEDS_YOU
    assert get_rule("disk.usage").default_severity == YELLOW
    assert SPECS["disk.usage"].counts_as == HEADS_UP


def test_groups_hold_every_spec_in_order():
    grouped = specs_by_group()
    assert [g for g, _ in grouped] == list(GROUPS)
    assert sum(len(specs) for _, specs in grouped) == len(SPECS)
    kubernetes = dict(grouped)["Kubernetes"]
    assert len(kubernetes) == len(CLUSTER_RULES) + 1  # + the Helm command check on hosts
    assert {s.id for s in dict(grouped)["Network"]} == set(NETWORK_RULES)


def test_applies_to_is_hosts_or_clusters_never_the_network():
    assert spec_for("disk.usage").applies_to == "hosts"
    assert spec_for("k8s.helm_cli_outdated").applies_to == "hosts"
    assert spec_for("k8s.node_not_ready").applies_to == "clusters"
    assert spec_for("network.dns_failing").applies_to == ""


def test_unknown_rule_is_a_key_error():
    with pytest.raises(KeyError):
        spec_for("evil.rule")
