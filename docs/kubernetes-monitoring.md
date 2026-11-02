# Monitoring Kubernetes with CronRack

CronRack answers "what needs my attention?" for a Kubernetes cluster too: node health,
crashing or stuck pods, apps with too few copies, Argo CD apps out of sync, an expiring API
certificate, outdated versions. Each problem comes with a plain "why" and a read-only
command you can copy. CronRack never runs anything in your cluster.

It is built k3s-first (tested on a two-node k3s cluster: one server node, one worker node
on the same LAN) and works on other Kubernetes distributions where that is easy.

This page has no YAML on purpose: it describes exactly what the agent needs, so you can
write the ServiceAccount, ClusterRole and binding yourself.

---

## 1. How it works

There are two parts, and both are **opt-in**. Without them, an agent behaves exactly as
before, and a host without Kubernetes keeps working unchanged.

**Role detection, on every node (`--k8s`).** The agent looks at the host itself, with no
cluster access and no token:

- Is the `k3s` service active? Then the host is a **k3s server**.
- Is `k3s-agent` active? Then it is a **k3s worker** (k3s calls it an "agent").
- Is `kubelet` running? Then it is a node of another Kubernetes distribution.
- It also reports `k3s --version` (or `kubelet --version`), the node name (the host name,
  lowercased), and `helm version --short` if Helm is installed.

The overview then shows a small "k8s server" or "k8s worker" label on the host's card, and
the host page gets a **Kubernetes** panel.

**The cluster view, on ONE node (`--k8s-cluster-view`).** One agent, normally on a server
node, also reads the cluster through the Kubernetes API:

- **Read-only.** It only sends GET requests: `list`, plus one `get`. It never writes,
  never uses `watch`, never follows redirects.
- **Its own, minimal credentials.** It uses a ServiceAccount token with exactly the rights
  in section 3. It **never** reads the admin kubeconfig (`/etc/rancher/k3s/k3s.yaml`); the
  agent refuses any token file under `/etc/rancher/`.
- **Outbound only.** It talks to the API server (by default `https://127.0.0.1:6443` on
  the same node) and to your CronRack server, like every agent. It still listens on no port.
- **Bounded.** Every request has a timeout, and one collection must finish within 30
  seconds. Lists are fetched in pages of 250 items (50 if a page is too big), each page at
  most 8 MiB, and the whole report stays below the server's WebSocket limit (256 KiB).

The server matches each Kubernetes node to a CronRack host **by name**: node name = host
name (first part, lowercased). A node without a matching host is listed as "without a
CronRack host". Only your own hosts are ever matched.

```
  server node                                   worker node
  ┌──────────────────────────────┐              ┌──────────────────────────┐
  │ cronrack-agent  --k8s        │              │ cronrack-agent  --k8s    │
  │   + cluster view ──GET──► API server :6443  │   (role only, no token)  │
  └──────────────┬───────────────┘              └────────────┬─────────────┘
                 └──────────── wss (outbound) ───► CronRack ◄─┘
```

## 2. What is collected, and what never is

**Collected** (only these fields; everything else in the API answers is dropped while
parsing):

| Area | Fields |
|---|---|
| Cluster | the `kube-system` namespace UID (a stable cluster ID), the Kubernetes version, the API server certificate's expiry (read from the TLS handshake) |
| Nodes | name, roles (from labels), Ready, Memory/Disk/PID pressure, kubelet version, OS/arch, allocatable CPU and memory, current usage from `metrics.k8s.io` if available |
| Pods | namespace, name, node, phase, ready containers, restarts, waiting reason (CrashLoopBackOff, ImagePullBackOff, CreateContainerConfigError, …), last termination reason (OOMKilled), age, the owning workload. Problem pods first. |
| Workloads | Deployments, StatefulSets, DaemonSets: desired / ready / available / updated, image references |
| Volume claims | phase, storage class, size |
| Events | the 20 newest **Warning** events, message cut to 256 characters |
| Helm | releases derived from labels and annotations on workloads (`app.kubernetes.io/managed-by=Helm`, `meta.helm.sh/release-name`, `helm.sh/chart`, `app.kubernetes.io/version`), plus k3s HelmChart objects (name, namespace, chart) |
| Argo CD | per Application: name, namespace, project, sync status, health, short revision, operation phase, last sync time. The Argo CD version from the `argocd-server` image tag. |
| Permissions | for a refused request: which resource and verb was denied (never the response body) |

**Never collected:** Secrets, ConfigMaps, environment variable values, container logs,
`exec`, Helm values (`valuesContent`, `set`, `spec.source.helm`), Argo CD sources,
parameters or repository credentials, annotations other than the Helm ones above, and the
admin kubeconfig. The tests plant marker strings in exactly these places and check that
they never reach a report.

Everything the cluster sends is treated as untrusted: the server checks every field
(Kubernetes names must be valid DNS names, control characters are refused, lengths and
list sizes are capped), and every page shows it as plain text.

## 3. The RBAC the agent needs

Create, in a namespace of your choice (for example `monitoring`):

1. a **ServiceAccount** for the agent, for example `cronrack-agent`;
2. a **ClusterRole** with exactly the rules below. It has to be a *Cluster*Role, because
   nodes are not namespaced and the agent lists pods in every namespace;
3. a **ClusterRoleBinding** that binds that ClusterRole to that ServiceAccount;
4. a **token** for the ServiceAccount (section 3.2).

### 3.1 The rules

| apiGroup | resources | verbs | Notes |
|---|---|---|---|
| `""` (core) | `namespaces` | `get` | restrict it with `resourceNames: ["kube-system"]`: it is only read for the cluster UID |
| `""` (core) | `nodes` | `list` | |
| `""` (core) | `pods` | `list` | all namespaces |
| `""` (core) | `persistentvolumeclaims` | `list` | |
| `""` (core) | `events` | `list` | the agent asks only for `type=Warning` |
| `apps` | `deployments`, `statefulsets`, `daemonsets` | `list` | Helm releases and the Argo CD version are read from these too |
| `metrics.k8s.io` | `nodes` | `list` | optional: CPU/memory usage (k3s ships metrics-server) |
| `helm.cattle.io` | `helmcharts` | `list` | optional: k3s HelmChart objects |
| `argoproj.io` | `applications` | `list` | optional: Argo CD apps |

That is all. In particular:

- **No `watch`**, and no `get` other than the one namespace. The agent re-lists on every
  report.
- **No `secrets`, `configmaps`, `pods/log`, `pods/exec` or `serviceaccounts/token`**, no
  write verb, no `*`.
- **Don't bind the built-in `view` role** (it can read far more than needed), and never
  `admin` or `cluster-admin`.
- **No rule for `/version` or API discovery is needed.** Kubernetes' default
  `system:public-info-viewer` and `system:discovery` bindings already allow them. Only if
  someone removed those defaults, add a `nonResourceURLs` rule for `/version` with `get`.
- **An optional API that isn't installed is fine.** If Argo CD, the HelmChart CRD or
  metrics-server is missing, the agent simply notes it as "not installed".

RBAC can't hide fields: a token that can list pods can read their env values. Prefer
Secrets via secretKeyRef/envFrom over plain env values, and treat this token as sensitive.
(The same goes for HelmChart `valuesContent` and Argo CD Application specs: the token can
read them, even though the agent throws them away.)

### 3.2 Which token

- **A short-lived token (recommended):** `kubectl create token <serviceaccount> -n
  <namespace> --duration=8760h` gives a token valid for up to a year. It is not stored in
  the cluster. The agent reads its expiry and CronRack warns **14 days before** it runs
  out (rule `k8s.token_expiring`), so it never stops silently. The API server may cap the
  duration (its `--service-account-max-token-expiration` setting); `--check-k8s` shows how
  many days the token you got really has. To renew: create a new token and run the
  installer again (section 4).
- **A long-lived token Secret** (a Secret of type `kubernetes.io/service-account-token`
  annotated with the ServiceAccount's name): it never expires and lives in etcd until you
  delete it. Convenient, but easy to forget, and anyone who can read Secrets in that
  namespace can take it. If you use one, keep it in its own namespace that only admins can
  read.

## 4. Installing

On **every** node you want to see as a Kubernetes node, add `--k8s` to the usual install
command. It asks nothing and needs no token:

```bash
curl -fsSL https://<cronrack-server>/agent/install.sh | sudo bash -s -- --server https://<cronrack-server> --k8s
```

(If your CronRack server uses its own CA, add `--cacert` and `--ca-file` as in
[DEPLOY.md](DEPLOY.md#part-2-an-agent-on-a-host). Run the install command again with
`--k8s` on a host that already has the agent: it keeps the rest of its settings. Use the
normal install command for this, not `--upgrade`: `--upgrade` never changes settings and
refuses the `--k8s` options.)

On **one** server node, use `--k8s-cluster-view` instead (it implies `--k8s`):

```bash
curl -fsSL https://<cronrack-server>/agent/install.sh | sudo bash -s -- --server https://<cronrack-server> --k8s-cluster-view
```

It then:

1. asks for the ServiceAccount token (typed or pasted, **not shown**, never printed or
   logged) and checks that it looks like a token;
2. writes it to `/etc/cronrack/k8s-token` as `root:cronrack`, mode `0640` (the file is
   created with these rights *before* the token is written);
3. copies the cluster's CA certificate to `/etc/cronrack/k8s-ca.crt` (it refuses a file
   that contains a private key);
4. writes a `[kubernetes]` section into `/etc/cronrack/agent.toml`:
   `enabled = true`, `cluster_view = true`, `api_server`, `token_file`, `ca_file`.

Run the install command with `--k8s-cluster-view` again to rotate the token: it asks
"Replace it? [y/N]". Upgrading the agent with `--upgrade` (see
[DEPLOY.md, 2.5](DEPLOY.md#25-upgrading-an-agent)) keeps the token, the CA copy and every
`[kubernetes]` setting as they are, and asks nothing. Other flags:

| Flag | What it does |
|---|---|
| `--k8s` | role detection only (every node) |
| `--k8s-cluster-view` | role detection + the cluster view (one node) |
| `--k8s-ca PATH` | the CA certificate to copy (default: the k3s server CA, below) |
| `--k8s-api URL` | the API server, `https://` only (default `https://127.0.0.1:6443`) |

**Where k3s keeps its CA:** on a **server** node,
`/var/lib/rancher/k3s/server/tls/server-ca.crt`; on a worker node,
`/var/lib/rancher/k3s/agent/server-ca.crt`. Copy only the `.crt`, never a `.key` file.
On other distributions, the CA is usually `/etc/kubernetes/pki/ca.crt` on a control-plane
node; pass it with `--k8s-ca`.

Don't start k3s with `--write-kubeconfig-mode 644` to make things "easier": that makes the
admin kubeconfig readable by every user on the host. The agent doesn't need it and refuses
to use it.

## 5. Checking that it works

First ask Kubernetes what the ServiceAccount may do (as an admin, on any machine with
`kubectl`):

```bash
kubectl auth can-i --list --as=system:serviceaccount:<namespace>:<serviceaccount>
kubectl auth can-i get secrets -A --as=system:serviceaccount:<namespace>:<serviceaccount>
```

The first should show exactly the rules from section 3.1. The second **must** answer `no`.

Then ask the agent itself, on the cluster-view node:

```bash
cd /opt/cronrack-agent/app && sudo -u cronrack ../venv/bin/python -m agent --check-k8s --config /etc/cronrack/agent.toml
```

It tries each API from section 3.1 once (with `limit=1`) and prints one line per API:
`ok`, `denied (403)` (that rule is missing from your ClusterRole), `not installed (404)`
(an optional API that isn't there, which is fine) or `unauthorized (401)` (the token is
wrong or expired). It also prints whether the API answered, how many days the API
certificate and the token have left, and which ServiceAccount the token belongs to. It
never prints the token or any cluster data. Exit code 0 means everything is fine, 3
means something is denied or unreachable, 2 means the agent isn't configured for it.

In CronRack, a refused request shows up as a yellow finding whose fix names the exact
rule to add and a command to check it, for example
`# The ClusterRole needs: apiGroups [''], resources [pods], verbs [list]` (`''` is the core
group) followed by `kubectl auth can-i list pods -A --as=system:serviceaccount:…`.

## 6. What CronRack checks

Like the host checks, each can be given another severity, threshold or an exception per
cluster through CronRack's rule overrides. There is no page for that yet (see the
CHANGELOG's known issues), so the defaults below are what you'll see.

| Finding | Default |
|---|---|
| Node not ready | red |
| Node under memory, disk or PID pressure | yellow |
| Pod crash-looping, killed for using too much memory (last 24 h), or bad configuration | red |
| Pod can't pull its image | yellow |
| Pod Pending for more than 5 minutes | yellow |
| Pod restarted more than 5 times in the last 24 h | yellow |
| Deployment/StatefulSet with no running copy | red |
| Deployment/StatefulSet with fewer copies than asked, DaemonSet not on every node | yellow |
| Volume claim Pending for more than 10 minutes (k3s' local-path waits for a pod first) | yellow |
| Argo CD app out of sync | yellow |
| Argo CD app degraded or missing | red |
| API certificate expires in under 30 days / under 7 days | yellow / red |
| Image with `:latest` or no tag | yellow |
| A newer k3s patch release in your version's line (needs update checks) | yellow |
| A newer k3s minor version is "stable" (needs update checks) | info |
| A newer Argo CD release (needs update checks) | yellow |
| Helm CLI on a host is older than the latest release (needs update checks) | info |
| The agent may not read something (with the missing RBAC rule) | yellow |
| A list couldn't be read in full, so problems may be hidden there | yellow |
| The cluster view stopped reporting | offline |
| The API is unreachable / the token is rejected / the token expires within 14 days | red / red / yellow |

**Restarts after a node reboot.** When a node reboots, every container on it restarts,
and Kubernetes records the last stop with the reason `Unknown`. Those restarts are not the
pod's fault, so a pod whose last stop says `Unknown` isn't flagged as "restarted
repeatedly" (a later real crash, say `Error`, counts again). Two limits:

- Kubernetes keeps one restart count per container, not one per cause. A pod with 4
  restarts from reboots and 2 real crashes shows 6, and is flagged after the second crash.
- `Unknown` also appears when the container runtime (containerd) itself crashed, without
  a reboot. Those restarts are skipped too; the node's own findings cover that case.

**When a cluster counts as offline.** By default after 180 seconds without a cluster view,
or Settings' "mark offline after" if that is longer. A threshold override for
`k8s.cluster_offline` replaces both; with the rule disabled the cluster is never marked
offline.

**Limits on new clusters.** One host may add at most 3 clusters (a reinstall gives the
cluster a new ID, so the old one stays until you delete it), and one account at most 20.
Past a limit the new cluster is refused and the server logs one warning per host per hour.
Delete a cluster you no longer use to free a slot.

Pod findings are keyed by the workload that owns the pod (for example `Deployment/web`),
not by the pod's name, which changes with every rollout. So "This is expected" still holds
after a restart. A cluster's status changes appear in Activity and go to Discord with the
same cooldown as hosts.

## 7. Online update checks (off by default)

To say whether k3s, Argo CD and Helm are up to date, the CronRack **server** has to ask
the internet what the newest versions are. That's why this is **off by default**:
CronRack doesn't phone home unless you turn it on.

Turn it on in **Settings → Update checks → "Check for updates online"**. Then, at most
once a day (and at most once an hour after a failure), the server's scheduler requests:

- `https://update.k3s.io/v1-release/channels` (k3s release channels)
- `https://api.github.com/repos/argoproj/argo-cd/releases/latest`
- `https://api.github.com/repos/helm/helm/releases/latest`

Only these three fixed URLs, with a 5-second timeout, no redirects and a 256 KiB size
limit. Nothing about your setup is sent: no host names, no versions, no IDs. Only the
version numbers from the answers are kept (in the database). If a request fails, nothing
breaks; the versions just aren't compared. The checks only run if at least one user turned
the setting on and there is a Kubernetes node or cluster. Turn the setting off again and
no request is made.

Without update checks, CronRack still shows the installed versions, with the hint "Turn on
update checks in Settings to compare with the latest releases". Kubernetes distributions
other than k3s are never compared (their release feed answers with a redirect).

If you restrict the server's egress, allow `update.k3s.io` and `api.github.com` on 443 for
this feature (see also [kubernetes.md](kubernetes.md), section 10).

## 8. Size limits, "truncated" and "incomplete"

A report must fit in one WebSocket message, so the agent caps its lists (for example 100
problem pods, 400 healthy pods, 200 workloads, 20 events) and always puts **problems
first**. Two different notices can appear on the cluster page:

- **"Not everything is listed"** (info): everything was read, but some healthy items
  didn't fit and are left out. Problems are listed first, so nothing that needs you is
  hidden.
- **"Not read in full"** (yellow): the agent couldn't read a list completely (an answer
  was too large even in small pages, too slow, or failed), so **problems may be hidden**
  there. The finding includes a `kubectl get …` command to look yourself.

## 9. Troubleshooting

- **The cluster doesn't appear.** Check the cluster-view agent: `sudo journalctl -u
  cronrack-agent -n 50 --no-pager`, then `--check-k8s` (section 5). A cluster appears once
  the agent can read the `kube-system` namespace (that's its ID).
- **"Nodes without a CronRack host".** The node's name and the host's name differ (k3s
  `--node-name`). Name them the same, or install the agent with `--k8s` on that host.
- **Two agents with `--k8s-cluster-view`.** Only one reports the cluster; the other takes
  over only if the first one goes quiet. Use it on one node.
- **The token expired.** Create a new one (section 3.2) and run the installer again with
  `--k8s-cluster-view`.
