# Security Policy

## Supported versions
CronRack is pre-release. Only the latest commit on `main` receives
security fixes. Once versioned releases exist, this section will list
which versions are supported.

## Reporting a vulnerability
Please do not open a public issue.

Report privately through GitHub:
https://github.com/diischm/cronrack/security/advisories/new

Include: what's affected (agent, server, installer), steps to
reproduce, and the impact you think it has.

## What to expect
- Acknowledgement within 7 days
- An assessment and fix plan within 30 days for confirmed issues
- Credit in the advisory, if you want it

## Scope
In scope: the agent (including its systemd sandbox and the read-only
Docker proxy), the server, the install and uninstall scripts, and the
default docker compose setup. A way to make an agent do anything beyond
reading and reporting (see docs/trust-the-agent.md, "Threat model") is
always in scope.

Out of scope: your own VPN or reverse proxy configuration, and
vulnerabilities in third-party dependencies that are already
publicly known (report those upstream).
