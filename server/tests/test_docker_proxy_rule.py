"""agent.docker_proxy_unavailable: the host has Docker access on, but the proxy can't be reached.

The agent sends {"check": "containers", "error": "DockerProxyUnavailable"} in the report's
errors list when [docker] enabled = true and nothing answers on /run/cronrack/docker.sock.
"""

from notifications.rules import BALANCED_SHOW_ONLY, SPECS
from rules.base import YELLOW
from rules.registry import RULES

from .sample import make_report
from .test_rules_engine import ids, run

RULE_ID = "agent.docker_proxy_unavailable"
PROXY_DOWN = {"check": "containers", "error": "DockerProxyUnavailable"}


def test_rule_is_registered_as_a_heads_up():
    rule = RULES[RULE_ID]
    assert rule.default_severity == YELLOW
    assert "proxy" in rule.why.lower()
    assert rule.fix_template == "sudo systemctl restart cronrack-docker-proxy.socket"


def test_fires_when_the_agent_reports_the_proxy_down():
    findings = run(make_report(containers=None, errors=[PROXY_DOWN]))
    assert ids(findings) == [RULE_ID]
    assert findings[0].subject == "proxy:docker"
    assert findings[0].severity == YELLOW


def test_fires_when_docker_behind_the_proxy_is_down():
    error = {"check": "containers", "error": "DockerEngineUnavailable"}
    findings = run(make_report(containers=None, errors=[error]))
    assert ids(findings) == [RULE_ID]
    assert findings[0].text == "Docker not answering behind the proxy"


def test_does_not_fire_on_other_errors_or_none():
    assert run(make_report(errors=[])) == []
    assert run(make_report(errors=[{"check": "containers", "error": "DockerError"}])) == []
    assert run(make_report(errors=[{"check": "updates", "error": "DockerProxyUnavailable"}])) == []
    assert run(make_report(containers=None)) == []


def test_fires_once_however_often_it_is_listed():
    findings = run(make_report(containers=None, errors=[PROXY_DOWN, PROXY_DOWN]))
    assert ids(findings) == [RULE_ID]


def test_has_a_notification_rule_like_every_other_check():
    spec = SPECS[RULE_ID]
    assert spec.group == "Services & containers"
    assert spec.applies_to == "hosts"
    assert spec.defaults["balanced"] == "alert"
    assert RULE_ID not in BALANCED_SHOW_ONLY
