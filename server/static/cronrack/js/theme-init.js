// PLACEHOLDER — Apo owns the final version.
// Loaded in <head> WITHOUT defer/async, so it runs before the page is drawn. It only sets
// four attributes on <html>; the CSS does the rest. That's how the page never flashes the
// wrong look on load.
//
//   data-skin    "glass" (default, also for visitors with an older Light/Dark choice) or
//                "legacy". localStorage "cronrack-skin".
//   data-theme   Legacy: the saved "cronrack-theme" (light | dark), else the system setting.
//                Glass is dark only, so it is always "dark" there; the saved Legacy choice
//                stays untouched for when the visitor switches back.
//   data-motion  "on" (default) | "off": Glass's drifting background glows.
//                localStorage "cronrack-motion". prefers-reduced-motion is handled in CSS/JS.
//   data-labels  "off" (default) | "on": Glass shows the status word next to each dot.
//                localStorage "cronrack-labels".
//
// A page can force a skin with <meta name="cronrack-skin-override" content="legacy"> placed
// before this script (the dev styleguide, which shows Glass and Legacy side by side), and
// the Legacy light/dark with <meta name="cronrack-legacy-theme-override"> (500.html: light).
// Storage can be blocked (private mode, strict settings): every read is in try/catch, and
// anything missing or unknown falls back to the default.
(function () {
  "use strict";
  const root = document.documentElement;

  function read(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (e) {
      return null; // blocked: use the default
    }
  }

  const override = document.querySelector('meta[name="cronrack-skin-override"]');
  let skin = override ? override.getAttribute("content") : read("cronrack-skin");
  if (skin !== "glass" && skin !== "legacy") {
    skin = "glass";
  }

  // 500.html: Legacy shows it light, as it always has (<meta name="cronrack-legacy-theme-override">).
  const legacyTheme = document.querySelector('meta[name="cronrack-legacy-theme-override"]');
  let theme = legacyTheme ? legacyTheme.getAttribute("content") : read("cronrack-theme");
  if (skin === "glass") {
    theme = "dark";
  } else if (theme !== "light" && theme !== "dark") {
    // Nothing saved (or a value we don't recognise): follow the system setting.
    const prefersDark =
      window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    theme = prefersDark ? "dark" : "light";
  }

  root.setAttribute("data-skin", skin);
  root.setAttribute("data-theme", theme);
  root.setAttribute("data-motion", read("cronrack-motion") === "off" ? "off" : "on");
  root.setAttribute("data-labels", read("cronrack-labels") === "on" ? "on" : "off");
})();
