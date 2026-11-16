// Offline page (templates/pwa/offline.html): "Try again" reloads.
// The service worker shows that page in place of the page that failed, so a reload asks the
// server for the real page again. Opened directly at /offline/, it goes to the overview.
// Feedback (seen and announced through the role="status" line): "Checking…" while it
// reloads; if the reload lands on this page again, "Still can't reach it." A sessionStorage
// flag carries that across the reload; without storage (private mode) only "Checking…" shows.
(() => {
  "use strict";
  const FLAG = "cronrack-offline-retry";
  const button = document.getElementById("offline-retry");
  const status = document.getElementById("offline-status");
  if (!button || !status) return;

  function storage() {
    try {
      return window.sessionStorage;
    } catch {
      return null;
    }
  }

  const store = storage();
  try {
    if (store && store.getItem(FLAG)) {
      store.removeItem(FLAG);
      status.textContent = "Still can't reach it.";
    }
  } catch {
    // Storage blocked: no "still" message, the page works the same.
  }

  button.addEventListener("click", () => {
    button.disabled = true;
    status.textContent = "Checking…";
    try {
      if (store) store.setItem(FLAG, "1");
    } catch {
      // See above.
    }
    if (window.location.pathname === "/offline/") window.location.assign("/");
    else window.location.reload();
  });
})();
