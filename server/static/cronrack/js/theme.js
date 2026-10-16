// PLACEHOLDER — Apo owns the final version.
// Theme: the top-bar toggle key (every page) and the Light / Dark / System picker
// (Settings > Appearance). Both show the same state and stay in sync both ways.
//   Light / Dark  saved in localStorage ("cronrack-theme"), like the toggle key does
//   System        the saved choice is removed; the page follows prefers-color-scheme,
//                 also when the device switches while the page is open
// theme-init.js already set <html data-theme> before first paint; this file only reacts.
// Storage can be blocked (private mode): every access is in try/catch, and the choice then
// still works for this page. Loaded with defer.
(function () {
  "use strict";
  const KEY = "cronrack-theme";
  const root = document.documentElement;
  const buttons = document.querySelectorAll("[data-theme-toggle]");
  const picker = document.getElementById("theme-picker");
  const radios = picker ? picker.querySelectorAll("input[type=radio][name=theme]") : [];
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  let unsaved = null; // the choice when storage is blocked: "light" | "dark" | null

  function saved() {
    let value = unsaved;
    try {
      value = window.localStorage.getItem(KEY);
    } catch (e) {
      // blocked: keep the in-page choice
    }
    return value === "light" || value === "dark" ? value : null;
  }

  function save(choice) {
    unsaved = choice;
    try {
      if (choice) {
        window.localStorage.setItem(KEY, choice);
      } else {
        window.localStorage.removeItem(KEY);
      }
    } catch (e) {
      // blocked: the choice works for this page only
    }
  }

  function systemTheme() {
    return media && media.matches ? "dark" : "light";
  }

  // Apply the saved choice (or the system's), then show it on every control.
  function apply() {
    const choice = saved();
    const theme = choice || systemTheme();
    root.setAttribute("data-theme", theme);
    buttons.forEach((button) => {
      button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    });
    radios.forEach((radio) => {
      radio.checked = radio.value === (choice || "system");
    });
  }

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      save(root.getAttribute("data-theme") === "dark" ? "light" : "dark");
      apply();
    });
  });

  radios.forEach((radio) => {
    radio.addEventListener("change", () => {
      if (radio.checked) {
        save(radio.value === "system" ? null : radio.value);
        apply();
      }
    });
  });

  if (media) {
    media.addEventListener("change", apply); // only changes the page while nothing is saved
  }
  // Another tab changed the theme.
  window.addEventListener("storage", (event) => {
    if (event.key === KEY || event.key === null) {
      apply();
    }
  });

  if (picker) {
    picker.hidden = false;
    const note = document.getElementById("theme-nojs");
    if (note) {
      note.hidden = true;
    }
  }
  apply();
})();
