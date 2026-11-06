# Network monitoring

CronRack can tell you **where** a network problem is: your router, the ISP's box, or the ISP.
It works with any home router: no SNMP, nothing to configure on the router. One agent checks
the path from its host to the internet, point by point:

    this host → Router → ISP box (optional) → Internet

Off by default. Turn it on for one host.

## Turn it on

On the host (see [DEPLOY.md](DEPLOY.md) for the full install command):

```bash
sudo bash install.sh --server https://SERVER --network
```

It shows the default gateway it found and asks you to confirm it. To add the ISP's box (use
its LAN-side address, private IPv4):

```bash
sudo bash install.sh --server https://SERVER --network --network-hop "ISP box=192.0.2.254"
```

`--network-hop` can be repeated (at most 2, in path order). Without a terminal, pass
`--network-gateway auto` (or the router's address). `--upgrade` keeps `[network]` untouched.

### Which host?

**Best: the CronRack server's own machine.** During a router or internet outage an agent can
only report if it can still reach the server, and the one next to the server always can.
CronRack prefers it automatically: the agent notices that the server's address is its own.
Otherwise the first host that reports network data is used. You can change it on the Network
page, or on a host's settings page.

If the measuring agent itself goes offline, the strip says **Network unknown**, never an old
"Network OK".

## What agent.toml looks like

```toml
[network]
enabled = true
gateway = "auto"                     # the default route, or an address like "192.0.2.1"
hops = [{ name = "ISP box", address = "192.0.2.254" }]   # optional, at most 2
internet = ["1.1.1.1", "8.8.8.8"]    # the default; at most 4 public addresses
dns = true                           # uses the system resolver
dns_name = "example.com"             # what it looks up
public_ip_check = false              # off: it talks to a third party
public_ip_url = "https://api.ipify.org"   # only used when public_ip_check = true
```

Every target comes from this file. Nothing from the server can make an agent probe anything.

## How it measures

- 5 probes per target, 200 ms apart, 1 s timeout each. A round takes at most 8 s and runs at
  most once per 30 s, however often reports are asked for.
- **No root, no capability.** It uses the kernel's unprivileged ping socket, allowed when the
  agent's group is inside `net.ipv4.ping_group_range` (systemd's default allows everyone). The
  service's sandbox needs no change. Otherwise it falls back to TCP connects (ports 53 and 80
  locally, 53 and 443 on the internet), where a refused connection counts as an answer; a box
  that silently drops those ports can look down, and the Network page says when TCP is used.
  CronRack never changes `ping_group_range`: that's a system setting for you to decide.
- IP addresses are not logged at info level. Nothing goes to a third party unless you turn on
  `public_ip_check`.

## What it says

| Status | Meaning |
|---|---|
| Network OK | Everything answers. |
| Router down | The first point doesn't answer; everything after it is "not tested". |
| \<name\> down | Your router answers, the next point doesn't (e.g. "ISP box down"). |
| Internet down | All your own points answer, no internet address does: likely the ISP. |
| Internet slow | Even the best internet target is above the limits (150 ms p95, or 5% loss). |
| DNS failing | The internet answers, but names can't be looked up. |
| Network unknown | The measuring agent isn't reporting. |

A new status must show up in **3 reports in a row** before it counts, so a blip doesn't alert
(change it on the Network page). "Router rebooted" (down for at most 10 minutes, then back)
and "Public IP changed" are activity entries only, never alerts.

## The network is its own thing

Like a cluster, the network has its own status, its own entries in the activity log and its
own Discord alerts, with the same cooldown as hosts. The host that measures it doesn't change
colour and gets no attention card because of the network: the strip at the top of the overview
is the indicator.

Rules: `network.router_down`, `network.local_down`, `network.internet_down`,
`network.internet_slow` (threshold: p95 in ms), `network.packet_loss` (threshold: %),
`network.dns_failing`, `network.public_ip_changed`. Overrides are set in the admin like
any other rule (enabled, severity, threshold). Leave host and cluster empty: there is one
network per user, so an override scoped to a host or a cluster doesn't apply to it.

## Data kept

The latest result is part of the host's latest report. A history row per report is kept for
48 hours, at most 3 000 rows per host, for the 24 h trend on the Network page.
