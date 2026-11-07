"""Every check CronRack can notify about: the single source of truth for Settings → Notifications.

One RuleSpec per rule in rules.registry, rules.k8s and rules.network. The guard test
(tests/test_notification_registry.py) fails when a check has no RuleSpec here, so a new
check can't ship without deciding how loud it is. See docs/adding-a-check.md.

A RuleSpec says, per preset, whether the rule alerts (Discord + page), shows (page and the
daily summary) or is off; whether it counts as "needs you" (red) or "heads up" (yellow);
and, for some rules, a fixed list of limits (never free text) that feed the rule's existing
threshold.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from rules.base import INFO, RED
from rules.k8s import CLUSTER_RULES
from rules.network import NETWORK_RULES
from rules.registry import RULES, get_rule

ALERT = "alert"  # dashboard + Discord
SHOW = "show"  # dashboard only, and the daily summary
OFF = "off"  # not shown, not sent (the agent still collects the data)
LEVELS = (ALERT, SHOW, OFF)
LEVEL_LABELS = {ALERT: "Alert", SHOW: "Show only", OFF: "Off"}

NEEDS_YOU = "needs_you"  # red: gets through quiet hours
HEADS_UP = "heads_up"  # yellow
COUNTS_AS = (NEEDS_YOU, HEADS_UP)
COUNTS_AS_LABELS = {NEEDS_YOU: "Needs you", HEADS_UP: "Heads up"}
COUNTS_AS_SEVERITY = {NEEDS_YOU: "red", HEADS_UP: "yellow"}

PRESETS = ("quiet", "balanced", "everything")
PRESET_LABELS = {
    "quiet": ("Quiet", "Only things that are broken"),
    "balanced": ("Balanced", "Broken things + important warnings"),
    "everything": ("Everything", "Every change, as it happens"),
}
DEFAULT_PRESET = "balanced"

GROUPS = ("Hosts", "Services & containers", "Kubernetes", "Network")


@dataclass(frozen=True)
class LimitChoice:
    key: str  # what the form posts and the database stores
    label: str
    value: float  # the rule's threshold


@dataclass(frozen=True)
class Limit:
    choices: tuple[LimitChoice, ...]
    default: str

    def value(self, key: str) -> float | None:
        """The threshold for a stored choice; None for anything that isn't a choice."""
        for choice in self.choices:
            if choice.key == key:
                return choice.value
        return None

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.choices)


@dataclass(frozen=True)
class RuleSpec:
    id: str
    group: str
    label: str
    description: str
    defaults: Mapping[str, str]  # preset -> alert | show | off
    counts_as: str  # needs_you | heads_up
    limit: Limit | None = None
    applies_to: str = ""  # "hosts" | "clusters" | "" (the network: one per owner)
    search: str = field(default="", compare=False)  # extra words the search box matches

    @property
    def search_text(self) -> str:
        return f"{self.label} {self.description} {self.id} {self.group} {self.search}".lower()


def _limit(unit: str, values: tuple[int, ...], default: int) -> Limit:
    return Limit(
        choices=tuple(LimitChoice(str(v), f"{v}{unit}", float(v)) for v in values),
        default=str(default),
    )


def _spec(
    rule_id: str,
    group: str,
    label: str,
    description: str,
    quiet: str | None = None,
    limit: Limit | None = None,
    search: str = "",
) -> RuleSpec:
    """Defaults follow the rule's own severity, so Balanced keeps today's behaviour:
    red and yellow alerted, info never did. Quiet keeps red, shows yellow and drops info,
    unless `quiet` says otherwise. Everything alerts on all of it."""
    severity = get_rule(rule_id).default_severity
    balanced = SHOW if severity == INFO else ALERT
    if quiet is None:
        quiet = ALERT if severity == RED else SHOW if balanced == ALERT else OFF
    if rule_id in CLUSTER_RULES:
        applies_to = "clusters"
    elif rule_id in NETWORK_RULES:
        applies_to = ""
    else:
        applies_to = "hosts"
    return RuleSpec(
        id=rule_id,
        group=group,
        label=label,
        description=description,
        defaults={"quiet": quiet, "balanced": balanced, "everything": ALERT},
        counts_as=NEEDS_YOU if severity == RED else HEADS_UP,
        limit=limit,
        applies_to=applies_to,
        search=search,
    )


H, S, K, N = GROUPS

# fmt: off
_SPECS = (
    # --- Hosts -------------------------------------------------------------------------------
    _spec("connectivity.offline", H, "Host offline", "No report for a few minutes",
          search="down silent dead"),
    _spec("disk.usage", H, "Disk almost full", "A disk is above the limit",
          limit=_limit("%", (80, 85, 90, 95), 85), search="space storage full"),
    _spec("updates.security_pending", H, "Security updates", "Security patches are waiting",
          quiet=SHOW, search="apt upgrade patch"),
    _spec("updates.pending", H, "Updates pending", "Normal package updates", quiet=OFF,
          search="apt upgrade"),
    _spec("updates.reboot_required", H, "Reboot required", "Updates need a restart", quiet=OFF,
          search="restart kernel"),
    # --- Services & containers ---------------------------------------------------------------
    _spec("systemd.unit_failed", S, "Service failed", "A systemd service stopped with an error",
          search="unit timer"),
    _spec("docker.container_exited_error", S, "Container stopped",
          "A container exited with an error", search="docker exit crash"),
    _spec("docker.container_restarting", S, "Container restarting",
          "A container keeps restarting", search="docker crash loop"),
    _spec("docker.container_unhealthy", S, "Container unhealthy",
          "Its own health check is failing", search="docker health"),
    # --- Kubernetes (cluster rules, and the Helm command on a host) --------------------------
    _spec("k8s.node_not_ready", K, "Node not ready", "A cluster node stopped working"),
    _spec("k8s.pod_crashloop", K, "Pods crash-looping", "Pods keep crashing"),
    _spec("k8s.argo_out_of_sync", K, "Argo CD out of sync", "An app doesn't match Git",
          quiet=OFF, search="argocd gitops"),
    _spec("k8s.api_cert_expiring", K, "Certificate expiring",
          "The cluster's API certificate runs out soon",
          limit=_limit(" days before", (7, 14, 30, 60), 30), search="cert tls"),
    _spec("k8s.cluster_offline", K, "Cluster stopped reporting",
          "No cluster data for a few minutes"),
    _spec("k8s.api_unreachable", K, "Kubernetes API unreachable",
          "The agent can't reach the API server"),
    _spec("k8s.token_rejected", K, "Agent token rejected",
          "The cluster refuses the agent's token"),
    _spec("k8s.token_expiring", K, "Agent token expires soon",
          "The agent's cluster token runs out soon"),
    _spec("k8s.permission_denied", K, "Agent lacks a permission",
          "The agent can't read part of the cluster", search="rbac"),
    _spec("k8s.node_pressure", K, "Node under pressure",
          "A node is short on memory, disk or processes"),
    _spec("k8s.pod_oomkilled", K, "Pod ran out of memory",
          "A container was killed for using too much memory", search="oom"),
    _spec("k8s.pod_config_error", K, "Pod configuration error",
          "A pod can't start because of its settings"),
    _spec("k8s.pod_image_pull", K, "Image can't be pulled",
          "A container image can't be downloaded"),
    _spec("k8s.pod_pending", K, "Pod not starting", "A pod has been waiting to start"),
    _spec("k8s.pod_restarts", K, "Pod restarting often", "A pod restarted several times today"),
    _spec("k8s.workload_unavailable", K, "App has no running copy",
          "A deployment has nothing running", search="deployment statefulset"),
    _spec("k8s.workload_degraded", K, "App has fewer copies than asked",
          "Some replicas are missing", search="deployment replicas"),
    _spec("k8s.daemonset_incomplete", K, "DaemonSet not on every node",
          "Some nodes lack a DaemonSet pod"),
    _spec("k8s.pvc_pending", K, "Volume claim pending", "Storage was asked for but not given",
          search="pvc storage"),
    _spec("k8s.argo_unhealthy", K, "Argo CD app unhealthy",
          "Argo CD says an app isn't working", search="argocd"),
    _spec("k8s.image_unpinned", K, "Image without a fixed version",
          "A container uses a moving tag like latest"),
    _spec("k8s.k3s_patch_behind", K, "k3s update available", "A k3s patch release is out"),
    _spec("k8s.k3s_minor_behind", K, "Newer k3s version is stable",
          "A newer k3s minor version is out"),
    _spec("k8s.argocd_outdated", K, "Argo CD update available",
          "A newer Argo CD release is out"),
    _spec("k8s.snapshot_truncated", K, "Cluster view cut short",
          "The cluster is too big to show in full"),
    _spec("k8s.snapshot_incomplete", K, "Cluster view incomplete",
          "Part of the cluster couldn't be read"),
    _spec("k8s.helm_cli_outdated", K, "Helm command is outdated",
          "The helm command on a host is old"),
    # --- Network -----------------------------------------------------------------------------
    _spec("network.router_down", N, "Router down", "Your router stopped answering"),
    _spec("network.local_down", N, "Switch or local point down",
          "A point on your network stopped answering", search="router switch isp box"),
    _spec("network.internet_down", N, "Internet down",
          "Your devices are fine, the internet isn't"),
    _spec("network.internet_slow", N, "Internet slow (latency)", "High latency to the internet",
          quiet=OFF, limit=_limit(" ms", (100, 150, 250, 500), 150), search="ping latency"),
    _spec("network.packet_loss", N, "Internet slow (packet loss)",
          "Packets to the internet get lost", quiet=OFF,
          limit=_limit("%", (2, 5, 10, 20), 5), search="loss"),
    _spec("network.dns_failing", N, "DNS failing", "Names can't be looked up"),
    _spec("network.public_ip_changed", N, "Public IP changed", "Your ISP gave you a new address",
          search="ip address"),
)
# fmt: on

SPECS: dict[str, RuleSpec] = {spec.id: spec for spec in _SPECS}

# Every rule the engine evaluates; the guard test compares these with SPECS.
ALL_RULE_IDS = frozenset(RULES) | frozenset(CLUSTER_RULES) | frozenset(NETWORK_RULES)


def spec_for(rule_id: str) -> RuleSpec:
    """The RuleSpec of a rule; KeyError for an unknown ID."""
    return SPECS[rule_id]


def specs_by_group() -> list[tuple[str, list[RuleSpec]]]:
    """[(group, [specs in registry order])] in GROUPS order."""
    return [(group, [s for s in _SPECS if s.group == group]) for group in GROUPS]
