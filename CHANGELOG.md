# Changelog

All notable changes to CronRack. Versions follow [Semantic Versioning](https://semver.org/);
before 1.0.0, any release may change things that worked before, and the notes say so.

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
