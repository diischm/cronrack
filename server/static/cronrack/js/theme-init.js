// PLACEHOLDER — Apo owns the final version.
// Loaded in <head> WITHOUT defer/async, so it runs before the page is drawn.
// It only sets <html data-theme="...">; the CSS does the rest. That's how the
// page never flashes the wrong theme on load.
(function () {
  "use strict";
  let theme = null;
  try {
    theme = window.localStorage.getItem("cronrack-theme");
  } catch (e) {
    // Storage can be blocked (private mode, strict settings): fall back below.
  }
  if (theme !== "light" && theme !== "dark") {
    // Nothing saved (or a value we don't recognise): follow the system setting.
    const prefersDark =
      window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    theme = prefersDark ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", theme);
})();
