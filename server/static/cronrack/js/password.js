// PLACEHOLDER — Apo owns the final version.
// Show/hide keys on password fields (components/password_field.html). Each key is a toggle:
// aria-pressed="true" while its password is shown as text; its name stays "Show password".
// The keys start hidden (they do nothing without JS) and are shown here. Before the form is
// sent every field goes back to type=password, so the browser never handles it as plain
// text. Nothing is read from the fields. Loaded with defer.
(function () {
  "use strict";
  document.querySelectorAll(".pw-toggle").forEach((key) => {
    const field = document.getElementById(key.getAttribute("aria-controls") || "");
    if (!field || field.type !== "password") {
      return;
    }
    function show(on) {
      field.type = on ? "text" : "password";
      key.setAttribute("aria-pressed", on ? "true" : "false");
    }
    key.addEventListener("click", () => show(field.type === "password"));
    if (field.form) {
      field.form.addEventListener("submit", () => show(false));
    }
    key.hidden = false;
  });
})();
