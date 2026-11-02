// Glass theme motion. Tilt, Number Flow, Progressive Blur and Dynamic Island adapted from
// SmoothUI (https://github.com/educlopez/smoothui), MIT License, Copyright (c) 2024 Eduardo Calvo;
// rebuilt in plain JS (the originals are React + Framer Motion). See THIRD_PARTY_NOTICES.md.
//
// Loaded with defer on every page (base.html). Everything here only does something while
// <html data-skin="glass">; glass.css draws it, this file only sets classes, the `hidden`
// attribute, textContent of fixed strings or numbers, and CSS custom properties through
// element.style.setProperty("--…") (CSSOM, allowed by the CSP; never a style attribute).
//
//   Tilt + glare   overview cards (host, attention, cluster) lean towards the mouse, at most
//                  8°, with a soft glare. Mouse/trackpad only ((hover: hover) and
//                  (pointer: fine)), never on touch, never with reduced motion.
//   Number Flow    each summary tile number (.g-flow) gets a column of 0–9 per digit that
//                  rolls to the value. The real number stays (visually hidden) for screen
//                  readers; overview.js keeps updating it, and a MutationObserver rolls on.
//   Scroll edge    the progressive blur at the top shows once the page has scrolled.
//   Island         the "Check all now" pill, driven by actions.js's "cronrack:progress"
//                  events. aria-hidden: the toast and the loader already tell screen readers.
(function () {
  "use strict";
  const root = document.documentElement;
  const MAX_TILT = 8; // degrees
  const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function isGlass() {
    return root.getAttribute("data-skin") === "glass";
  }

  // --- Tilt + glare ---------------------------------------------------------------------
  // Overview cards, and the styleguide's sample cards.
  const TILT_CARDS =
    "#rack .rack-unit, #cluster-rack .rack-unit, #g-attention-list .g-attn, .sg-glass-cards :is(.g-attn, .rack-unit)";
  let tilted = null; // the card currently tilted
  let pending = null; // the latest pointer event, applied once per frame
  let frame = 0;

  function canTilt() {
    return isGlass() && finePointer.matches && !reducedMotion.matches;
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
    card.style.setProperty("--g-gx", (50 + nx * 50).toFixed(1) + "%");
    card.style.setProperty("--g-gy", (50 + ny * 50).toFixed(1) + "%");
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
  document.documentElement.addEventListener("pointerleave", resetTilt, { passive: true });
  document.addEventListener("pointerdown", resetTilt, { passive: true });
  window.addEventListener("blur", resetTilt);
  reducedMotion.addEventListener("change", resetTilt);

  // --- Number Flow ------------------------------------------------------------------------
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

  // --- Progressive blur at the top edge -------------------------------------------------------
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

  // --- Dynamic Island: "Check all now" --------------------------------------------------------
  const island = document.getElementById("g-island");
  const islandText = document.getElementById("g-island-text");
  const islandCount = document.getElementById("g-island-count");
  let islandTimer = 0;

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  function count(value) {
    return Number.isInteger(value) && value >= 0 ? value : 0;
  }

  function showIsland(text, counter, state) {
    window.clearTimeout(islandTimer);
    island.classList.remove("is-leaving", "is-done", "is-error");
    if (state) {
      island.classList.add(state);
    }
    islandText.textContent = text;
    islandCount.textContent = counter;
    island.hidden = false;
  }

  function hideIsland(afterMs) {
    window.clearTimeout(islandTimer);
    islandTimer = window.setTimeout(() => {
      if (reducedMotion.matches) {
        island.hidden = true;
        return;
      }
      island.classList.add("is-leaving");
      islandTimer = window.setTimeout(() => {
        island.hidden = true;
        island.classList.remove("is-leaving");
      }, 260);
    }, afterMs);
  }

  if (island && islandText && islandCount) {
    document.addEventListener("cronrack:progress", (event) => {
      const detail = event.detail || {};
      if (detail.action !== "check-all" || !isGlass()) {
        return;
      }
      if (detail.phase === "start") {
        showIsland("Checking all hosts…", "", "");
      } else if (detail.phase === "asked") {
        const asked = count(detail.asked);
        const offline = count(detail.offline);
        showIsland(
          asked ? "Waiting for " + plural(asked, "report") + "…" : "No host online to ask",
          offline ? offline + " offline" : "",
          asked ? "" : "is-done"
        );
      } else if (detail.phase === "done") {
        showIsland(count(detail.asked) ? "Overview updated" : "Nothing to check", "", "is-done");
        hideIsland(2000);
      } else if (detail.phase === "error") {
        showIsland("Check failed", "", "is-error");
        hideIsland(2500);
      }
    });
  }

  // Switching to Legacy (Settings, or another tab): drop any tilt and the island.
  document.addEventListener("cronrack:appearance", () => {
    if (!isGlass()) {
      resetTilt();
      if (island) {
        island.hidden = true;
      }
    }
  });
})();
