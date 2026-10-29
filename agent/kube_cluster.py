"""The Kubernetes cluster view: API JSON in, a small report dict out.

- parse_* turn one API response into plain dicts/lists. They only ever copy fields
  from a fixed whitelist, never raise, and treat every value as untrusted text.
- collect_cluster_view() asks the API client for each resource and assembles the
  "cluster" part of the report, keeping problems first when a list is too long.

The token that reads these objects can see much more than we report (env values,
Helm values, Argo CD sources). That data is never copied: there is no code path
that reads `env`, `valuesContent`, `set`, `repo`, `spec.source*` or annotations
other than the Helm release name/namespace. fixtures/k8s has planted secrets and
the tests prove none of them comes out.
"""

import functools
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from agent import kube_api
from agent.kube_api import (
    ApiError,
    DeadlineExceeded,
    Denied,
    NotInstalled,
    Resource,
    Unauthorized,
)

# Caps per list. When a list is longer, problems are kept and healthy items dropped.
MAX_NODES = 50
MAX_PROBLEM_PODS = 100
MAX_OK_PODS = 400
MAX_OK_NAMESPACES = 100
MAX_WORKLOADS = 200
MAX_PVCS = 100
MAX_HELM = 100
MAX_ARGO = 100
MAX_EVENTS = 20
MAX_IMAGES = 10
MAX_DENIED = 20
MAX_MESSAGE = 256

# A restart count only matters if the last restart was recent: restartCount never
# goes down until the pod is recreated. Below RESTART_PROBLEM_MIN it is normal noise.
RECENT_SECONDS = 86_400
RESTART_PROBLEM_MIN = 3
MAX_AGE_SECONDS = 315_360_000  # 10 years
MAX_COUNT = 1_000_000

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_VERSION = re.compile(r"v\d+\.\d+\.\d+[0-9A-Za-z.+-]{0,40}")
_REASON = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_WORD = re.compile(r"[A-Za-z0-9._+-]{1,120}")
_ROLE = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")
_REVISION = re.compile(r"[A-Za-z0-9._-]{1,7}")
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]{0,254}(?:@sha256:[0-9a-f]{64})?")
_IMAGE_TAG = re.compile(r":(v?\d+\.\d+\.\d+[0-9A-Za-z.+-]*)$")
_SUBJECT = re.compile(r"[A-Za-z0-9:_.@-]{1,253}")
_STORAGE = re.compile(r"\d+(?:\.\d+)?(?:Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?")
_QUANTITY = re.compile(r"(\d+(?:\.\d+)?|\.\d+)(n|u|m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei)?")
_QUANTITY_FACTORS = {
    None: 1.0,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
    "Ki": 2.0**10,
    "Mi": 2.0**20,
    "Gi": 2.0**30,
    "Ti": 2.0**40,
    "Pi": 2.0**50,
    "Ei": 2.0**60,
}

POD_PHASES = frozenset({"Pending", "Running", "Succeeded", "Failed", "Unknown"})
PRESSURE_CONDITIONS = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")
# Waiting states that are a normal step of starting up, not a problem.
BENIGN_WAITING = frozenset({"ContainerCreating", "PodInitializing"})
SYNC_STATUSES = frozenset({"Synced", "OutOfSync", "Unknown"})
HEALTH_STATUSES = frozenset(
    {"Healthy", "Progressing", "Degraded", "Suspended", "Missing", "Unknown"}
)
OPERATION_PHASES = frozenset({"Running", "Terminating", "Failed", "Error", "Succeeded"})
PVC_PHASES = frozenset({"Pending", "Bound", "Lost"})
PHASE_COUNT_KEYS = ("total", "running", "pending", "succeeded", "failed", "problem")


def never_raises(default: Callable[[], Any]):
    """Last line of defence for parsers: whatever happens, return the empty result."""

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            # Deliberately broad: odd API data must never break the whole report.
            except Exception:  # noqa: BLE001
                return default()

        return wrapper

    return decorate


# --- tiny helpers: every value is untrusted ------------------------------------------


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _items(data: Any) -> list[dict]:
    return _dicts(_dict(data).get("items"))


def _text(value: Any, limit: int) -> str | None:
    """A single line of text, control characters replaced, cut to `limit`. None if empty."""
    if not isinstance(value, str):
        return None
    text = _CONTROL_CHARS.sub(" ", value[: limit * 4]).strip()[:limit]
    return text or None


def _name(value: Any) -> str | None:
    return _text(value, 253)


def _match(pattern: re.Pattern, value: Any) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _int(value: Any, maximum: int = MAX_COUNT) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, maximum))


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _age(value: Any, now: datetime) -> int | None:
    stamp = _timestamp(value)
    if stamp is None:
        return None
    return max(0, min(int(now.timestamp() - stamp), MAX_AGE_SECONDS))


def parse_quantity(value: Any) -> float | None:
    """A Kubernetes quantity ("250m", "470300Ki", "1.5Gi") as a plain number, or None."""
    match = _QUANTITY.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    return float(match[1]) * _QUANTITY_FACTORS[match[2]]


def _millicores(value: Any) -> int | None:
    cores = parse_quantity(value)
    return None if cores is None else round(cores * 1000)


def _mebibytes(value: Any) -> int | None:
    size = parse_quantity(value)
    return None if size is None else round(size / 2**20)


# --- cluster identity and version -----------------------------------------------------


@never_raises(lambda: None)
def parse_namespace_uid(data: Any) -> str | None:
    """The kube-system namespace's UID: it identifies the cluster."""
    return _match(_UUID, _dict(_dict(data).get("metadata")).get("uid"))


@never_raises(lambda: None)
def parse_version(data: Any) -> str | None:
    return _match(_VERSION, _dict(data).get("gitVersion"))


# --- nodes -----------------------------------------------------------------------------


@never_raises(dict)
def parse_node_metrics(data: Any) -> dict[str, dict]:
    """node name -> current usage, from metrics.k8s.io (a snapshot, no history)."""
    usage = {}
    for item in _items(data):
        name = _name(_dict(item.get("metadata")).get("name"))
        if name is None:
            continue
        current = _dict(item.get("usage"))
        usage[name] = {
            "cpu_used_m": _millicores(current.get("cpu")),
            "mem_used_mi": _mebibytes(current.get("memory")),
        }
    return usage


@never_raises(list)
def parse_nodes(data: Any, metrics: dict[str, dict] | None = None) -> list[dict]:
    """One dict per node. Nodes that are not ready or under pressure come first."""
    metrics = metrics if isinstance(metrics, dict) else {}
    nodes = []
    for item in _items(data):
        meta = _dict(item.get("metadata"))
        name = _name(meta.get("name"))
        if name is None:
            continue
        status = _dict(item.get("status"))
        conditions = {
            c.get("type"): c.get("status") == "True" for c in _dicts(status.get("conditions"))
        }
        info = _dict(status.get("nodeInfo"))
        capacity = _dict(status.get("capacity"))
        used = _dict(metrics.get(name))
        prefix = "node-role.kubernetes.io/"
        roles = sorted(
            {
                role
                for label in _dict(meta.get("labels"))
                if isinstance(label, str)
                and label.startswith(prefix)
                and (role := _match(_ROLE, label[len(prefix) :]))
            }
        )[:5]
        nodes.append(
            {
                "name": name,
                "roles": roles,
                "ready": conditions.get("Ready") is True,
                "pressure": [c for c in PRESSURE_CONDITIONS if conditions.get(c)],
                "kubelet": _match(_VERSION, info.get("kubeletVersion")),
                "os": _text(info.get("operatingSystem"), 32),
                "arch": _text(info.get("architecture"), 32),
                "cpu_m": _millicores(capacity.get("cpu")),
                "mem_mi": _mebibytes(capacity.get("memory")),
                "cpu_used_m": used.get("cpu_used_m"),
                "mem_used_mi": used.get("mem_used_mi"),
            }
        )
    nodes.sort(key=lambda n: (n["ready"] and not n["pressure"], n["name"]))
    return nodes


# --- pods ------------------------------------------------------------------------------


@never_raises(lambda: "Pod/unknown")
def owner_of(pod: Any) -> str:
    """ "Kind/name" of what runs this pod. Pods of a Deployment give the Deployment name.

    A pod's own name changes on every rollout; the owner stays, so findings keyed on
    it (and "this is expected" decisions) survive a restart.
    """
    meta = _dict(_dict(pod).get("metadata"))
    for ref in _dicts(meta.get("ownerReferences")):
        kind, name = _match(_WORD, ref.get("kind")), _name(ref.get("name"))
        if not kind or not name:
            continue
        if kind == "ReplicaSet":
            pod_hash = _dict(meta.get("labels")).get("pod-template-hash")
            if isinstance(pod_hash, str) and pod_hash and name.endswith(f"-{pod_hash}"):
                return f"Deployment/{name[: -len(pod_hash) - 1]}"
        return f"{kind}/{name}"
    return f"Pod/{_name(meta.get('name')) or 'unknown'}"


def _container_statuses(status: dict) -> tuple[list[dict], list[dict]]:
    return _dicts(status.get("initContainerStatuses")), _dicts(status.get("containerStatuses"))


def _pod_reason(status: dict, phase: str) -> str | None:
    init, regular = _container_statuses(status)
    for container in init + regular:
        waiting = _dict(_dict(container.get("state")).get("waiting"))
        reason = _match(_REASON, waiting.get("reason"))
        if reason and reason not in BENIGN_WAITING:
            return reason
    reason = _match(_REASON, status.get("reason"))  # "Evicted", "NodeLost" ...
    if reason:
        return reason
    if phase == "Pending":
        for condition in _dicts(status.get("conditions")):
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                return _match(_REASON, condition.get("reason"))
    return None


def _last_termination(status: dict, now: datetime) -> tuple[str | None, int | None]:
    """Reason and age of the newest previous termination of any container."""
    init, regular = _container_statuses(status)
    best: tuple[float, str | None, int | None] | None = None
    for container in init + regular:
        terminated = _dict(_dict(container.get("lastState")).get("terminated"))
        if not terminated:
            continue
        finished = terminated.get("finishedAt")
        candidate = (
            _timestamp(finished) or 0.0,
            _match(_REASON, terminated.get("reason")),
            _age(finished, now),
        )
        if best is None or candidate[0] > best[0]:
            best = candidate
    return (best[1], best[2]) if best else (None, None)


@never_raises(list)
def parse_pods(data: Any, now: datetime) -> list[dict]:
    """A compact dict per pod: where, state, readiness, restarts and why it is unhappy."""
    pods = []
    for item in _items(data):
        meta = _dict(item.get("metadata"))
        name = _name(meta.get("name"))
        if name is None:
            continue
        spec, status = _dict(item.get("spec")), _dict(item.get("status"))
        phase = status.get("phase") if status.get("phase") in POD_PHASES else "Unknown"
        init, regular = _container_statuses(status)
        total = len(_dicts(spec.get("containers"))) or len(regular)
        last, last_age = _last_termination(status, now)
        pods.append(
            {
                "ns": _name(meta.get("namespace")) or "unknown",
                "name": name,
                "node": _name(spec.get("nodeName")),
                "phase": phase,
                "ready": [sum(1 for c in regular if c.get("ready") is True), total],
                "restarts": min(
                    sum(_int(c.get("restartCount")) for c in init + regular), MAX_COUNT
                ),
                "reason": _pod_reason(status, phase),
                "last": last,
                "last_age_s": last_age,
                "age_s": _age(meta.get("creationTimestamp"), now),
                "owner": owner_of(item),
            }
        )
    return pods


def _recent(age: int | None) -> bool:
    return age is not None and age < RECENT_SECONDS


def is_problem_pod(pod: dict) -> bool:
    """Whether a pod deserves a place in the (capped) list of problem pods."""
    if pod["phase"] in ("Failed", "Unknown", "Pending") or pod["reason"]:
        return True
    ready, total = pod["ready"]
    if pod["phase"] == "Running" and ready < total:
        return True
    if pod["last"] == "OOMKilled" and _recent(pod["last_age_s"]):
        return True
    return pod["restarts"] >= RESTART_PROBLEM_MIN and _recent(pod["last_age_s"])


def _severity(pod: dict) -> int:
    if pod["phase"] in ("Failed", "Unknown"):
        return 2
    return 3 if pod["reason"] else 1


@never_raises(lambda: ([], {}, dict.fromkeys(PHASE_COUNT_KEYS, 0)))
def split_pods(pods: Any, max_ok: int = MAX_OK_PODS) -> tuple[list[dict], dict, dict]:
    """(problem pods worst first, healthy pods by namespace, counts).

    Healthy pods are [name, node, restarts, age_s] lists, at most `max_ok` of them in at
    most MAX_OK_NAMESPACES namespaces. Counts always cover every pod.
    """
    pods = _dicts(pods)
    counts = {"total": len(pods), "running": 0, "pending": 0, "succeeded": 0, "failed": 0}
    problems, healthy = [], []
    for pod in pods:
        key = str(pod.get("phase", "")).lower()
        if key in counts:
            counts[key] += 1
        (problems if is_problem_pod(pod) else healthy).append(pod)
    counts["problem"] = len(problems)
    problems.sort(key=lambda p: (-_severity(p), -p["restarts"], p["ns"], p["name"]))

    ok: dict[str, list] = {}
    kept = 0
    for pod in sorted(healthy, key=lambda p: (p["ns"], p["name"])):
        if kept >= max_ok or (pod["ns"] not in ok and len(ok) >= MAX_OK_NAMESPACES):
            continue
        ok.setdefault(pod["ns"], []).append(
            [pod["name"], pod["node"], pod["restarts"], pod["age_s"]]
        )
        kept += 1
    return problems, ok, counts


# --- workloads, Helm, Argo CD ------------------------------------------------------------


def _images(spec: dict) -> list[str]:
    containers = _dicts(_dict(_dict(spec.get("template")).get("spec")).get("containers"))
    images: list[str] = []
    for container in containers:
        image = _match(_IMAGE, container.get("image"))
        if image and image not in images:
            images.append(image)
    return images[:MAX_IMAGES]


def _helm_release(meta: dict) -> str | None:
    """The Helm release a workload belongs to (annotation first, then the instance label)."""
    annotations, labels = _dict(meta.get("annotations")), _dict(meta.get("labels"))
    release = _name(annotations.get("meta.helm.sh/release-name"))
    if release is None and labels.get("app.kubernetes.io/managed-by") == "Helm":
        release = _name(labels.get("app.kubernetes.io/instance"))
    return release


WORKLOAD_COUNTS = {
    # kind: (desired, ready, available, updated) as (source, key) pairs
    "Deployment": (("spec", "replicas"), "readyReplicas", "availableReplicas", "updatedReplicas"),
    "StatefulSet": (("spec", "replicas"), "readyReplicas", "availableReplicas", "updatedReplicas"),
    "DaemonSet": (
        ("status", "desiredNumberScheduled"),
        "numberReady",
        "numberAvailable",
        "updatedNumberScheduled",
    ),
}


@never_raises(list)
def parse_workloads(kind: str, data: Any) -> list[dict]:
    """Deployments, StatefulSets or DaemonSets: desired/ready/available counts and images."""
    if kind not in WORKLOAD_COUNTS:
        return []
    (desired_from, desired_key), ready_key, available_key, updated_key = WORKLOAD_COUNTS[kind]
    workloads = []
    for item in _items(data):
        meta = _dict(item.get("metadata"))
        name = _name(meta.get("name"))
        if name is None:
            continue
        spec, status = _dict(item.get("spec")), _dict(item.get("status"))
        desired_source = spec if desired_from == "spec" else status
        # An absent spec.replicas means 1; an absent status count means 0.
        desired = _int(desired_source.get(desired_key, 1 if kind != "DaemonSet" else 0))
        workloads.append(
            {
                "kind": kind,
                "ns": _name(meta.get("namespace")) or "unknown",
                "name": name,
                "desired": desired,
                "ready": _int(status.get(ready_key)),
                "available": _int(status.get(available_key)),
                "updated": _int(status.get(updated_key)),
                "misscheduled": _int(status.get("numberMisscheduled")),
                "images": _images(spec),
                "helm": _helm_release(meta),
            }
        )
    return workloads


def is_unhealthy_workload(workload: dict) -> bool:
    return workload["available"] < workload["desired"] or workload["misscheduled"] > 0


@never_raises(list)
def helm_from_workloads(items: Any) -> list[dict]:
    """Helm releases seen in workload metadata (labels and annotations, no cluster secrets)."""
    releases: dict[tuple[str, str], dict] = {}
    for item in _dicts(items):
        meta = _dict(item.get("metadata"))
        name = _helm_release(meta)
        if name is None:
            continue
        annotations, labels = _dict(meta.get("annotations")), _dict(meta.get("labels"))
        ns = (
            _name(annotations.get("meta.helm.sh/release-namespace"))
            or _name(meta.get("namespace"))
            or "unknown"
        )
        entry = releases.setdefault(
            (ns, name),
            {"name": name, "ns": ns, "chart": None, "app_version": None, "source": "labels"},
        )
        entry["chart"] = entry["chart"] or _match(_WORD, labels.get("helm.sh/chart"))
        entry["app_version"] = entry["app_version"] or _match(
            _WORD, labels.get("app.kubernetes.io/version")
        )
    return [releases[key] for key in sorted(releases)]


def _chart_name(spec: dict) -> str | None:
    """ "traefik-39.0.701+up39.0.7" from a chart URL or name. Never the repo or values."""
    chart = spec.get("chart")
    if not isinstance(chart, str):
        return None
    base = chart.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1].removesuffix(".tgz")
    base = _match(_WORD, base)
    version = _match(_WORD, spec.get("version"))
    if base and version and version not in base:
        base = f"{base}-{version}"
    return base


@never_raises(list)
def parse_helmcharts(data: Any) -> list[dict]:
    """k3s HelmChart resources: name, namespace and chart only (never values, set or repo)."""
    charts = []
    for item in _items(data):
        meta, spec = _dict(item.get("metadata")), _dict(item.get("spec"))
        name = _name(meta.get("name"))
        if name is None:
            continue
        ns = _name(spec.get("targetNamespace")) or _name(meta.get("namespace")) or "unknown"
        charts.append(
            {
                "name": name,
                "ns": ns,
                "chart": _chart_name(spec),
                "app_version": None,
                "source": "helmchart",
            }
        )
    return charts


@never_raises(list)
def parse_argo_apps(data: Any) -> list[dict]:
    """Argo CD Applications: sync and health state. Never spec.source (repo, Helm values)."""
    apps = []
    for item in _items(data):
        meta = _dict(item.get("metadata"))
        name = _name(meta.get("name"))
        if name is None:
            continue
        status = _dict(item.get("status"))
        sync = _dict(status.get("sync"))
        operation = _dict(status.get("operationState"))
        sync_status = _dict(sync).get("status")
        health = _dict(status.get("health")).get("status")
        phase = operation.get("phase")
        revision = sync.get("revision")
        apps.append(
            {
                "name": name,
                "ns": _name(meta.get("namespace")) or "unknown",
                "project": _name(_dict(item.get("spec")).get("project")),
                "sync": sync_status if sync_status in SYNC_STATUSES else "Unknown",
                "health": health if health in HEALTH_STATUSES else "Unknown",
                "rev": _match(_REVISION, revision[:7]) if isinstance(revision, str) else None,
                "op": phase if phase in OPERATION_PHASES else None,
                "synced_at": _match(_TIMESTAMP, operation.get("finishedAt")),
            }
        )
    return apps


@never_raises(lambda: None)
def argocd_version(workloads: Any) -> str | None:
    """The Argo CD version, from the image tag of the argocd-server Deployment."""
    for workload in _dicts(workloads):
        if (
            workload.get("name") != "argocd-server"
            or workload.get("kind", "Deployment") != "Deployment"
        ):
            continue
        for image in workload.get("images") or []:
            match = _IMAGE_TAG.search(image.split("@")[0]) if isinstance(image, str) else None
            if match:
                return match[1]
    return None


# --- storage and events --------------------------------------------------------------------


@never_raises(list)
def parse_pvcs(data: Any, now: datetime) -> list[dict]:
    """PersistentVolumeClaims, anything not Bound first."""
    claims = []
    for item in _items(data):
        meta, spec, status = (_dict(item.get(k)) for k in ("metadata", "spec", "status"))
        name = _name(meta.get("name"))
        if name is None:
            continue
        size = _dict(status.get("capacity")).get("storage")
        if size is None:
            size = _dict(_dict(spec.get("resources")).get("requests")).get("storage")
        phase = status.get("phase")
        claims.append(
            {
                "ns": _name(meta.get("namespace")) or "unknown",
                "name": name,
                "phase": phase if phase in PVC_PHASES else "Unknown",
                "class": _name(spec.get("storageClassName")),
                "size": _match(_STORAGE, size),
                "age_s": _age(meta.get("creationTimestamp"), now),
            }
        )
    claims.sort(key=lambda c: (c["phase"] == "Bound", c["ns"], c["name"]))
    return claims


def _event_time(event: dict) -> Any:
    series = _dict(event.get("series"))
    for value in (
        event.get("lastTimestamp"),
        series.get("lastObservedTime"),
        event.get("eventTime"),
        event.get("firstTimestamp"),
        _dict(event.get("metadata")).get("creationTimestamp"),
    ):
        if _timestamp(value) is not None:
            return value
    return None


@never_raises(list)
def parse_warning_events(data: Any, now: datetime) -> list[dict]:
    """The newest warning events, with the message cut to MAX_MESSAGE characters."""
    events = []
    for item in _items(data):
        if item.get("type") != "Warning":
            continue
        meta, target = _dict(item.get("metadata")), _dict(item.get("involvedObject"))
        kind, name = _match(_WORD, target.get("kind")), _name(target.get("name"))
        if not kind or not name:
            continue
        count = _int(item.get("count")) or _int(_dict(item.get("series")).get("count")) or 1
        stamp = _event_time(item)
        events.append(
            {
                "ns": _name(meta.get("namespace")) or _name(target.get("namespace")) or "unknown",
                "obj": f"{kind}/{name}",
                "reason": _match(_REASON, item.get("reason")),
                "msg": _text(item.get("message"), MAX_MESSAGE) or "",
                "count": count,
                "age_s": _age(stamp, now),
                "_at": _timestamp(stamp) or 0.0,
            }
        )
    events.sort(key=lambda e: -e["_at"])
    for event in events:
        del event["_at"]
    return events[:MAX_EVENTS]


# --- putting the view together ---------------------------------------------------------------


class _Run:
    """One collection: asks the client for each resource and remembers what went wrong."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.denied: list[dict] = []
        self.missing: set[str] = set()
        self.cut: list[str] = []
        self.error: str | None = None
        self.reached = False  # did the API answer at all (even with 403/404)?

    def mark_cut(self, name: str) -> None:
        if name not in self.cut:
            self.cut.append(name)

    def fetch(self, resource: Resource, name: str, *, listing: bool = True) -> Any:
        """The response body, or None if it could not be read (the reason is remembered)."""
        if self.error:
            return None
        try:
            if listing:
                items, truncated = self.client.list_all(resource.path, resource.query)
                if truncated:
                    self.mark_cut(name)
                body: Any = {"items": items}
            else:
                body = self.client.get_json(resource.path, resource.query)
            self.reached = True
            return body
        except Denied as exc:
            self.reached = True
            entry = {"g": exc.group, "r": exc.resource, "v": exc.verb}
            if entry not in self.denied and len(self.denied) < MAX_DENIED:
                self.denied.append(entry)
        except NotInstalled:
            self.reached = True
            if resource.optional:
                self.missing.add(resource.group)
            else:
                self.mark_cut(name)
        except DeadlineExceeded:
            self.mark_cut(name)
        except Unauthorized:
            self.error = "unauthorized"
        except ApiError as exc:
            # Refused or untrusted TLS never gets better; a timeout or bad answer before
            # anything worked means the API isn't usable. Later on it only costs one list.
            if exc.kind in ("refused", "tls") or not self.reached:
                self.error = exc.kind
            else:
                self.mark_cut(name)
        return None


def _cap(items: list, limit: int, name: str, run: _Run) -> list:
    if len(items) > limit:
        run.mark_cut(name)
    return items[:limit]


def _token_days(exp: Any, now: datetime) -> int | None:
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return None
    return int((exp - now.timestamp()) // 86_400)


def collect_cluster_view(client: Any, now: datetime) -> dict:
    """The "cluster" part of the report. Problems come first when a list has to be cut.

    `truncated` is true when a list was cut or could not be read completely, and `cut`
    names those lists. It never raises for API trouble: that shows up in api.error.
    """
    run = _Run(client)
    sub, exp = None, None
    try:
        client.start()  # reads the token file again, so a rotated token works
        details = client.token_details()
        sub, exp = details.get("sub"), details.get("exp")
    except Unauthorized:
        run.error = "unauthorized"

    ns_body = run.fetch(kube_api.NAMESPACE, "namespace", listing=False)
    version_body = run.fetch(kube_api.VERSION, "version", listing=False)
    nodes_body = run.fetch(kube_api.NODES, "nodes")
    metrics_body = run.fetch(kube_api.NODE_METRICS, "node_metrics")
    pods_body = run.fetch(kube_api.PODS, "pods")
    deployments_body = run.fetch(kube_api.DEPLOYMENTS, "workloads")
    statefulsets_body = run.fetch(kube_api.STATEFULSETS, "workloads")
    daemonsets_body = run.fetch(kube_api.DAEMONSETS, "workloads")
    pvcs_body = run.fetch(kube_api.PVCS, "pvcs")
    events_body = run.fetch(kube_api.EVENTS, "events")
    charts_body = run.fetch(kube_api.HELMCHARTS, "helm")
    apps_body = run.fetch(kube_api.APPLICATIONS, "argo")

    nodes = parse_nodes(nodes_body, parse_node_metrics(metrics_body))
    problems, ok_pods, counts = split_pods(parse_pods(pods_body, now))
    kept_ok = sum(len(v) for v in ok_pods.values())
    if kept_ok < counts["total"] - counts["problem"]:
        run.mark_cut("ok_pods")

    workloads = (
        parse_workloads("Deployment", deployments_body)
        + parse_workloads("StatefulSet", statefulsets_body)
        + parse_workloads("DaemonSet", daemonsets_body)
    )
    argocd = argocd_version(workloads)
    workloads.sort(key=lambda w: (not is_unhealthy_workload(w), w["ns"], w["kind"], w["name"]))

    raw_workloads = [
        i for body in (deployments_body, statefulsets_body, daemonsets_body) for i in _items(body)
    ]
    helm = helm_from_workloads(raw_workloads)
    known = {release["name"] for release in helm}
    helm += [chart for chart in parse_helmcharts(charts_body) if chart["name"] not in known]
    helm.sort(key=lambda h: (h["ns"], h["name"]))

    apps = parse_argo_apps(apps_body)
    apps.sort(
        key=lambda a: (a["sync"] == "Synced" and a["health"] == "Healthy", a["ns"], a["name"])
    )

    view = {
        "uid": parse_namespace_uid(ns_body),
        "version": parse_version(version_body),
        "api": {"error": run.error, "cert_days": client.api_cert_days()},
        "sa": _match(_SUBJECT, sub),
        "token_days": _token_days(exp, now),
        "denied": run.denied,
        "missing": sorted(run.missing),
        "nodes": _cap(nodes, MAX_NODES, "nodes", run),
        "problem_pods": _cap(problems, MAX_PROBLEM_PODS, "problem_pods", run),
        "ok_pods": ok_pods,
        "pod_counts": counts,
        "workloads": _cap(workloads, MAX_WORKLOADS, "workloads", run),
        "helm": _cap(helm, MAX_HELM, "helm", run),
        "argo": _cap(apps, MAX_ARGO, "argo", run),
        "argocd_version": argocd,
        "pvcs": _cap(parse_pvcs(pvcs_body, now), MAX_PVCS, "pvcs", run),
        "events": parse_warning_events(events_body, now),
    }
    view["truncated"] = bool(run.cut)
    view["cut"] = run.cut
    return view


# --- shrinking a view that is too big for one message ---------------------------------------------

SHRINK_STEPS = 5


def shrink_cluster_view(view: dict, step: int) -> dict | None:
    """A copy of `view` with the next least important data removed, or None if step 1-5 has
    nothing to remove. Steps (each keeps what the earlier ones kept):

    1 healthy pods, 2 healthy workloads, 3 events, 4 workloads/Helm/Argo/PVCs entirely,
    5 nodes and problem pods (only identity, errors and counts remain).
    """
    shrunk = dict(view)
    cut = list(view.get("cut") or [])

    def drop(key: str, empty: Any) -> None:
        if view.get(key):
            shrunk[key] = empty
            if key not in cut:
                cut.append(key)

    if step == 1:
        drop("ok_pods", {})
    elif step == 2:
        workloads = _dicts(view.get("workloads"))
        unhealthy = [w for w in workloads if is_unhealthy_workload(w)]
        if len(unhealthy) < len(workloads):
            shrunk["workloads"] = unhealthy
            if "workloads" not in cut:
                cut.append("workloads")
    elif step == 3:
        drop("events", [])
    elif step == 4:
        for key in ("workloads", "helm", "argo", "pvcs"):
            drop(key, [])
    elif step == 5:
        for key in ("nodes", "problem_pods"):
            drop(key, [])
    if len(cut) == len(view.get("cut") or []) and shrunk == view:
        return None
    shrunk["truncated"] = True
    shrunk["cut"] = cut
    return shrunk
