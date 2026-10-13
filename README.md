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

## Security model
- The server can only request actions by name; the allowlist lives on the host. Commands from the server are never executed.
- Agent tokens are stored hashed, and all agent-reported data is treated as untrusted.

## Run it locally

Everything below runs on your own machine, for development. Commands are run from the
repository root unless a step says otherwise.

### 1. Prerequisites

- Python 3.12 (`python3.12 --version`)
- git
- pipx, for pre-commit (`sudo apt install pipx` on Debian/Ubuntu)

### 2. Get the code and install

    git clone https://github.com/diischm/cronrack.git
    cd cronrack
    python3.12 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements-dev.txt
    pipx install pre-commit
    pre-commit install

`source .venv/bin/activate` only lasts for one terminal. Run it again in every new
terminal you open for CronRack.

### 3. Environment

    cp server/.env.example server/.env

Open `server/.env` and set `DEBUG=1` (development mode: readable error pages, and
`SECRET_KEY` may stay as it is). Then load it into your shell:

    set -a; . server/.env; set +a

Like the venv, this lasts for one terminal. `server/.env` is gitignored; never commit it,
and never use `DEBUG=1` on a real server.

### 4. Create the database

    cd server
    python manage.py migrate

The database is a single file: `server/data/db.sqlite3`.

### 5. Start the server

    uvicorn cronrack.asgi:application --reload

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

The scheduler marks hosts offline when they stop reporting. In a second terminal:

    source .venv/bin/activate
    set -a; . server/.env; set +a
    cd server
    python manage.py check_hosts --loop

It checks every 30 seconds (change that with `--every 60`). Stop it with Ctrl+C.

### 9. Try the agent

From the repository root:

    python -m agent --once

This runs the checks on your machine and prints the report as JSON. It does not
connect to the server yet. Without `/etc/cronrack/agent.toml` it uses the defaults.
Checks for tools you don't have (for example Docker) show `null`.

### 10. Open it from your phone (same network)

Find your computer's address on the network:

    hostname -I

Say it prints `192.168.1.50`. Add that address to `ALLOWED_HOSTS` in `server/.env`:

    ALLOWED_HOSTS=192.168.1.50,localhost,127.0.0.1

Then reload the environment and start the server on all network interfaces:

    set -a; . server/.env; set +a
    cd server
    uvicorn cronrack.asgi:application --reload --host 0.0.0.0

On the phone, open http://192.168.1.50:8000/.

Only do this on a network you trust, such as your home Wi-Fi. `--host 0.0.0.0` lets
every device on the network reach the server, it uses plain HTTP, and `DEBUG=1`
shows detailed error pages. Go back to the plain command from step 5 when you're done.

### 11. Reset the local database

Stop the server and the scheduler, then:

    rm server/data/db.sqlite3
    cd server
    python manage.py migrate

The next visit sends you to `/setup/` again. This deletes all local hosts, tokens and
activity.

### 12. Run the checks

From the repository root:

    pre-commit run --all-files
    pytest

pre-commit runs the same linters as on every commit (ruff, gitleaks, file checks);
pytest runs the agent and server tests. CI runs both, so run them before you push.


## Development setup

    pipx install pre-commit
    pre-commit install

Hooks run on every commit: ruff (lint + format), gitleaks (secrets),
large-file and YAML checks. CI runs the same checks, so skipping hooks
with --no-verify won't get anything past review.
