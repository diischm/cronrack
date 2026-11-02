"""Strict validation of everything an agent sends.

Agent data is untrusted (container and unit names come from the host, and a
stolen token lets anyone send anything), so these serializers accept exactly the
format agent/report.py builds and nothing else:

- exact JSON types: "3" is not a number, 1 is not a boolean, 3 is not a string
- every key present (a check the agent couldn't run is null, not missing)
- unknown keys rejected at every level
- length limits on every string and list, and no control characters in strings
- Kubernetes names must be DNS-1123 names; one that isn't is replaced, not refused
  (KubeNameField), so a single odd pod name can't hide the rest of the cluster
- keys added after agent 0.1.0 (kubernetes) are optional, so older agents keep working

If agent/report.py changes, change this file in the same pull request.
"""

import re
from collections.abc import Mapping
from datetime import datetime

from rest_framework import serializers
from rest_framework.settings import api_settings

# Lists longer than this are refused. A homelab host has nowhere near 500 containers.
MAX_ITEMS = 500
# Names end up in finding subjects like "container:<name>" (Suppression.subject is
# 200 characters), so they must fit with the longest prefix ("container:").
MAX_NAME = 190

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class StrictCharField(serializers.CharField):
    """A string, as-is: no type coercion, no trimming, no control characters."""

    default_error_messages = {  # noqa: RUF012 - DRF reads this class attribute
        "invalid": "Must be a string.",
        "control": "Must not contain control characters.",
    }

    def __init__(self, **kwargs):
        kwargs.setdefault("trim_whitespace", False)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        if _CONTROL_CHARS.search(data):
            self.fail("control")
        return data


class StrictPatternField(StrictCharField):
    """A strict string that must match a regular expression completely."""

    default_error_messages = {  # noqa: RUF012 - DRF reads this class attribute
        **StrictCharField.default_error_messages,
        "pattern": "Not in the expected format.",
    }

    def __init__(self, pattern: str, **kwargs):
        self.pattern = re.compile(pattern)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not self.pattern.fullmatch(value):
            self.fail("pattern")
        return value


class StrictIntegerField(serializers.IntegerField):
    """A JSON integer. Rejects floats, numeric strings and booleans (True is an int in Python)."""

    def to_internal_value(self, data):
        if isinstance(data, bool) or not isinstance(data, int):
            self.fail("invalid")
        return data


class StrictBooleanField(serializers.BooleanField):
    """Exactly true or false; not 1, 0, "true" or "yes"."""

    def to_internal_value(self, data):
        if not isinstance(data, bool):
            self.fail("invalid")
        return data


class StrictChoiceField(serializers.ChoiceField):
    """One of a fixed set of strings, compared as-is."""

    def to_internal_value(self, data):
        if not isinstance(data, str) or data not in self.choices:
            self.fail("invalid_choice", input=str(data)[:50])
        return data


class StrictSerializer(serializers.Serializer):
    """A JSON object with exactly the declared keys. Unknown keys are an error."""

    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            raise serializers.ValidationError(
                {api_settings.NON_FIELD_ERRORS_KEY: ["Must be an object."]}
            )
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            # Only the first few names, cut short: the error is logged, never trusted.
            raise serializers.ValidationError(
                {str(key)[:50]: ["Unknown field."] for key in unknown[:5]}
            )
        return super().to_internal_value(data)


def _list(child, **kwargs):
    return serializers.ListField(child=child, max_length=MAX_ITEMS, **kwargs)


# --- report sections (same order as agent/report.py) ---------------------------


class UpdatesSerializer(StrictSerializer):
    total = StrictIntegerField(min_value=0, max_value=100_000)
    security = StrictIntegerField(min_value=0, max_value=100_000)

    def validate(self, attrs):
        if attrs["security"] > attrs["total"]:
            raise serializers.ValidationError("security can't be more than total.")
        return attrs


class ContainerSerializer(StrictSerializer):
    name = StrictCharField(max_length=MAX_NAME)
    state = StrictCharField(max_length=32)
    status = StrictCharField(max_length=255, allow_blank=True)
    health = StrictChoiceField(choices=["healthy", "unhealthy", "starting"], allow_null=True)
    exit_code = StrictIntegerField(min_value=-(2**31), max_value=2**31 - 1, allow_null=True)


class TimerSerializer(StrictSerializer):
    timer = StrictCharField(max_length=MAX_NAME)
    activates = StrictCharField(max_length=MAX_NAME, allow_null=True)
    last_run = StrictCharField(max_length=64, allow_null=True)
    next_run = StrictCharField(max_length=64, allow_null=True)


class DiskSerializer(StrictSerializer):
    mount = StrictCharField(max_length=MAX_NAME)
    percent = StrictIntegerField(min_value=0, max_value=100)


class ErrorSerializer(StrictSerializer):
    check = StrictCharField(max_length=32)
    error = StrictCharField(max_length=100)


# --- kubernetes (agent/collectors.py collect_node_role, agent/kube_cluster.py) -----------
#
# The limits below are the agent's own caps (kube_cluster.py MAX_*), so a report the
# agent builds is never refused for being "too big", and a hostile one still is.

K8S_NAME = 253  # a Kubernetes object name (DNS subdomain)
K8S_MAX_NODES = 50
K8S_MAX_PROBLEM_PODS = 100
K8S_MAX_OK_PODS = 400
K8S_MAX_OK_NAMESPACES = 100
K8S_MAX_WORKLOADS = 200
K8S_MAX_PVCS = 100
K8S_MAX_HELM = 100
K8S_MAX_ARGO = 100
K8S_MAX_EVENTS = 20
K8S_MAX_IMAGES = 10
K8S_MAX_DENIED = 20
K8S_MAX_CUT = 20

K8S_COUNT = 1_000_000
K8S_MAX_AGE = 315_360_000  # 10 years, in seconds

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_VERSION = r"v\d+\.\d+\.\d+[0-9A-Za-z.+-]{0,40}"
# An Argo CD image tag may come without the "v" and is cut only by the image's length.
_TAG_VERSION = r"v?\d+\.\d+\.\d+[0-9A-Za-z.+-]{0,200}"
_REASON = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}"
_SUBJECT = r"[A-Za-z0-9:_.@-]{1,253}"
_IMAGE = r"[A-Za-z0-9][A-Za-z0-9._/:+-]{0,254}(?:@sha256:[0-9a-f]{64})?"
_IMAGE_MAX = 255 + len("@sha256:") + 64
_STORAGE = r"\d+(?:\.\d+)?(?:Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?"
_CUT_NAME = r"[a-z_]{1,32}"
# DNS-1123, as Kubernetes itself checks names: a label (namespaces) or a subdomain
# (pods, nodes, workloads, claims, Argo CD apps, Helm releases).
_DNS_LABEL = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
_DNS_PART = r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
_DNS_SUBDOMAIN = rf"{_DNS_PART}(?:\.{_DNS_PART})*"
# "Kind/name": what owns a pod, what an event is about. The only place a / is allowed.
_OBJECT_REF = rf"[A-Z][A-Za-z0-9]{{0,62}}/(?=.{{1,{K8S_NAME}}}$){_DNS_SUBDOMAIN}"
K8S_LABEL = 63
K8S_REF = 63 + 1 + K8S_NAME

# What a name that isn't one becomes. Not a valid name itself, so it can't be mistaken
# for a real object.
INVALID_K8S_NAME = "(invalid name)"
_REVISION = r"[A-Za-z0-9._-]{1,7}"

POD_PHASES = ["Pending", "Running", "Succeeded", "Failed", "Unknown"]
PRESSURE_CONDITIONS = ["MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"]
API_ERRORS = ["tls", "refused", "timeout", "unauthorized", "bad_response"]
WORKLOAD_KINDS = ["Deployment", "StatefulSet", "DaemonSet"]
SYNC_STATUSES = ["Synced", "OutOfSync", "Unknown"]
HEALTH_STATUSES = ["Healthy", "Progressing", "Degraded", "Suspended", "Missing", "Unknown"]
OPERATION_PHASES = ["Running", "Terminating", "Failed", "Error", "Succeeded"]
PVC_PHASES = ["Pending", "Bound", "Lost", "Unknown"]


def _k8s_count(**kwargs):
    return StrictIntegerField(min_value=0, max_value=K8S_COUNT, **kwargs)


def _k8s_age(**kwargs):
    return StrictIntegerField(min_value=0, max_value=K8S_MAX_AGE, **kwargs)


class KubeNameField(serializers.CharField):
    """A Kubernetes name. A string that isn't one becomes INVALID_K8S_NAME.

    Only the field is refused, not the report: the pod or app is still listed (it may be
    the broken one) and its problems still count; the odd name is never stored or shown.
    Anything that isn't a string (or null where allowed) still refuses the report.
    """

    default_error_messages = {"invalid": "Must be a string."}  # noqa: RUF012 - read by DRF

    def __init__(self, pattern: str, longest: int, **kwargs):
        self.pattern = re.compile(pattern)
        self.longest = longest
        kwargs.setdefault("trim_whitespace", False)
        kwargs.setdefault("allow_blank", True)  # "" is not refused, it is replaced
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        if len(data) > self.longest or not self.pattern.fullmatch(data):
            return INVALID_K8S_NAME
        return data

    def run_validation(self, data=serializers.empty):
        if data == "":
            return INVALID_K8S_NAME
        return super().run_validation(data)


def _k8s_name(**kwargs):
    """Pods, nodes, workloads, claims, Argo CD apps, Helm releases: a DNS-1123 subdomain."""
    return KubeNameField(_DNS_SUBDOMAIN, K8S_NAME, **kwargs)


def _k8s_namespace(**kwargs):
    """A namespace: a DNS-1123 label (no dots, at most 63)."""
    return KubeNameField(_DNS_LABEL, K8S_LABEL, **kwargs)


def _k8s_ref(**kwargs):
    """ "Kind/name" of an object."""
    return KubeNameField(_OBJECT_REF, K8S_REF, **kwargs)


def _k8s_version(**kwargs):
    return StrictPatternField(_VERSION, max_length=64, **kwargs)


class KubeNodeSerializer(StrictSerializer):
    """What this host is in a cluster: collectors.collect_node_role()."""

    role = StrictChoiceField(choices=["server", "agent", "node", "none"])
    dist = StrictChoiceField(choices=["k3s", "kubernetes"], allow_null=True)
    name = StrictCharField(max_length=63)
    k3s = _k8s_version(allow_null=True)
    kubelet = _k8s_version(allow_null=True)
    helm = _k8s_version(allow_null=True)


class KubeApiSerializer(StrictSerializer):
    error = StrictChoiceField(choices=API_ERRORS, allow_null=True)
    cert_days = StrictIntegerField(min_value=-100_000, max_value=1_000_000, allow_null=True)


class KubeDeniedSerializer(StrictSerializer):
    g = StrictCharField(max_length=63, allow_blank=True)
    r = StrictCharField(max_length=63)
    v = StrictCharField(max_length=16)


class KubeClusterNodeSerializer(StrictSerializer):
    name = _k8s_name()
    roles = serializers.ListField(child=StrictCharField(max_length=63), max_length=5)
    ready = StrictBooleanField()
    pressure = serializers.ListField(
        child=StrictChoiceField(choices=PRESSURE_CONDITIONS), max_length=len(PRESSURE_CONDITIONS)
    )
    kubelet = _k8s_version(allow_null=True)
    os = StrictCharField(max_length=32, allow_null=True)
    arch = StrictCharField(max_length=32, allow_null=True)
    cpu_m = StrictIntegerField(min_value=0, max_value=10**12, allow_null=True)
    mem_mi = StrictIntegerField(min_value=0, max_value=10**12, allow_null=True)
    cpu_used_m = StrictIntegerField(min_value=0, max_value=10**12, allow_null=True)
    mem_used_mi = StrictIntegerField(min_value=0, max_value=10**12, allow_null=True)


class KubeProblemPodSerializer(StrictSerializer):
    ns = _k8s_namespace()
    name = _k8s_name()
    node = _k8s_name(allow_null=True)
    phase = StrictChoiceField(choices=POD_PHASES)
    ready = serializers.ListField(child=_k8s_count(), min_length=2, max_length=2)
    restarts = _k8s_count()
    reason = StrictPatternField(_REASON, max_length=64, allow_null=True)
    last = StrictPatternField(_REASON, max_length=64, allow_null=True)
    last_age_s = _k8s_age(allow_null=True)
    age_s = _k8s_age(allow_null=True)
    owner = _k8s_ref()


class OkPodsField(serializers.Field):
    """Healthy pods by namespace: {"apps": [[name, node, restarts, age_s], ...]}.

    At most 100 namespaces and 400 pods in total (the agent's caps).
    """

    default_error_messages = {  # noqa: RUF012 - DRF reads this class attribute
        "invalid": "Must be an object of lists of [name, node, restarts, age] lists.",
        "too_many": "Too many healthy pods.",
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._ns = _k8s_namespace()
        self._name = _k8s_name()
        self._node = _k8s_name(allow_null=True)
        self._restarts = _k8s_count()
        self._age = _k8s_age(allow_null=True)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or len(data) > K8S_MAX_OK_NAMESPACES:
            self.fail("invalid")
        result: dict[str, list] = {}
        total = 0
        for ns, pods in data.items():
            if not isinstance(pods, list):
                self.fail("invalid")
            total += len(pods)
            if total > K8S_MAX_OK_PODS:
                self.fail("too_many")
            rows = []
            for pod in pods:
                if not isinstance(pod, list) or len(pod) != 4:
                    self.fail("invalid")
                rows.append(
                    [
                        self._name.run_validation(pod[0]),
                        self._node.run_validation(pod[1]),
                        self._restarts.run_validation(pod[2]),
                        self._age.run_validation(pod[3]),
                    ]
                )
            # Two bad namespaces both become INVALID_K8S_NAME: keep the pods of both.
            result.setdefault(self._ns.run_validation(ns), []).extend(rows)
        return result

    def to_representation(self, value):
        return value


class KubePodCountsSerializer(StrictSerializer):
    total = _k8s_count()
    running = _k8s_count()
    pending = _k8s_count()
    succeeded = _k8s_count()
    failed = _k8s_count()
    problem = _k8s_count()


class KubeWorkloadSerializer(StrictSerializer):
    kind = StrictChoiceField(choices=WORKLOAD_KINDS)
    ns = _k8s_namespace()
    name = _k8s_name()
    desired = _k8s_count()
    ready = _k8s_count()
    available = _k8s_count()
    updated = _k8s_count()
    misscheduled = _k8s_count()
    images = serializers.ListField(
        child=StrictPatternField(_IMAGE, max_length=_IMAGE_MAX),
        max_length=K8S_MAX_IMAGES,
    )
    helm = _k8s_name(allow_null=True)


class KubeHelmSerializer(StrictSerializer):
    name = _k8s_name()
    ns = _k8s_namespace()
    chart = StrictCharField(max_length=120, allow_null=True)
    app_version = StrictCharField(max_length=120, allow_null=True)
    source = StrictChoiceField(choices=["labels", "helmchart"])


class KubeArgoSerializer(StrictSerializer):
    name = _k8s_name()
    ns = _k8s_namespace()
    project = _k8s_name(allow_null=True)
    sync = StrictChoiceField(choices=SYNC_STATUSES)
    health = StrictChoiceField(choices=HEALTH_STATUSES)
    rev = StrictPatternField(_REVISION, max_length=7, allow_null=True)
    op = StrictChoiceField(choices=OPERATION_PHASES, allow_null=True)
    synced_at = StrictPatternField(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", max_length=20, allow_null=True
    )


class KubePvcSerializer(StrictSerializer):
    ns = _k8s_namespace()
    name = _k8s_name()
    phase = StrictChoiceField(choices=PVC_PHASES)
    # "class" is a Python keyword, so it is declared as `class_` and renamed below.
    class_ = _k8s_name(allow_null=True)
    size = StrictPatternField(_STORAGE, max_length=32, allow_null=True)
    age_s = _k8s_age(allow_null=True)

    def get_fields(self):
        fields = super().get_fields()
        fields["class"] = fields.pop("class_")
        return fields


class KubeEventSerializer(StrictSerializer):
    ns = _k8s_namespace()
    obj = _k8s_ref()
    reason = StrictPatternField(_REASON, max_length=64, allow_null=True)
    msg = StrictCharField(max_length=256, allow_blank=True)
    count = _k8s_count()
    age_s = _k8s_age(allow_null=True)


class ClusterViewSerializer(StrictSerializer):
    """The cluster read through the Kubernetes API: kube_cluster.collect_cluster_view()."""

    uid = StrictPatternField(_UUID, max_length=36, allow_null=True)
    version = _k8s_version(allow_null=True)
    api = KubeApiSerializer()
    sa = StrictPatternField(_SUBJECT, max_length=253, allow_null=True)
    token_days = StrictIntegerField(min_value=-100_000, max_value=1_000_000, allow_null=True)
    denied = KubeDeniedSerializer(many=True, max_length=K8S_MAX_DENIED)
    missing = serializers.ListField(child=StrictCharField(max_length=63), max_length=10)
    nodes = KubeClusterNodeSerializer(many=True, max_length=K8S_MAX_NODES)
    problem_pods = KubeProblemPodSerializer(many=True, max_length=K8S_MAX_PROBLEM_PODS)
    ok_pods = OkPodsField()
    pod_counts = KubePodCountsSerializer()
    workloads = KubeWorkloadSerializer(many=True, max_length=K8S_MAX_WORKLOADS)
    helm = KubeHelmSerializer(many=True, max_length=K8S_MAX_HELM)
    argo = KubeArgoSerializer(many=True, max_length=K8S_MAX_ARGO)
    argocd_version = StrictPatternField(_TAG_VERSION, max_length=255, allow_null=True)
    pvcs = KubePvcSerializer(many=True, max_length=K8S_MAX_PVCS)
    events = KubeEventSerializer(many=True, max_length=K8S_MAX_EVENTS)
    truncated = StrictBooleanField()
    cut = serializers.ListField(
        child=StrictPatternField(_CUT_NAME, max_length=32), max_length=K8S_MAX_CUT
    )
    # Lists the agent could not read in full: problems may be missing from them.
    incomplete = serializers.ListField(
        child=StrictPatternField(_CUT_NAME, max_length=32), max_length=K8S_MAX_CUT
    )


class KubernetesSerializer(StrictSerializer):
    """The report's `kubernetes` key: this host's node role and, on one node, the cluster."""

    node = KubeNodeSerializer()
    cluster = ClusterViewSerializer(allow_null=True)


class ReportSerializer(StrictSerializer):
    """The report built by agent/report.py: build_report()."""

    hostname = StrictCharField(max_length=253)
    agent_version = StrictCharField(max_length=32)
    timestamp = StrictCharField(max_length=20)
    updates = UpdatesSerializer(allow_null=True)
    reboot_required = StrictBooleanField(allow_null=True)
    containers = ContainerSerializer(many=True, allow_null=True, max_length=MAX_ITEMS)
    failed_units = _list(StrictCharField(max_length=MAX_NAME), allow_null=True)
    timers = TimerSerializer(many=True, allow_null=True, max_length=MAX_ITEMS)
    disks = DiskSerializer(many=True, allow_null=True, max_length=MAX_ITEMS)
    # Only agents with [kubernetes] enabled send it (agent 0.2.0 and later); null when
    # its collector crashed.
    kubernetes = KubernetesSerializer(required=False, allow_null=True)
    errors = ErrorSerializer(many=True, max_length=50)

    def validate_timestamp(self, value: str) -> str:
        """UTC with a 'Z', whole seconds: 2026-10-12T12:00:00Z (agent/report.py)."""
        if not _TIMESTAMP.fullmatch(value):
            raise serializers.ValidationError("Expected YYYY-MM-DDTHH:MM:SSZ.")
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise serializers.ValidationError("Not a real date and time.") from None
        return value


# --- messages ------------------------------------------------------------------


class HelloSerializer(StrictSerializer):
    type = StrictChoiceField(choices=["hello"])
    agent_version = StrictCharField(max_length=32)
    hostname = StrictCharField(max_length=253)
    os = StrictCharField(max_length=100)


class ReportMessageSerializer(StrictSerializer):
    type = StrictChoiceField(choices=["report"])
    report = ReportSerializer()
