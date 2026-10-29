"""Online update checks: three fixed URLs, no redirects, strict parsing, opt-in, never raises."""

import json
import urllib.error
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.utils import timezone

from clusters.models import UpdateCheck
from clusters.updates import (
    ARGOCD_URL,
    HELM_URL,
    K3S_URL,
    MAX_BYTES,
    RETRY_INTERVAL,
    SUCCESS_INTERVAL,
    UpdateCheckError,
    fetch_json,
    latest_versions,
    maybe_check_updates,
    parse_github_release,
    parse_k3s_channels,
)
from cronrack.outbound import opener
from hosts.models import Host
from prefs.models import Prefs

from .sample import make_kube_report

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).parent / "fixtures" / "updates"
NOW = timezone.now()
ALLOWED = {K3S_URL, ARGOCD_URL, HELM_URL}


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def raw(name):
    return (FIXTURES / f"{name}.json").read_bytes()


BODIES = {
    K3S_URL: raw("k3s_channels"),
    ARGOCD_URL: raw("github_argocd_latest"),
    HELM_URL: raw("github_helm_latest"),
}


def response(body: bytes, status=200):
    result = mock.MagicMock()
    result.status = status
    result.read.side_effect = lambda n=-1: body if n < 0 else body[:n]
    result.__enter__.return_value = result
    return result


class FakeInternet:
    """Stands in for the shared opener: records every request, answers from BODIES."""

    def __init__(self, bodies=None, error=None):
        self.bodies = dict(BODIES if bodies is None else bodies)
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(
            (request.full_url, request.get_method(), dict(request.header_items()), timeout)
        )
        if self.error is not None:
            raise self.error
        return response(self.bodies[request.full_url])

    @property
    def urls(self):
        return [r[0] for r in self.requests]


@pytest.fixture
def internet():
    fake = FakeInternet()
    with mock.patch("clusters.updates._opener", fake):
        yield fake


@pytest.fixture
def opted_in(alice, host):
    Prefs.objects.create(owner=alice, check_updates_online=True)
    return alice


@pytest.fixture
def k8s_host(host):
    Host.objects.filter(pk=host.pk).update(latest_report=make_kube_report(cluster=None))
    return host


# --- the parsers --------------------------------------------------------------------------------


def test_k3s_channels_keep_only_known_channel_ids_with_a_valid_version():
    channels = parse_k3s_channels(load("k3s_channels"))

    assert channels["stable"] == "v1.36.5+k3s1"
    assert channels["latest"] == "v1.37.1+k3s1"
    assert channels["testing"] == "v1.18.2-rc3+k3s1"
    assert channels["v1.30"] == "v1.30.14+k3s2"
    assert channels["v1.31"] == "v1.31.14+k3s1"
    assert "v1.16-testing" not in channels  # no "latest" value, and not a channel we use
    assert "v1.99" not in channels  # garbage version
    assert "../etc/passwd" not in channels  # odd channel id


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "x",
        {},
        {"data": "x"},
        {"data": []},
        {"data": [None, 5]},
        {"data": [{"id": "v1.31", "latest": "v1.31.1+k3s1"}]},  # no stable channel
        {"data": [{"id": "stable", "latest": "garbage"}]},
    ],
)
def test_k3s_channels_garbage(data):
    with pytest.raises(UpdateCheckError):
        parse_k3s_channels(data)


def test_github_release_keeps_only_the_tag():
    assert parse_github_release(load("github_argocd_latest")) == {"latest": "v3.5.6"}
    assert parse_github_release(load("github_helm_latest")) == {"latest": "v4.3.1"}


@pytest.mark.parametrize(
    "change",
    [
        {"draft": True},
        {"prerelease": True},
        {"tag_name": "latest"},
        {"tag_name": None},
        {"tag_name": "v1.0.0; rm -rf /"},
        {"draft": "false"},
        {"tag_name": "v" + "1." * 40 + "1"},
    ],
)
def test_github_release_refuses_drafts_prereleases_and_garbage(change):
    with pytest.raises(UpdateCheckError):
        parse_github_release({**load("github_argocd_latest"), **change})


@pytest.mark.parametrize("data", [None, [], "x", 5, {}])
def test_github_release_garbage(data):
    with pytest.raises(UpdateCheckError):
        parse_github_release(data)


# --- fetch_json ---------------------------------------------------------------------------------------


def test_fetch_requests_the_url_with_a_timeout_and_no_data():
    fake = FakeInternet()
    with mock.patch("clusters.updates._opener", fake):
        assert fetch_json(HELM_URL)["tag_name"] == "v4.3.1"

    [(url, method, headers, timeout)] = fake.requests
    assert (url, method, timeout) == (HELM_URL, "GET", 5)
    assert headers["User-agent"] == "CronRack"
    assert headers["Accept"] == "application/vnd.github+json"
    assert set(headers) <= {"User-agent", "Accept"}  # nothing about the user's setup


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://update.k3s.io/v1-release/channels",
        K3S_URL + "?x=1",
        "file:///etc/passwd",
        "https://api.github.com/repos/helm/helm/releases",
        "",
        None,
    ],
)
def test_other_urls_are_never_requested(url):
    fake = FakeInternet()
    with mock.patch("clusters.updates._opener", fake), pytest.raises(UpdateCheckError):
        fetch_json(url)
    assert fake.requests == []


def test_the_shared_opener_refuses_redirects():
    from cronrack.outbound import NoRedirect

    assert any(isinstance(h, NoRedirect) for h in opener.handlers)
    assert (
        NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None
    )


def test_the_alert_module_still_uses_the_shared_opener():
    from alerts import discord

    assert discord._opener is opener


def test_a_redirect_is_an_error_not_followed():
    error = urllib.error.HTTPError(K3S_URL, 302, "Found", {}, None)
    with (
        mock.patch("clusters.updates._opener", FakeInternet(error=error)),
        pytest.raises(UpdateCheckError, match="http_302"),
    ):
        fetch_json(K3S_URL)


@pytest.mark.parametrize(
    "error", [TimeoutError(), urllib.error.URLError("dns"), ConnectionResetError(), OSError("tls")]
)
def test_network_trouble_is_an_error_with_a_short_name(error):
    with (
        mock.patch("clusters.updates._opener", FakeInternet(error=error)),
        pytest.raises(UpdateCheckError) as caught,
    ):
        fetch_json(K3S_URL)
    assert len(str(caught.value)) < 40


def test_a_non_2xx_answer_is_an_error():
    fake = mock.Mock()
    fake.open.return_value = response(b"{}", status=500)
    with (
        mock.patch("clusters.updates._opener", fake),
        pytest.raises(UpdateCheckError, match="http_500"),
    ):
        fetch_json(K3S_URL)


def test_an_oversized_body_is_refused_after_reading_only_a_bit_more_than_the_cap():
    fake = mock.Mock()
    reply = response(b" " * (MAX_BYTES + 10))
    fake.open.return_value = reply
    with (
        mock.patch("clusters.updates._opener", fake),
        pytest.raises(UpdateCheckError, match="too_large"),
    ):
        fetch_json(K3S_URL)
    reply.read.assert_called_once_with(MAX_BYTES + 1)


@pytest.mark.parametrize("body", [b"", b"not json", b"{", b"\xff\xfe", b"[" * 100_000])
def test_bad_json_is_an_error(body):
    fake = mock.Mock()
    fake.open.return_value = response(body)
    with (
        mock.patch("clusters.updates._opener", fake),
        pytest.raises(UpdateCheckError, match="bad_json"),
    ):
        fetch_json(K3S_URL)


# --- when it runs ---------------------------------------------------------------------------------------


def test_toggle_off_means_zero_requests(alice, k8s_host, internet):
    Prefs.objects.create(owner=alice, check_updates_online=False)
    assert maybe_check_updates(NOW) == []
    assert internet.requests == []
    assert UpdateCheck.objects.count() == 0


def test_no_prefs_row_means_zero_requests(k8s_host, internet):
    assert maybe_check_updates(NOW) == []
    assert internet.requests == []


def test_nothing_to_compare_means_zero_requests(opted_in, internet):
    assert maybe_check_updates(NOW) == []  # host has no Kubernetes data, and there is no cluster
    assert internet.requests == []


def test_a_cluster_is_enough_reason(opted_in, cluster, internet):
    assert sorted(maybe_check_updates(NOW)) == ["argocd", "helm", "k3s"]


def test_a_kubernetes_node_is_enough_reason(opted_in, k8s_host, internet):
    assert sorted(maybe_check_updates(NOW)) == ["argocd", "helm", "k3s"]


def test_only_the_three_urls_are_ever_requested(opted_in, cluster, internet):
    maybe_check_updates(NOW)
    assert sorted(internet.urls) == sorted(ALLOWED)
    assert set(internet.urls) == ALLOWED


def test_results_are_stored_as_parsed_versions_only(opted_in, cluster, internet):
    maybe_check_updates(NOW)

    k3s = UpdateCheck.objects.get(source="k3s")
    assert k3s.data["stable"] == "v1.36.5+k3s1"
    assert k3s.checked_at == k3s.attempted_at == NOW
    assert k3s.last_error == ""
    assert UpdateCheck.objects.get(source="argocd").data == {"latest": "v3.5.6"}
    assert UpdateCheck.objects.get(source="helm").data == {"latest": "v4.3.1"}
    assert "distractor" not in json.dumps([u.data for u in UpdateCheck.objects.all()])


def test_once_a_day_after_a_success(opted_in, cluster, internet):
    maybe_check_updates(NOW)
    internet.requests.clear()

    assert maybe_check_updates(NOW + SUCCESS_INTERVAL - timedelta(seconds=1)) == []
    assert internet.requests == []
    assert len(maybe_check_updates(NOW + SUCCESS_INTERVAL)) == 3
    assert len(internet.requests) == 3


def test_a_failure_is_retried_after_an_hour_not_before(opted_in, cluster):
    broken = FakeInternet(error=TimeoutError())
    with mock.patch("clusters.updates._opener", broken):
        assert maybe_check_updates(NOW) == []
        row = UpdateCheck.objects.get(source="k3s")
        assert row.last_error == "TimeoutError"
        assert row.checked_at is None
        count = len(broken.requests)
        assert maybe_check_updates(NOW + RETRY_INTERVAL - timedelta(seconds=1)) == []
        assert len(broken.requests) == count
        maybe_check_updates(NOW + RETRY_INTERVAL)
        assert len(broken.requests) == 2 * count


def test_a_failure_keeps_the_old_data_and_a_later_success_clears_the_error(
    opted_in, cluster, internet
):
    maybe_check_updates(NOW)
    later = NOW + SUCCESS_INTERVAL
    with mock.patch("clusters.updates._opener", FakeInternet(error=TimeoutError())):
        maybe_check_updates(later)
    row = UpdateCheck.objects.get(source="helm")
    assert row.data == {"latest": "v4.3.1"}
    assert row.last_error == "TimeoutError"
    assert row.checked_at == NOW

    with mock.patch("clusters.updates._opener", FakeInternet()):
        maybe_check_updates(later + RETRY_INTERVAL)
    row.refresh_from_db()
    assert row.last_error == ""
    assert row.checked_at == later + RETRY_INTERVAL


def test_bad_data_from_one_source_does_not_stop_the_others(opted_in, cluster):
    fake = FakeInternet({**BODIES, K3S_URL: b'{"data": "nonsense"}'})
    with mock.patch("clusters.updates._opener", fake):
        assert sorted(maybe_check_updates(NOW)) == ["argocd", "helm"]
    assert UpdateCheck.objects.get(source="k3s").last_error == "bad_shape"


def test_it_never_raises_even_when_the_database_part_crashes(opted_in, cluster, caplog):
    with mock.patch("clusters.updates.wanted", side_effect=RuntimeError("boom")):
        assert maybe_check_updates(NOW) == []
    with mock.patch("clusters.updates._check_one", side_effect=RuntimeError("boom")):
        assert maybe_check_updates(NOW) == []
    assert "boom" not in caplog.text  # only class names are logged


def test_errors_are_logged_by_class_name_only(opted_in, cluster, caplog):
    with mock.patch("clusters.updates._opener", FakeInternet(error=OSError("secret detail"))):
        maybe_check_updates(NOW)
    assert "secret detail" not in caplog.text
    assert "OSError" in caplog.text


# --- who gets the data -----------------------------------------------------------------------------------------


def test_latest_versions_only_for_owners_who_opted_in(alice, bob, cluster, internet):
    Prefs.objects.create(owner=alice, check_updates_online=True)
    Prefs.objects.create(owner=bob, check_updates_online=False)
    maybe_check_updates(NOW)

    assert latest_versions(alice.pk)["argocd"] == {"latest": "v3.5.6"}
    assert latest_versions(bob.pk) == {}
    assert latest_versions(999_999) == {}


def test_latest_versions_ignores_empty_rows(alice):
    Prefs.objects.create(owner=alice, check_updates_online=True)
    UpdateCheck.objects.create(source="k3s", data={})
    assert latest_versions(alice.pk) == {}


def test_a_cluster_rule_uses_the_data_only_when_opted_in(
    alice, host, django_capture_on_commit_callbacks
):
    """End to end: an old k3s is flagged only for an owner who turned update checks on."""
    from agents.consumers import ingest_report

    from .sample import make_cluster

    UpdateCheck.objects.create(source="k3s", data=parse_k3s_channels(load("k3s_channels")))
    old = make_cluster(
        version="v1.30.1+k3s1",
        nodes=[{**make_cluster()["nodes"][0], "kubelet": "v1.30.1+k3s1"}],
    )

    ingest_report.func(host.pk, make_kube_report(cluster=old), "c")
    from clusters.models import Cluster

    assert Cluster.objects.get().status == "green"  # toggle off: quiet

    Prefs.objects.create(owner=alice, check_updates_online=True)
    ingest_report.func(host.pk, make_kube_report(cluster=old), "c")
    assert Cluster.objects.get().status == "yellow"  # patch release available in v1.30
