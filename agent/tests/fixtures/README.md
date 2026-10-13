# Test fixtures

**These files are synthetic.** They were written by hand to look like real output on
Ubuntu 24.04, not captured from a machine. Package versions, container names, timer names
and disk sizes are made up. Replace each file with real output from a homelab host as soon
as possible, then re-run the tests.

| File | Command that produces it |
|------|--------------------------|
| `apt_upgradable.txt` | `LC_ALL=C apt list --upgradable 2>/dev/null` |
| `docker_ps.txt` | `docker ps -a --format '{{json .}}'` |
| `systemctl_failed.txt` | `systemctl --failed --no-legend --plain` |
| `systemctl_timers.txt` | `LC_ALL=C TZ=UTC systemctl list-timers --all --no-legend --plain` |
| `df.txt` | `LC_ALL=C df -P -T` (note the `-T`: it adds the filesystem type column) |

To capture real output (adjust the file name for each command):

```sh
LC_ALL=C TZ=UTC systemctl list-timers --all --no-legend --plain > systemctl_timers.txt
```

Before committing real output, check it for anything you don't want in a public repo
(hostnames, usernames, internal domain names, container names that reveal too much).
