// PLACEHOLDER — Apo owns the final version.
// Phone floating nav (base.html .nav-dock, pages.css): scrolling down slides it out of
// view, scrolling up brings it back. Only toggles classes; all looks and motion live in
// pages.css, which also ignores these classes from 900px (desktop top bar).
//
// Always shown: near the top or bottom of the page, when the page can't scroll, while
// anything inside it has keyboard focus, and with prefers-reduced-motion. Hidden while
// the host preview <dialog> is open (it's a bottom sheet too). Hidden means moved off
// screen with a transform: still in the accessibility tree and reachable with Tab.
(function () {
  "use strict";
  const dock = document.getElementById("nav-dock");
  if (!dock) {
    return; // logged out: no nav
  }
  const JITTER_PX = 8; // ignore smaller movements in one direction
  const EDGE_PX = 64; // "near the top / bottom of the page"
  const phone = window.matchMedia("(max-width: 899.98px)");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const root = document.documentElement;

  let anchorY = window.scrollY; // where the current one-direction movement started
  let queued = false;

  function setHidden(hidden) {
    dock.classList.toggle("is-hidden", hidden);
  }

  function update() {
    queued = false;
    const y = Math.max(0, window.scrollY);
    const maxY = root.scrollHeight - window.innerHeight;
    const pinned = !phone.matches || reduced.matches || maxY <= EDGE_PX
      || y <= EDGE_PX || y >= maxY - EDGE_PX || dock.contains(document.activeElement);
    if (pinned) {
      setHidden(false);
      anchorY = y;
      return;
    }
    const moved = y - anchorY;
    if (moved > JITTER_PX) {
      setHidden(true);
      anchorY = y;
    } else if (moved < -JITTER_PX) {
      setHidden(false);
      anchorY = y;
    }
  }

  // At most one update per frame, however many scroll events arrive.
  function queue() {
    if (!queued) {
      queued = true;
      window.requestAnimationFrame(update);
    }
  }

  window.addEventListener("scroll", queue, { passive: true });
  window.addEventListener("resize", queue, { passive: true });
  phone.addEventListener("change", queue);
  reduced.addEventListener("change", queue);

  // Tab into a hidden nav: show it right away (pages.css does the same with :focus-within).
  dock.addEventListener("focusin", () => setHidden(false));

  // The host preview is a bottom sheet: keep the nav out of its way while it's open.
  document.addEventListener("cronrack:preview-open", () => dock.classList.add("is-covered"));
  document.addEventListener("cronrack:preview-closed", () => dock.classList.remove("is-covered"));

  update();
})();
