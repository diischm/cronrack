---
name: CronRack
description: A quiet server rack for your homelab; colour appears only where something needs you.
colors:
  graphite-ground: "#0b0c0f"
  unit-face: "#15161a"
  float-graphite: "#17181d"
  glass-fill: "rgba(255, 255, 255, 0.035)"
  glass-fill-raised: "rgba(255, 255, 255, 0.06)"
  glass-fill-pressed: "rgba(255, 255, 255, 0.09)"
  hairline: "rgba(255, 255, 255, 0.08)"
  hairline-strong: "rgba(255, 255, 255, 0.14)"
  recessed-well: "rgba(0, 0, 0, 0.35)"
  paper-white: "#f5f5f7"
  text-secondary: "rgba(235, 235, 245, 0.74)"
  text-tertiary: "rgba(235, 235, 245, 0.68)"
  rack-orange: "#f26b2e"
  rack-orange-hover: "#ff7a3d"
  rack-orange-pressed: "#e05f25"
  on-orange: "#111111"
  signal-red: "#ff453a"
  signal-red-text: "#ff968c"
  signal-yellow: "#ffd60a"
  signal-green: "#32d74b"
  signal-grey: "#8e8e93"
typography:
  page-title:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "28px"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.025em"
  page-title-desktop:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "34px"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.025em"
  section-title:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "17px"
    fontWeight: 650
    letterSpacing: "-0.01em"
  card-title:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "15.5px"
    fontWeight: 700
  body:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "14px"
    fontWeight: 500
    fontFeature: "\"cv11\""
  label:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "13px"
    fontWeight: 600
  caption:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "12px"
    fontWeight: 500
  code:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "13px"
    fontWeight: 400
rounded:
  hairline: "2px"
  inner: "10px"
  field: "12px"
  track: "14px"
  card: "16px"
  chassis: "20px"
  dock: "26px"
  pill: "999px"
spacing:
  space-1: "4px"
  space-2: "8px"
  space-3: "12px"
  space-4: "16px"
  space-5: "20px"
  space-6: "24px"
  space-8: "32px"
  gap: "16px"
  section: "32px"
components:
  button:
    backgroundColor: "{colors.glass-fill}"
    textColor: "{colors.paper-white}"
    rounded: "{rounded.pill}"
    padding: "0 18px"
    height: "44px"
  button-hover:
    backgroundColor: "{colors.glass-fill-raised}"
  button-primary:
    backgroundColor: "{colors.rack-orange}"
    textColor: "{colors.on-orange}"
    rounded: "{rounded.pill}"
    padding: "0 18px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.rack-orange-hover}"
  button-primary-pressed:
    backgroundColor: "{colors.rack-orange-pressed}"
  button-icon:
    backgroundColor: "{colors.glass-fill}"
    rounded: "{rounded.pill}"
    size: "44px"
  chip-filter:
    backgroundColor: "{colors.glass-fill}"
    textColor: "{colors.text-secondary}"
    rounded: "{rounded.pill}"
    padding: "0 13px"
    height: "34px"
  chip-filter-pressed:
    backgroundColor: "{colors.glass-fill-raised}"
    textColor: "{colors.paper-white}"
  card:
    backgroundColor: "{colors.glass-fill}"
    rounded: "{rounded.card}"
    padding: "20px"
  input:
    backgroundColor: "{colors.recessed-well}"
    textColor: "{colors.paper-white}"
    rounded: "{rounded.field}"
    padding: "0 16px"
    height: "48px"
  nav-item-current:
    backgroundColor: "{colors.glass-fill-raised}"
    textColor: "{colors.paper-white}"
    rounded: "{rounded.inner}"
    height: "44px"
  segment-selected:
    backgroundColor: "{colors.glass-fill-pressed}"
    textColor: "{colors.paper-white}"
    rounded: "{rounded.inner}"
    height: "44px"
  rack-unit:
    backgroundColor: "{colors.unit-face}"
    textColor: "{colors.paper-white}"
    rounded: "{rounded.inner}"
---

# Design System: CronRack

## Overview

**Creative North Star: "The Quiet Rack"**

CronRack looks like a server rack seen at night: graphite units in a recessed bay, held by ears
with their screws, and one orange mark (the logo) that belongs to the brand rather than to any
status. Nothing on the page asks for attention unless the rule engine says it should. When it
does, the signal is small and exact: a soft tint creeping in from a unit's left edge, a word,
a thin line icon. Everything else stays neutral grey on near-black.

The default **Glass** skin builds that rack from translucent white-on-graphite surfaces (white at
3.5–9% alpha), 1px hairlines and a recessed well, with blur only on surfaces that float over
content (the sticky top band, the phone nav dock, dialogs, toasts). Density is moderate. The
overview is one scannable column of attention cards, filter chips and rack units, and details
open inline or on their own pages. Motion is smooth and short, mostly transform and opacity, and
it all switches off together.

The rack metaphor stays (chassis, bay, units, meters, vents), but Glass has no lamps, LEDs or
glows. The pilot lamps and neumorphic hardware belong to the **Legacy** skin (see Components ›
Legacy skin).

**Key Characteristics:**
- Near-black graphite ground with layered translucent glass and hairline borders.
- One accent, orange, reserved for the logo, the primary key, "See all" links, the focus ring
  and the loader.
- Status shown by words, soft tints, short gradients and small line icons, never by colour
  alone. OK shows nothing.
- Pill-shaped keys and chips, 16px cards, 10px rack units.
- Calm motion (`cubic-bezier(0.22, 1, 0.36, 1)`), all of it off under reduced motion.
- Dark only (Glass). Light and dark themes exist only in Legacy.

## Colors

A neutral graphite palette with a single warm accent and four signal colours that appear only
when they carry meaning.

### Primary
- **Rack Orange** (`rack-orange`): the logo bars (at 100% / 60% / 35% opacity), the primary key
  (Add host), "See all" links, the 2px focus ring and the loader bars. Hover lifts to **Rack
  Orange Bright** (`rack-orange-hover`), press settles to `rack-orange-pressed`. Text on it is
  near-black (`on-orange`), never white.

### Neutral
- **Graphite Ground** (`graphite-ground`): the page background everywhere in Glass.
- **Unit Face** (`unit-face`): the solid face of a rack unit, so the status gradient has
  something opaque to mix into.
- **Float Graphite** (`float-graphite`): the island and toasts, which float above content.
- **Glass Fill / Raised / Pressed** (`glass-fill`, `glass-fill-raised`, `glass-fill-pressed`):
  cards and keys at rest, on hover / current, and the selected segment.
- **Hairline / Hairline Strong** (`hairline`, `hairline-strong`): 1px borders on cards, and on
  keys, inputs and the current-item pill.
- **Recessed Well** (`recessed-well`): inputs, segmented-control tracks and the rack bay, with a
  soft inner shade.
- **Paper White** (`paper-white`): primary text and links.
- **Text Secondary / Tertiary** (`text-secondary`, `text-tertiary`): muted and faint text. Both
  are brighter than the design reference (0.66 / 0.46) so they keep at least 4.5:1 on every surface.

### Signal (status only)
- **Signal Red** (`signal-red`): "Needs you" and offline. Red text uses the lighter
  `signal-red-text` for contrast.
- **Signal Yellow** (`signal-yellow`): "Heads up", and disk meters from 85%.
- **Signal Green** (`signal-green`): only where "working" matters (the network chain). OK rack
  units get no tint at all.
- **Signal Grey** (`signal-grey`): offline-waiting, maintenance, info, and the neutral resting
  state of the rack gradient.

### Named Rules
**The One Accent Rule.** Orange is brand, never status. If something orange appears that isn't
the logo, the primary key, a "See all" link, the focus ring or the loader, it is a bug. Current
nav items, selected segments and pressed chips are the grey pill, never orange.

**The Earned Colour Rule.** Every surface is neutral until the backend's rule engine marks it.
Colour then appears at low strength (a status tint peaks at 11% mixed into the unit face) and
always next to a word or an icon shape.

## Typography

**Interface Font (Glass):** Inter 4.1 variable, self-hosted (with system-ui, -apple-system,
Segoe UI fallbacks), using the `cv11` character variant. Glass is dark only. The standalone
install and offline pages use Inter too.
**Interface Font (Legacy):** Manrope variable, self-hosted. Legacy has light and dark themes.
**Code / Numbers:** JetBrains Mono variable, self-hosted, in both skins.

**Character:** a neutral, Apple-adjacent sans for calm reading at small sizes, with a mono for
commands, values and anything copied into a terminal. Tabular numbers on counts.

### Hierarchy
- **Page title** (700, 28px phone / 34px desktop, line-height 1.15, -0.025em): the one title on
  non-overview pages. The overview has none.
- **Section title** (650, 17px, -0.01em): headings inside pages, paired with an orange "See all".
- **Card title** (700, 15.5px): host and attention-card names, ellipsised to one line.
- **Body** (500, 14px): summaries, settings rows, key labels (600).
- **Label** (600, 13px): field labels, chips (500, 13.5px), "See all".
- **Caption** (500, 12px): readout labels, version meta, status words.
- **Wordmark** (700, 19px phone / 26px desktop, -0.01em): "CronRack" beside the bars.

### Named Rules
**The No-Shouting Rule.** Weight, not size, makes hierarchy. The type scale tops out at 34px,
and nothing on the overview is larger than the wordmark.

## Layout

A single centred column on phones, and a shell grid on desktop (≥900px) whose first row is the
sticky top bar (72px phone, 84px desktop) holding the logo, the nav pill track and the version.
Pages cap at 1600px with 32px side padding on desktop and 16px on phones, both padded for safe
areas (notch, landscape insets).

Spacing follows a 4px scale (`space-1` to `space-8`). Two fixed rhythms do most of the work:
**gap** (16px) between cards and **section** (32px) between groups. Breakpoints are 600, 700,
900 (the main phone/desktop switch), 1200 and 1500px. On phones the main nav becomes a floating
glass dock at the bottom; filter chips wrap (never scroll sideways), keep 44px targets and leave
the Add host key on the right. Tables scroll inside their own box, never the page.

**The Glanceable Column Rule.** The overview reads top to bottom with no section titles: the
answer in words ("3 things need you" / "All good") → network strip → attention cards → filter
chips + Add host → rack → clusters → recent activity. Details expand inline or live one tap away.

## Elevation & Depth

Depth comes from layering, not shadows. Surfaces at rest are flat translucent fills with a
hairline. "Lower" means a recessed well with a soft inner shade, and "higher" means a slightly
brighter fill. Shadows are rare, dark and structural, never coloured. Blur appears only where a
surface floats over moving content.

### Shadow Vocabulary
- **Well shade** (`box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.5)`): inputs and recessed tracks.
- **Float shadow** (`box-shadow: 0 18px 40px -18px rgba(0, 0, 0, 0.9)`): the phone nav dock,
  island and toasts.
- **Dialog top edge** (`box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.10)`): a neutral top
  highlight on dialogs.
- **Blur** (`blur(14px)`; strong `blur(24px) saturate(140%)`): the sticky top band (a
  three-layer progressive blur), the phone dock, dialogs, toasts. Never on cards. Browsers
  without backdrop-filter get solid fills.

### Named Rules
**The No-Glow Rule.** No coloured box-shadow, halo, neon or blurred coloured light, anywhere.
Status never casts light.

## Shapes

Soft but not bubbly. Interactive keys and chips are full pills (999px); icon keys are circles
(44px). Containers step down in radius as they nest: phone dock 26px → chassis 20px → card 16px
→ track 14px → field 12px → rack unit / inner pill 10px → meters 2px. Every container has a 1px
hairline. Rack ears carry small round screws with a slanted slot, the only skeuomorphic detail
left in Glass. Icons are inline SVG line icons at 1.7–2px stroke with round caps.

## Components

### Buttons ("keys")
- **Shape:** full pill (999px), at least 44px tall, 18px side padding; icon keys are 44px circles.
- **Default:** glass fill, strong hairline, paper-white 600 14px label.
- **Primary:** rack orange with near-black 700 label. It is the only orange key on a page
  (Add host on the overview).
- **Hover / Press:** fill brightens; a press scales to 0.96 over 180ms (transform only). Disabled
  fades to 50%; `aria-disabled` keys keep readable tertiary text on a transparent fill.
- **Danger:** a default key with `signal-red-text` label.

### Filter chips
- **Style:** 34px pill, glass fill, hairline, secondary text at 500 13.5px, words plus a count
  that rolls to its value (Number Flow).
- **State:** the pressed chip is the grey pill (`glass-fill-raised`, white text). A zero-count
  chip is `aria-disabled` but keeps at least 4.5:1. No dots in chips.

### Cards / Containers
- **Corner Style:** 16px.
- **Background:** glass fill with a 1px hairline; readouts inside use the raised fill, no border,
  12px radius.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Internal Padding:** 20px phone, 22px desktop.

### Attention cards
A neutral card per thing that needs you: name + status word ("Needs you" / "Heads up", always
visible), a one-line summary, and type + chevron, each ellipsised. Expanded issue lines start
with the small coloured line icon (triangle = needs you, circle "!" = heads up), not a dot. The header is a button; opening it grows `.g-attn-more` underneath (grid rows
0fr → 1fr). Closed cards tilt toward the pointer (mouse only, up to 8°, no glare).

### Inputs / Fields
- **Style:** 48px tall, recessed well, strong hairline, 12px radius, 500 16px text (16px stops
  iOS zoom), placeholder at 55% white.
- **Focus:** the global 2px orange outline, offset 3px.
- **Error:** `signal-red-text`, weight 600, under the field.

### Segmented control and switch
- **Segmented:** a recessed 14px track with 4px padding; the selected segment is the pressed fill
  with a hairline ring, 44px tall.
- **Switch:** 46 × 28 track. Off: dark track, light knob. On: light track, dark knob. It is
  neutral, not orange.

### Navigation
- **Desktop:** a 54px glass pill track in the top bar; items are 44px, 500 14px (current 600). A
  soft highlight follows the pointer, and the grey current-item pill slides to the clicked item
  (450ms, transform only).
- **Phone:** a floating dock at the bottom (26px radius, strong blur, float shadow), one row of
  icon + 11px label items, the current one a grey pill.
- **Never orange, never a dot.**

### Rack unit (signature)
Hosts and clusters sit as units in a recessed bay inside a chassis with ears and screws. Each
unit shows the name (+ Kubernetes role), a one-line summary, a readout panel (Updates /
Containers / Disk; for clusters Nodes / Pods / k3s), vents and a chevron. Status is a short
gradient from the left edge (the signal colour at 11% → 4% → plain face by 30% of the width),
which crossfades over 700ms when the status changes. The unit is a button: it slides out a
little on hover and expands inline when opened. A red or yellow unit shows its status word next
to the name; an OK unit shows none and has no tint. All status words sit in its accessible
name, and "Show status labels" makes the rest visible too.

### Meters
Thin (2px-radius) bars for CPU / RAM / Disk with small icons and no numbers by default. They are
neutral grey and turn yellow only when the related rule is active; Glass has no red meter. The
value shows on hover or tap.

### Network strip
One line, `You → … → Router → … → ISP box → Internet`, as small line icons joined by a line.
Only the icon colour and the line change (green, yellow, dashed grey after a break, red at the
break), with one short sentence and "More info →".

### Loader
The three logo bars wiggle with a ~120ms stagger on a quiet card, shown only after 300ms. With
motion off it fades gently instead.

### Legacy skin (summary)
Legacy is the original tactile rack, in Manrope, with light (`#E4E4E8` ground) and dark
(`#2A2A30`) themes. It has neumorphic keys with rims, lips and catch-lights; bezelled pilot lamps
with radial-gradient lenses and soft glows; screws; and a 22px card radius. Its tokens live in
`tokens.css` and its rules in `components.css` / `pages.css`. Glass overrides these tokens under
`[data-skin="glass"]` and must never change Legacy's look. Don't extend Legacy unless asked, but
keep it working in both themes.

## Do's and Don'ts

### Do:
- **Do** scope every Glass rule under `[data-skin="glass"]`; Glass loads last, so the prefix
  wins without `!important`.
- **Do** keep text at least 4.5:1 on its surface (3:1 at 24px and up), including faint text on
  glass and text on the strongest red tint.
- **Do** use the 2px orange focus ring, offset 3px, on every interactive element.
- **Do** pair every status colour with a word or an icon shape.
- **Do** animate only transform and opacity (plus the registered `--g-st` colour and the
  grid-rows expand), 180–700ms on `cubic-bezier(0.22, 1, 0.36, 1)`, and turn all decorative
  motion off when `data-motion="off"` or under `prefers-reduced-motion`.
- **Do** keep touch targets at least 44 × 44px on phones.
- **Do** keep fonts self-hosted (Inter, Manrope, JetBrains Mono); never load from a CDN.

### Don't:
- **Don't** add status dots, LEDs or lamps in Glass (the activity-row marker is the one
  exception).
- **Don't** add glows: no coloured box-shadow, halos or neon.
- **Don't** use orange for current, selected or "on" states; those are the grey pill or the
  light switch track.
- **Don't** put section titles on the overview.
- **Don't** put background pills behind label text; filter chips are the only chip pattern.
- **Don't** use emoji; use inline SVG line icons.
- **Don't** put `backdrop-filter` on cards; blur is for floating surfaces only.
- **Don't** write inline `style=""`, `<script>` or `on*=` handlers. Set dynamic sizes with
  classes, SVG attributes or `el.style.setProperty` from static JS.
