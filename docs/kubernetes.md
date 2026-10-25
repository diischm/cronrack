# Running CronRack on Kubernetes (experimental)

> **Experimental.** Docker Compose is the supported way to run CronRack. This guide is
> for testing and learning; I write my own manifests from it.

This page lists everything the CronRack image expects from Kubernetes: environment
variables, ports, paths, volumes, the user it runs as, probes, resources and the
limits that come from SQLite. It contains no manifests; snippets show single fields only.

The setup it was written for: a k3s cluster with two nodes on the same LAN, a
**server node** (x86_64, Ubuntu 24.04, HDD, the k3s server) and a **worker node**
(arm64, e.g. a Raspberry Pi 4B, a k3s agent), with k3s' default ingress controller,
Traefik.

**Network.** The nodes talk to each other over the LAN only:

- The worker joins the server node by its **LAN IP**: `k3s agent --server
  https://<server-lan-ip>:6443 --token <token>` (or
  `K3S_URL=https://<server-lan-ip>:6443` with the install script).
- Node-to-node traffic stays on the LAN: the API server (6443/tcp), flannel's VXLAN
  between pods on different nodes (8472/udp) and kubelet (10250/tcp). If a firewall runs
  on the nodes, allow these from the LAN.
- **Tailscale is not part of the cluster.** It is only for SSH to the nodes and for
  reaching the Ingress (Traefik on 80/443) remotely, e.g. the dashboard from a phone.
- **A node with several interfaces** (LAN plus `tailscale0`, Docker bridges, …): k3s
  picks the interface of the default route. To be sure it uses the LAN, start k3s on
  every node with `--node-ip <that node's LAN IP>` and `--flannel-iface <LAN interface,
  e.g. eth0>`. Check with `kubectl get nodes -o wide`: `INTERNAL-IP` must be the LAN IP.

---

## 1. The image

`ghcr.io/diischm/cronrack`, built by `.github/workflows/image.yml` for `linux/amd64`
and `linux/arm64`.

| Tag | When |
|---|---|
| `sha-<short commit>` | every push to `main` |
| `X.Y.Z` and `X.Y` | every `vX.Y.Z` tag |

There is no `latest`: name the exact tag, or better the digest
(`ghcr.io/diischm/cronrack@sha256:…`), so a pod restart never pulls a different image.

**Pulling it.** A new GHCR package starts **private**, even when the repository is
public. Two options:

- **Make it public** (no credentials needed in the cluster): GitHub → your profile →
  Packages → `cronrack` → Package settings → Danger Zone → Change visibility → Public.
  Under "Manage Actions access", the `cronrack` repository should have write access
  (it gets it automatically when the workflow first pushes the package).
- **Keep it private**: create a classic personal access token with only the
  `read:packages` scope, store it in a `kubernetes.io/dockerconfigjson` Secret for
  `ghcr.io` (user name: your GitHub user), and reference it with `imagePullSecrets` in
  the pod spec. Give the token an expiry date and rotate it.

## 2. One image, three roles

The image's entrypoint (`server/docker-entrypoint.sh`) picks a role from the first
argument, i.e. the container's `args`:

| Role | What it runs | Runs as |
|---|---|---|
| `migrate` | `manage.py migrate`, then exits 0 (non-zero on failure) | a Job, or an init container of the web pod |
| `web` (also: no arguments) | prints the first-run setup code, then `exec uvicorn` on port 8000 | a long-running container, exactly **one** |
| `scheduler` | `exec manage.py check_hosts --loop --every 30 --heartbeat-file /tmp/check_hosts.alive` | a long-running container, exactly **one** |
| anything else | run as-is, e.g. `python manage.py createsuperuser` | `kubectl exec`, or a one-off pod |

- **`web` never migrates.** Run `migrate` before it, every time the image changes. As
  long as migrations are missing, `/readyz` answers 503 and the web pod gets no traffic,
  so a forgotten migration shows up as "not ready" instead of errors.
- Extra arguments after `scheduler` are appended, e.g. `args: ["scheduler", "--every", "60"]`.
- Every role `exec`s, so the real program is PID 1 and gets SIGTERM directly (see 7).

**First run.** The web container prints the one-time setup code that `/setup/` asks for:

```bash
kubectl logs <web pod> -c <web container> | grep "setup code"
```

It changes every time the web container starts, until the first account exists.

## 3. Ports and paths

Everything is on **one port, 8000** (plain HTTP inside the cluster; TLS ends at Traefik):

| Path | What |
|---|---|
| `/` and everything else | the pages and JSON |
| `/ws/agent/` | the agents' WebSocket, on the same port 8000 (no separate Service port) |
| `/static/…` | CSS, JS, fonts, served by the app itself (WhiteNoise) |
| `/healthz` | liveness: `ok`, no database, no cookies |
| `/readyz` | readiness: `ready` (200) only if the database answers and every migration is applied, otherwise `not ready` (503) |

`/healthz` and `/readyz` are answered before Django's Host check, so they work with the
`Host: <pod IP>:8000` header that kubelet probes send. Every other path with an unknown
Host header gets 400. Neither probe response contains any detail; the reason for a 503
is in the log.

## 4. Environment variables

| Variable | Required | Default | Secret | Roles | What |
|---|---|---|---|---|---|
| `SECRET_KEY` | **yes** | — | **yes** | all | Django's signing key. Same value for every role, and keep it: changing it logs everyone out. Put it in a Secret. |
| `CRONRACK_DOMAIN` | yes for `web` | — | no | web | The name browsers and agents use, e.g. `<tailscale-name>`. Bare name: no `https://`, no port. Feeds `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` and the agent install command. |
| `BEHIND_PROXY` | yes behind Traefik: `1` | `0` | no | web | Believe `X-Forwarded-Proto` (needed for secure cookies and the https redirect). |
| `FORWARDED_ALLOW_IPS` | **yes** for `web` | — | no | web | Addresses/networks uvicorn believes `X-Forwarded-For`/`-Proto` from: the k3s pod network, `10.42.0.0/16` (Traefik's pod IP changes). Comma-separated IPs and CIDRs; `*` and malformed entries are refused. See section 10 for the NetworkPolicy this needs. |
| `ALLOWED_HOSTS` | no | — | no | web | Extra host names, comma-separated. |
| `CSRF_TRUSTED_ORIGINS` | no | — | no | web | Extra origins, comma-separated, each with scheme (`https://…`). |
| `SQLITE_PATH` | no | `/data/db.sqlite3` (set in the image) | no | all | Must be absolute. Leave it. |
| `LOG_LEVEL` | no | `info` | no | all | `debug`, `info`, `warning`, `error`, `critical`. |
| `LOG_FORMAT` | no | `plain` | no | all | `json`: one JSON object per line. |
| `TIME_ZONE` | no | `UTC` | no | all | |
| `CRONRACK_ENABLE_ADMIN` | no | `0` | no | web | Django's `/admin/`. Leave it off. |
| `DEBUG` | no | `0` | no | all | Never `1` in a cluster. |
| `POD_NAME`, `POD_NAMESPACE`, `NODE_NAME` | no | — | no | web | From the Downward API (`fieldRef: metadata.name`, `metadata.namespace`, `spec.nodeName`). With `POD_NAME` and `NODE_NAME` set, the sidebar footer shows "Served by pod … on node …". |

The settings check every value when a process starts and stop with a clear message
(`DEBUG must be 1 or 0 …`, `FORWARDED_ALLOW_IPS must be IP addresses or networks …`).
The Discord webhook URL is not an environment variable: it is set in the app and stored
in the database.

Set `enableServiceLinks: false` in the pod spec. Otherwise Kubernetes adds a
`<SERVICE>_PORT=tcp://…` variable for every Service in the namespace, and a Service
named after a program's own settings prefix (for example `uvicorn`, which reads
`UVICORN_*`) could change how it starts.

## 5. Volumes, user and file system

| Path | What | Volume |
|---|---|---|
| `/data` | `db.sqlite3` plus `db.sqlite3-wal` and `db.sqlite3-shm` while it runs | a PersistentVolumeClaim, `ReadWriteOnce`, shared by web, scheduler and migrate |
| `/tmp` | scratch space; the scheduler's heartbeat file | an `emptyDir` per container (or per pod) |

Nothing else is written: the root file system can be read-only.

- **User:** UID 10001, GID 10001, set numerically in the Dockerfile (`USER 10001:10001`),
  so `runAsNonRoot: true` can verify it. `runAsUser`/`runAsGroup: 10001` match it.
- **`fsGroup: 10001`:** for volume types that support it, Kubernetes makes the volume
  group-owned by 10001; `/data` in the image is `0770` (owner and group may write).
  k3s' default `local-path` provisioner creates its volume directories world-writable
  (`0777`) and doesn't apply `fsGroup`, so the app can write either way.
- The rest of a hardened `securityContext` fits as well: `readOnlyRootFilesystem: true`,
  `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`,
  `seccompProfile.type: RuntimeDefault`, and `automountServiceAccountToken: false`
  (CronRack never talks to the Kubernetes API).

**Why one ReadWriteOnce volume works for several pods:** `ReadWriteOnce` means "one
*node*", not "one pod": pods on the same node can share it. Don't use
`ReadWriteOncePod`, which really is one pod and would lock the scheduler out. Never put
the database on NFS or SMB: SQLite's WAL mode needs a local file system, shared by
processes on one machine.

**Backups** work as with Docker, in the web container:

```bash
kubectl exec <web pod> -c <web container> -- python -c 'import sqlite3, sys, tempfile
with tempfile.NamedTemporaryFile() as tmp:
    sqlite3.connect("/data/db.sqlite3").backup(sqlite3.connect(tmp.name))
    sys.stdout.buffer.write(open(tmp.name, "rb").read())' > cronrack-backup-$(date +%F).sqlite3
```

## 6. Probes

| Probe | Container | Check | Timings |
|---|---|---|---|
| startup | web | `httpGet /healthz :8000` | `periodSeconds: 2`, `timeoutSeconds: 3`, `failureThreshold: 30` (up to 60 s to start) |
| liveness | web | `httpGet /healthz :8000` | `periodSeconds: 20`, `timeoutSeconds: 3`, `failureThreshold: 3` |
| readiness | web | `httpGet /readyz :8000` | `periodSeconds: 10`, `timeoutSeconds: 5`, `failureThreshold: 3` |
| liveness | scheduler | `exec`: the heartbeat file is younger than 120 s | `initialDelaySeconds: 60`, `periodSeconds: 30`, `timeoutSeconds: 5`, `failureThreshold: 3` |

The scheduler's check, as in `docker-compose.yml`:
`python -c "import os, sys, time; sys.exit(time.time() - os.stat('/tmp/check_hosts.alive').st_mtime > 120)"`.

Why these:

- **Liveness uses `/healthz`, never `/readyz`.** A busy or not-yet-migrated database
  isn't fixed by restarting the web container; it only takes the pod out of traffic.
- **Readiness every 10 s is cheap**: `/readyz` checks for pending migrations once, and
  after the first "all applied" only runs `SELECT 1`. `failureThreshold: 3` rides out
  a moment when SQLite is busy with a long write.
- **The startup probe** covers the first start (setup code, Python imports; a few
  seconds, more on an HDD right after a reboot), so the liveness probe can be strict.

## 7. Stopping (SIGTERM)

- **web:** uvicorn handles SIGTERM itself: it stops accepting connections, closes the
  agents' WebSockets with code 1012 ("service restart"), gives requests in flight up to
  20 s (`--timeout-graceful-shutdown 20`) and exits. Agents reconnect by themselves,
  with backoff starting at 1 s. Kubernetes' default `terminationGracePeriodSeconds: 30`
  is enough; don't go below 25.
- **scheduler:** has its own SIGTERM handler. A pass that's running finishes (a second
  or so; a Discord request gives up after 5 s), then it exits 0; between passes it
  exits at once. Without the handler (Python as PID 1 ignores SIGTERM), Docker waited
  10 s and then killed it (exit 137).

## 8. CPU and memory

Measured with `docker stats` on the Docker Compose stack (x86_64, one agent
connected). "Light use": logging in, then about 80 rounds of overview, host detail,
overview.json and activity pages, roughly 8 requests per second. 100 % = one CPU core,
so 1 % = 10 millicores (m).

| | CPU idle | CPU light use | CPU peak | Memory |
|---|---|---|---|---|
| web | 0.1 % ≈ 1 m | 4-7 % ≈ 65 m on average | 27.6 % ≈ 280 m | 79-81 MiB |
| scheduler | 0 % | 0 % | 3.4 % ≈ 34 m (during a pass) | 42 MiB, flat |

Recommendation:

| | requests | limits |
|---|---|---|
| web | `cpu: 50m`, `memory: 128Mi` | `memory: 256Mi`; CPU: no limit, or `1` |
| scheduler | `cpu: 10m`, `memory: 64Mi` | `memory: 128Mi`; CPU: no limit, or `200m` |

The math:

- **web memory:** 81 MiB measured, +50 % headroom ≈ 120 → **128Mi** request. Limit
  about 3× the measurement (**256Mi**): memory grows a little with every connected agent
  and with the first requests after a start. Hitting the memory limit gets the container
  killed (OOMKilled), so leave room.
- **web CPU:** the average during use was 65 m, so a **50m** request reserves enough for
  a mostly idle homelab app. Peaks reached 280 m. A CPU limit only slows the app down
  (throttling) instead of killing it; without a limit, peaks use what's free. If you set
  one, `1` (1000 m) keeps every peak unthrottled.
- **scheduler:** 42 MiB flat → **64Mi** request (+50 %), **128Mi** limit. CPU is
  near zero, 34 m for a moment each pass → **10m** request.
- These numbers come from an x86 machine. An arm64 worker such as a Raspberry Pi is slower
  per core, but CronRack is pinned to the server node anyway (section 9).

## 9. Constraints (read this before writing manifests)

**SQLite means one of each, on one node.**

- **web: exactly one replica.** SQLite has one writer at a time, and two more things
  live inside the web process only:
  - the **channel layer** (Django Channels' `InMemoryChannelLayer`): "Check now" and
    "an agent connected again, close its old socket" only reach sockets in the *same*
    process. With two web pods, "Check now" would silently miss agents connected to the
    other pod.
  - the **cache** (`LocMemCache`), where the **login rate limits** live: two pods would
    each count failed logins separately, doubling what an attacker gets.
- **Use `strategy: Recreate`** for the web workload, not RollingUpdate. A rolling update
  briefly runs the old and the new pod side by side: two writers, two channel layers,
  and the new pod's start clears every agent's "connected" flag while the old pod still
  holds their sockets.
- **web and scheduler must be on the same node** and share the one `ReadWriteOnce`
  volume. Pin them to the server node with
  `nodeSelector: {kubernetes.io/hostname: <server-node-name>}`
  (or the equivalent `nodeAffinity`). With k3s' default `local-path` provisioner, the
  volume is a directory on that node's disk (by default under
  `/var/lib/rancher/k3s/storage/`): here, the HDD. That's fine for CronRack's small
  writes; just know that the data is on the server node only, so back it up (section 5), and
  if the server node is down, CronRack is down.
- **Scaling out** needs other parts first: PostgreSQL instead of SQLite, a Redis
  channel layer (`channels-redis`) and a shared cache (Redis) for the rate limits.
  None of that is in CronRack today.

### web + scheduler: one pod or two Deployments?

Both keep the two processes on one node with one volume.

**(1) Two containers in one pod** (one Deployment, `replicas: 1`, `strategy: Recreate`,
`migrate` as an init container):

- *Same node and volume:* guaranteed; Kubernetes schedules a pod as one unit.
- *Restart coupling:* each container restarts on its own after a crash or a failed
  liveness probe. Deleting the pod, a node drain or a new image restarts both, which is
  what you want anyway: they must run the same version.
- *Probes:* per container, as in section 6. The pod counts as ready when the web
  container is (the scheduler has no readiness probe), and only the Service routes to it.
- *Migrations:* the init container runs before both containers, every time the pod
  starts. Migrations are quick and idempotent, so that costs nothing.
- *Simplicity:* one workload, one place to pin the node, one image tag. Resource
  requests add up for the pod.

**(2) Two Deployments** (web and scheduler, each `replicas: 1`, `strategy: Recreate`,
both pinned to the server node; `migrate` as a Job):

- *Same node and volume:* only because both are pinned to the same node. If the
  pinning of one of them is changed or forgotten, it can land on the worker node, where the
  volume isn't, and stay Pending.
- *Restart coupling:* none: each can be restarted or rolled on its own.
- *Probes:* the same per container.
- *Migrations:* the Job must finish before *both* Deployments start the new image.
  That needs ordering (Helm hooks or Argo CD sync waves), and during an upgrade the two
  can briefly run different image versions.
- *Simplicity:* more YAML, two image tags to keep in step.

**Recommendation for this setup: (1), one pod with two containers.** CronRack needs
exactly one of each, on the same node, with the same image, and (1) gets all three by
construction, with less to keep in sync. (2) only pays off when the parts scale or
deploy separately, which SQLite rules out.

## 10. Security that Docker Compose gave for free

- **Only Traefik may reach port 8000.** In Docker Compose, only Caddy could talk to the
  app. In a cluster, every pod can reach every other pod by default, and with
  `FORWARDED_ALLOW_IPS=10.42.0.0/16` uvicorn believes `X-Forwarded-For` from *any* pod.
  A compromised pod could then fake its client address and get around the per-address
  login rate limit, or claim "this was https". So add a **NetworkPolicy**: ingress to
  the web pod only from the Traefik pods (in k3s: namespace `kube-system`, label
  `app.kubernetes.io/name: traefik`), on port 8000. k3s enforces NetworkPolicies out of
  the box (its embedded network policy controller). The scheduler and migrate need no
  ingress at all.
- **Egress:** the app and the scheduler send Discord alerts (HTTPS to `discord.com`) and
  need DNS. If you restrict egress, allow those.
- **Secrets:** only `SECRET_KEY` (section 4). Logs never contain agent tokens, the setup
  code's hash or report contents; the one-time setup code itself is printed once, on
  purpose.

## 11. Traefik instead of Caddy

**What Caddy did, and who does it now:**

| Caddy did | Now |
|---|---|
| TLS for HTTPS and WSS | Traefik (see "TLS" below) |
| redirect http → https | Traefik's `web` entry point should redirect to `websecure` (k3s: through a `HelmChartConfig` for Traefik). Django also redirects plain-http requests to https by itself (`SECURE_SSL_REDIRECT`, with `BEHIND_PROXY=1`), so nothing is ever served over http either way. |
| HSTS | Django sends it on every response it makes, static files included, when the request came in over https. Traefik's own error pages (e.g. a 502 while the pod restarts) don't have it unless you add a `headers` middleware. |
| static files, with cache headers | the app (WhiteNoise); nothing to configure |
| `Cache-Control: no-cache` on pages | the app; nothing to configure |
| replacing client-sent `X-Forwarded-*` | Traefik does this by default: it only trusts incoming forwarded headers from addresses in `forwardedHeaders.trustedIPs` (none by default) |
| `default_sni` (TLS for clients that connect to an IP address and send no server name) | Traefik: the default certificate of the `TLSStore` named `default`. Only matters if browsers or agents use an IP instead of the name. |
| HTTP/3 off | Traefik only serves HTTP/3 when it is turned on |

**WebSockets** work through Traefik with no extra configuration: an `Ingress` or
`IngressRoute` for `CRONRACK_DOMAIN` that sends `/` to the web Service on port 8000
covers `/ws/agent/` too. Agents connect to `wss://CRONRACK_DOMAIN/ws/agent/` on 443.

**Keepalive.** uvicorn pings every agent every 20 s (`--ws-ping-interval 20`), the
agent answers with a pong, and the agent sends its report every 60 s. So traffic flows
both ways at least every 20 s, and an idle-connection timeout won't cut the socket.
Still, **check once that an agent stays connected for more than 2 minutes through
Traefik**: the host stays green, and on the host
`journalctl -u cronrack-agent --since "-5 min"` shows no `Disconnected:` line. If agents
drop every 60 s (or every 180 s), the cause is Traefik's entry point timeouts. The
setting to adjust is on the `websecure` entry point:
`entryPoints.websecure.transport.respondingTimeouts.readTimeout` (and, if the drops come
every 180 s, `…respondingTimeouts.idleTimeout`). In k3s, pass it to Traefik as an
additional argument through a `HelmChartConfig`, e.g.
`--entryPoints.websecure.transport.respondingTimeouts.readTimeout=0s` (0 = no timeout).
I'm not certain that Traefik's defaults cut WebSockets at all; this is the place to
look if they do.

**TLS: two good options.**

- **Tailscale certificates** (a MagicDNS name, `<tailscale-name>`, with HTTPS enabled in
  the Tailscale
  admin console). Trusted by every browser and by the agents' system CA store: **agents
  need no `--ca-file`**. Run `tailscale cert <tailscale-name>` on the server node and load
  the result into a `kubernetes.io/tls` Secret that the Ingress uses. Tailscale certificates are
  valid for 90 days, so renewing (and updating the Secret) has to be automated, e.g. a
  systemd timer on the server node. (Tailscale's Kubernetes operator can also do this; it is
  a bigger piece of software to run.)
- **cert-manager**, with either
  - a **private CA** (a `CA` or self-signed `ClusterIssuer`): works for any name, but
    browsers and agents don't know that CA. Agents then need it at install time:
    `install.sh --ca-file <the CA's certificate>` (docs/DEPLOY.md 2.2), as with Caddy's
    `tls internal`; or
  - **Let's Encrypt with a DNS-01 challenge** for a real domain you own: trusted
    everywhere, no `--ca-file`, no ports open to the internet, but it needs an API token
    for your DNS provider.

Whatever you pick, the agent must trust the certificate Traefik presents for
`CRONRACK_DOMAIN`: a publicly trusted one (Tailscale, Let's Encrypt), or your own CA
given to `install.sh --ca-file`. Moving an existing server from Caddy's CA to another
certificate means re-running the install command on every host with the new
`--ca-file` (or without it, for a publicly trusted certificate).

## 12. Checklist after the first deploy

1. `kubectl get pods`: the web pod `Running` and `READY` (all containers), the migrate
   init container or Job `Completed`.
2. `kubectl logs <web pod> -c <web container> | grep "setup code"` → open
   `https://CRONRACK_DOMAIN/setup/`, create the account.
3. `curl -sS https://CRONRACK_DOMAIN/readyz` → `ready`.
4. Add a host, install the agent, the host turns green.
5. Wait more than 2 minutes: the host stays green and the agent's journal shows no
   `Disconnected:` (section 11).
6. Delete the web pod: the new one is ready within a minute, the agent reconnects
   within seconds.
