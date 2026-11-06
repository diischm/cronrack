# Changelog

All notable changes to CronRack. Versions follow [Semantic Versioning](https://semver.org/);
before 1.0.0, any release may change things that worked before, and the notes say so.

## [Unreleased]

### Added

- **Network checks (opt-in, works with any router).** One agent checks the path from its host
  to the internet, point by point, and CronRack shows where a problem is: "Router down", "ISP
  box down", "Internet down", "Internet slow", "DNS failing". `install.sh --network`, with
  `--network-hop "ISP box=IP"` for extra points. No root and no router setup; every target
  comes from `agent.toml`. See docs/network-monitoring.md.
- **The network is its own subject**, like a cluster: its own status, activity entries and
  Discord alerts (same cooldown and rule overrides, `network.*`). A new status must hold for 3
  reports before it counts. The measuring host doesn't change colour. "Router rebooted" and
  "Public IP changed" are activity entries only.
- **Network page** (`/network/`, from "More info" on the strip): each point's status in words,
  latency, packet loss and a 24 h trend, DNS, last router reboot, which agent measures it.
  Default: the agent on the CronRack server's own machine, because it can still report during
  an outage.

### Changed

- **Glass overview:** the network strip comes first, and there's no header row any more: no
  headline, no "Check all now" (Check now on cards and units stays), "updated …" moved to the
  top bar. "+ Add host" sits on the chips row. No visible section titles (they stay for screen
  readers), "See all →" at the bottom of Clusters and "While you were away". Dots only on
  activity rows, not on attention cards or chips. A bigger logo.

### Changed

- **Glass is calmer.** No glows anywhere: the orange background glows, the light running
  around red cards, the tilt glare, coloured shadows and status-tinted cards are gone.
  Surfaces are neutral graphite; status colour is only the small dot (and a disk bar from
  85%); orange only marks the logo, Add host, "See all", the focus ring and the "Check all
  now" pill. The current page in the nav is a grey pill, without an orange dot.
- **The server rack in Glass** for hosts and clusters (ears, bay, LED, readouts, vents). A
  click opens a unit in place: its issues, last report, Check now and Open host (clusters:
  nodes, pods, k3s version, Open cluster). Several can be open; they stay open when the
  page refreshes.
- **Overview layout:** headline, tiles, then "Needs attention" next to "While you were
  away" (up to 10 entries, scrolling in its box), then the Hosts and Clusters racks. Empty
  sections aren't shown at all. Clusters that need you get an attention card too, so the
  headline's count always matches the cards. Legacy still shows the newest 3 entries.
- **Settings > Appearance:** "Moving background" is replaced by **Reduce animations** (off
  by default). Your device's reduced-motion setting always turns them off too.
- **"Check all now" tells the truth:** it waits for each host's new report (up to 15 s) and
  for the page to refresh, then says "All 5 hosts reported" or "4 of 5 hosts reported".
  Offline hosts count as not reported.
- Glass's top bar stays at the top while you scroll. Smoother motion: the page refresh
  morphs instead of jumping (where the browser supports it), the headline changes word by
  word, a changed status pulses its dot once, new activity slides in.

## [0.2.0] - Unreleased

Kubernetes monitoring: CronRack now answers "what needs my attention?" for a k3s (or other
Kubernetes) cluster too. Opt-in and read-only. See `docs/kubernetes-monitoring.md`.

### Added

- **Glass, the new default look**: dark graphite, frosted-glass cards, an orange accent and
  two slow orange background glows. The old look stays as **Legacy**, with its own Light /
  Dark / System choice, and looks exactly as before. Switch in Settings > Appearance >
  Theme. Everyone lands on Glass, also with a saved Light or Dark; that choice is kept for
  when you switch back to Legacy. Browser-only (saved in this browser, like the theme was).
  - Two new Glass settings: **Moving background** (on by default; off keeps the glows
    still) and **Show status labels** (off by default; shows the word next to each dot).
  - Status in Glass is a **flat dot** (red, yellow, green, grey). Accessibility, a
    conscious choice (WCAG 1.4.1, use of colour): by default the dot's colour alone shows
    the status to a sighted user. Mitigations: every dot carries its words for screen
    readers (and as a tooltip), every card says what is wrong in plain text, and "Show
    status labels" puts the word next to every dot. The four summary tiles always show
    the words.
  - Moving parts, all off with "reduce motion": card tilt and glare (mouse only), a light
    running around red cards, rolling counts, a "Check all now" progress pill, tabs on the
    host page, a step bar and terminal look on Add host.
  - Font: Inter 4.1 (self-hosted, only downloaded in Glass). No new requests to other
    sites; the Content Security Policy is unchanged.
  - Components adapted from SmoothUI (MIT); see `THIRD_PARTY_NOTICES.md`.
- **Kubernetes role on every node** (`--k8s`): the host's card shows "k8s server" or
  "k8s worker", and the host page gets a Kubernetes panel (role, node, k3s version, node
  health, this node's pods). No cluster access or token needed.
- **A read-only cluster view** from one node (`--k8s-cluster-view`): nodes, pods, workloads,
  volume claims, warning events, Helm releases, Argo CD apps, versions and the API
  certificate, read with a ServiceAccount token that may only `list` (and `get` the
  `kube-system` namespace). Never Secrets, env values, logs, Helm values or Argo CD sources;
  never the admin kubeconfig. The exact RBAC is documented (no YAML shipped).
- **Cluster page** (`/clusters/<id>/`), phone first: summary readouts, problems first with
  a plain "why" and a copy-only `kubectl` command, then every section. Tables become
  cards on phones. Refreshes itself like the overview. A "Clusters" rack on the overview
  and a "Clusters" nav item once you have a cluster. Rename or delete a cluster.
- **Kubernetes findings**: node not ready or under pressure, crash loops, out-of-memory
  kills, image pull errors, Pending pods, workloads without enough copies, Pending volume
  claims, Argo CD out of sync or degraded, expiring API certificate or token, images
  without a fixed tag, missing permissions (with the exact RBAC rule), lists that couldn't
  be read in full. Cluster status changes go to Activity and Discord like hosts.
- **Online update checks, off by default**: with "Check for updates online" turned on in
  Settings, the server compares k3s, Argo CD and Helm with the newest releases once a day
  (three fixed URLs, nothing about your setup is sent).
- **`--check-k8s`**: the agent prints what its ServiceAccount may read, one line per API.
- Agent **0.2.0**. New installer flags: `--k8s`, `--k8s-cluster-view`, `--k8s-ca`, `--k8s-api`.
- **Limits on new clusters**: one host's agent may add at most 3 clusters, and one account
  at most 20. A new cluster past a limit is refused (no cluster, no event, no Discord
  alert); the host's own report is still saved, clusters you already have keep updating,
  and the server log says so at most once an hour per host (IDs only). Deleting a cluster
  frees a slot. This stops a stolen agent token from creating clusters, and an alert for
  each, without end.
- **`install.sh --upgrade`**: one non-interactive command updates an installed agent:
  `sudo curl -fsSL --cacert /etc/cronrack/ca.crt https://SERVER/agent/install.sh | sudo
  bash -s -- --server https://SERVER --upgrade`. It asks nothing and needs no terminal
  (safe from scripts and Ansible), and keeps the token, the CA, the Kubernetes token and
  `[kubernetes]` settings and the current Docker choice. It refuses when no agent is
  installed (exit 3) and when `--server` is another server than the agent's (exit 4;
  `--server-change` allows the move); it doesn't combine with options that change
  settings (exit 2). An agent that is already current is left alone ("already up to
  date", nothing restarted). After the restart it checks that the agent keeps running;
  if not, it puts the previous code back and exits 6 (5 if that fails too). The
  `websockets` library in the venv is not rolled back. An agent that is up to date but
  whose service is not running gives exit 7 and is not started. `--upgrade` never changes
  whether the service is enabled (a disabled one stays disabled). Exit codes 0-7 are
  listed in `--help` and `docs/DEPLOY.md`. Only one install or upgrade runs at a time, and a
  download that breaks off half-way runs nothing (the script is one function).
- **`/opt/cronrack-agent/INSTALLED`**: every install and upgrade records the agent version,
  the package's SHA-256, the server's host name and the install time (UTC) as `key=value`
  lines (`root:root`, 644, no secrets), for `--upgrade` and for scripts that decide
  whether a host needs an upgrade. Format in `docs/DEPLOY.md`, 2.5.
- **Automated installer tests** (`tests/install/`): `install.sh` runs for real in
  throwaway Docker containers with no network. CI runs them (and fails if Docker is
  missing) and now runs `shellcheck` on the shell scripts.

### Fixed

- **The `k8s.cluster_offline` override now works.** A threshold set for this rule (for one
  cluster or all of them) decides when the cluster turns offline, on the cluster page and
  in the scheduler alike; before, both used their own fixed 180 seconds. With the rule
  disabled, the cluster is never marked offline. Without an override nothing changes: 180
  seconds, or Settings' "mark offline after" if that is longer.
- **A node reboot no longer makes every pod "restarted repeatedly" for a day.** Restarts
  whose last stop has reason `Unknown` (the container runtime lost the container, as on
  a reboot) don't count for "Pod restarting often", in the agent's problem list or the
  server's finding. Limits in `docs/kubernetes-monitoring.md`, section 6.
- **Re-running the installer no longer removes Docker access when you press Enter.** The
  Docker question's default now follows the current choice: "Docker access is on. Keep
  it? [Y/n]" when it's on (only `n` removes it), "[y/N]" when it's off.
- **The footer shows the real version**: `SERVER_VERSION` is now 0.2.0 (it said 0.1.0),
  and a test keeps it equal to the newest version in this file.
- Installer argument errors now exit with 2 instead of 1.

### Known issues

- **The host offline threshold ignores a `connectivity.offline` override.** Hosts still
  turn offline after Settings' "mark offline after"; a threshold override for that rule
  has no effect (the cluster rule above was fixed, the host one not yet).

### Upgrading

- **Back up the database first** (`docs/DEPLOY.md`): this release has migrations, and two
  of them rebuild tables on SQLite (activity events and suppressions can now belong to a
  cluster instead of a host).
- Old agents (0.1.0) keep working unchanged. A 0.2.0 agent sends Kubernetes data only to a
  server that supports it.
- To upgrade an agent from now on, use `--upgrade` (`docs/DEPLOY.md`, 2.5). The first
  upgrade of an agent installed before this release has no `INSTALLED` record yet, so it
  always installs (it can't tell "already up to date") and writes the record.

## [0.1.0] - Unreleased

The first release: the MVP. One question, answered from your phone in seconds:
"What needs my attention?"

### What it does

- **Hosts check in live.** A small agent on each host opens one encrypted WebSocket
  connection to your server and keeps it open. It sends a report every minute, and
  immediately when you press "Check now". The agent never listens on a port.
- **Dead-man's switch.** A host that stops reporting turns **offline** (by default about
  1.5 to 2 minutes after it goes quiet; the threshold is in Settings) and you get an alert.
- **Package updates.** Pending updates, with security updates counted separately (red),
  and the "reboot required" flag (yellow). Debian/Ubuntu (`apt`) hosts.
- **Docker containers.** Unhealthy, restarting, and exited-with-an-error containers are
  flagged per container. Opt-in on each host, because the `docker` group is equivalent
  to root.
- **systemd.** Failed units are flagged; every timer's last and next run is listed.
- **Disks.** Usage per real filesystem (no tmpfs, overlay, squashfs, ...): yellow from 85%,
  red from 95%.
- **Status page for your phone.** Red / yellow / green / offline per host and the top
  issues first, plus a desktop layout with a sidebar. A host page with every check.
  An activity list of status changes.
- **Discord alerts** when a host's status *changes*, never repeatedly. A "send test alert"
  button. Only real Discord webhook URLs are accepted.
- **Versions** of the server (sidebar footer) and of each host's agent (host page).

### Running it

- **One `docker compose up -d`**: Caddy (HTTPS and WSS, the only published ports: 80
  redirects, 443), the app (Django + Channels under uvicorn) and the scheduler.
  Hardened containers: non-root, read-only filesystem, no Linux capabilities,
  no-new-privileges. Health checks for all three, including a public `/healthz`.
- **HTTPS out of the box** with Caddy's own certificate authority (`tls internal`); a real
  domain (Let's Encrypt) and Tailscale certificates are documented options.
- **Agent installer**, one line, from your own server: checks the download's SHA-256,
  runs the agent as an unprivileged `cronrack` user in a sandboxed systemd service, asks
  for the token with hidden input. `--ca-file` for Caddy's CA, `--new-token`,
  `--uninstall`; re-running upgrades and keeps the config and token.
- **Guides**: `docs/DEPLOY.md` (server, agents, updates, backups, troubleshooting) and
  `scripts/smoke_test.sh` (checks a throwaway copy of the whole stack).

### Security

- Agents only connect out; the server can only send "welcome" and "collect", never
  commands. Agent tokens: one per host, stored only as a SHA-256 hash, shown once,
  rotatable and revocable. The agent socket accepts only the token, never browser cookies.
- Everything agents report is validated against a strict schema with size limits and
  always escaped in pages.
- Every page and API only ever shows your own hosts. Login rate limiting; the first-run
  setup page locks itself after the first account.
- Hardening from an external audit:
  - The login rate limit is enforced for every login (also Django's `/admin/login/`), and
    there is a per-address limit (20 failures per 15 minutes) so a flood of made-up
    usernames can't push a lockout out of the cache. `/admin/` is off unless
    `CRONRACK_ENABLE_ADMIN=1` (on by default only with `DEBUG=1`).
  - `/setup/` asks for a one-time setup code printed in `docker compose logs app`, and
    two simultaneous setups can't create two accounts. SQLite runs in WAL mode with
    immediate write locks, shared safely by the app and the scheduler.
  - An agent can't flood the server with reports (at most one every 4 seconds per
    connection, except answers to "Check now"), and "Check now" doesn't ask a host that
    reported seconds ago. A host whose status flaps sends at most one Discord alert per
    minute; changes in between are summed up in one alert with the current status.
  - The "connected" flag is cleared on server start and when a host goes offline, and an
    agent reconnecting can no longer leave its host marked disconnected.
  - New `Permissions-Policy` header. The agent runs its commands with a fixed `PATH`
    (systemd's default plus `/snap/bin`), whatever `PATH` it was started with.

### Changed (container layout)

- **The database lives in `/data`.** The image sets `SQLITE_PATH=/data/db.sqlite3`, and
  docker compose mounts the same `data` volume at `/data` (it was `/app/server/data`).
  Nothing to migrate: the volume and its files are unchanged, so upgrading keeps your
  database. Commands that used the old path inside the app (`data/db.sqlite3`) now use
  `/data/db.sqlite3`; the backup and restore commands in `docs/DEPLOY.md` are updated.
  The containers write only to `/data` and `/tmp`.
- **Settings are checked strictly.** A yes/no setting with a typo (`DEBUG=ture`) stops
  the server with a clear message instead of quietly meaning "off". New optional
  settings: `CSRF_TRUSTED_ORIGINS` (extra origins), `LOG_LEVEL`. `FORWARDED_ALLOW_IPS`
  accepts networks (`10.42.0.0/16`) as well as addresses, and a malformed entry or `*`
  is refused.
- **One image, several roles.** The image's entrypoint takes a role: `migrate` (apply
  database migrations, then exit), `web` (the web server; it no longer migrates) and
  `scheduler`; anything else runs as-is. docker compose has a new one-shot `migrate`
  service, and `app` and `scheduler` start only after it succeeded, so
  `docker compose up -d` still does everything in one command.
  **Note:** with no arguments the image now runs `web`, which does *not* apply
  migrations. If you run the image without docker compose, run it once with `migrate`
  first (and after every update).
- **The app serves its own static files** (CSS, JS, fonts) with WhiteNoise, gzipped,
  with the same caching as before: content-hashed files are cached for good, everything
  else is re-checked. Caddy now passes every request to the app, and the security
  headers (CSP, nosniff, Permissions-Policy, HSTS) come from the app for static files
  too. The `static` volume is no longer used; after updating you may remove it with
  `docker volume rm cronrack_static`. Pages and JSON without their own Cache-Control
  get `no-cache` from the app (Caddy used to add it).
- **Logging**: everything (CronRack, Django, uvicorn) goes to stdout in one format;
  `LOG_FORMAT=json` gives one JSON object per line. Errors (500s) are now logged with
  `DEBUG=0` as well; before, Django only printed them in debug mode. Successful
  `/healthz` and `/readyz` calls are left out of the access log (failed ones stay in).
  An invalid report from an agent is logged with field paths and error codes only,
  never with the values or unknown keys it contained.
- **Images for amd64 and arm64** are built by CI (`.github/workflows/image.yml`) and
  pushed to `ghcr.io/diischm/cronrack` from `main` (`sha-<commit>`) and version tags
  (`X.Y.Z`, `X.Y`), then scanned with Trivy. Docker Compose still builds the image
  locally, as before.
- **Graceful stop**: the scheduler handles SIGTERM itself and exits right away (before,
  `docker compose stop` waited 10 s and killed it); the web server gets up to 20 s to
  finish requests and closes agent connections cleanly, and agents reconnect.
- With the optional `POD_NAME` and `NODE_NAME` variables set, the sidebar shows which
  pod and node served the page (nothing changes without them).
- **`/readyz`**: a second health check that says "ready" only when the database answers
  and every migration has been applied (`/healthz` stays the cheap "process is up" check).

### Fixed

- HTTPS failed with "internal error" (Firefox `SSL_ERROR_INTERNAL_ERROR_ALERT`) when
  `CRONRACK_DOMAIN` was an IP address, such as a LAN or Tailscale IP: clients send no
  server name (SNI) to an IP, so Caddy couldn't pick the certificate. The Caddyfile now
  sets `default_sni`, moved to `caddy/Caddyfile` (the folder is mounted, so edits apply
  with `docker compose restart caddy`; run `docker compose up -d` once after updating).

### Known issues

Rough edges in 0.1.0, as found in the release check. None of them lose data or weaken
the security model.

- Discord alerts and the installer will be verified on a real homelab (Raspberry Pi agent) before the v0.1.0 tag.
- **Findings can't be silenced or tuned in the UI yet.** A host with one pending security
  update stays red until it's updated. "This is expected", snooze, maintenance mode and
  per-rule settings come in MVP+ and v1. The only setting today is the offline threshold.
- **Certificate warnings with the default `tls internal`** until you import Caddy's root
  certificate on each device. On phones that takes several taps in system settings.
  A real domain or Tailscale certificates avoid it, but take more setup.
- **One name only.** The server answers to exactly one name (`CRONRACK_DOMAIN`). Using
  both a LAN name and a Tailscale name at the same time isn't supported.
- **Some agent connection errors are terse.** A server name that doesn't resolve is
  logged as `gaierror`, and a closed port as `ConnectionRefusedError`.
  `docs/DEPLOY.md` (troubleshooting) translates them.
- **Requests from the server machine itself show the Docker gateway as their address**
  (Docker's port forwarding hides it). The login rate limit counts per address and
  username, so for those requests it counts per username. Clients on your network normally
  keep their real address; that hasn't been verified from a second machine yet.
- **Timers are listed, not judged.** There's no alert yet for a timer that stopped
  running.
- **Discord alerts are tested with unit tests only** (the HTTP request is faked); the
  release check didn't post to a real webhook.
- **The installer was re-tested in a Debian container with a stub `systemctl`**, not on a
  Raspberry Pi again. The manual test plan is in `docs/DEPLOY.md`; run it on a real Pi
  before tagging the release.
- **Single server process.** Restarting the app drops every agent connection (agents
  reconnect by themselves, with backoff of up to 5 minutes) and resets the login rate limit
  counters. That's by design for a homelab, but worth knowing.
- **Fixed Docker subnet.** The compose network uses `172.29.80.0/24` so that the app can
  trust exactly Caddy's address. If that range is already used on your machine, edit
  three values in `docker-compose.yml` (its comments say which).
- **No published image yet.** `docker compose up -d` builds the image on your machine.
  Multi-arch images on GHCR are on the roadmap.
- **SQLite, no automatic backups.** `docs/DEPLOY.md` shows a one-command backup and restore.
- **Some page templates are still marked as placeholders** (the host page in particular is
  plain); the final designs land in a later release.
- `python manage.py check --deploy` reports one warning, `security.W021` (HSTS preload).
  That's on purpose: preloading is a hard-to-undo public registration.

[0.1.0]: https://github.com/diischm/cronrack/releases/tag/v0.1.0
