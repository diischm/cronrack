// PLACEHOLDER — Apo owns the final version.
// "Check all now" on the overview: POST /hosts/collect-all/ with the CSRF token
// in a header, show the small loader while agents report, then reload the page
// so the cards show the fresh data. Loaded with defer.
(function () {
  "use strict";
  // Agents answer a collect request within a few seconds; give them time to report.
  const RELOAD_AFTER_MS = 4000;

  const form = document.querySelector("[data-collect-all]");
  const loader = document.querySelector("[data-collect-loader]");
  const error = document.querySelector("[data-collect-error]");
  if (!form || !loader || !error) {
    return;
  }
  const button = form.querySelector("button");

  function setBusy(busy) {
    loader.hidden = !busy;
    button.disabled = busy;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    setBusy(true);
    try {
      const response = await fetch(form.action, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": form.elements.csrfmiddlewaretoken.value },
      });
      if (!response.ok) {
        throw new Error(String(response.status));
      }
      window.setTimeout(() => window.location.reload(), RELOAD_AFTER_MS);
    } catch (e) {
      setBusy(false);
      error.textContent = "Couldn't reach the server. Try again.";
      error.hidden = false;
    }
  });
})();
