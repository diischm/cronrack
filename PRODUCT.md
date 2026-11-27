# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Self-hosters and homelab admins who run their own servers, Docker hosts, Kubernetes clusters and
home networks. They check CronRack between other things, on a phone or at a desk; both are primary
contexts and every screen must work equally well on each. The job is always the same: find out in
a couple of seconds whether anything needs them, and if so, what and where.

## Product Purpose

CronRack is an open-source, self-hosted monitor for servers, Docker, Kubernetes and home networks.
It answers one question: **"What needs my attention?"** A small agent on each Linux host reports
updates, reboot-required, systemd units, Docker containers, disks, CPU/RAM/temperature metrics,
Kubernetes (optional) and network checks (optional) to a self-hosted Django server. The server's
rule engine decides what is red ("Needs you"), yellow ("Heads up") or fine, shows it on the
dashboard and sends Discord alerts only when something changes.

Success: a user opens the overview and knows within seconds whether to act, without reading.

## Positioning

- Self-hosted and private by construction: no SaaS, no telemetry, no third-party requests. Users
  reach it over Tailscale or their own domain.
- The agent never listens on a port; it opens one outbound WSS connection to the server, runs
  sandboxed, and reads Docker through a read-only proxy.
- Attention-first rather than metrics-first: the rule engine (after user settings, snoozes and
  maintenance windows) decides what matters, and the interface shows only that by default.

## Operating Context

- Runs at home via Docker Compose or k3s; one always-on Linux machine hosts the server.
- Installable as a PWA on the phone (install page with QR code, offline page).
- Alerts go to Discord; quiet hours, daily summary and per-rule Alert / Show only / Off levels
  with presets (Quiet / Balanced / Everything).
- The overview refreshes itself while visible (`/overview.json` every 60s) and must degrade to an
  "Offline, retrying" note rather than going blank.

## Capabilities and Constraints

- Server: Django 6 + Channels, SQLite, uvicorn. Frontend: Django templates in `server/templates/`,
  plain CSS in `server/static/cronrack/css/` (loaded tokens → components → pages → glass), one plain
  JS file per feature in `server/static/cronrack/js/`. No build step, no framework.
- Two skins: **Glass** (default; every rule in `glass.css` scoped under `[data-skin="glass"]`) and
  **Legacy** (the older tactile rack UI). Glass is dark only (`theme-init.js` forces
  `data-theme="dark"` for it); only Legacy has light and dark themes. Motion, labels and
  entrance-animation preferences are applied by `theme-init.js` before first paint. Every
  change must work in Glass, Legacy light and Legacy dark, with every preference.
- Strict CSP: no inline `<script>`, `style=""` or `on*=` handlers; no `eval`. Dynamic sizes go
  through classes, SVG attributes or CSSOM from static JS.
- Host data (hostnames, container/unit names, mount paths, log text) is untrusted and is always
  rendered as text, never as HTML.
- Status is computed only by the backend rule engine; the frontend never derives red/yellow.
- Fonts are self-hosted: Inter for Glass (dark only) and the standalone install and offline
  pages; Manrope for Legacy (light and dark); JetBrains Mono for code and numbers in both.
- Terminology: Finding, Rule, Preset, Alert / Show only / Off, Needs you / Heads up, Subject
  (a host, a cluster or the network), Presenter.
- Templates marked `PLACEHOLDER — Apo owns the final version` are working placeholders owned by
  the frontend maintainer (Apo); keep their data usage and behaviour when redesigning.
- Open: `FRONTEND-HANDOFF.md` is referenced in code comments but is not in the repository; its
  contents are unknown and must not be assumed.

## Brand Commitments

Name and mark: **CronRack**; the logo is three horizontal orange bars stacked vertically at
100% / 60% / 32% opacity, shown large in the header. The loading animation is the same three bars
wiggling with a short stagger.

The server-rack metaphor (rack units, meters) is part of the identity. Pilot lamps and LED-style
status dots belong only to the Legacy skin; the Glass skin shows no lamps or dots.

Binding rules from the repository owner (Dimitar), recorded as given:

1. Minimal overview, readable at a glance; details live on their own pages or behind
   expand / "More info →".
2. No status dots or LED lights (sole exception: the colour marker on activity rows). Status uses
   soft tints, short gradients, small coloured line icons or a coloured line, always next to text.
3. No section titles on the overview.
4. No glows: no coloured box-shadow, halos or neon.
5. Fewer colours: neutral by default; colour only when it means something. Orange is the only
   accent. Active nav/tab items are grey/white, not orange.
6. No background pills behind label text (filter chips are fine).
7. Small inline-SVG line icons; no emoji.
8. Smooth 200–500ms motion; always honour reduced motion.
9. Subtle card tilt on hover is kept.

Status language: red = "Needs you", yellow = "Heads up", ok = usually nothing ("All good" where
needed), offline = "Offline · last seen …", maintenance = "Maintenance · until …".

## Evidence on Hand

- Sample data for the overview at `/dev/preview/` and `/dev/preview/?state=empty` (DEBUG only), and
  a style guide at `/dev/` templates (`server/templates/dev/`).
- README screenshot/GIF and the public site cronrack.xyz (separate repo, `cronrack-site`).
- No testimonials, user counts or benchmarks exist; none may be invented. Sample data uses only
  placeholder names (`node-a`, `edge-pi`, `192.0.2.x`, `example.lan`), never real homelab details.

## Product Principles

1. **Attention first.** The default view shows what needs the user now; everything else is one
   step away, not on the first screen.
2. **Prefer less.** Fewer words, fewer colours, fewer boxes. When unsure, remove.
3. **Never colour alone.** Every status carries a word or icon shape.
4. **Trustworthy by default.** Private, self-hosted, no outside requests, untrusted data treated as
   text, and a page that never blanks when the server is unreachable.
5. **Same truth everywhere.** Phone and desktop, Glass and Legacy (light and dark), with or without motion
   all show the same status with equal clarity.

## Accessibility & Inclusion

- Text contrast at least 4.5:1 (3:1 for text 24px and larger), including faint text on glass.
- Focus ring at least 3:1 against both the background and recessed wells; everything keyboard
  reachable in a logical order.
- Status never conveyed by colour alone.
- Touch targets at least 44×44px on phones; no horizontal page scroll at 360–1280px (tables scroll
  inside their own box).
- `prefers-reduced-motion` and the in-app motion setting respected everywhere.
- Expand/collapse controls expose `aria-expanded`; tabs use proper roles; charts have text
  summaries.
