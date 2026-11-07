# Adding a check

A "check" is something CronRack notices on a host, a cluster or the network: a failed
service, a full disk, DNS failing. Every check is a **rule** with a stable ID such as
`disk.usage`. Adding one touches five places, in this order.

## 1. Agent payload (only if the data isn't collected yet)

- `agent/` collects it with a standard command (list arguments, a timeout, no shell) and
  adds one key to the report in `agent/report.py`. `null` when it can't be read.
- Never collect secrets or file contents. Send summaries and counts.
- Tests: `agent/tests/` for the parser, with real command output as fixtures.

## 2. Server validation

- Mirror the new key in `server/agents/serializers.py` (strict types, size limits). Agent
  data is untrusted.
- Tests: `server/tests/test_report_serializer.py`, including oversized and hostile values.

## 3. Server finding (the rule)

- Add a `Rule` to `server/rules/registry.py` (host), `server/rules/k8s.py` (cluster) or
  `server/rules/network.py` (network): `id`, `title`, `category`, `default_severity`
  (red = needs you, yellow = heads up, info), `why`, an optional copy-only `fix_template`,
  and `default_threshold` if it has a limit.
- The finding's `subject` must be a **stable key** (`mount:/`, `unit:x.service`,
  `container:nginx`, `node:node-a`), never a value or text. The fingerprint is built from
  rule ID + scope + subject, and one problem keeps one notification row (one open message,
  one "now red", one "resolved") only while that stays the same. Put numbers in `text`.
- Tests: the rule fires, doesn't fire, and uses its threshold.

## 4. RuleSpec and preset defaults

- Add a `_spec(...)` line to `server/notifications/rules.py` in the right group (Hosts,
  Services & containers, Kubernetes, Network) with a short label and description, like
  the rows on Settings → Notifications.
- Defaults come from the rule's severity: Balanced alerts on red and yellow and only shows
  info; Quiet keeps red, shows yellow, drops info; Everything alerts on all of it. Pass
  `quiet=` only when Quiet should differ.
- If the user may change the rule's threshold, give it a `limit=` with a few fixed choices
  (never free text) whose default is the rule's `default_threshold`.

## 5. Tests, then the guard test

- `server/tests/test_notification_registry.py` has the guard test: it **fails** while a
  rule has no RuleSpec, or a RuleSpec has no rule. It also checks that Balanced keeps the
  rule's severity behaviour and that limit defaults match the thresholds.
- Run `pytest` and `ruff check .`. The new rule then shows up on Settings → Notifications
  by itself, with its group, search words and 3-way control.
