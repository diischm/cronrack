"""The notification rule registry (notifications/rules.py): one RuleSpec per check.

The guard test below FAILS when a check exists without a RuleSpec, or a RuleSpec names a
check that doesn't exist. See docs/adding-a-check.md.
"""

import pytest

from notifications.rules import (
    ALERT,
    BALANCED_SHOW_ONLY,
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


def test_there_are_50_rules_today():
    # Not a rule for the future, just a check that nothing was lost or doubled.
    # 43 before host metrics, plus the 7 resource rules (rules/resources.py).
    assert len(SPECS) == 50


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
    """Upgrading must not change what alerts: red and yellow alerted, info never did.

    The only exceptions are new heads-up rules that are deliberately show-only on
    Balanced (BALANCED_SHOW_ONLY); they can't change an existing behaviour.
    """
    spec, rule = SPECS[rule_id], get_rule(rule_id)
    expected = SHOW if rule.default_severity == INFO or rule_id in BALANCED_SHOW_ONLY else ALERT
    assert spec.defaults["balanced"] == expected
    assert spec.counts_as == (NEEDS_YOU if rule.default_severity == RED else HEADS_UP)


def test_the_show_only_exceptions_are_exactly_the_two_new_noisy_rules():
    assert frozenset({"host.swap_high", "docker.container_cpu_high"}) == BALANCED_SHOW_ONLY
    for rule_id in BALANCED_SHOW_ONLY:
        assert get_rule(rule_id).default_severity == YELLOW
        assert SPECS[rule_id].defaults == {"quiet": OFF, "balanced": SHOW, "everything": ALERT}


# id -> (quiet, balanced, everything, counts_as, limit values, default limit)
RESOURCE_SPECS = {
    "host.cpu_high": (SHOW, ALERT, ALERT, HEADS_UP, [80, 90, 95], 90),
    "host.memory_high": (SHOW, ALERT, ALERT, HEADS_UP, [15, 10, 5], 10),
    "host.swap_high": (OFF, SHOW, ALERT, HEADS_UP, [50, 75, 90], 75),
    "host.cpu_temp_high": (SHOW, ALERT, ALERT, HEADS_UP, [70, 80, 85], 80),
    "disk.inodes_low": (SHOW, ALERT, ALERT, HEADS_UP, [85, 90, 95], 90),
    "docker.container_cpu_high": (OFF, SHOW, ALERT, HEADS_UP, [80, 90, 95], 90),
    "docker.container_memory_near_limit": (SHOW, ALERT, ALERT, HEADS_UP, [85, 90, 95], 90),
}


@pytest.mark.parametrize("rule_id", sorted(RESOURCE_SPECS))
def test_resource_rule_presets_and_limits(rule_id):
    quiet, balanced, everything, counts_as, values, default = RESOURCE_SPECS[rule_id]
    spec = SPECS[rule_id]
    assert spec.defaults == {"quiet": quiet, "balanced": balanced, "everything": everything}
    assert spec.counts_as == counts_as
    assert [c.value for c in spec.limit.choices] == values
    assert spec.limit.value(spec.limit.default) == default == get_rule(rule_id).default_threshold
    assert spec.applies_to == "hosts"
    assert spec.group == ("Services & containers" if rule_id.startswith("docker.") else "Hosts")


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
