// PLACEHOLDER — Apo owns the final version.
// The theme toggle key: flips <html data-theme>, saves the choice, and keeps
// aria-pressed in sync for screen readers. Loaded with defer.
(function () {
  "use strict";
  const root = document.documentElement;
  const buttons = document.querySelectorAll("[data-theme-toggle]");

  function isDark() {
    return root.getAttribute("data-theme") === "dark";
  }

  function syncButtons() {
    buttons.forEach((button) => {
      button.setAttribute("aria-pressed", isDark() ? "true" : "false");
    });
  }

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      const next = isDark() ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try {
        window.localStorage.setItem("cronrack-theme", next);
      } catch (e) {
        // Can't save (storage blocked): the switch still works for this page.
      }
      syncButtons();
    });
  });

  syncButtons();
})();
