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
In scope: the agent, the server, the install script, and the
default docker compose setup.

Out of scope: your own VPN or reverse proxy configuration, and
vulnerabilities in third-party dependencies that are already
publicly known (report those upstream).
