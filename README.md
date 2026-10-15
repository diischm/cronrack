# CronRack

**What needs my attention? Your homelab, answered in 10 seconds.**

CronRack is a self-hosted, open-source, phone-first homelab assistant. A small agent on each
host collects checks (updates, containers, systemd, disks) and reports to a self-hosted Django
server, which shows a red/yellow/green status page and alerts you only when something changes.
You reach it through your own VPN. It is never a hosted service and never phones home.

![Project Screenshot](https://i.imgur.com/Jhtz2pX.png)

## MVP

1. Agent check-ins, with alerts for hosts that stop checking in (dead-man's switch)
2. Pending package updates (security updates separated) and reboot-required flag
3. Docker container health (unhealthy, restarting, exited with error)
4. Failed systemd units and systemd timer last/next run
5. Disk usage per real filesystem
6. Mobile status page and Discord webhook alerts

## Not yet

Kubernetes checks, CVE scanning, deprecation/EOL checks, log summaries, local security posture,
cron job wrappers, approved remote actions, external heartbeat.

## What you need

- **For the server:** one always-on Linux machine with Docker and Docker Compose
  (`docker compose version` must work), plus `git`.
- **For each host you want to monitor:** Linux with systemd, Python 3.11 or newer,
  `python3-venv` and `curl` (on Debian/Ubuntu: `sudo apt install python3 python3-venv curl`).
- **A way to reach the server:** Tailscale (recommended: works from anywhere, real
  certificates, nothing to open on your router) or your home network (LAN).

## Install the server

All commands run on the server machine.

### 1. Get the files

The server image is built from the repository, so clone it:

```bash
git clone https://github.com/diischm/cronrack.git
```

```bash
cd cronrack
```

Every later server command runs in this `cronrack` folder (the one with `docker-compose.yml`).

### 2. Create the config

Copy the example file:

```bash
cp .env.example .env
```

Write a random `SECRET_KEY` into it:

```bash
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')|" .env
```

Set the name you will open the dashboard with. With Tailscale (Option A below) that's the
server's MagicDNS name, such as `myserver.tail1234.ts.net`. On the LAN (Option B) it's a
name your DNS resolves, or the server's IP address. Just the name: no `https://`, no port,
no slash.

```bash
sed -i "s|^CRONRACK_DOMAIN=.*|CRONRACK_DOMAIN=myserver.tail1234.ts.net|" .env
```

What's in `.env`:

| Variable | Required? | What it does |
|---|---|---|
| `CRONRACK_DOMAIN` | Required | The one name the dashboard and the agents use. The app accepts requests and form posts only for this name (it sets Django's allowed hosts and CSRF trusted origin from it), Caddy makes its certificate for it, and the Add host page builds the install command from it. |
| `SECRET_KEY` | Required | Signs logins. The server refuses to start without it. Keep it secret and keep it the same (changing it logs everyone out). |
| `BEHIND_PROXY` | Keep `1` | Tells the app that Caddy sits in front of it. Already set in `.env.example`. |
| `DEBUG` | Keep `0` | Never `1` on a server. Already set in `.env.example`. |
| `TIME_ZONE` | Optional | Server time zone, default `UTC`. Example: `TIME_ZONE=Europe/Berlin`. |
| `ALLOWED_HOSTS` | Optional | Extra comma-separated names the app answers to. Usually not needed. |

### 3. Start it

```bash
docker compose up -d
```

The first start builds the image, which takes a few minutes on a small machine. Check it:

```bash
docker compose ps
```

After about a minute, `app`, `scheduler` and `caddy` all show `(healthy)`.

### 4. Create your admin account

Open `https://<CRONRACK_DOMAIN>/` in a browser (for Tailscale, finish Option A first).
On the first visit CronRack sends you to `/setup/`, where you choose a username and
password. There is no default account. Do this right away: until the account exists,
anyone who can reach the server could create it. Afterwards `/setup/` is gone.

### 5. Reach it securely

Pick one.

#### Option A: Tailscale (recommended)

`tailscale serve` puts the dashboard on your tailnet at `https://myserver.tail1234.ts.net`.
Tailscale gets a real, trusted certificate for that name and renews it by itself, so
browsers show no warning and agents need no `--ca-file`. Between Tailscale and CronRack
the traffic stays inside the server machine.

1. Install Tailscale on the server and on every host you want to monitor. In the
   Tailscale admin console, on the DNS page, turn on **MagicDNS** and **HTTPS
   Certificates**. The server's name is shown on the Machines page.
2. Set `CRONRACK_DOMAIN` in `.env` to that full name, for example
   `myserver.tail1234.ts.net` (step 2). This is the only variable that needs it: the app
   builds its allowed hosts and CSRF trusted origin (`https://myserver.tail1234.ts.net`)
   from it.
3. Make Caddy listen only on the server itself, on port 8443, so Tailscale is the only way
   in. Create `docker-compose.override.yml` next to `docker-compose.yml` (Docker Compose
   reads it automatically). `!override` needs Docker Compose v2.24 or newer: check with
   `docker compose version`, and update Docker Compose if yours is older.

   ```bash
   printf 'services:\n  caddy:\n    ports: !override\n      - "127.0.0.1:8443:443"\n' > docker-compose.override.yml
   ```

   Apply it:

   ```bash
   docker compose up -d
   ```

4. Put Tailscale in front of it:

   ```bash
   sudo tailscale serve --bg https+insecure://127.0.0.1:8443
   ```

   `https+insecure` means Tailscale doesn't check Caddy's internal certificate. That's
   fine here: that connection never leaves the machine. `--bg` keeps it running after
   you close the terminal and after reboots. Check it with `tailscale serve status`.

5. Open `https://myserver.tail1234.ts.net/`.

#### Option B: Caddy's own certificate on your LAN

Without changes, Caddy listens on ports 80 and 443 and signs a certificate for
`CRONRACK_DOMAIN` with its own small certificate authority (CA). Nothing trusts that CA
yet, so:

- Your browser warns the first time. Continue anyway, or import the CA to remove the
  warning (see [docs/DEPLOY.md](docs/DEPLOY.md#17-the-root-certificate-trusting-your-server)).
- Agents need the CA's root certificate (`--ca-file`). Save it on the server:

  ```bash
  docker compose exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > cronrack-ca.crt
  ```

  Copy `cronrack-ca.crt` to each host you add (for example with `scp`). The Add host page
  shows this same command under "Server uses Caddy's own certificate".

The CA lives in the `cronrack_caddy_data` volume. Keep that volume: if it's deleted, Caddy
makes a new CA and every agent needs the new certificate. (Docker Compose puts the project
name in front of volume names. `docker-compose.yml` sets it to `cronrack` with
`name: cronrack`, so the volumes are called `cronrack_*` whatever your folder is called.
`docker volume ls` shows the real names.)

[docs/DEPLOY.md](docs/DEPLOY.md) is the longer guide: installing Docker on Ubuntu, DNS
names, the firewall, a real public domain with Let's Encrypt, and restoring backups.

## Add a host to monitor

1. In the dashboard, click **Add host**, give the host a name and click **Create**. The
   next page shows the install command and the host's token. **The token is shown only
   once**, so keep the page open until the installer has asked for it.

2. On the host, paste the install command. With Tailscale (Option A) it looks like this:

   ```bash
   curl -fsSL https://myserver.tail1234.ts.net/agent/install.sh | sudo bash -s -- --server https://myserver.tail1234.ts.net
   ```

   With Caddy's own certificate (Option B), go to the folder with `cronrack-ca.crt` and use
   the second command from the page instead:

   ```bash
   curl -fsSL --cacert cronrack-ca.crt https://<your-server>/agent/install.sh | sudo bash -s -- --server https://<your-server> --ca-file cronrack-ca.crt
   ```

   When it asks for the token, paste it and press Enter (typing is hidden on purpose).
   The installer:
   - creates a `cronrack` system user with no login, no home and no sudo;
   - downloads the agent from your server and checks its SHA-256 checksum;
   - installs it in `/opt/cronrack-agent`, owned by root so the agent can't change itself,
     with its one dependency (`websockets`, from PyPI) in its own Python environment;
   - writes the server address and token to `/etc/cronrack/agent.toml`, readable only by
     root and the agent;
   - asks whether the agent may read Docker, only if Docker is installed (the `docker`
     group is equivalent to root, so the default is no);
   - installs and starts the `cronrack-agent` systemd service.

3. The host turns green, yellow or red within a minute. If it stays on "Waiting", check
   the agent on the host:

   ```bash
   systemctl status cronrack-agent
   ```

   ```bash
   journalctl -u cronrack-agent -n 50
   ```

Installer options (`install.sh --help` prints the same):

| Option | What it does |
|---|---|
| `--server URL` | Your server's address, e.g. `https://myserver.tail1234.ts.net`. Required for installing. A re-run always updates it in the config. |
| `--ca-file PATH` | Root certificate to trust, for Caddy's own CA (Option B). Used for the download and copied to `/etc/cronrack/ca.crt` for the agent. |
| `--new-token` | On a re-run: ask for a new token. Without it, a re-run keeps the existing token. |
| `--uninstall` | Remove the agent (see below). |

## Everyday tasks

### Update the server

In the `cronrack` folder:

```bash
git pull
```

```bash
docker compose up -d --build
```

The app applies database changes on start. CSS and JavaScript file names change with
their content, so browsers pick up the new versions by themselves: no hard refresh needed.
See [CHANGELOG.md](CHANGELOG.md) for what changed.

### Update an agent

Re-run the same install command on the host. It installs the agent version your server
has and keeps the config and token, so it doesn't ask for the token again. Update the
server first.

### Uninstall an agent

On the host, with the start of your install command (add `--cacert cronrack-ca.crt` after
`curl` for Option B):

```bash
curl -fsSL https://myserver.tail1234.ts.net/agent/install.sh | sudo bash -s -- --uninstall
```

If you already have the script as a file: `sudo bash install.sh --uninstall`.

It stops and removes the service, `/opt/cronrack-agent` and the `cronrack` user, then asks
`Also delete /etc/cronrack (agent.toml with the token, ca.crt)? [y/N]`. Answer `y` to
delete it. The default keeps it (owned by root only), and a later install picks it up again.
Then delete the host in the dashboard (host settings, Delete), which also cancels its token.

### Move a host to a new server address

1. In the dashboard, open the host's settings and click **Rotate token**. The old token
   stops working, and the page shows the new token and the install command with the
   current server address.
2. On the host, run that command with `--new-token` added at the end, and paste the new
   token when asked:

   ```bash
   curl -fsSL https://<new-server>/agent/install.sh | sudo bash -s -- --server https://<new-server> --new-token
   ```

   Without `--new-token`, a re-run keeps the old token and the agent can't connect.

### Back up the server

Everything CronRack knows (account, hosts, token hashes, settings, history) is one SQLite
file, `db.sqlite3`, in the Docker volume `cronrack_data`. This makes a consistent copy
while CronRack keeps running:

```bash
docker compose exec -T app python -c 'import sqlite3, sys, tempfile
with tempfile.NamedTemporaryFile() as tmp:
    sqlite3.connect("data/db.sqlite3").backup(sqlite3.connect(tmp.name))
    sys.stdout.buffer.write(open(tmp.name, "rb").read())' > cronrack-backup-$(date +%F).sqlite3
```

Keep it somewhere else, and private: it contains your password hash and the Discord
webhook URL. Also keep a copy of `.env`. With Option B, also back up the `cronrack_caddy_data`
volume so agents keep trusting a rebuilt server. [docs/DEPLOY.md](docs/DEPLOY.md#part-3-updating-backing-up-and-restoring)
shows how, and how to restore.

## Troubleshooting

**A host stays on "Waiting".**
The agent hasn't connected. Run `journalctl -u cronrack-agent -n 50` on the host.
`TLS certificate check failed` is a certificate problem (see the last question).
`server refused the connection (HTTP 403)` means a wrong or old token: rotate it and
re-run with `--new-token`. `gaierror` means the host can't resolve the server name.

**A host shows offline.**
Its agent stopped reporting for longer than the limit under Settings ("Mark a host
offline after", 90 seconds by default). On the host, `systemctl status cronrack-agent`
shows whether it runs. On the server, `docker compose ps` must show the `scheduler` as
healthy.

**Logging in or saving a form gives "403 Forbidden" (CSRF) behind Tailscale.**
`CRONRACK_DOMAIN` must be exactly the name in the browser's address bar, e.g.
`myserver.tail1234.ts.net`, not `myserver` or an IP address. After editing `.env`, run
`docker compose up -d` (a plain `restart` doesn't reread `.env`).

**The app won't start: "SECRET_KEY environment variable is required".**
`.env` has no `SECRET_KEY`. Run the `sed` command from step 2, then `docker compose up -d`.
`docker compose logs app` shows the app's errors.

**The agent reports certificate errors.**
With Option B, the agent needs `--ca-file`: copy `cronrack-ca.crt` to the host and re-run
the install command with `--cacert` and `--ca-file` (Option B). With Option A, `--server`
must be `https://` plus the full `.ts.net` name, and the host must be on your tailnet.
If it worked before and broke after rebuilding the server, the `cronrack_caddy_data`
volume was deleted: give every agent the new `cronrack-ca.crt` with `--ca-file`.

More answers: [docs/DEPLOY.md, Part 4](docs/DEPLOY.md#part-4-troubleshooting).

## Security

The agent opens one outgoing, encrypted connection to your server and never listens on a
port, so nothing can connect to it. Each host's token is shown once and stored only as a
hash. The agent is read-only: the server can't send it commands. CronRack is never a hosted
service and never phones home.

- **Outbound only.** The server can only send fixed messages ("welcome", "collect"). It can
  never make an agent run a command; any future action is allowed only by name, from a list
  kept on the host itself.
- **Least privilege.** The agent runs as its own unprivileged user under a locked-down
  systemd service. Docker access is opt-in, because the `docker` group equals root.
- **Untrusted data.** Everything an agent reports is checked against a strict schema
  before the server stores or shows it, and every page only shows your own hosts.

## Where everything lives

On the server (paths relative to the `cronrack` folder; Docker volume names as
`docker volume ls` shows them):

| Where | What it is | Back it up? |
|---|---|---|
| `docker-compose.yml` | Defines the three services | No (in git) |
| `.env` | Your config, including `SECRET_KEY` | Yes, keep it private |
| `docker-compose.override.yml` | Only with Option A: Caddy on `127.0.0.1:8443` | Yes (or recreate it) |
| `caddy/Caddyfile` | Caddy's config | Only if you edited it |
| volume `cronrack_data` (`/app/server/data/db.sqlite3` in the app) | The database | Yes |
| volume `cronrack_static` | CSS, JS and fonts, refilled on every app start | No |
| volume `cronrack_caddy_data` | Caddy's certificates and its own CA | Yes with Option B |
| volume `cronrack_caddy_config` | Caddy's saved runtime config | No |
| `docker compose logs app` (or `scheduler`, `caddy`) | Logs, kept by Docker | No |

On each monitored host (created by `install.sh`):

| Where | What it is |
|---|---|
| `/opt/cronrack-agent/app` | Agent code (owned by root, read-only for the agent) |
| `/opt/cronrack-agent/venv` | Its Python environment (owned by root) |
| `/etc/cronrack/agent.toml` | Config and token (`root:cronrack`, mode 0640): keep private |
| `/etc/cronrack/ca.crt` | Only if installed with `--ca-file` (`root:cronrack`, 0640) |
| `/etc/systemd/system/cronrack-agent.service` | The systemd service |
| `/etc/systemd/system/cronrack-agent.service.d/docker.conf` | Only if you allowed Docker access |
| `cronrack` user and group | System user, no home, no login shell |
| `journalctl -u cronrack-agent` | Logs |

`--uninstall` removes everything in this table except `/etc/cronrack`, which it asks about
first.

## Run it locally

Everything below runs on your own machine, for development. Commands are run from the
repository root unless a step says otherwise.

### 1. Prerequisites

- Python 3.12 (`python3.12 --version`)
- git
- pipx, for pre-commit (`sudo apt install pipx` on Debian/Ubuntu)

### 2. Get the code and install

```bash
git clone https://github.com/diischm/cronrack.git
cd cronrack
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pipx install pre-commit
pre-commit install
```

`source .venv/bin/activate` only lasts for one terminal. Run it again in every new
terminal you open for CronRack.

### 3. Environment

```bash
cp server/.env.example server/.env
```

Open `server/.env` and set `DEBUG=1` (development mode: readable error pages, and
`SECRET_KEY` may stay as it is). Then load it into your shell:

```bash
set -a; . server/.env; set +a
```

Like the venv, this lasts for one terminal. `server/.env` is gitignored; never commit it,
and never use `DEBUG=1` on a real server.

### 4. Create the database

```bash
cd server
python manage.py migrate
```

The database is a single file: `server/data/db.sqlite3`.

### 5. Start the server

```bash
uvicorn cronrack.asgi:application --reload
```

(still inside `server/`). `--reload` restarts the server when you change a file.
Stop it with Ctrl+C.

### 6. First run: create the admin account

Open http://127.0.0.1:8000/. With no account yet, every page sends you to `/setup/`.
Pick a username and password there. There is no default password.

### 7. Pages to open

| URL | What it is |
|---|---|
| http://127.0.0.1:8000/ | The overview (the status page) |
| http://127.0.0.1:8000/admin/ | Django admin, with the same account |
| http://127.0.0.1:8000/dev/preview/ | The overview filled with sample hosts, to check the design. Only exists with `DEBUG=1`. Add `?state=empty` for the "no hosts" page |

Make the browser window narrower than 900px to see the phone layout, wider for the
desktop layout.

### 8. Run the scheduler

Hosts that stop reporting show as offline on the page even without the scheduler (that's
worked out when the page is built). The scheduler is what sends the offline alerts and
records the status changes in Activity, so run it to test those. In a second terminal:

```bash
source .venv/bin/activate
set -a; . server/.env; set +a
cd server
python manage.py check_hosts --loop
```

It checks every 30 seconds (change that with `--every 60`). Stop it with Ctrl+C.

### 9. Try the agent

From the repository root:

```bash
python -m agent --once
```

This runs the checks on your machine and prints the report as JSON. It does not
connect to the server yet. Without `/etc/cronrack/agent.toml` it uses the defaults.
Checks for tools you don't have (for example Docker) show `null`.

### 10. Open it from your phone (same network)

Find your computer's address on the network:

```bash
hostname -I
```

Say it prints `192.168.1.50`. Add that address to `ALLOWED_HOSTS` in `server/.env`:

```bash
ALLOWED_HOSTS=192.168.1.50,localhost,127.0.0.1
```

Then reload the environment and start the server on all network interfaces:

```bash
set -a; . server/.env; set +a
cd server
uvicorn cronrack.asgi:application --reload --host 0.0.0.0
```

On the phone, open http://192.168.1.50:8000/.

Only do this on a network you trust, such as your home Wi-Fi. `--host 0.0.0.0` lets
every device on the network reach the server, it uses plain HTTP, and `DEBUG=1`
shows detailed error pages. Go back to the plain command from step 5 when you're done.

### 11. Reset the local database

Stop the server and the scheduler, then:

```bash
rm server/data/db.sqlite3
cd server
python manage.py migrate
```

The next visit sends you to `/setup/` again. This deletes all local hosts, tokens and
activity.

### 12. Run the checks

From the repository root:

```bash
pre-commit run --all-files
pytest
```

pre-commit runs the same linters as on every commit (ruff, gitleaks, file checks);
pytest runs the agent and server tests. CI runs both, so run them before you push. Skipping the hooks with `--no-verify` won't get
anything past review.
