"""Kubernetes rules. They read the cluster snapshot (the agent's cluster view), not a host report.

Every rule is category "kubernetes". The snapshot passed the strict serializers, but it
still comes from an agent, so names are only ever used as text and as shell-quoted fix
values. Fixes are read-only commands to copy; CronRack never runs them.

Subjects name what the problem is about, not the pod that happens to run it. A pod's own
name changes on every rollout, so pods use "pod:<namespace>/<Kind>/<owner>": "This is
expected" then keeps working after a restart.

Version rules need `ctx.latest` (the newest released versions). It is filled only when
the owner turned online update checks on; otherwise these rules stay quiet.
"""

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from rules.base import RED, YELLOW, Finding, Rule, RuleContext, ago
from rules.versions import is_behind, minor_of

INFO = "info"

# The certificate turns red this many days before it expires (yellow from the threshold).
K8S_CERT_RED_DAYS = 7
# Longest subject a Suppression can hold (rules.models.Suppression.subject).
MAX_SUBJECT = 200

IMAGE_PULL_REASONS = frozenset({"ImagePullBackOff", "ErrImagePull", "InvalidImageName"})
CONFIG_REASONS = frozenset({"CreateContainerConfigError"})

_SERVICE_ACCOUNT = re.compile(r"system:serviceaccount:([a-z0-9][a-z0-9.-]*):([a-z0-9][a-z0-9.-]*)")
FALLBACK_SERVICE_ACCOUNT_FIX = "kubectl get serviceaccounts -A"


def _subject(prefix: str, *parts: str) -> str:
    """'pod:apps/Deployment/web', or 'pod:#<hash>' if that would not fit in a Suppression."""
    subject = f"{prefix}:" + "/".join(parts)
    if len(subject) <= MAX_SUBJECT:
        return subject
    return f"{prefix}:#{hashlib.sha256(subject.encode()).hexdigest()[:16]}"


def _items(snapshot: Mapping[str, Any], key: str) -> list:
    value = snapshot.get(key)
    return value if isinstance(value, list) else []


def _api(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    api = snapshot.get("api")
    return api if isinstance(api, Mapping) else {}


def _service_account(snapshot: Mapping[str, Any]) -> tuple[str, str] | None:
    match = _SERVICE_ACCOUNT.fullmatch(snapshot.get("sa") or "")
    return (match[1], match[2]) if match else None


# --- the cluster itself ----------------------------------------------------------------------


def check_cluster_offline(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    # last_seen_at is when the last snapshot arrived. No snapshot yet means "waiting".
    if ctx.last_seen_at is None:
        return []
    silent = (ctx.now - ctx.last_seen_at).total_seconds()
    threshold = rule.threshold(ctx)
    if threshold is None or silent <= threshold:
        return []
    return [rule.finding(ctx, f"No cluster data for {ago(silent)}")]


def check_api_unreachable(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    error = _api(snapshot).get("error")
    if error not in ("refused", "timeout", "tls", "bad_response"):
        return []
    return [rule.finding(ctx, f"The agent can't reach the Kubernetes API ({error})")]


def check_token_rejected(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    if _api(snapshot).get("error") != "unauthorized":
        return []
    account = _service_account(snapshot)
    finding = rule.finding(
        ctx,
        "The cluster rejected the agent's token",
        namespace=account[0] if account else "",
        account=account[1] if account else "",
    )
    if account is None:
        finding = replace(finding, fix=FALLBACK_SERVICE_ACCOUNT_FIX)
    return [finding]


def check_token_expiring(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    days = snapshot.get("token_days")
    threshold = rule.threshold(ctx)
    if days is None or threshold is None or days >= threshold:
        return []
    account = _service_account(snapshot)
    finding = rule.finding(
        ctx,
        f"The agent's token expires in {days} days" if days >= 0 else "The agent's token expired",
        namespace=account[0] if account else "",
        account=account[1] if account else "",
    )
    if account is None:
        finding = replace(finding, fix=FALLBACK_SERVICE_ACCOUNT_FIX)
    return [finding]


def check_permission_denied(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    findings = []
    for item in _items(snapshot, "denied"):
        group, resource, verb = item["g"], item["r"], item["v"]
        reference = f"{resource}.{group}" if group else resource
        findings.append(
            rule.finding(
                ctx,
                f"The agent may not {verb} {reference}",
                subject=f"rbac:{group}/{resource}/{verb}",
                group=group,
                resource=resource,
                verb=verb,
                reference=reference,
                account=snapshot.get("sa") or "SERVICE_ACCOUNT",
            )
        )
    return findings


def check_api_cert_expiring(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    days = _api(snapshot).get("cert_days")
    threshold = rule.threshold(ctx)
    if days is None or threshold is None or days >= threshold:
        return []
    severity = RED if days < min(K8S_CERT_RED_DAYS, threshold) else YELLOW
    text = f"API certificate expires in {days} days" if days >= 0 else "API certificate expired"
    return [rule.finding(ctx, text, severity=severity)]


def check_snapshot_truncated(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    if snapshot.get("truncated") is not True:
        return []
    cut = ", ".join(snapshot.get("cut") or []) or "some lists"
    return [rule.finding(ctx, f"Not everything fits in one report (cut: {cut})")]


# --- nodes -------------------------------------------------------------------------------------


def check_node_not_ready(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    return [
        rule.finding(
            ctx, f"Node {n['name']} is not ready", _subject("node", n["name"]), node=n["name"]
        )
        for n in _items(snapshot, "nodes")
        if not n["ready"]
    ]


def check_node_pressure(rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    return [
        rule.finding(
            ctx,
            f"Node {n['name']} under pressure ({', '.join(n['pressure'])})",
            _subject("node", n["name"]),
            node=n["name"],
        )
        for n in _items(snapshot, "nodes")
        if n["pressure"]
    ]


# --- pods ----------------------------------------------------------------------------------------


def _pod_check(predicate: Callable[[Rule, RuleContext, dict, str], bool], text: str):
    """A check that fires once per workload that has at least one pod matching `predicate`.

    `text` may use {pod} (the first matching pod) and {more} ("" or " (and 2 more)").
    """

    def check(rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
        groups: dict[str, list[dict]] = {}
        for pod in _items(snapshot, "problem_pods"):
            subject = _subject("pod", pod["ns"], pod["owner"])
            if predicate(rule, ctx, pod, subject):
                groups.setdefault(subject, []).append(pod)
        findings = []
        for subject, pods in groups.items():
            first = pods[0]
            more = f" (and {len(pods) - 1} more)" if len(pods) > 1 else ""
            findings.append(
                rule.finding(
                    ctx,
                    text.format(pod=first["name"], more=more, reason=first["reason"]),
                    subject,
                    ns=first["ns"],
                    pod=first["name"],
                )
            )
        return findings

    return check


def _reason_is(*reasons: str):
    return lambda rule, ctx, pod, subject: pod["reason"] in reasons


def _oomkilled(rule: Rule, ctx: RuleContext, pod: dict, subject: str) -> bool:
    age, window = pod["last_age_s"], rule.threshold(ctx, subject)
    return pod["last"] == "OOMKilled" and age is not None and window is not None and age < window


def _pending(rule: Rule, ctx: RuleContext, pod: dict, subject: str) -> bool:
    age, limit = pod["age_s"], rule.threshold(ctx, subject)
    if pod["phase"] != "Pending" or age is None or limit is None or age <= limit:
        return False
    # Image and config problems have their own rules: one problem, one finding.
    return pod["reason"] not in IMAGE_PULL_REASONS | CONFIG_REASONS


def _restarts(rule: Rule, ctx: RuleContext, pod: dict, subject: str) -> bool:
    # A restart count never goes down, so only a recent restart (the same 24 hours as
    # the OOM rule) counts: otherwise the pod would stay yellow for as long as it lives.
    limit, age = rule.threshold(ctx, subject), pod["last_age_s"]
    return (
        limit is not None and pod["restarts"] > limit and age is not None and age < RESTART_WINDOW
    )


RESTART_WINDOW = 86_400


# --- workloads -----------------------------------------------------------------------------------


def _workload_subject(w: Mapping[str, Any]) -> str:
    return _subject("workload", w["ns"], w["kind"], w["name"])


def _workload_check(predicate, text: str, kinds=("Deployment", "StatefulSet")):
    def check(rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
        return [
            rule.finding(
                ctx,
                text.format(**w),
                _workload_subject(w),
                ns=w["ns"],
                kind=w["kind"],
                name=w["name"],
            )
            for w in _items(snapshot, "workloads")
            if w["kind"] in kinds and predicate(w)
        ]

    return check


def _unpinned(image: str) -> bool:
    if "@sha256:" in image:
        return False
    last = image.rsplit("/", 1)[-1]
    tag = last.partition(":")[2]
    return tag in ("", "latest")


def check_image_unpinned(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    findings = []
    for w in _items(snapshot, "workloads"):
        loose = [image for image in w["images"] if _unpinned(image)]
        if loose:
            findings.append(
                rule.finding(
                    ctx,
                    f"{w['name']} uses {loose[0]} without a fixed version",
                    _workload_subject(w),
                    ns=w["ns"],
                    kind=w["kind"],
                    name=w["name"],
                )
            )
    return findings


# --- storage and Argo CD -------------------------------------------------------------------------


def check_pvc_pending(rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
    findings = []
    for claim in _items(snapshot, "pvcs"):
        subject = _subject("pvc", claim["ns"], claim["name"])
        limit, age = rule.threshold(ctx, subject), claim["age_s"]
        if claim["phase"] == "Pending" and age is not None and limit is not None and age > limit:
            findings.append(
                rule.finding(
                    ctx,
                    f"Volume claim {claim['name']} is still pending",
                    subject,
                    ns=claim["ns"],
                    name=claim["name"],
                )
            )
    return findings


def _argo_check(predicate, text: str):
    def check(rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext) -> list[Finding]:
        return [
            rule.finding(
                ctx,
                text.format(**app),
                _subject("argo", app["ns"], app["name"]),
                ns=app["ns"],
                name=app["name"],
            )
            for app in _items(snapshot, "argo")
            if predicate(app)
        ]

    return check


# --- versions (need ctx.latest) ------------------------------------------------------------------


def _latest(ctx: RuleContext, source: str) -> Mapping[str, str]:
    data = ctx.latest.get(source)
    return data if isinstance(data, Mapping) else {}


def _k3s_nodes(snapshot: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(node name, version) for nodes running k3s. Other distributions get no version check."""
    return [
        (n["name"], n["kubelet"])
        for n in _items(snapshot, "nodes")
        if isinstance(n.get("kubelet"), str) and "+k3s" in n["kubelet"]
    ]


def check_k3s_patch_behind(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    channels = _latest(ctx, "k3s")
    behind: list[tuple[str, str]] = []  # (installed, newest in its own minor)
    for _name, version in _k3s_nodes(snapshot):
        minor = minor_of(version)
        newest = channels.get(f"v{minor[0]}.{minor[1]}") if minor else None
        if is_behind(version, newest) and (version, newest) not in behind:
            behind.append((version, newest))
    if not behind:
        return []
    version, newest = behind[0]
    return [rule.finding(ctx, f"k3s {version} has a newer release in its line: {newest}")]


def check_k3s_minor_behind(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    stable = _latest(ctx, "k3s").get("stable")
    stable_minor = minor_of(stable)
    if stable_minor is None:
        return []
    for _name, version in _k3s_nodes(snapshot):
        minor = minor_of(version)
        if minor is not None and minor < stable_minor:
            return [rule.finding(ctx, f"k3s {version} is older than the stable channel ({stable})")]
    return []


def check_argocd_outdated(
    rule: Rule, snapshot: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    installed, newest = snapshot.get("argocd_version"), _latest(ctx, "argocd").get("latest")
    if not is_behind(installed, newest):
        return []
    namespace = next(
        (w["ns"] for w in _items(snapshot, "workloads") if w["name"] == "argocd-server"), "argocd"
    )
    return [rule.finding(ctx, f"Argo CD {installed} is behind {newest}", ns=namespace)]


def check_helm_cli_outdated(
    rule: Rule, report: Mapping[str, Any], ctx: RuleContext
) -> list[Finding]:
    """A host rule: the Helm command on this host, from the report's kubernetes.node."""
    kube = report.get("kubernetes")
    node = kube.get("node") if isinstance(kube, Mapping) else None
    installed = node.get("helm") if isinstance(node, Mapping) else None
    newest = _latest(ctx, "helm").get("latest")
    if not is_behind(installed, newest):
        return []
    return [rule.finding(ctx, f"The Helm command is {installed}; the newest is {newest}")]


# --- the rules -----------------------------------------------------------------------------------

DESCRIBE_POD = "kubectl -n {ns} describe pod {pod}"
DESCRIBE_WORKLOAD = "kubectl -n {ns} describe {kind} {name}"
SERVICE_ACCOUNT_FIX = "kubectl -n {namespace} get serviceaccount {account}"
GET_NODES = "kubectl get nodes -o wide"


def _k8s(**kwargs) -> Rule:
    return Rule(category="kubernetes", **kwargs)


_CLUSTER_RULES = (
    _k8s(
        id="k8s.cluster_offline",
        title="Cluster stopped reporting",
        default_severity=RED,
        default_threshold=180,  # seconds without a snapshot
        check=check_cluster_offline,
        why=(
            "The agent that watches this cluster stopped sending its view. The cluster "
            "may be fine; CronRack just can't see it."
        ),
        fix_template="sudo journalctl -u cronrack-agent -n 50 --no-pager",
    ),
    _k8s(
        id="k8s.api_unreachable",
        title="Kubernetes API unreachable",
        default_severity=RED,
        check=check_api_unreachable,
        why=(
            "The agent can't talk to the Kubernetes API, so nothing in the cluster is "
            "being checked."
        ),
        fix_template="sudo systemctl status k3s",
    ),
    _k8s(
        id="k8s.token_rejected",
        title="Agent token rejected",
        default_severity=RED,
        check=check_token_rejected,
        why="The cluster no longer accepts the agent's token (it expired or was deleted).",
        fix_template=SERVICE_ACCOUNT_FIX,
    ),
    _k8s(
        id="k8s.token_expiring",
        title="Agent token expires soon",
        default_severity=YELLOW,
        default_threshold=14,  # days
        check=check_token_expiring,
        why="The agent's token runs out soon. After that, cluster monitoring stops.",
        fix_template=SERVICE_ACCOUNT_FIX,
    ),
    _k8s(
        id="k8s.permission_denied",
        title="Agent lacks a permission",
        default_severity=YELLOW,
        check=check_permission_denied,
        why=(
            "The agent's role doesn't allow this, so that part of the cluster is "
            "missing from CronRack."
        ),
        fix_template=(
            "# The ClusterRole needs: apiGroups [{group}], resources [{resource}], verbs [{verb}]\n"
            "kubectl auth can-i {verb} {reference} -A --as={account}"
        ),
    ),
    _k8s(
        id="k8s.node_not_ready",
        title="Node not ready",
        default_severity=RED,
        check=check_node_not_ready,
        why="The node isn't accepting work; its pods are moved or stuck.",
        fix_template="kubectl describe node {node}",
    ),
    _k8s(
        id="k8s.node_pressure",
        title="Node under pressure",
        default_severity=YELLOW,
        check=check_node_pressure,
        why=("The node is short on memory, disk or process IDs and may start evicting pods."),
        fix_template="kubectl describe node {node}",
    ),
    _k8s(
        id="k8s.pod_crashloop",
        title="Pod crash-looping",
        default_severity=RED,
        check=_pod_check(_reason_is("CrashLoopBackOff"), "{pod} keeps crashing{more}"),
        why="The container keeps crashing and Kubernetes keeps restarting it.",
        fix_template="kubectl -n {ns} logs {pod} --previous --tail=50 --all-containers",
    ),
    _k8s(
        id="k8s.pod_oomkilled",
        title="Pod ran out of memory",
        default_severity=RED,
        default_threshold=86_400,  # seconds: only a kill in the last day counts
        check=_pod_check(_oomkilled, "{pod} was killed for using too much memory{more}"),
        why="The container used more memory than its limit and was killed.",
        fix_template=DESCRIBE_POD,
    ),
    _k8s(
        id="k8s.pod_config_error",
        title="Pod configuration error",
        default_severity=RED,
        check=_pod_check(_reason_is(*CONFIG_REASONS), "{pod} can't start: bad configuration{more}"),
        why=("The pod refers to a ConfigMap or Secret key that doesn't exist, so it can't start."),
        fix_template=DESCRIBE_POD,
    ),
    _k8s(
        id="k8s.pod_image_pull",
        title="Image can't be pulled",
        default_severity=YELLOW,
        check=_pod_check(_reason_is(*IMAGE_PULL_REASONS), "{pod} can't pull its image{more}"),
        why=("Kubernetes can't download the image (typo, missing tag or a private registry)."),
        fix_template=DESCRIBE_POD,
    ),
    _k8s(
        id="k8s.pod_pending",
        title="Pod not starting",
        default_severity=YELLOW,
        default_threshold=300,  # seconds in Pending
        check=_pod_check(_pending, "{pod} is still pending{more}"),
        why=(
            "The pod hasn't been scheduled or started for a while (no room, an unbound "
            "volume or a node selector that matches nothing)."
        ),
        fix_template=DESCRIBE_POD,
    ),
    _k8s(
        id="k8s.pod_restarts",
        title="Pod restarting often",
        default_severity=YELLOW,
        default_threshold=5,  # restarts; only counted if the last one was in the last day
        check=_pod_check(_restarts, "{pod} restarted repeatedly{more}"),
        why="The pod restarted repeatedly in the last day.",
        fix_template="kubectl -n {ns} logs {pod} --previous --tail=50",
    ),
    _k8s(
        id="k8s.workload_unavailable",
        title="App has no running copy",
        default_severity=RED,
        check=_workload_check(
            lambda w: w["desired"] > 0 and w["available"] == 0,
            "{name} has no running copy (0/{desired})",
        ),
        why="None of this app's copies is running.",
        fix_template=DESCRIBE_WORKLOAD,
    ),
    _k8s(
        id="k8s.workload_degraded",
        title="App has fewer copies than asked",
        default_severity=YELLOW,
        check=_workload_check(
            lambda w: 0 < w["available"] < w["desired"],
            "{name} has {available}/{desired} copies running",
        ),
        why="Fewer copies are running than asked for.",
        fix_template=DESCRIBE_WORKLOAD,
    ),
    _k8s(
        id="k8s.daemonset_incomplete",
        title="DaemonSet not on every node",
        default_severity=YELLOW,
        check=_workload_check(
            lambda w: w["available"] < w["desired"] or w["misscheduled"] > 0,
            "{name} runs on {available}/{desired} nodes",
            kinds=("DaemonSet",),
        ),
        why="Not every node runs this DaemonSet's pod.",
        fix_template="kubectl -n {ns} describe daemonset {name}",
    ),
    _k8s(
        id="k8s.pvc_pending",
        title="Volume claim pending",
        default_severity=YELLOW,
        default_threshold=600,  # seconds
        check=check_pvc_pending,
        why=(
            "The volume claim has no storage yet. With local-path a claim waits until a "
            "pod uses it, hence the delay before this shows."
        ),
        fix_template="kubectl -n {ns} describe pvc {name}",
    ),
    _k8s(
        id="k8s.argo_out_of_sync",
        title="Argo CD app out of sync",
        default_severity=YELLOW,
        check=_argo_check(lambda a: a["sync"] == "OutOfSync", "{name} differs from Git"),
        why="The cluster differs from what Git says.",
        fix_template="kubectl -n {ns} describe application {name}",
    ),
    _k8s(
        id="k8s.argo_unhealthy",
        title="Argo CD app unhealthy",
        default_severity=RED,
        check=_argo_check(lambda a: a["health"] in ("Degraded", "Missing"), "{name} is {health}"),
        why="Argo CD says this app is broken or its resources are gone.",
        fix_template="kubectl -n {ns} describe application {name}",
    ),
    _k8s(
        id="k8s.api_cert_expiring",
        title="API certificate expires soon",
        default_severity=YELLOW,
        default_threshold=30,  # days; red under K8S_CERT_RED_DAYS
        check=check_api_cert_expiring,
        why=(
            "The API server's certificate expires soon. k3s renews certificates close to "
            "expiry when the k3s service restarts."
        ),
        fix_template=(
            "echo | openssl s_client -connect 127.0.0.1:6443 2>/dev/null"
            " | openssl x509 -noout -enddate"
        ),
    ),
    _k8s(
        id="k8s.image_unpinned",
        title="Image without a fixed version",
        default_severity=YELLOW,
        check=check_image_unpinned,
        why=("Without a fixed tag, a restart can silently run different code than before."),
        fix_template=(
            "kubectl -n {ns} get {kind} {name} "
            "-o jsonpath='{{.spec.template.spec.containers[*].image}}'"
        ),
    ),
    _k8s(
        id="k8s.k3s_patch_behind",
        title="k3s update available",
        default_severity=YELLOW,
        check=check_k3s_patch_behind,
        why=(
            "A newer patch release of your k3s version exists. Patch releases carry bug "
            "and security fixes."
        ),
        fix_template=GET_NODES,
    ),
    _k8s(
        id="k8s.k3s_minor_behind",
        title="Newer k3s version is stable",
        default_severity=INFO,
        check=check_k3s_minor_behind,
        why="A newer k3s minor version is the stable one. Not urgent; plan an upgrade.",
        fix_template=GET_NODES,
    ),
    _k8s(
        id="k8s.argocd_outdated",
        title="Argo CD update available",
        default_severity=YELLOW,
        check=check_argocd_outdated,
        why="A newer Argo CD release exists.",
        fix_template=(
            "kubectl -n {ns} get deploy argocd-server "
            "-o jsonpath='{{.spec.template.spec.containers[0].image}}'"
        ),
    ),
    _k8s(
        id="k8s.snapshot_truncated",
        title="Cluster view cut short",
        default_severity=INFO,
        check=check_snapshot_truncated,
        why=(
            "The cluster is larger than one report can carry, so some healthy items are not listed."
        ),
    ),
)

CLUSTER_RULES: dict[str, Rule] = {rule.id: rule for rule in _CLUSTER_RULES}

# A host rule (it reads the host's report), registered with the other host rules.
HELM_CLI_RULE = _k8s(
    id="k8s.helm_cli_outdated",
    title="Helm command is outdated",
    default_severity=INFO,
    check=check_helm_cli_outdated,
    why="The Helm command on this host is older than the newest release.",
    fix_template="helm version --short",
)
