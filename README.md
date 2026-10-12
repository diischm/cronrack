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
