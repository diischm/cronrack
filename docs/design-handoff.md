# Design handoff: the Impeccable review

A note from Apo to Dimitar. It explains what changed in the frontend after a design review,
why, and which of your decisions it touches. The only server change is one line in the service
worker's precache list (finding 13).

## a. What this is

I ran Impeccable (a design review skill for coding agents) on the frontend at
`c053ff5`: a technical audit of the whole app, and a design critique of the overview
(`/dev/preview/`) and the settings pages.

| Review | Score | Band |
|---|---|---|
| Audit (accessibility, performance, theming, responsive, code integrity) | 13/20 | Acceptable |
| Critique: Overview | 23/40 | Acceptable |
| Critique: Settings | 26/40 | Acceptable |

The fixes are in three PRs. Each merges into main on its own, in any order: #62 and #63 both
edit `notifications.html` and its tests, but in different places.

1. [#61](https://github.com/diischm/cronrack/pull/61) **ui: status never shown by colour
   alone** (findings 1, 2, 6, 7, 14, 15)
2. [#62](https://github.com/diischm/cronrack/pull/62) **ui: confirm before a preset resets
   changed rules** (finding 4)
3. [#63](https://github.com/diischm/cronrack/pull/63) **ui: one way to save across Settings,
   44px targets, Inter on install and offline** (findings 8, 10, 11, 13)

Finding numbers below are from the ranked review list, not GitHub issues.

This PR also adds `PRODUCT.md` (who CronRack is for, what it must do, your binding design rules
word for word) and `DESIGN.md` (the Glass design system as it is in the code, with a summary of
Legacy). Impeccable and other design agents read both before they change anything.

## b. What changed and why

Finding numbers are from the ranked review list (P0 = most urgent).

| Finding | Priority | What you see now | Files |
|---|---|---|---|
| 1 | P0 | Attention cards and rack units always show "Needs you" or "Heads up" next to the name. Before, the word was hidden and the card's dot was transparent, so at 360px every card looked the same. OK hosts still show nothing. | `css/glass.css` |
| 2 | P1 | The overview opens with one visible line that answers the question: "3 things need you", "All good" or "No hosts yet". It refreshes with the page. | `templates/hosts/overview.html`, `js/overview.js`, `css/glass.css` |
| 4 | P1 | Picking a preset when you have changed rules asks first: "Switch to Quiet? The 3 rules you changed go back to Quiet's settings." Focus starts on "Keep my changes". No changed rules, no question. Without JS the form posts as before, and the hint under the presets says what a preset does. | `templates/prefs/notifications.html`, `js/notifications.js` |
| 6 | P1 | Legacy's orange primary key: white text now passes 3:1 as large text on the whole gradient (see section c). | `css/tokens.css` |
| 7 | P2 | Expanded issue lines on the overview start with the small coloured line icon (triangle for needs you, circle "!" for heads up) instead of a dot. | `css/glass.css` |
| 8 | P2 | Every control on Notifications is at least 44px tall at every width: group headers, the 3-way control, the expand arrow, inputs, the volume slider, "Applies to" checkboxes and the search box. | `css/notifications.css` |
| 10 | P2 | Settings → General saves as you go, like Notifications, with a "Saved" toast. The three Save keys hide when JS runs. Without JS they post the form as before. A field the server rejects falls back to a normal submit, so the error shows next to the field. "Send test alert" waits for a pending save, so it tests the URL you just typed. The password keeps its own key. No view change was needed: the view already answers a redirect on success and 400 on errors. | `templates/prefs/index.html`, new `js/prefs.js` |
| 11 | P2 | All rule groups on Notifications start closed, each with its count. The phone page is much shorter; the search still opens every group with a match. The view's `group.open` is no longer read. | `templates/prefs/notifications.html` |
| 13 | P2 | The install and offline pages use Inter, like Glass. The service worker now precaches `InterVariable.woff2` instead of the Manrope file (the offline page was its only user), so the offline page shows Inter with no network. Inter is 352 KB against Manrope's 25 KB, stored once per install. | `css/install.css`, `css/offline.css`, `server/pwa/views.py` (`PRECACHE_STATIC`, one line) |
| 14 | P2 | OK rack units have no green tint. Only red and yellow units are tinted. | `css/glass.css` |
| 15 | P3 | The `glass.css` comments that described "the small dot" on cards and chips are rewritten to match what the code does. | `css/glass.css` |

All paths are under `server/static/cronrack/` or `server/templates/`. Tests were updated or added
in `server/tests/test_glass_calm.py`, `test_overview_glass.py` and `test_notification_views.py`.

## c. Decisions that change something you set up

**Finding 1 (#61) narrows your WCAG 1.4.1 exception in `glass.css`.** Your comment called "the dot's colour
alone tells a sighted user the status" a conscious choice, with "Show status labels" as the way
out. On the overview the dot was transparent and the word hidden, so nothing at all showed red
versus yellow: at 360px every attention card looked the same. Now:

- problem states ("Needs you", "Heads up", or the backend's own status label) always show
  their word on attention cards and rack units;
- OK shows nothing, as your status table says;
- the dot stays where it was (activity rows, host-page findings and header), and the
  "Show status labels" setting stays and still shows every other word.

**Design system token changes** (marked "Design system PR" in `tokens.css`, like the others):

| Token | Before | After | Contrast (white label, large text) |
|---|---|---|---|
| `--accent-hi` (Legacy light and dark) | `#F08551` | `#E66E3A` | 2.57:1 → 3.16:1 |

The rest of the gradient already passed: `--accent-mid` `#E0612C` 3.55:1, `--accent-lo` `#CC501F`
4.44:1. Same hue, a little deeper at the light end.

New colours on Glass use existing tokens only. The line icons are `#ff453a` (5.41:1 on a card)
and `#ffd60a` (13.05:1); the headline is `#f5f5f7` on the ground (17.96:1).

## d. Left alone on purpose

- Glass is dark only. `PRODUCT.md` and `DESIGN.md` now say so plainly; only Legacy has light
  and dark.
- The gentle overshoot easing on the island (`glass.css`, `cubic-bezier(.34,1.56,.64,1)`).
- The "Served by pod … on node …" line in the header.
- The logo (three bars, top left on phones). The review said it can read as a menu icon; I kept
  it.
- The 10px readout labels in the rack.
- The rack look: chassis, bay, units, vents.
- All other server code: no views, models, migrations or settings changed. The one Python line
  is the precache entry for finding 13 (#63).

## e. Open questions for you

1. **Finding 3, hosts appear twice.** A host that needs you shows as an attention card and again in
   the rack, with the same details when opened. On phones the rack starts below the fold. Keep
   both, or filter attention hosts out of the rack by default?
2. **Finding 5, orange or grey for selected states.** Notifications uses orange for the selected
   "Alert" segment, switches that are on, the volume slider and the preset tick. Appearance
   switches are grey, and your rule 5 says neutral by default. The agent instructions allow
   orange switches. Which one should both pages use?

## f. Limits of the review

- Impeccable's live overlay couldn't run: our strict CSP (`script-src 'self'`) blocks the script
  it injects. That's the CSP working.
- Its automatic scans of `/settings/*` only reached the login page, so the settings pages were
  checked by hand in a signed-in browser.
- Before the fixes, Legacy was checked in code only, not on screen. The fixes were then checked
  on screen in Glass, Legacy light and Legacy dark, at 360px and 1440px, with status labels on
  and off and with reduced motion on.

## g. FRONTEND-HANDOFF.md is missing

`tokens.css` ("Based on FRONTEND-HANDOFF.md section 4.1 (source of truth)"), `components.css`
and `server/hosts/presenters.py` refer to `FRONTEND-HANDOFF.md`, and many templates cite
"handoff 5.x" sections. The file isn't in the repository. Could you add it, or say where it
lives? Until then nobody, person or agent, should guess what it says.
