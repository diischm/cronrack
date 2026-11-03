// Glass theme motion. Tilt, Fluid Hover Highlight, Number Flow, Progressive Blur, Expandable
// Cards and Dynamic Island adapted from SmoothUI (https://github.com/educlopez/smoothui), MIT
// License, Copyright (c) 2024 Eduardo Calvo; rebuilt in plain JS (the originals are React +
// Framer Motion). See THIRD_PARTY_NOTICES.md.
//
// Loaded with defer on every page (base.html). Everything here only does something while
// <html data-skin="glass">; glass.css draws it, this file only sets classes, aria-expanded,
// the `hidden` attribute, textContent of fixed strings or numbers, and CSS custom properties
// through element.style.setProperty("--…") (CSSOM, allowed by the CSP; never a style attribute).
//
//   Tilt           the summary tiles and the "Needs attention" cards lean towards the mouse,
//                  at most 8°, and return smoothly. No glare. Mouse/trackpad only ((hover:
//                  hover) and (pointer: fine)), never on touch, never with motion off.
//                  Rack units don't tilt (glass.css slides them).
//   Nav            desktop with a mouse: a soft highlight follows the pointer across the top
//                  nav items; the grey pill sits under the current item and slides to the
//                  clicked one.
//   Units          a rack unit's Glass <button> opens / closes its .g-unit-more region inline
//                  (.is-open on the <li>, aria-expanded on the button). Delegated, so units
//                  that overview.js builds on a refresh work too (overview and /clusters/).
//   Meters         .g-meter gets its width from data-percent and fills once on page load.
//   Number Flow    each summary tile number (.g-flow) gets a column of 0–9 per digit that
//                  rolls to the value. The real number stays (visually hidden) for screen
//                  readers; overview.js keeps updating it, and a MutationObserver rolls on.
//   Scroll edge    the progressive blur under the sticky top bar shows once the page scrolled.
//   Island         the "Check all now" pill, driven by actions.js's "cronrack:progress"
//                  events. aria-hidden: the toast and the loader tell screen readers about the
//                  start, and the final "All N hosts reported" goes to #copy-status.
//   Entrance       theme-init.js set data-entrance="on"; it is removed once the first-load
//                  stagger is over.
// Motion is "allowed" only in Glass, with data-motion="on" (Settings > Appearance > Reduce
// animations off) and without prefers-reduced-motion.
(function () {
  "use strict";
  const root = document.documentElement;
  const MAX_TILT = 8; // degrees
  const ENTRANCE_MS = 1200;
  const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const desktop = window.matchMedia("(min-width: 900px)");

  function isGlass() {
    return root.getAttribute("data-skin") === "glass";
  }

  function motionOK() {
    return isGlass() && root.getAttribute("data-motion") !== "off" && !reducedMotion.matches;
  }

  // --- Entrance: first page load only ---------------------------------------------------------
  if (root.hasAttribute("data-entrance")) {
    window.setTimeout(() => root.removeAttribute("data-entrance"), ENTRANCE_MS);
  }

  // --- Tilt -------------------------------------------------------------------------------------
  const TILT_CARDS = "#g-attention-list .g-attn, .summary > .tile, .sg-glass-cards .g-attn";
  let tilted = null; // the card currently tilted
  let pending = null; // the latest pointer event, applied once per frame
  let frame = 0;

  function canTilt() {
    return motionOK() && finePointer.matches;
  }

  function resetTilt() {
    if (!tilted) {
      return;
    }
    tilted.classList.remove("g-tilting");
    tilted.style.setProperty("--g-rx", "0deg");
    tilted.style.setProperty("--g-ry", "0deg");
    tilted = null;
  }

  function applyTilt() {
    frame = 0;
    const event = pending;
    pending = null;
    if (!event) {
      return;
    }
    const card = event.target instanceof Element ? event.target.closest(TILT_CARDS) : null;
    if (card !== tilted) {
      resetTilt();
    }
    if (!card || !canTilt()) {
      return;
    }
    const box = card.getBoundingClientRect();
    if (!box.width || !box.height) {
      return;
    }
    // -1 … 1 from the card's centre.
    const nx = Math.max(-1, Math.min(1, ((event.clientX - box.left) / box.width) * 2 - 1));
    const ny = Math.max(-1, Math.min(1, ((event.clientY - box.top) / box.height) * 2 - 1));
    card.style.setProperty("--g-rx", (-ny * MAX_TILT).toFixed(2) + "deg");
    card.style.setProperty("--g-ry", (nx * MAX_TILT).toFixed(2) + "deg");
    card.classList.add("g-tilting");
    tilted = card;
  }

  document.addEventListener(
    "pointermove",
    (event) => {
      if (event.pointerType !== "mouse" && event.pointerType !== "pen") {
        return; // never on touch
      }
      pending = event;
      if (!frame) {
        frame = window.requestAnimationFrame(applyTilt);
      }
    },
    { passive: true }
  );
  // Leaving the window or pressing ends the tilt (a refresh replaces the cards anyway).
  root.addEventListener("pointerleave", resetTilt, { passive: true });
  document.addEventListener("pointerdown", resetTilt, { passive: true });
  window.addEventListener("blur", resetTilt);
  reducedMotion.addEventListener("change", resetTilt);

  // --- Nav: hover highlight + sliding pill ------------------------------------------------------
  const nav = document.querySelector(".bottom-nav");
  const navPill = nav && nav.querySelector(".g-nav-pill");
  const navHl = nav && nav.querySelector(".g-nav-hl");
  const PILL_BASE = 100; // px: the CSS width of both boxes, scaled with --g-nav-s

  function place(box, item) {
    box.style.setProperty("--g-nav-x", item.offsetLeft + "px");
    box.style.setProperty("--g-nav-s", String(item.offsetWidth / PILL_BASE));
  }

  function placePill() {
    if (!nav || !navPill) {
      return;
    }
    const current = nav.querySelector('.seg[aria-current="page"]');
    const on = isGlass() && desktop.matches && current !== null;
    nav.classList.toggle("g-nav--live", on);
    if (on) {
      place(navPill, current);
    }
  }

  if (nav && navPill && navHl) {
    nav.addEventListener("pointerover", (event) => {
      const item = event.target instanceof Element ? event.target.closest(".seg") : null;
      if (!item || event.pointerType !== "mouse" || !isGlass() || !desktop.matches) {
        return;
      }
      place(navHl, item);
      navHl.classList.add("is-on");
    });
    nav.addEventListener("pointerleave", () => navHl.classList.remove("is-on"));
    nav.addEventListener("click", (event) => {
      const item = event.target instanceof Element ? event.target.closest(".seg") : null;
      if (item && nav.classList.contains("g-nav--live")) {
        place(navPill, item); // slides there while the next page loads
      }
    });
    placePill();
    // Transitions only after the first placement, so the pill never flies in on load.
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => nav.classList.toggle("g-nav--ready", motionOK()));
    });
    desktop.addEventListener("change", placePill);
    window.addEventListener("resize", () => {
      nav.classList.remove("g-nav--ready");
      placePill();
    });
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(placePill); // Inter changes the item widths
    }
  }

  // --- Rack units: expand inline ----------------------------------------------------------------
  document.addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest("button.g-unit") : null;
    if (!button) {
      return;
    }
    const slot = button.closest(".rack__slot");
    if (!slot) {
      return;
    }
    const open = !slot.classList.contains("is-open");
    slot.classList.toggle("is-open", open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
    // Closing: its keys leave the tab order at once, not after the 0.5 s height animation.
    const region = document.getElementById(button.getAttribute("aria-controls") || "");
    if (region) {
      region.inert = !open;
    }
  });

  // --- Meters: width from data-percent, fill once on load ---------------------------------------
  document.querySelectorAll(".g-meter[data-percent]").forEach((meter) => {
    const p = Math.min(100, Math.max(0, Number(meter.getAttribute("data-percent")) || 0));
    meter.style.setProperty("--g-v", String(p));
    if (motionOK()) {
      meter.classList.add("g-meter--fill");
    }
  });

  // --- Number Flow ------------------------------------------------------------------------------
  function digitColumn() {
    const digit = document.createElement("span");
    digit.className = "g-digit";
    const col = document.createElement("span");
    col.className = "g-digit__col";
    for (let n = 0; n <= 9; n += 1) {
      const cell = document.createElement("span");
      cell.textContent = String(n);
      col.appendChild(cell);
    }
    digit.appendChild(col);
    return digit;
  }

  function flow(number, digits) {
    const value = (number.textContent || "").trim();
    if (!/^\d{1,6}$/.test(value)) {
      digits.replaceChildren(); // not a count: show the plain number instead
      number.parentElement.classList.remove("g-flowing");
      return;
    }
    while (digits.children.length < value.length) {
      digits.prepend(digitColumn());
    }
    while (digits.children.length > value.length) {
      digits.firstElementChild.remove();
    }
    Array.from(digits.children).forEach((digit, i) => {
      digit.firstElementChild.style.setProperty("--g-d", value[i]);
    });
    number.parentElement.classList.add("g-flowing");
  }

  document.querySelectorAll(".g-flow").forEach((number) => {
    const digits = document.createElement("span");
    digits.className = "g-digits g-only";
    digits.setAttribute("aria-hidden", "true");
    number.after(digits);
    flow(number, digits);
    // Roll only after the first value is in place, so the page never animates on load.
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => digits.classList.add("g-digits--live"));
    });
    new MutationObserver(() => flow(number, digits)).observe(number, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  });

  // --- Progressive blur under the top bar -------------------------------------------------------
  const edge = document.getElementById("g-edge-top");
  let edgeQueued = false;

  function updateEdge() {
    edgeQueued = false;
    edge.classList.toggle("is-on", window.scrollY > 4);
  }

  if (edge) {
    window.addEventListener(
      "scroll",
      () => {
        if (!edgeQueued) {
          edgeQueued = true;
          window.requestAnimationFrame(updateEdge);
        }
      },
      { passive: true }
    );
    updateEdge();
  }

  // --- Dynamic Island: "Check all now" ----------------------------------------------------------
  //   start  { total }               "Checking N hosts…" (accordion bars)
  //   done   { reported, total }     tick + "All N hosts reported" / "X of N hosts reported",
  //                                  sent only after the overview has actually refreshed
  //   error                          "Check failed"
  const island = document.getElementById("g-island");
  const islandText = document.getElementById("g-island-text");
  const islandCount = document.getElementById("g-island-count");
  const ISLAND_OUT_MS = 600;
  let islandTimer = 0;

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  function count(value) {
    return Number.isInteger(value) && value >= 0 ? value : 0;
  }

  function showIsland(text, state) {
    window.clearTimeout(islandTimer);
    island.classList.remove("is-done", "is-error");
    if (state) {
      island.classList.add(state);
    }
    islandText.textContent = text;
    islandCount.textContent = "";
    if (island.hidden) {
      island.hidden = false;
      if (!motionOK()) {
        island.classList.add("is-on");
        return;
      }
      // One frame in the closed state first, so it springs in.
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => island.classList.add("is-on"));
      });
    } else {
      island.classList.add("is-on");
    }
  }

  function hideIsland(afterMs) {
    window.clearTimeout(islandTimer);
    islandTimer = window.setTimeout(() => {
      island.classList.remove("is-on");
      islandTimer = window.setTimeout(
        () => {
          island.hidden = true;
        },
        motionOK() ? ISLAND_OUT_MS : 0
      );
    }, afterMs);
  }

  if (island && islandText && islandCount) {
    document.addEventListener("cronrack:progress", (event) => {
      const detail = event.detail || {};
      if (detail.action !== "check-all" || !isGlass()) {
        return;
      }
      const total = count(detail.total);
      if (detail.phase === "start") {
        showIsland(total ? "Checking " + plural(total, "host") + "…" : "Checking all hosts…", "");
      } else if (detail.phase === "done") {
        const reported = count(detail.reported);
        let text = "No host online to ask";
        if (total && reported >= total) {
          text = "All " + plural(total, "host") + " reported";
        } else if (total) {
          text = reported + " of " + plural(total, "host") + " reported";
        }
        showIsland(text, "is-done");
        hideIsland(1600);
        // The island is aria-hidden: say the result through the page's shared announcer.
        const announcer = document.getElementById("copy-status");
        if (announcer) {
          announcer.textContent = text;
        }
      } else if (detail.phase === "error") {
        showIsland("Check failed", "is-error");
        hideIsland(2500);
      }
    });
  }

  // Switching skin or motion (Settings, or another tab): drop any tilt, re-place the nav pill,
  // hide the island in Legacy.
  document.addEventListener("cronrack:appearance", () => {
    resetTilt();
    placePill();
    if (nav) {
      nav.classList.toggle("g-nav--ready", motionOK());
    }
    if (!isGlass() && island) {
      island.classList.remove("is-on");
      island.hidden = true;
    }
  });
})();
