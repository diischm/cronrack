# Deploying CronRack in your homelab

This guide takes you from an empty Ubuntu Server to a running CronRack server, with an
agent on a Raspberry Pi (or any Debian/Ubuntu host) reporting to it. Every command is
meant to be copied as it is. Lines starting with `#` are comments; you don't type them.

- [Part 1: the server](#part-1-the-server)
- [Part 2: an agent on a host](#part-2-an-agent-on-a-host)
- [Part 3: updating, backing up and restoring](#part-3-updating-backing-up-and-restoring)
- [Part 4: troubleshooting](#part-4-troubleshooting)
- [Appendix: installer test plan](#appendix-installer-test-plan)

How it fits together:

```
 phone / laptop ──https──►┐
                          │   CronRack server (docker compose)
 agent on the Pi ──wss───►├─► caddy :80 :443 ──► app :8000 (Django) ──► SQLite volume
 agent on the NAS ──wss──►┘                       scheduler (marks silent hosts offline)
```

Only Caddy is reachable from the network, on ports 80 (it only redirects to https) and
443. The app and the scheduler have no open ports. Agents open the connection to the
server and keep it open; nothing ever connects *to* an agent.

---

## Part 1: the server

You need: a machine running Ubuntu Server 22.04 or 24.04 (a VM, a mini PC, the NAS), with
a fixed IP address on your network, and SSH access to it.

### 1.1 Install Docker

CronRack runs as three containers, started by Docker Compose. Install Docker Engine from
Docker's own package repository (Ubuntu's `docker.io` package is often too old for the
`docker compose` command). These are the steps from
[docs.docker.com/engine/install/ubuntu](https://docs.docker.com/engine/install/ubuntu/);
if they differ from what's below, follow that page.

```bash
# Docker's signing key and repository
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

# Docker Engine and the compose plugin
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Check
sudo docker run --rm hello-world
docker compose version
```

This guide writes `docker compose ...`. Either put `sudo` in front of every `docker`
command, or add yourself to the `docker` group once (`sudo usermod -aG docker $USER`, then
log out and back in). Be aware that the `docker` group is equivalent to root on that
machine: anyone in it can start a container that mounts `/`.

### 1.2 Get CronRack

```bash
cd ~
git clone https://github.com/diischm/cronrack.git
cd cronrack
# Optional: run exactly a release instead of the latest code
git checkout v0.1.0
```

Until the v0.1.0 tag exists, use main.

All further server commands run in this `~/cronrack` folder (the one with
`docker-compose.yml`).

### 1.3 Pick the name you'll use

Browsers and agents must reach the server by **one name or IP address**, and the HTTPS
certificate is made for exactly that. Names and IP addresses (a LAN IP or your Tailscale
IP) both work for `CRONRACK_DOMAIN`. Pick one:

| Option | Example | Works from |
|---|---|---|
| A local DNS name | `cronrack.home.lan` | your home network, if your router / Pi-hole / AdGuard resolves it |
| Your Tailscale MagicDNS name | `<tailscale-name>` | every device in your tailnet, at home or away |
| The server's LAN IP address | `192.0.2.50` | your home network; nothing to set up, but harder to remember |
| The server's Tailscale IP | `<tailscale-ip>` (`tailscale ip -4`) | every device in your tailnet; nothing to set up |

With an IP address, skip "Make the name resolve" below: there is nothing to resolve.

**Make the name resolve.** For a local name, add a DNS record in your router or Pi-hole
pointing the name at the server's IP. For a quick test on one Linux machine you can
also add it to that machine's `/etc/hosts`:

```bash
echo "192.0.2.50 cronrack.home.lan" | sudo tee -a /etc/hosts
```

Phones can't do that, so use router DNS or Tailscale for them. With Tailscale, install
it on the server (`curl -fsSL https://tailscale.com/install.sh | sh`, then
`sudo tailscale up`), turn on MagicDNS in the Tailscale admin console, and use the
name shown by `tailscale status`.

Check from another machine: `ping cronrack.home.lan` must show the server's IP.

### 1.4 Create `.env`

```bash
cp .env.example .env
# Write a random SECRET_KEY into .env (the exact command is also in .env.example):
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')|" .env
# Set your name from step 1.3:
sed -i "s|^CRONRACK_DOMAIN=.*|CRONRACK_DOMAIN=cronrack.home.lan|" .env
chmod 600 .env
cat .env | grep -v '^#' | grep .
```

You should see four lines: `CRONRACK_DOMAIN=...`, `SECRET_KEY=` followed by a long
random string, `BEHIND_PROXY=1` and `DEBUG=0`.

- `SECRET_KEY` signs logins. The server refuses to start without it. Keep it secret, and
  keep it the same (changing it logs everyone out). `.env` is never committed to git.
- `CRONRACK_DOMAIN` is only the name: no `https://`, no port, no slash.
- `BEHIND_PROXY=1` tells the app that Caddy sits in front of it.
- `DEBUG=0`: never set this to 1 on a server.
- `CRONRACK_ENABLE_ADMIN` (optional, not in the list above because it's commented out):
  Django's admin pages at `/admin/`. Off by default on a server, and CronRack's own
  pages cover everything, so leave it off. To turn it on, add `CRONRACK_ENABLE_ADMIN=1`
  to `.env` and run `docker compose up -d`. Its login has the same rate limit as the
  normal login page.

### 1.5 Start it

```bash
docker compose up -d
```

The first start builds the server image (a few minutes on a small machine). Then check:

```bash
docker compose ps
```

After about a minute all three services should show `(healthy)`:

```
NAME                   SERVICE     STATUS                    PORTS
cronrack-app-1         app         Up 2 minutes (healthy)    8000/tcp
cronrack-caddy-1       caddy       Up 2 minutes (healthy)    0.0.0.0:80->80/tcp, ..., 0.0.0.0:443->443/tcp, ...
cronrack-scheduler-1   scheduler   Up 2 minutes (healthy)    8000/tcp
```

Only `caddy` has `0.0.0.0:...->` entries: those are the published ports. `8000/tcp` on the
app only means "listens inside the Docker network".

There is a fourth service, `migrate`, that you don't see in that list: it applies the
database migrations, then exits, and `app` and `scheduler` only start after it finished
successfully. `docker compose ps -a` shows it as `Exited (0)`, which is what it should be.

Logs, if something isn't healthy: `docker compose logs app` (or `migrate`, `scheduler`,
`caddy`).

### 1.6 First run: create your account (do this right away)

First get the one-time **setup code** from the app's log:

```bash
docker compose logs app | grep "setup code"
```

It looks like `CronRack first-run setup code: K7QF-9M2X-HT4P-WB8C`. Only someone who can
run `docker compose` on the server can read it, so nobody else on your network can
create the account before you.

Open `https://cronrack.home.lan/` (your name) in a browser.

1. The browser warns that the certificate isn't trusted. That's expected with Caddy's own
   certificate authority (next step makes it go away). Continue anyway this once.
2. CronRack sends you to `/setup/`. Enter the setup code (dashes and upper/lower case
   don't matter), then choose a username and a strong password. There is no default
   account and no default password.

Good to know:

- The code works once. Every restart of the app makes a new one (and the old one stops
  working), so after `docker compose restart app` read the log again.
- After 5 wrong codes from one address, `/setup/` refuses that address for 15 minutes.
- Once the account exists, no code is printed any more and `/setup/` answers "not found".

### 1.7 The root certificate (trusting your server)

By default Caddy runs its own small certificate authority (CA) and signs the certificate
for your name with it (`tls internal` in `caddy/Caddyfile`). Nothing trusts that CA until you
tell it to. Its root certificate is in the `caddy_data` volume; print it to a file:

```bash
docker compose exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > cronrack-ca.crt
```

`cronrack-ca.crt` is public (it's a certificate, not a key), but install it only on your
own devices. You'll use it for:

- **Agents:** the installer gets it with `--ca-file` (Part 2). Required.
- **Your browsers (optional, removes the warning):**
  - Ubuntu/Debian desktop (Chrome, curl, ...): `sudo cp cronrack-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`.
    Firefox has its own list: Settings → Privacy & Security → Certificates → View
    Certificates → Authorities → Import.
  - iPhone: send yourself the file, open it, install the profile under Settings → General
    → VPN & Device Management, then turn it on under Settings → General → About →
    Certificate Trust Settings.
  - Android: Settings → Security → Encryption & credentials → Install a certificate → CA
    certificate (the exact menu names differ between phone makers).

The CA stays the same as long as the `caddy_data` volume exists. **Don't delete that volume**
(`docker compose down -v` would): a new CA means every agent needs the new certificate.

**Other certificate options** (edit the `tls` line in `caddy/Caddyfile`; it explains all
three): a real public domain gets a trusted Let's Encrypt certificate automatically (then
agents need no `--ca-file`), and Tailscale can issue trusted certificates for your
MagicDNS name (`<tailscale-name>`). Restart Caddy after editing:
`docker compose restart caddy` (see
[Applying Caddyfile edits](#applying-caddyfile-edits)).

### 1.8 Firewall

The server only needs **80/tcp and 443/tcp** for CronRack (plus SSH for you). With UFW:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

Good to know: ports that Docker publishes **bypass UFW** (Docker adds its own firewall
rules). What really keeps CronRack to 80 and 443 is that `docker-compose.yml` publishes
nothing else; `docker compose ps` shows it. To make CronRack reachable **only over
Tailscale**, bind Caddy to the server's Tailscale IP (`tailscale ip -4`) with a local
`docker-compose.override.yml` next to `docker-compose.yml` (Docker Compose reads it
automatically; keep it out of git):

```yaml
services:
  caddy:
    ports: !override
      - "<tailscale-ip>:80:80"
      - "<tailscale-ip>:443:443"
```

Then `docker compose up -d` again. Never forward ports 80/443 on your router to CronRack
unless you deliberately want it on the internet. It is built to be reached over your own
network or VPN.

---

## Part 2: an agent on a host

The agent runs on every machine you want to watch. Tested on Debian 12/13, Ubuntu 22.04+
and Raspberry Pi OS 12 (Bookworm). It needs Python 3.11 or newer, `python3-venv`, `curl`
and systemd:

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv curl
python3 --version   # 3.11 or newer
```

The host must resolve your server name (step 1.3): `ping cronrack.home.lan`.

### 2.1 Add the host in CronRack

In the web UI: **Add host**, give it a name (e.g. `pi`), save. The next page shows:

- the **token** for this host, **shown only once**. Keep the page open until the
  installer has asked for it;
- the install command, and under "Server uses Caddy's own certificate" the variant
  with `--ca-file`, which is the one you need with the default `tls internal`.

### 2.2 Copy the root certificate to the host

On the server (in `~/cronrack`), if you haven't already:

```bash
docker compose exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > cronrack-ca.crt
scp cronrack-ca.crt pi@raspberrypi.local:
```

(`pi@raspberrypi.local` is your user and the host's address.)

### 2.3 Run the installer

SSH to the host, go to the folder with `cronrack-ca.crt` (your home folder after `scp`),
and paste the `--ca-file` command from the Add host page. It looks like this:

```bash
curl -fsSL --cacert cronrack-ca.crt https://cronrack.home.lan/agent/install.sh \
  | sudo bash -s -- --server https://cronrack.home.lan --ca-file cronrack-ca.crt
```

`--cacert` makes curl trust your server's CA while downloading; `--ca-file` makes the
installer copy it to `/etc/cronrack/ca.crt` for the agent. The installer:

1. creates a `cronrack` system user (no login, no home, no sudo);
2. downloads the agent from **your** server and checks its SHA-256 checksum;
3. installs it to `/opt/cronrack-agent` (owned by root, so the agent can't change itself);
4. asks for the **token**. Paste it and press Enter. You won't see it while typing; that's
   on purpose, so it never lands on screen or in your shell history. It's written to
   `/etc/cronrack/agent.toml`, readable only by root and the agent (mode 640);
5. asks about Docker, **only if Docker is installed**: "Give the agent Docker access?
   [y/N]". Being in the `docker` group is equivalent to root on that host. Answer `y` only
   if you want container checks and accept that trade-off; the default is no. Without
   Docker you'll see "Docker not found: container checks will be off. Re-run this
   installer after installing Docker to enable them." That's fine; the install continues;
6. installs and starts the `cronrack-agent` systemd service, and writes
   `/opt/cronrack-agent/INSTALLED`, a small record of what is installed (2.5).

Within a few seconds the host in the web UI changes from "Waiting for first report" to
green, yellow or red.

**Kubernetes nodes (optional):** add `--k8s` on every k3s/Kubernetes node to show its role,
and `--k8s-cluster-view` on one server node to monitor the cluster read-only (it asks for
a ServiceAccount token). What it needs and how to set it up:
[kubernetes-monitoring.md](kubernetes-monitoring.md).

### 2.4 Check on it

```bash
systemctl status cronrack-agent      # should say "active (running)"
journalctl -u cronrack-agent -f      # live log; Ctrl+C to stop watching
```

A healthy log looks like:

```
INFO agent: CronRack agent starting; server https://cronrack.home.lan
INFO agent.connection: Connected; reporting every 60s (updates, reboot, containers, units, timers, disks)
```

The host's page in CronRack shows the agent's and the server's version at the bottom.

### 2.5 Upgrading an agent

**Do you need to?** An older agent keeps working with a newer server, so you only need
to upgrade an agent when the **agent** changed. `CHANGELOG.md` says when it did, and the
host's page shows the agent's version next to the server's.

On the host, run:

```bash
sudo curl -fsSL --cacert /etc/cronrack/ca.crt https://cronrack.home.lan/agent/install.sh \
  | sudo bash -s -- --server https://cronrack.home.lan --upgrade
```

- `--cacert /etc/cronrack/ca.crt` is the CA the installer copied there during the
  install, so you don't need `cronrack-ca.crt` again. `/etc/cronrack` is readable only by
  root and the agent, which is why the **curl** runs with `sudo` too.
- If your server has a publicly trusted certificate (e.g. Tailscale, Option A in the
  README), there is no `/etc/cronrack/ca.crt`: leave out `sudo` and `--cacert` before the
  `|`, as in your install command.
- `--server` must be the server the agent already uses (`server_url` in
  `/etc/cronrack/agent.toml`). Upper or lower case, a trailing `/` and an explicit `:443`
  don't matter; anything else does.

`--upgrade` **asks nothing and needs no terminal**, so you can run it from a script, cron
or Ansible. It:

1. refuses (exit 3) if no agent is installed here: run the normal install instead;
2. refuses (exit 4) if `--server` is a different server. Add `--server-change` if you
   really want to move the agent (it then updates `server_url`);
3. downloads the agent and checks its SHA-256, exactly like the install;
4. stops right there if that exact version is already installed: "already up to date
   (0.2.0)", nothing is restarted, exit 0. If the agent service is **not running** at that
   moment, it exits 7 instead and still starts nothing: something stopped it, so look at
   `systemctl status cronrack-agent` and `journalctl -u cronrack-agent` first;
5. otherwise installs the new code, updates its one library (`websockets`) in the venv,
   re-applies the owners and permissions, restarts the service and watches it for a few
   seconds to check that it **keeps running**. Whether the service starts at boot is left
   as it was: a service you disabled (`systemctl disable cronrack-agent`) stays disabled;
6. prints `Upgraded the CronRack agent: 0.1.0 -> 0.2.0` and what it kept.

**What it keeps, untouched:** the token, the CA certificate (`/etc/cronrack/ca.crt`),
the Kubernetes token and every `[kubernetes]` setting, and your current Docker choice
(on stays on, off stays off). `agent.toml` is not rewritten at all, except `server_url`
with `--server-change` and `ca_file` with `--ca-file new-ca.crt` (allowed, to replace the
CA). Other options that change settings (`--new-token`, `--k8s…`, `--network…`, `--uninstall`) are
refused with `--upgrade` (exit 2): run the install command without `--upgrade` for them.

**Automatic rollback:** if the new version doesn't keep running, the installer puts the
previous code back (with its unit file), restarts it and exits with 6: "upgrade failed,
rolled back to X.Y.Z". `INSTALLED` is only written once the new version runs, so it still
describes the old one. **Not** rolled back: the `websockets` library in the venv, and a
`server_url` or CA that this run changed with `--server-change` or `--ca-file`. If the
rollback itself doesn't come up, you get exit 5 and the agent is down. Either way, see
why with `journalctl -u cronrack-agent -n 50`.

**From a script or Ansible:** run the command in a shell with `set -o pipefail` (e.g.
Ansible's `shell` module with `executable: /bin/bash`), so a failed download (server
down, wrong CA) makes the command fail instead of giving bash an empty script that
"succeeds". Only one install or upgrade runs at a time on a host; a second one stops
with exit 1. The whole script is one function called on its last line, so a download
that breaks off half-way runs nothing.

| Exit code | Meaning |
|---|---|
| 0 | installed, upgraded, or already up to date |
| 1 | any other failure (e.g. download or checksum failed; nothing was changed) |
| 2 | invalid arguments, or `--upgrade` with an option that changes settings |
| 3 | `--upgrade`, but no agent is installed here |
| 4 | `--upgrade`, but `--server` is not the agent's server (add `--server-change`) |
| 5 | `--upgrade`: the agent did not keep running, and the rollback didn't either. The agent is **down** |
| 6 | `--upgrade`: the new version did not keep running; the previous version was put back and runs (`websockets` is not rolled back) |
| 7 | `--upgrade`: already up to date, but the agent service is not running. Nothing was changed or started; see `systemctl status cronrack-agent` and `journalctl -u cronrack-agent` |

**The `INSTALLED` file.** Every install and upgrade writes `/opt/cronrack-agent/INSTALLED`
(`root:root`, mode 644, no secrets). It's what `--upgrade` compares with, and what a
script can read to decide whether a host needs an upgrade:

```
# CronRack agent install record. Written by install.sh; do not edit.
format=1
version=0.2.0
sha256=<SHA-256 of the agent package, 64 hex characters>
server_host=cronrack.home.lan
installed_at=2026-11-02T10:00:00Z
```

One `key=value` per line, lines starting with `#` are comments. `sha256` matches the
first word of `https://<server>/agent/download/cronrack-agent.tar.gz.sha256`, so "needs an
upgrade" is simply "the two differ". `server_host` is only the host name (never the token
or the full URL); `installed_at` is UTC. New keys may be added later (`format` changes
only if the meaning of existing keys does). Agents installed before this file existed
don't have it; their first `--upgrade` writes it.

### 2.6 Re-run to change settings

Run the **install command** again (without `--upgrade`). It also upgrades the agent,
keeps `/etc/cronrack/agent.toml` and the token, and **doesn't ask for the token** unless
you pass `--new-token`. It needs a terminal, because it may ask questions.

| You want to... | Run the install command with |
|---|---|
| upgrade the agent | use `--upgrade` instead (2.5) |
| point it at a different server address | `--server https://new-name` (updates `server_url`) |
| use a new token (after "Rotate token" in the host's settings) | `--new-token` (asks for it, replaces only the token) |
| change the CA certificate | `--ca-file new-ca.crt` |
| turn Docker checks on or off | nothing extra; it asks about Docker again |
| turn on Kubernetes | `--k8s` or `--k8s-cluster-view` ([kubernetes-monitoring.md](kubernetes-monitoring.md)) |
| turn on network checks | `--network`, optionally `--network-before "NAME=IP"` (a switch before the router; **upgrade the server first**, an older server refuses reports with these points) and `--network-hop "NAME=IP"` (a box after the router) ([network-monitoring.md](network-monitoring.md)) |

The questions, in this order (each only when it applies):

1. `A Kubernetes token is already installed. Replace it? [y/N]` (with `--k8s-cluster-view`,
   if there is one) and/or `Kubernetes token:` (hidden input);
2. `Token:` (hidden input), only on the first install or with `--new-token`;
3. the Docker question, only if Docker is installed. **Its default follows your current
   choice**, so pressing Enter never changes it: `Docker access is on. Keep it? [Y/n]`
   when access is on now (only `n` removes it), `Give the agent Docker access? [y/N]`
   when it's off.

A re-run without `--ca-file` keeps using `/etc/cronrack/ca.crt` from the last install.

### 2.7 Uninstall

```bash
curl -fsSL --cacert cronrack-ca.crt https://cronrack.home.lan/agent/install.sh \
  | sudo bash -s -- --uninstall
```

This stops and removes the service, `/opt/cronrack-agent` and the `cronrack` user. It asks
before deleting `/etc/cronrack` (your config with the token, and the CA certificate). If
you keep it, it's switched to `root:root`, and a later install picks it up again. Then
delete the host in the web UI (host settings → Delete), which also invalidates its token.

---

## Part 3: updating, backing up and restoring

### 3.1 Update the server

Back up first (3.2). Then, in `~/cronrack`:

```bash
git pull                     # or: git fetch --tags && git checkout v0.2.0
docker compose up -d --build
docker compose ps            # wait until all three are (healthy) again
```

`docker compose up -d` first runs the one-shot `migrate` service, which applies any new
database migrations, and starts `app` and `scheduler` only after it succeeded. If an
update seems stuck, `docker compose logs migrate` shows why. Read `CHANGELOG.md` for
anything special.

**The database moved to `/data` inside the containers.** Earlier builds mounted the
`data` volume at `/app/server/data`; now the same volume is mounted at `/data` and the
image sets `SQLITE_PATH=/data/db.sqlite3`. The volume and the files in it stay exactly
as they are, so `git pull` and `docker compose up -d --build` keep your database. Only
if you wrote your own commands or scripts with the old path (`data/db.sqlite3` inside
the app), change them to `/data/db.sqlite3`, like the backup commands below.
Then upgrade the agents with `--upgrade` on each host (2.5), if the CHANGELOG says the
agent changed; they download the new agent from your server. An older agent keeps
working with a newer server in the meantime.

### 3.2 Back up

Everything CronRack knows (account, hosts, token hashes, settings, history) is one SQLite
database in the `data` volume: `db.sqlite3`, plus `db.sqlite3-wal` and `db.sqlite3-shm`
next to it while the app runs (SQLite's WAL mode, which lets the app and the scheduler
use the database at the same time). Don't copy those files by hand; this makes a
consistent copy while CronRack keeps running:

```bash
docker compose exec -T app python -c 'import sqlite3, sys, tempfile
with tempfile.NamedTemporaryFile() as tmp:
    sqlite3.connect("/data/db.sqlite3").backup(sqlite3.connect(tmp.name))
    sys.stdout.buffer.write(open(tmp.name, "rb").read())' > cronrack-backup-$(date +%F).sqlite3
ls -l cronrack-backup-*.sqlite3
```

Keep the backup somewhere else (another machine, your NAS). It contains your password
hash and the Discord webhook URL, so treat it as private.

Also worth keeping: your `.env` (without the same `SECRET_KEY` everyone has to log in
again after a restore; nothing else breaks), and Caddy's CA, so agents keep trusting a
rebuilt server. The CA's private key is in the `caddy_data` volume; to save the whole
volume, stop Caddy for a moment:

```bash
docker compose stop caddy
docker compose run --rm --no-deps -T --entrypoint tar caddy czf - -C /data . > caddy-data-$(date +%F).tar.gz
docker compose start caddy
```

This file contains the CA's private key (`root.key`): keep it private.

### 3.3 Restore

```bash
docker compose stop app scheduler
docker compose run --rm --no-deps -T --entrypoint python app -c 'import sqlite3, sys, tempfile
with tempfile.NamedTemporaryFile() as tmp:
    tmp.write(sys.stdin.buffer.read()); tmp.flush()
    sqlite3.connect(tmp.name).backup(sqlite3.connect("/data/db.sqlite3"))' < cronrack-backup-2026-10-13.sqlite3
docker compose up -d
```

(Use your backup's file name.) It runs as the app's own user, so the restored file has
the right owner. Agents reconnect by themselves.

To restore Caddy's volume on a new server, after creating `.env` and before the first
`docker compose up -d`:

```bash
docker compose run --rm --no-deps -T --entrypoint tar caddy xzf - -C /data < caddy-data-2026-10-13.tar.gz
```

---

## Part 4: troubleshooting

### A host is stuck on "Waiting for first report"

The agent hasn't managed to connect and report. On that host:

```bash
systemctl status cronrack-agent
journalctl -u cronrack-agent -n 50
```

| Log says | Meaning | Fix |
|---|---|---|
| `TLS certificate check failed (wrong host name or untrusted CA; see ca_file)` | The agent doesn't trust the server's certificate, or `server_url` uses a different name than the certificate | Re-run the installer with `--ca-file cronrack-ca.crt` (2.2–2.3). `server_url` must be `https://` + exactly your `CRONRACK_DOMAIN` |
| `server refused the connection (HTTP 403)` | The token is wrong, rotated or revoked, or the host was deleted | Rotate the token in the host's settings, then re-run the installer with `--new-token` |
| `gaierror` | The host can't resolve the server name | Fix DNS (1.3): `ping cronrack.home.lan` must work on the host |
| `ConnectionRefusedError`, or timeouts | Nothing answers on 443 | Is the server up (`docker compose ps`)? A firewall in between? |
| `config error: ... is accessible by others` | `/etc/cronrack/agent.toml` has the wrong mode | Run the command the message prints, then `sudo systemctl restart cronrack-agent` |
| `Connected; reporting every 60s` but still waiting | Rare; the first report failed validation | `docker compose logs app` on the server, and please open an issue |

A host that reported before and then stops shows **offline** about 1.5 to 2 minutes after
its agent disconnects (the scheduler checks every 30 seconds; the threshold is in
Settings).

### Certificate errors

- **Browser warning "not secure" / `NET::ERR_CERT_AUTHORITY_INVALID`**: expected with
  `tls internal` until you import `cronrack-ca.crt` on that device (1.7).
- **`curl: (60) SSL certificate problem: unable to get local issuer certificate`** during
  install: the `--cacert cronrack-ca.crt` part is missing, or the file isn't in the
  current folder.
- **Firefox `SSL_ERROR_INTERNAL_ERROR_ALERT`, or curl `TLS alert, internal error (592)` /
  `tlsv1 alert internal error`**, every time:
  - You opened a **name** that isn't `CRONRACK_DOMAIN`. Caddy only has a certificate for
    that one name or IP. Use exactly that.
  - `CRONRACK_DOMAIN` is an **IP address** and your `caddy/Caddyfile` has no
    `default_sni {$CRONRACK_DOMAIN}` line in its first `{ ... }` block (Caddyfiles from
    before this fix). Clients send no server name (SNI) when they connect to an IP, and
    without that line Caddy can't pick the certificate, even though its log says the
    certificate was obtained. Update to the current Caddyfile and apply it (next section).
- **A certificate name mismatch** (curl `no alternative certificate subject name matches`,
  Firefox `SSL_ERROR_BAD_CERT_DOMAIN`): you connected by an IP address while
  `CRONRACK_DOMAIN` is a name (or the other way round). Use exactly `CRONRACK_DOMAIN`.
- **It worked, and after rebuilding the server agents fail with "TLS certificate check
  failed"**: the `caddy_data` volume was deleted, so Caddy made a new CA. Restore the
  volume (3.3), or give every agent the new `cronrack-ca.crt` with `--ca-file`.

### Applying Caddyfile edits

`docker-compose.yml` mounts the whole `caddy/` folder (read-only) into the Caddy
container, not the single file. That matters because most editors, `sed -i` and `mv`
save by writing a *new* file: a single-file mount would keep showing Caddy the old copy.
With the folder mount, an edit only needs:

```bash
docker compose restart caddy
docker compose logs caddy --tail 20   # check for errors
```

**Once, when updating from an older version** that had the `Caddyfile` in the repository
root: run `docker compose up -d` (not `restart`). The Caddy container has to be recreated
to switch to the `caddy/` folder; a restart or a reboot keeps the old mount, whose file no
longer exists.

### A blank white page, or "Bad Request (400)"

- **Blank page**: you opened the server under a different name than `CRONRACK_DOMAIN`
  (Caddy answers unknown names with an empty page and never passes them on). Use exactly
  that name.
- **Bad Request (400)** is Django's `DisallowedHost`: the request reached the app with a
  name it doesn't know. With docker compose that means `.env` changed but the containers
  weren't recreated: run `docker compose up -d` after every `.env` change (a plain
  `restart` does not reread `.env`).

### The server won't start

```bash
docker compose ps
docker compose logs app
```

- `SECRET_KEY environment variable is required`: `.env` is missing the key (step 1.4).
- No `setup code` line in `docker compose logs app`: an account already exists (then
  `/setup/` answers "not found" and you log in at `/login/`), or the log was cut off;
  `docker compose restart app` prints a fresh code.
- `CRONRACK_DOMAIN must be a bare host name`: remove `https://`, ports or slashes.
- `DEBUG must be 1 or 0` (or `BEHIND_PROXY`, `CRONRACK_ENABLE_ADMIN`): a typo in a
  yes/no setting. Use `1` or `0`; a typo is an error instead of quietly meaning "off".
- `LOG_LEVEL must be one of …`, `CSRF_TRUSTED_ORIGINS entries must be origins …`,
  `SQLITE_PATH must be an absolute path …`: the message says what is expected.
- `set CRONRACK_DOMAIN in .env` from `docker compose` itself: there is no `.env` in this
  folder, or the line is missing.
- `Pool overlaps with other one on this network`: another Docker network already uses
  `172.29.80.0/24`. Change the subnet, `ip_range` and `x-caddy-ip` in
  `docker-compose.yml` to a free range (the comments there explain how).
- Port 80 or 443 `address already in use`: see the next section.

### caddy fails: port 443 already in use

`docker compose up -d` stops with `address already in use` (or `failed to bind`) for
port 443 (or 80): something else on this machine holds the port. Find out what:

```bash
sudo ss -ltnp 'sport = :443'
sudo ss -ltnp 'sport = :80'
```

(Without `sudo` you still see *that* something listens, but not which program.) A
program that listens on one address only, e.g. your Tailscale IP, is enough to block
Docker. Common causes:

- **`tailscale serve`** (or `tailscale funnel`) on the default HTTPS port. Check with
  `tailscale serve status`; remove it with `sudo tailscale serve reset`, or run your
  own serve on another port (`tailscale serve --bg --https=8443 <port>`).
- **A k3d or k3s load balancer**: k3d's `serverlb` container, or k3s' ServiceLB
  (Traefik) publishing 80/443 on the node. `docker ps` shows a k3d container with
  `0.0.0.0:443->`; for k3s, `kubectl get svc -A` shows a `LoadBalancer` on 443. Don't
  run CronRack's Compose stack and a cluster's ingress on the same machine's 443.
- **Another web server**: nginx, Apache, another Caddy (`systemctl status nginx` …).

Free the port, then **recreate caddy**:

```bash
docker compose up -d --force-recreate caddy
docker compose ps        # caddy must show 0.0.0.0:443->443/tcp under PORTS
```

`--force-recreate` matters. A caddy container that was created while the port was
busy can come back up "healthy" (its health check only asks Caddy itself) but with **no
published ports**: `docker compose ps` shows an empty PORTS column and `docker port
cronrack-caddy-1` prints nothing. A plain `docker compose up -d` or `restart` keeps
that container; recreating it publishes the ports again. Connected agents reconnect on
their own (their retry backoff tops out at 5 minutes).

### Checking the whole stack

`scripts/smoke_test.sh` builds and starts a separate, throwaway copy of the stack
(other ports, its own volumes), checks HTTPS, the redirect, that the agent socket
refuses connections without a token and that only Caddy publishes ports, then removes
it again. It doesn't touch your running CronRack or `.env`:

```bash
scripts/smoke_test.sh
```

---

## Appendix: installer test plan

`tests/install/` runs `deploy/install.sh` for real, as root, in throwaway Debian
containers with a fake `systemctl` and no network (`pytest tests/install`; it needs Docker
and is skipped without it, except in CI). It covers `--upgrade` (refusals, exit codes,
everything kept, no questions, "already up to date", rollback), the `INSTALLED` record and
the Docker question's default. What it can't cover is a real systemd and a real server, so
run these by hand on a test host (a VM or spare Pi) before a release. Each one starts from
the state the previous one left.

| # | Scenario | Steps | Expected |
|---|---|---|---|
| 1 | Fresh install, **no Docker**, `--ca-file` | Host without Docker. Run the `--ca-file` command, paste the token | No Docker question; prints "Docker not found: container checks will be off. Re-run this installer after installing Docker to enable them."; exits 0; service enabled and started; `/etc/cronrack/agent.toml` and `/etc/cronrack/ca.crt` are `root:cronrack` mode 640; `agent.toml` has `server_url`, `token`, `ca_file = "/etc/cronrack/ca.crt"`; token not visible on screen; host turns green/yellow/red in the UI |
| 2 | Wrong certificate | Run the install command **without** `--cacert` | curl fails with `(60) SSL certificate problem`; nothing installed |
| 3 | **Re-run with existing config** | Run the same command again | No token question; prints "Token kept (pass --new-token to replace it)"; `agent.toml` unchanged; service restarted |
| 4 | Re-run with only `--server` | Run with `--server https://<domain>/` and no `--ca-file` | Download still works (uses the installed `ca.crt`); `server_url` updated (trailing slash removed) |
| 5 | **`--new-token`** | Rotate the token in the UI, re-run with `--new-token`, paste the new one | "Token replaced"; exactly one `token =` line, the new one; every other line unchanged; still `root:cronrack` 640; agent reconnects |
| 6 | `--new-token`, bad input | Re-run with `--new-token`, type `not a token` | Refused with "doesn't look like a CronRack token; nothing was changed"; old token kept |
| 7 | Docker installed later | Install Docker, re-run; answer `y`, then re-run and press Enter, then re-run and answer `n` | First question ends in `[y/N]`; `y` writes `/etc/systemd/system/cronrack-agent.service.d/docker.conf` (`SupplementaryGroups=docker`); the next question is "Docker access is on. Keep it? [Y/n]" and Enter keeps the file; `n` removes it; the service is started every time |
| 7a | **`--upgrade`, already current** | Run the `--upgrade` command from 2.5 | "Already up to date (X.Y.Z)"; exit 0; `systemctl status` shows the service was not restarted |
| 7b | **`--upgrade` after a server update that changed the agent** | Update the server (3.1), run the `--upgrade` command | No questions; "Upgraded the CronRack agent: old -> new"; `agent.toml` unchanged (`sudo sha256sum` before and after); Docker drop-in unchanged; `/opt/cronrack-agent/INSTALLED` has the new version; host page shows the new agent version |
| 7c | `--upgrade` from a script | `ssh host 'sudo curl ... \| sudo bash -s -- --server ... --upgrade'; echo $?` (no terminal) | Works the same, exit 0 |
| 8 | **`--uninstall`, keep config** | `... \| sudo bash -s -- --uninstall`, answer `n` | Service stopped and disabled; unit file and drop-ins removed; `/opt/cronrack-agent` gone; `cronrack` user and group gone; `/etc/cronrack` kept as `root:root` |
| 9 | Reinstall over a kept config | Run the install command | No token question; `agent.toml` and `ca.crt` back to `root:cronrack` 640; agent connects |
| 10 | **`--uninstall`, delete config** | `--uninstall`, answer `y` | `/etc/cronrack`, `/opt/cronrack-agent` and the user are all gone |

On a real host, also check after 1, 5 and 9: `systemctl status cronrack-agent` is
active and `journalctl -u cronrack-agent` shows `Connected`.
