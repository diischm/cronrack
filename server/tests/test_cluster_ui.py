"""Part 3: the Kubernetes UI. Overview role chips and cluster cards, the host page's
Kubernetes panel, the cluster page, the dev preview and the JS rules."""

import re
from pathlib import Path

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from clusters.models import Cluster, UpdateCheck
from cronrack.dev_views import XSS_IMG, XSS_SCRIPT
from prefs.models import Prefs

from .sample import make_cluster, make_kube_report, make_report
from .test_cluster_views import live, pod  # noqa: F401 - live is a fixture
from .test_preview import scan

pytestmark = pytest.mark.django_db

STATIC_JS = Path(__file__).resolve().parent.parent / "static" / "cronrack" / "js"
STATIC_CSS = STATIC_JS.parent / "css"


def page(client: Client, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200, url
    return response.content.decode()


def section(html: str, start: str, end: str) -> str:
    """The part of html from `start` up to the next `end`."""
    begin = html.index(start)
    return html[begin : html.index(end, begin + len(start))]


def set_snapshot(cluster: Cluster, snapshot: dict, status: str = "red") -> None:
    now = timezone.now()
    Cluster.objects.filter(pk=cluster.pk).update(
        status=status, latest_snapshot=snapshot, snapshot_received_at=now, last_seen_at=now
    )


def k8s_panel(client: Client, host) -> str:
    return section(
        page(client, f"/hosts/{host.pk}/"), 'aria-labelledby="panel-kubernetes"', "</section>"
    )


# --- overview: role chips ---------------------------------------------------------------------


@pytest.mark.parametrize(("role", "word"), [("server", "k8s server"), ("agent", "k8s worker")])
def test_host_card_shows_the_k8s_role_as_icon_and_word(alice_client: Client, host, role, word):
    host.latest_report = make_kube_report(cluster=None, role=role)
    host.status = "green"
    host.save()

    unit = section(page(alice_client, "/"), '<a class="rack-unit', "</a>")

    label = section(unit, '<span class="role-label rack-unit__role">', "</span></span>")
    assert word in label
    assert '<svg class="role-label__icon"' in label and 'aria-hidden="true"' in label
    # Read as part of the unit's link: "nas, All good, k8s server, …"
    assert (
        '<span class="visually-hidden">, </span><span class="role-label rack-unit__role">' in unit
    )


def test_host_card_without_kubernetes_has_no_role_label(alice_client: Client, host):
    host.latest_report = make_report()
    host.status = "green"
    host.save()

    rack = section(page(alice_client, "/"), '<ul class="rack well" id="rack"', "</ul>")
    assert "rack-unit__role" not in rack
    assert "k8s " not in rack


# --- overview: cluster cards ------------------------------------------------------------------


def test_overview_shows_a_cluster_card_in_its_own_rack(alice_client: Client, live):  # noqa: F811
    html = page(alice_client, "/")

    assert '<h2 class="section-title" id="clusters-title">Clusters</h2>' in html
    rack = section(html, '<ul class="rack well" id="cluster-rack"', "</ul>")
    assert f'href="/clusters/{live.pk}/"' in rack
    assert '<span class="rack-unit__name">nas-cluster</span>' in rack
    assert 'class="lamp lamp--red"' in rack
    assert "status-icon--bad" in rack and "Needs you" in rack  # lamp + words, never colour only
    # A line in the card body, so phones (no front panel there) see it too.
    assert (
        '<span class="rack-unit__issue-text cluster-unit__facts">Nodes 2/2 ready · 1 problem pod</span>'
        in rack
    )
    assert "keeps crashing" in rack


def test_overview_without_clusters_has_no_cluster_rack(alice_client: Client, host):
    html = page(alice_client, "/")
    assert 'id="cluster-rack"' not in html
    assert 'id="cluster-unit-template"' not in html


def test_offline_cluster_card_has_an_unlit_lamp_and_the_offline_line(
    alice_client: Client,
    live,  # noqa: F811
):
    Cluster.objects.filter(pk=live.pk).update(status="offline")

    rack = section(page(alice_client, "/"), 'id="cluster-rack"', "</ul>")
    assert "lamp--off" in rack
    assert "status-icon" not in rack
    assert "<strong>Offline</strong> · last data" in rack


def test_cluster_unit_template_holds_no_cluster_data(alice_client: Client, live):  # noqa: F811
    blank = section(page(alice_client, "/"), '<template id="cluster-unit-template">', "</template>")
    assert "nas-cluster" not in blank
    assert "href=" not in blank
    assert "data-" not in blank


def test_overview_json_has_clusters_and_roles(alice_client: Client, live, host):  # noqa: F811
    data = alice_client.get("/api/v1/overview.json").json()
    [card] = data["clusters"]
    assert card["url"] == f"/clusters/{live.pk}/"
    assert card["nodes"] == {"ready": 2, "total": 2}
    assert data["hosts"][0]["k8s_role"] == "server"


# --- host page: the Kubernetes panel ------------------------------------------------------------


def test_host_page_has_the_kubernetes_panel(alice_client: Client, live, host):  # noqa: F811
    panel = k8s_panel(alice_client, host)

    assert '<h2 class="app-panel__title" id="panel-kubernetes">Kubernetes</h2>' in panel
    assert "k8s server" in panel
    assert "v1.31.2+k3s1" in panel
    assert "Ready" in panel
    assert "web-5d9f7c8b6-x2k4q" in panel  # this node's problem pod
    assert "CrashLoopBackOff" in panel
    assert f'href="/clusters/{live.pk}/"' in panel and "Open cluster" in panel
    assert "Turn on update checks in Settings" in panel


def test_host_panel_shows_an_update_badge_with_update_checks(alice, alice_client, live, host):  # noqa: F811
    Prefs.objects.create(owner=alice, check_updates_online=True)
    UpdateCheck.objects.create(
        source="k3s", data={"stable": "v1.33.1+k3s1", "v1.31": "v1.31.9+k3s1"}
    )

    panel = k8s_panel(alice_client, host)
    assert "Update available" in panel
    assert "v1.31.9+k3s1" in panel
    assert "status-icon--warn" in panel
    assert "Turn on update checks" not in panel


def test_host_panel_says_up_to_date(alice, alice_client, live, host):  # noqa: F811
    Prefs.objects.create(owner=alice, check_updates_online=True)
    UpdateCheck.objects.create(
        source="k3s", data={"stable": "v1.31.2+k3s1", "v1.31": "v1.31.2+k3s1"}
    )
    panel = k8s_panel(alice_client, host)
    assert "Up to date" in panel
    assert "status-icon--good" in panel


def test_host_without_kubernetes_has_no_kubernetes_panel(alice_client: Client, host):
    host.latest_report = make_report()
    host.status = "green"
    host.save()
    assert "panel-kubernetes" not in page(alice_client, f"/hosts/{host.pk}/")


def test_host_panel_lists_this_nodes_healthy_pods_after_the_problems(
    alice_client: Client,
    live,  # noqa: F811
    host,
):
    snapshot = make_cluster(
        problem_pods=[pod()],
        ok_pods={"apps": [["db-0", "nas", 0, 60], ["elsewhere-1", "worker-1", 0, 60]]},
    )
    set_snapshot(live, snapshot)
    panel = k8s_panel(alice_client, host)
    assert panel.index("web-5d9f7c8b6-x2k4q") < panel.index("db-0")
    assert "elsewhere-1" not in panel  # runs on another node


# --- the cluster page ---------------------------------------------------------------------------


def test_cluster_page_header_is_a_mini_rack_with_lamp_and_words(alice_client, live):  # noqa: F811
    header = section(
        page(alice_client, f"/clusters/{live.pk}/"),
        '<header class="host-rack rack-chassis surface">',
        "</header>",
    )
    assert '<h1 class="page-title host-unit__name">nas-cluster</h1>' in header
    assert "lamp--red" in header
    assert "status-icon--bad" in header and "Needs you" in header
    assert 'href="/clusters/"' in header  # back
    assert f'href="/clusters/{live.pk}/settings/"' in header  # rename lives there


def test_cluster_page_puts_problems_first_then_every_section(alice_client, live):  # noqa: F811
    html = page(alice_client, f"/clusters/{live.pk}/")
    order = [
        'id="cluster-tiles"',
        ">Needs your attention<",
        ">Nodes<",
        ">Workloads<",
        ">Pods<",
        ">Helm releases<",
        ">Argo CD apps<",
        ">Versions &amp; updates<",
        ">Recent warnings<",
        ">Volume claims (PVCs)<",
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_summary_tiles(alice_client: Client, live):  # noqa: F811
    tiles = section(page(alice_client, f"/clusters/{live.pk}/"), 'id="cluster-tiles"', "</dl>")
    for label in ("Nodes ready", "All pods", "Workloads healthy", "Argo CD synced", "Updates"):
        assert label in tiles
    assert 'id="tile-nodes">2/2<' in tiles
    assert 'id="tile-pods">2 running · 1 problem<' in tiles
    assert 'id="tile-updates">Off<' in tiles  # update checks are off by default


def test_findings_have_why_and_a_copy_only_fix(alice_client: Client, live):  # noqa: F811
    problems = section(page(alice_client, f"/clusters/{live.pk}/"), 'id="cluster-issues"', "</ul>")

    assert "keeps crashing" in problems
    assert "Kubernetes keeps restarting it" in problems  # the why
    ids = re.findall(r'<pre class="code well" id="(fix-[0-9a-f]{12})">', problems)
    assert ids
    for fix_id in ids:
        assert f'class="btn btn--sm js-copy" aria-controls="{fix_id}"' in problems
    assert "kubectl -n apps logs web-5d9f7c8b6-x2k4q --previous" in problems


def test_red_findings_come_before_yellow_ones(alice_client: Client, live):  # noqa: F811
    problems = section(page(alice_client, f"/clusters/{live.pk}/"), 'id="cluster-issues"', "</ul>")
    severities = re.findall(r"finding finding--(red|yellow|info)", problems)
    rank = {"red": 0, "yellow": 1, "info": 2}
    assert severities
    assert severities == sorted(severities, key=rank.get)


def test_tables_carry_labels_for_the_phone_cards(alice_client: Client, live):  # noqa: F811
    nodes = section(page(alice_client, f"/clusters/{live.pk}/"), ">Nodes<", "</table>")
    assert '<div class="stack-table table-scroll well">' in nodes
    assert '<td data-label="Ready">' in nodes
    assert '<td data-label="Version">' in nodes


def test_problem_pods_first_then_healthy_ones_by_namespace(alice_client, live):  # noqa: F811
    snapshot = make_cluster(
        problem_pods=[pod()],
        ok_pods={"apps": [["web-ok-1", "nas", 0, 60]], "kube-system": [["coredns-1", "nas", 0, 9]]},
    )
    set_snapshot(live, snapshot)
    pods = section(page(alice_client, f"/clusters/{live.pk}/"), ">Pods<", "</section>")

    assert pods.index("web-5d9f7c8b6-x2k4q") < pods.index("web-ok-1")
    assert pods.index(">apps<") < pods.index(">kube-system<")
    # Each namespace is collapsible (disclosure.js); ids never come from agent data.
    keys = re.findall(
        r'class="btn btn--sm show-more js-disclosure" aria-controls="(pods-ns-\d+)"', pods
    )
    assert keys == ["pods-ns-1", "pods-ns-2"]
    for key in keys:
        assert f'id="{key}"' in pods


def test_argo_status_is_icon_and_word(alice_client: Client, live):  # noqa: F811
    argo = {
        "name": "web",
        "ns": "argocd",
        "project": "default",
        "sync": "OutOfSync",
        "health": "Degraded",
        "rev": "a1b2c3d",
        "op": "Failed",
        "synced_at": "2026-10-28T10:00:00Z",
    }
    set_snapshot(live, make_cluster(argo=[argo]))
    table = section(page(alice_client, f"/clusters/{live.pk}/"), ">Argo CD apps<", "</table>")
    assert "OutOfSync" in table and "status-icon--warn" in table
    assert "Degraded" in table and "status-icon--bad" in table
    assert "a1b2c3d" in table


def test_versions_section_says_how_to_compare(alice_client: Client, live):  # noqa: F811
    versions = section(
        page(alice_client, f"/clusters/{live.pk}/"), ">Versions &amp; updates<", "</section>"
    )
    assert "v1.31.2+k3s1" in versions
    assert "Turn on update checks in Settings to compare with the latest releases" in versions
    assert 'href="/settings/"' in versions


def test_versions_section_compares_when_update_checks_are_on(alice, alice_client, live):  # noqa: F811
    Prefs.objects.create(owner=alice, check_updates_online=True)
    UpdateCheck.objects.create(
        source="k3s", data={"stable": "v1.33.1+k3s1", "v1.31": "v1.31.9+k3s1"}
    )
    html = page(alice_client, f"/clusters/{live.pk}/")
    versions = section(html, ">Versions &amp; updates<", "</section>")
    assert "v1.31.9+k3s1" in versions and "Update available" in versions
    assert "v1.33.1+k3s1" in versions and "Newer minor version" in versions
    assert 'id="tile-updates">1<' in html


def test_truncated_and_incomplete_notices(alice_client: Client, live):  # noqa: F811
    set_snapshot(
        live, make_cluster(truncated=True, cut=["ok_pods"], incomplete=["pods"]), status="yellow"
    )
    notices = section(page(alice_client, f"/clusters/{live.pk}/"), 'id="cluster-notices"', "</ul>")
    assert "Not everything is listed" in notices and "ok_pods" in notices
    assert "Not read in full" in notices and "pods" in notices


def test_no_notices_when_everything_was_read(alice_client: Client, live):  # noqa: F811
    assert 'id="cluster-notices"' not in page(alice_client, f"/clusters/{live.pk}/")


def test_waiting_cluster_page(alice_client: Client, cluster):
    html = page(alice_client, f"/clusters/{cluster.pk}/")
    assert "Waiting for the first report from this cluster" in html
    assert "status-icon--waiting" in html
    assert 'id="cluster-tiles"' not in html


def test_cluster_page_loads_cluster_js_and_names_its_json(alice_client, live):  # noqa: F811
    html = page(alice_client, f"/clusters/{live.pk}/")
    assert '<script src="/static/cronrack/js/cluster.js" defer></script>' in html
    assert '<script src="/static/cronrack/js/copy.js" defer></script>' in html
    assert '<script src="/static/cronrack/js/disclosure.js" defer></script>' in html
    assert f'data-json-url="/api/v1/clusters/{live.pk}/detail.json"' in html
    assert 'id="cluster-new" hidden' in html  # "New data · Show latest", shown by cluster.js
    assert f'href="/clusters/{live.pk}/"' in section(html, 'id="cluster-new"', "</p>")
    assert '<template id="finding-template">' in html


def test_finding_template_holds_no_data(alice_client: Client, live):  # noqa: F811
    blank = section(
        page(alice_client, f"/clusters/{live.pk}/"),
        '<template id="finding-template">',
        "</template>",
    )
    assert "keeps crashing" not in blank
    assert "kubectl" not in blank
    assert "href=" not in blank


def test_cluster_page_is_csp_clean(alice_client: Client, live):  # noqa: F811
    scanned = scan(page(alice_client, f"/clusters/{live.pk}/"))
    assert scanned.inline_script_text == ""
    assert scanned.inline_styles == []
    assert scanned.event_handlers == []


def test_hostile_data_renders_as_text_on_every_new_part(alice_client, host, live):  # noqa: F811
    hostile = make_cluster(
        problem_pods=[pod(name=XSS_SCRIPT, ns=XSS_IMG, node="nas", reason="CrashLoopBackOff")],
        ok_pods={XSS_IMG: [[XSS_SCRIPT, "nas", 0, 5]]},
        helm=[
            {
                "name": XSS_SCRIPT,
                "ns": XSS_IMG,
                "chart": XSS_SCRIPT,
                "app_version": XSS_IMG,
                "source": "labels",
            }
        ],
        argo=[
            {
                "name": XSS_IMG,
                "ns": XSS_SCRIPT,
                "project": XSS_IMG,
                "sync": "Synced",
                "health": "Healthy",
                "rev": None,
                "op": None,
                "synced_at": None,
            }
        ],
    )
    set_snapshot(live, hostile)
    Cluster.objects.filter(pk=live.pk).update(name=XSS_SCRIPT)
    for url in ("/", f"/clusters/{live.pk}/", f"/hosts/{host.pk}/", "/clusters/"):
        html = page(alice_client, url)
        assert XSS_SCRIPT not in html, url
        assert "<img src=x" not in html, url
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html, url


# --- dev preview --------------------------------------------------------------------------------

STATES = ["healthy", "broken", "xss", "truncated", "offline", "waiting"]


@override_settings(DEBUG=False)
def test_cluster_preview_is_404_without_debug(alice_client: Client):
    assert alice_client.get("/dev/preview/cluster/").status_code == 404
    assert alice_client.get("/dev/preview/cluster/?state=broken").status_code == 404


@override_settings(DEBUG=True)
@pytest.mark.parametrize("state", STATES)
def test_cluster_preview_states_render_csp_clean(alice_client: Client, state):
    html = page(alice_client, f"/dev/preview/cluster/?state={state}")
    scanned = scan(html)
    assert scanned.inline_script_text == ""
    assert scanned.inline_styles == []
    assert scanned.event_handlers == []
    assert "<script>alert(1)</script>" not in html


@override_settings(DEBUG=True)
def test_cluster_preview_healthy_and_broken(alice_client: Client):
    healthy = page(alice_client, "/dev/preview/cluster/")
    assert "Nothing needs you" in healthy
    assert "status-icon--good" in healthy

    broken = page(alice_client, "/dev/preview/cluster/?state=broken")
    problems = section(broken, 'id="cluster-issues"', "</ul>").lower()
    for text in ("keeps crashing", "too much memory", "pull its image", "not ready"):
        assert text in problems, text
    assert "status-icon--bad" in broken


@override_settings(DEBUG=True)
def test_cluster_preview_xss_state_is_escaped(alice_client: Client):
    html = page(alice_client, "/dev/preview/cluster/?state=xss")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in html


def test_preview_samples_have_the_same_keys_as_the_real_context(live):  # noqa: F811
    from clusters.presenters import cluster_context
    from clusters.samples import preview_context

    real = cluster_context(live, timezone.now())
    for state in STATES:
        sample = preview_context(state)
        assert set(sample) == set(real), state
        assert set(sample["cluster"]) == set(real["cluster"]), state
        assert set(sample["tiles"]) == set(real["tiles"]), state
        assert set(sample["versions"]) == set(real["versions"]), state


@override_settings(DEBUG=True)
def test_overview_preview_shows_a_cluster_card_and_role_labels(alice_client: Client):
    html = page(alice_client, "/dev/preview/")
    assert 'id="cluster-rack"' in html
    assert "k8s server" in html and "k8s worker" in html


# --- JS and CSS rules ------------------------------------------------------------------------------


def test_cluster_js_follows_the_js_rules():
    source = (STATIC_JS / "cluster.js").read_text()
    assert source.startswith("// PLACEHOLDER — Apo owns the final version.")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert banned not in source, banned
    assert "console." not in source
    # Fetches and links only ever go to the cluster's own fixed URLs.
    assert r"/^\/api\/v1\/clusters\/\d+\/detail\.json$/" in source
    assert r"/^\/clusters\/\d+\/$/" in source
    assert r"/^[0-9a-f]{12,64}$/" in source  # fix ids come from the hex fingerprint only


def test_overview_js_builds_cluster_units_from_the_template():
    source = (STATIC_JS / "overview.js").read_text()
    assert "cluster-unit-template" in source
    assert r"/^\/clusters\/\d+\/$/" in source
    assert "rack-unit__role" in source


def test_new_css_uses_only_tokens():
    """Every colour in the new components comes from tokens.css, so a theme can restyle it."""
    css = (STATIC_CSS / "components.css").read_text()
    block = css[css.index("/* Kubernetes UI") :]
    block = block[: block.index("/* end Kubernetes UI */")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", block)
    for name in (".role-label", ".stack-table", ".finding"):
        assert name in block
