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


## Kubernetes fixtures (`k8s/`)

**These come from a real (throwaway) k3d lab, sanitized.** They are the JSON the Kubernetes
API returns for read-only `GET` requests, so the parsers are tested against real field
names and shapes. Two sets of hand-made files cover what the lab didn't have.

| File | What it is |
|------|------------|
| `nodes.json`, `node_metrics.json` | `/api/v1/nodes`, `/apis/metrics.k8s.io/v1beta1/nodes` (1 server, 2 workers) |
| `pods.json` | `/api/v1/pods` (17 pods, Argo CD, k3s add-ons). Has a planted env value and annotation |
| `deployments.json`, `statefulsets.json`, `daemonsets.json` | `/apis/apps/v1/...` (Helm labels, Argo CD images) |
| `pvcs.json`, `events_warning.json` | `/api/v1/persistentvolumeclaims`, `/api/v1/events?fieldSelector=type=Warning` |
| `helmcharts.json` | `/apis/helm.cattle.io/v1/helmcharts` (k3s). Has planted `valuesContent`, `set` and `repo` |
| `argo_applications.json` | `/apis/argoproj.io/v1alpha1/applications`. Has planted Helm values/parameters and `repoURL` |
| `namespace_kube_system.json`, `version.json`, `discovery_argoproj.json` | cluster UID, `/version`, API discovery |
| `pods_broken.json` | hand-made: CrashLoopBackOff, ImagePullBackOff, ErrImagePull, CreateContainerConfigError, OOMKilled last state, long Pending, Evicted, one healthy pod |
| `deployments_unpinned.json` | hand-made: `:latest`, no tag, digest-pinned, one unavailable |
| `status_403.json`, `status_404.json`, `status_410.json` | hand-made `Status` bodies for Forbidden, NotFound and Expired |
| `list_page1.json`, `list_page2.json` | hand-made: a 2-page list, page 1 has a `continue` token |

**Planted secrets.** Strings named `SENTINEL-SECRET-1` to `-9` sit where the token can read
secrets but the agent must never copy them (env value, HelmChart `valuesContent`/`set`/`repo`,
Argo `spec.source.helm` and `repoURL`, annotations). The tests assert `SENTINEL` appears in no
parser output and not in the cluster view. `test_the_fixtures_really_contain_the_sentinels`
fails if a re-sanitize loses them.

### Re-capturing and sanitizing

1. Capture read-only, from a lab cluster (never a real one), for example
   `kubectl get pods -A -o json > raw/pods.json`, one file per resource above.
   Events: `kubectl get events -A --field-selector type=Warning -o json`.
2. Sanitize with a script kept **outside the repo** and run it with `python3 -I`. It must:
   - drop `metadata.managedFields` and the `kubectl.kubernetes.io/last-applied-configuration`
     annotation (and k3s's compressed `objectset.rio.cattle.io/applied` annotation);
   - replace every UID with a fake UUID (the same real UID always gives the same fake one);
   - rename nodes to `server-1`, `worker-1`, `worker-2` everywhere;
   - replace internal IPs (10.x, 172.16-31.x, 192.168.x) with `192.0.2.x` (TEST-NET) and MACs;
   - replace environment values and any node `K3S_TOKEN`;
   - rename personal app, namespace and registry names to `example-app`, `apps`, `ghcr.io/example/...`
     (public images such as `docker.io/rancher/*` and `quay.io/argoproj/*` stay);
   - plant the sentinel strings listed above.
3. Check each file is under 500 KB and run `gitleaks detect --no-git --source agent/tests/fixtures/k8s`.
4. Run `pytest agent`. The numbers asserted in `test_kube_cluster.py` (pod counts, workloads)
   are the ones in these files; update them if the capture changes.
