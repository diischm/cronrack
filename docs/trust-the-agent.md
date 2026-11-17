# Trust the agent: what it can and can't do on your host

This page is for anyone who has to decide whether to run the CronRack agent on a machine they
care about. It states what the agent does, what stops it from doing more, and the command that
checks each claim on your own host. You don't have to take our word for any of it.

**Short version.** The agent runs as an unprivileged user in a tight systemd sandbox. It opens
one outgoing TLS connection to your CronRack server and listens on no port. The server can
send it two fixed messages and nothing else. It has no update mechanism and nothing in it runs
code it receives. If your CronRack server is fully taken over, the attacker can read the
reports your hosts send. They cannot run anything on those hosts.

Contents: [runs as](#what-it-runs-as) · [sandbox](#the-systemd-sandbox) ·
[Docker](#docker-a-read-only-proxy-not-the-docker-group) · [commands](#the-fixed-commands-it-runs) ·
[what it reads](#what-it-reads-and-never-reads) · [network](#network-one-outgoing-tls-connection) ·
[server messages](#the-two-messages-the-server-can-send) · [no auto-update](#no-auto-update) ·
[--dry-run](#see-exactly-what-is-sent---dry-run) · [egress lock](#optional-lock-outgoing-traffic-to-the-server) ·
[install](#installing-it-the-verifiable-way) · [uninstall](#removing-it) ·
[threat model](#threat-model-the-cronrack-server-is-compromised) · [not built yet](#what-does-not-exist-yet)

## What it runs as

| | |
|---|---|
| User | `cronrack`, a system user with no login shell (`/usr/sbin/nologin`), no home, no password |
| Groups | `cronrack` only. Not `docker`, not `sudo`, not `adm` |
| Privileges | none: no capabilities, `NoNewPrivileges=yes` (sudo and setuid binaries can't raise it) |
| Code | `/opt/cronrack-agent/app` and its venv, owned by root. The agent can't change its own code |
| Config | `/etc/cronrack/agent.toml`, `root:cronrack 0640`. The agent can read its token but not rewrite its config |
| Writes | nothing that lasts. The whole filesystem is read-only to it (`ProtectSystem=strict`); it only gets a private, empty `/tmp` that is thrown away when it stops |

Check it yourself:

```bash
id cronrack
getent passwd cronrack
ls -l /etc/cronrack/ /opt/cronrack-agent/
systemctl show cronrack-agent -p User -p Group -p SupplementaryGroups -p NoNewPrivileges
```

## The systemd sandbox

The unit file in the repository, [`deploy/cronrack-agent.service`](../deploy/cronrack-agent.service),
is the exact file `install.sh` installs. It comes inside the checksummed package. On top of
the user and the read-only filesystem it sets:

| Setting | Effect |
|---|---|
| `CapabilityBoundingSet=` `AmbientCapabilities=` | no Linux capabilities at all |
| `ProtectHome=yes` `PrivateTmp=yes` `PrivateDevices=yes` | `/home`, `/root` and `/run/user` invisible; private `/tmp`; no real devices in `/dev` |
| `ProtectProc=invisible` | other users' processes are hidden in `/proc` |
| `ProtectKernelModules/KernelLogs/KernelTunables/ControlGroups/Clock/Hostname=yes` | can't load modules, read the kernel log, change sysctls, cgroups, the clock or the host name |
| `RestrictNamespaces=yes` `RestrictRealtime=yes` `RestrictSUIDSGID=yes` `LockPersonality=yes` | no new namespaces, no realtime scheduling, no setuid files, no personality changes |
| `MemoryDenyWriteExecute=yes` | no memory that is both writable and executable (no injected machine code) |
| `SystemCallFilter=@system-service` plus `~@privileged @resources @mount @swap @reboot @module @raw-io @debug @cpu-emulation @obsolete @clock` | only ordinary service system calls; blocked calls fail with `EPERM` |
| `SystemCallArchitectures=native` | no 32-bit system call side door |
| `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK` | IP (server, opt-in network checks, Kubernetes API), unix (D-Bus for `systemctl`, the Docker proxy), netlink (address lookups). No raw or packet sockets |
| `InaccessiblePaths=-/run/docker.sock -/run/containerd -/run/k3s/containerd -/run/podman -/run/snapd.socket ...` | container engine sockets are hidden, even if someone made one world-writable |
| `UMask=0077` `RemoveIPC=yes` | anything it could create is private; IPC objects are removed when it stops |

**Score.** `systemd-analyze security` rates units from 0 (locked down) to 10 (exposed):

| Unit | Before (agent 0.4.0) | Now (agent 0.5.0) |
|---|---|---|
| `cronrack-agent.service` | 7.8 EXPOSED | 1.3 OK |
| `cronrack-docker-proxy.service` | (didn't exist) | 0.4 SAFE |

The remaining points come from things the agent needs: it must reach the network (to talk to
the server), use IP and unix sockets, and read `/proc/stat` and `/proc/meminfo` for the
metrics (so `ProcSubset=pid` is off).

One thing the sandbox doesn't decide: the agent can talk to the system D-Bus (that's how
`systemctl` asks for failed units and timers). What an unprivileged user may do over D-Bus,
such as starting or stopping a unit, is decided by polkit, and its defaults refuse that to
a system user like `cronrack` without a login session. Check with
`sudo -u cronrack systemctl restart cron` (it must fail with "Access denied" or
"Interactive authentication required").

An optional [egress lock](#optional-lock-outgoing-traffic-to-the-server)
brings it lower still.

Check it yourself:

```bash
systemd-analyze security cronrack-agent
systemd-analyze security cronrack-docker-proxy
systemctl cat cronrack-agent                # the unit plus any drop-ins
```

## Docker: a read-only proxy, not the docker group

Anyone in the `docker` group can start a container that mounts `/` and change any file: the
group is root in all but name. So the agent is never added to it.

When you answer yes to "Give the agent Docker access?", `install.sh` installs a small proxy
instead, [`agent/docker_proxy.py`](../agent/docker_proxy.py) (standard library only):

- `cronrack-docker-proxy.socket`: systemd creates `/run/cronrack/docker.sock` as
  `root:cronrack 0660`, so only the agent's group can connect. No network port.
- `cronrack-docker-proxy.service`: the proxy, started by systemd on the first connection. It
  runs as a throwaway user (`DynamicUser=yes`) whose only extra group is `docker`, with
  **no network at all** (`PrivateNetwork=yes`, `IPAddressDeny=any`,
  `RestrictAddressFamilies=AF_UNIX`) and the same sandbox as the agent. Its only input is the
  socket systemd hands it. Started any other way, it refuses to run.

**The allowlist** (in code, `ROUTES` in `docker_proxy.py`) is exactly three routes. Only these
requests pass, as exact matches, and the agent uses all three:

| Request | What the agent gets back |
|---|---|
| `GET /version` | `ApiVersion` |
| `GET /v1.N/containers/json` or `?all=1` | per container: `Id`, `Names`, `State`, `Status` |
| `GET /v1.N/containers/<hex id>/stats?stream=false[&one-shot=true]` | CPU, memory and network counters (network names replaced by `net0`, `net1`, ...) |

Container inspect (`GET /v1.N/containers/<id>/json`) is **not** on the list: the agent doesn't
need it, and its answer is where a container's environment variables live. Crash loops are
seen from the list's `State` ("restarting") instead.

Everything else is answered with **403 before Docker is asked**: every other method (`POST`,
`PUT`, `DELETE`, ...), every other path (inspect, `exec`, `create`, `start`, `attach`, `logs`, `export`,
`archive`, `build`, `images`, `volumes`, `secrets`, `events`, ...), any other query, and any
path trick (`..`, `%2e`, doubled slashes, a trailing slash, absolute URLs). Requests with a body,
`Transfer-Encoding`, `Upgrade` or `Expect` are refused too. Headers from the agent are not
passed on, and only one request per connection is read: a second one sent along is dropped.
It answers at most 4 connections at once, each must send its request within 5 seconds, and
a fifth gets 503 straight away, so a stuck client can't block it.

**The answers are rebuilt**, not passed through. Only the fields in the table above are copied
into a new JSON answer. Container environment variables, commands, labels, mounts, host config,
network addresses and health check output never leave the proxy, even if the agent were
compromised and asked for them. The tests plant a secret environment value in a container and
check it never reaches the agent (`agent/tests/test_docker_proxy.py`).

Check it yourself:

```bash
id cronrack                                    # no docker group
ls -l /run/cronrack/docker.sock                # srw-rw---- root cronrack
systemctl cat cronrack-docker-proxy.service
# As the agent user: allowed, then refused.
sudo -u cronrack curl -s --unix-socket /run/cronrack/docker.sock http://d/version
sudo -u cronrack curl -s -o /dev/null -w '%{http_code}\n' --unix-socket /run/cronrack/docker.sock \
  -X POST http://d/v1.45/containers/create     # 403
sudo -u cronrack curl -s -o /dev/null -w '%{http_code}\n' --unix-socket /run/cronrack/docker.sock \
  http://d/v1.45/images/json                   # 403
journalctl -u cronrack-docker-proxy -n 20      # each refusal is logged (method only)
```

If the proxy doesn't answer while Docker access is on, or Docker behind it doesn't, the
server shows the heads-up **Docker proxy unavailable** (`agent.docker_proxy_unavailable`).

## The fixed commands it runs

Commands are fixed lists of words in [`agent/collectors.py`](../agent/collectors.py), run without
a shell, with a 15-second timeout, a fixed `PATH`, `LC_ALL=C` and `TZ=UTC`:

| Check | Command |
|---|---|
| Updates | `apt list --upgradable` |
| Failed services | `systemctl --failed --no-legend --plain` |
| Timers | `systemctl list-timers --all --no-legend --plain` |
| Disks | `df -P -T` |
| Kubernetes role (opt-in) | `systemctl is-active k3s`, `... k3s-agent`, `... kubelet`, then `k3s --version`, `kubelet --version`, `helm version --short` |

That's all of them. Nothing the server sends becomes a command, an argument or a file path.

The guard tests ([`agent/tests/test_guards.py`](../agent/tests/test_guards.py)) read the agent's
source as syntax trees, with renamed imports resolved, and fail CI if code is added that runs
a shell or a non-literal command, uses `eval`/`exec`/`pickle`/`importlib` or `__builtins__`
tricks, writes files, changes `sys.path`, fetches URLs, or listens for connections. They are a
tripwire against such code slipping in, not proof against someone who sets out to hide it:
reading the diff, and checking the package hash, is what covers that.

## What it reads, and never reads

It reads:

- `/etc/cronrack/agent.toml` (its config) and the files it names (CA certificates, the
  Kubernetes token file)
- `/etc/os-release` (the OS name), and whether `/var/run/reboot-required` exists
- `/proc/stat`, `/proc/loadavg`, `/proc/meminfo`, `/proc/diskstats`, `/proc/self/mountinfo`,
  `/proc/net/route` and `/sys/class/thermal/*/{type,temp}` (metrics and the default gateway)
- with Docker access, the proxy's stripped answers above
- with the Kubernetes cluster view (opt-in), read-only `GET`s of nodes, pods, events,
  persistent volume claims, deployments, statefulsets, daemonsets, node metrics, k3s HelmCharts
  and Argo CD applications. Not Secrets. Only status fields go into the report; pod specs and
  env values are dropped ([docs/kubernetes-monitoring.md](kubernetes-monitoring.md))

It never reads: container environment variables, file contents (beyond the files above),
SSH keys of any kind, logs (no `journalctl`, no container logs), shell history, or anything in
`/home` and `/root` (they are hidden from it). Reports hold summaries and counts: package
counts, unit and container names and states, disk percentages, metric numbers.

## Network: one outgoing TLS connection

- It connects out to `server_url` from its config (`wss://.../ws/agent/`) and keeps that one
  connection open. It listens on no port: `sudo ss -tlpn | grep -i python` shows nothing for it.
- TLS certificate checking is always on: `CERT_REQUIRED`, host name check, TLS 1.2 minimum.
  There is no switch to turn it off. `ca_file` adds a private CA (e.g. Caddy's internal
  one); it never replaces verification ([`agent/connection.py`](../agent/connection.py)).
- Plain `ws://` is accepted only to `localhost`, `127.0.0.1` and `::1`.
- The token is sent once per connection in the `Authorization` header. It is never logged.
- The only other traffic is what you switch on in `agent.toml`: network checks
  (`[network]`, to the router and targets you list) and the Kubernetes API.

## The two messages the server can send

[`agent/protocol.py`](../agent/protocol.py), `parse_server_message()`, is the only door:

```json
{"type": "welcome", "interval_seconds": 60, "enabled_checks": ["updates", "disks"]}
{"type": "collect"}
```

- `welcome` sets the report interval (10 to 86,400 seconds) and can only *narrow* the checks
  `agent.toml` already allows. It can't add a check.
- `collect` asks for a report now (at most one every 5 seconds). The most a server can
  cause this way is extra load: `apt list`, `df` and the other checks every few seconds.
- Anything else is dropped: an unknown type, an extra or missing field, a wrong type, a value
  out of range, more than 64 KiB, a binary frame, invalid JSON. Only the message type is
  logged, never the content. The tests feed it hostile messages (`exec`, `update`,
  `action`, look-alike type names, shell syntax in check names).

## No auto-update

The agent has no update code: nothing downloads, unpacks or replaces anything, and it can't
write to its own code (`/opt/cronrack-agent` is root's, the filesystem is read-only to it).
It changes only when **you** run `install.sh` as root. The guard tests fail if a download path,
a file write, `sys.path` changes or dynamic imports are added.

## See exactly what is sent: --dry-run

```bash
cd /opt/cronrack-agent/app
sudo -u cronrack ../venv/bin/python -m agent --dry-run --config /etc/cronrack/agent.toml
```

It builds one report with the same code, user and config as the service (but outside the
service's sandbox), prints the exact JSON it would send, and exits. It doesn't connect to the server and doesn't need the
token. `--once` is the older name for the same thing. Opt-in network checks still probe the
points in your `[network]` table.

## Optional: lock outgoing traffic to the server

```bash
sudo bash install.sh --server https://SERVER --egress-lock
```

This writes `/etc/systemd/system/cronrack-agent.service.d/egress.conf`:

```ini
[Service]
IPAddressDeny=any
IPAddressAllow=127.0.0.53 192.0.2.10
```

The addresses are your server's, resolved at install time, plus the DNS servers in
`/etc/resolv.conf` (`127.0.0.53` with systemd-resolved), plus the Kubernetes API if the
cluster view is on. Exact addresses only: not all of `localhost`, which would let it reach
every local service. The installer shows the list before it asks "Continue?", and stops
without changing anything if the server's name doesn't resolve.

**If the server's address changes, re-run the installer**: a normal re-run (and
`--upgrade --server-change`) resolves and rewrites the lock. A plain `--upgrade` keeps it as
it is. `--no-egress-lock` removes it. The agent can still send DNS queries to your resolvers;
the lock stops direct connections, not a determined DNS tunnel.

It is refused while network checks are on. Those must reach your router, a LAN switch,
public addresses such as `1.1.1.1`, a DNS name and, optionally, a public-IP service whose
address changes. So that agent can't be locked to one address. Run network checks on one
host only (ideally the server's own machine) and lock the others.

Check it: `systemctl show cronrack-agent -p IPAddressDeny -p IPAddressAllow`.

## Installing it the verifiable way

Don't pipe it into a shell. Download it, read it, check the package, then run it:

```bash
curl -fsSLO https://SERVER/agent/install.sh
less install.sh
```

`install.sh` downloads the agent package from your server and checks it against the server's
`.sha256`. That only proves the download is complete, because both come from the same server.
To check the package against something the server doesn't control, build its hash yourself
from the source. The package is built byte for byte the same way on any machine:

```bash
git clone https://github.com/diischm/cronrack && cd cronrack
git checkout <the version your server runs>
python3 scripts/agent-package-sha256.py        # first line: the package's SHA-256
```

Then install with that hash. Anything else is refused before it is unpacked:

```bash
sudo bash install.sh --server https://SERVER --expect-sha256 <hash>
```

Before changing anything, `install.sh` prints what it will do (user, files, Docker, egress lock,
service) and asks `Continue? [Y/n]`. For scripts and Ansible, `--yes` skips that question.
With no terminal, every other question takes its default (the current setting), and a fresh
install still needs a terminal to type the token. Without a terminal and without `--yes` it
refuses rather than waiting.

Signed releases (Sigstore/cosign signatures on a `SHA256SUMS` file) are planned with the
release pipeline. Until then the source checkout above is the independent check.

## Removing it

```bash
sudo bash install.sh --uninstall           # or: sudo bash uninstall.sh
sudo bash install.sh --uninstall --purge   # also delete /etc/cronrack (the token)
```

[`deploy/uninstall.sh`](../deploy/uninstall.sh) stops and removes `cronrack-agent.service`,
`cronrack-docker-proxy.socket` and `.service`, the drop-in directory (egress lock),
`/opt/cronrack-agent`, `/run/cronrack` and the `cronrack` user and group, and lists each thing
it removed. It asks before deleting `/etc/cronrack`; kept, it is given to root. It downloads
nothing. CronRack keeps no files in `/var`. Delete the host on the server too.

## Threat model: the CronRack server is compromised

Assume an attacker fully controls your CronRack server: its code, its database, its TLS key.
What can they do to a host running the agent?

**They can see its reports, and nothing else.** In detail:

| They can | Because |
|---|---|
| Read every report the hosts send (package counts, unit and container names and states, disks, metrics, Kubernetes status) | that's the agent's job |
| Collect the agent tokens as agents connect | tokens authenticate agents *to* the server; they give no access to the hosts |
| Make agents report more often (every 10 s at most, or on `collect`, at most one every 5 s) or report fewer checks | `welcome` and `collect` allow exactly that |
| Stop talking to agents | then the agents retry in the background |

| They cannot | Because |
|---|---|
| Run a command, script or code on a host | the server can send only `welcome` and `collect`; nothing received is executed; commands are fixed lists ([protocol](#the-two-messages-the-server-can-send), [guard tests](../agent/tests/test_guards.py)) |
| Push an update to the agent | there is no update code, and the agent can't write its own files ([no auto-update](#no-auto-update)) |
| Turn on a check the host's `agent.toml` doesn't allow, or point the agent at another server | the server can only narrow the checks; `server_url` and everything else come from the root-owned config |
| Use the agent to control Docker | the agent isn't in the docker group, and the proxy allows only the read-only GETs above |
| Read container secrets, files, SSH keys or logs | the agent doesn't read them, and the proxy strips them |
| Reach the host through the agent | the agent listens on no port |

**The one exception is when you run the installer.** `install.sh` runs as root and installs
what the server serves. A compromised server could hand out a modified installer or package,
and you would run it with root rights at your next install or upgrade. That's why the install
section above says to read `install.sh` before running it and to pass `--expect-sha256` with a
hash built from the source. With `--expect-sha256`, a package the attacker changed is refused.
Read install.sh itself before you run it: no checksum can vouch for a script the server hands
you.

Two more parts of that install-time trust, stated plainly:

- The installer runs `pip install --require-hashes` for `websockets==17.2` from PyPI, as
  root. [`agent/requirements.txt`](../agent/requirements.txt) lists the SHA-256 of every file
  PyPI publishes for that version (each wheel for each platform, plus the source archive), and
  pip refuses any other file. So a changed file on PyPI or on the way is refused. What you
  still trust is the list itself, which comes in the checksummed package.
- **PyPI only.** That pip ignores the host's own pip setup: `PIP_CONFIG_FILE=/dev/null` skips
  every `pip.conf`, `--isolated` skips the `PIP_*` environment variables, and
  `--index-url https://pypi.org/simple` is the one index. This matters on a Raspberry Pi:
  Raspberry Pi OS adds [piwheels](https://www.piwheels.org) in `/etc/pip.conf`, and piwheels
  builds its own wheels, whose hashes aren't PyPI's. Without this, pip could pick a piwheels
  wheel (on 32-bit Pi OS especially) and then refuse it.
- **A platform with no matching wheel** on PyPI gets the source archive instead. Its hash is
  in the list too, so pip builds from the checked file. To build it, pip first fetches its
  build tools (setuptools) from PyPI into a temporary build environment; pip does not check
  those against hashes. If the build needs a C compiler that isn't there, websockets installs
  without its optional C speedups and works the same, a little slower.
- `--upgrade` asks nothing so it can run from scripts. Running it **unattended** (cron,
  Ansible on a timer) means every upgrade installs whatever the server serves at that time,
  without anyone reading it. Upgrade by hand, or pin `--expect-sha256` in your automation.

The same holds for a man in the middle: TLS verification can't be switched off, and without the
server's private key nobody can impersonate the server.

## What does not exist yet

Said plainly, so nothing here reads as more than it is:

- **No privileged helper.** CronRack's design includes an opt-in, root-owned oneshot service
  for checks that need root. It has not been built. So the SSH, UFW, Fail2Ban,
  listening-port and unattended-upgrades checks **are not available** in this version, and
  no CronRack process runs as root. The one process with more than the agent's rights is the
  [Docker proxy](#docker-a-read-only-proxy-not-the-docker-group), and only if you turn Docker
  access on: it is in the docker group, which is why it has no network and passes on only
  three read-only requests.
- **No actions.** The `actions` table in `agent.toml` is reserved and ignored. The agent can't
  restart, update or change anything.
- **No signed releases yet.** See [Installing it the verifiable way](#installing-it-the-verifiable-way).

## Reporting a problem

Found a way around any of this? Please report it privately, as described in
[SECURITY.md](../SECURITY.md).
