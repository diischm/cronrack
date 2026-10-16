// PLACEHOLDER — Apo owns the final version.
// Show/hide keys on the host detail page ("Show 4 more", "Show last run").
// Markup (components/show_more.html):
//   <button class="js-disclosure" aria-controls="ID" aria-expanded="true" hidden>
//     <span class="js-disclosure-verb">Hide</span> 4 more</button>
// The server sends the list open and the key hidden, so without JS everything is visible.
// Here each list is closed, its key shown, and every click flips the list's `hidden`,
// aria-expanded and the "Show"/"Hide" word together. Loaded with defer. Never logs anything.
(function () {
  "use strict";

  function setOpen(button, target, open) {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    target.hidden = !open;
    const verb = button.querySelector(".js-disclosure-verb");
    if (verb) {
      verb.textContent = open ? "Hide" : "Show";
    }
  }

  document.querySelectorAll("button.js-disclosure").forEach((button) => {
    const target = document.getElementById(button.getAttribute("aria-controls") || "");
    if (!target) {
      return; // nothing to control: leave the key hidden
    }
    setOpen(button, target, false);
    button.hidden = false;
    button.addEventListener("click", () => {
      setOpen(button, target, button.getAttribute("aria-expanded") !== "true");
    });
  });
})();
