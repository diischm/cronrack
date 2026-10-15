// PLACEHOLDER — Apo owns the final version.
// Fetch actions (handoff 5.4, 5.7): "Check now", "Check all now" and "Send test alert".
//
// Each is a real <form method="post" class="js-fetch-action"> with {% csrf_token %}, so it
// works without JavaScript (the server redirects back with a flash message). With JS, this
// file sends the same POST with fetch (X-CSRFToken header, Accept: application/json) and
// shows the result inline, so the browser never lands on a raw JSON page.
//
// Form attributes (fixed values from the template, no host data):
//   data-action      "check-host" | "check-all" | "test-alert"
//   data-result      id of the aria-live element next to the key
//   data-loader      id of the small logo-bar loader (optional)
//   data-status-url  status.json URL to poll (check-host only)
//
// Messages go in with textContent only. Error text is shown exactly as the backend sent it.
// Nothing is logged. Loaded with defer.
(function () {
  "use strict";
  const LOADER_DELAY_MS = 300; // fast answers never flash the loader
  const POLL_EVERY_MS = 1000;
  const POLL_MAX_MS = 15000;
  const OVERVIEW_REFRESH_MS = 5000;
  const NETWORK_ERROR = "Couldn't reach the server. Try again.";

  function csrfToken(form) {
    const input = form.elements.namedItem("csrfmiddlewaretoken");
    if (input && input.value) {
      return input.value;
    }
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function wait(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch (e) {
      return null;
    }
  }

  // POST the form. Resolves to { status, data } (data is null if the body wasn't JSON).
  async function send(form) {
    const response = await fetch(form.action, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken(form), Accept: "application/json" },
    });
    return { status: response.status, data: await readJson(response) };
  }

  // The time of the host's latest saved report (ISO 8601 string, or null if it never
  // reported). It only changes when a new report arrives, so it's a safe change signal.
  async function reportReceivedAt(url) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error(String(response.status));
    }
    const data = await readJson(response);
    return data && typeof data.report_received_at === "string" ? data.report_received_at : null;
  }

  // The backend's error text as-is, or a generic line when there is none (e.g. a 403 page).
  function errorText(result) {
    const error = result.data && result.data.error;
    return typeof error === "string" && error ? error : NETWORK_ERROR;
  }

  function succeeded(result) {
    return result.status >= 200 && result.status < 300 && result.data && result.data.ok === true;
  }

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  // UI helpers for one form: result line, loader (after 300ms), disabled key.
  function controls(form) {
    const result = document.getElementById(form.dataset.result || "");
    const loader = document.getElementById(form.dataset.loader || "");
    // A form can have more than one key (overview: phone icon key + desktop labelled key).
    const buttons = form.querySelectorAll("button[type=submit], button:not([type])");
    let loaderTimer = null;

    return {
      say(message, isError) {
        if (!result) {
          return;
        }
        result.classList.toggle("action-result--error", Boolean(isError));
        // Clear first, so the same message twice in a row is announced again.
        result.textContent = "";
        window.setTimeout(() => {
          result.textContent = message;
        }, 50);
      },
      busy(on) {
        buttons.forEach((button) => {
          button.disabled = on;
        });
        window.clearTimeout(loaderTimer);
        if (!loader) {
          return;
        }
        if (on) {
          loaderTimer = window.setTimeout(() => {
            loader.hidden = false;
          }, LOADER_DELAY_MS);
        } else {
          loader.hidden = true;
        }
      },
    };
  }

  // Check now: note the latest report time, ask, then poll status.json until a newer
  // report is saved (max 15s). last_seen_human ("40s ago") is never compared: it changes
  // every second on its own.
  async function checkHost(form, ui) {
    const statusUrl = form.dataset.statusUrl;
    const before = await reportReceivedAt(statusUrl);

    const result = await send(form);
    if (!succeeded(result)) {
      ui.say(errorText(result), true);
      return;
    }
    const deadline = Date.now() + POLL_MAX_MS;
    while (Date.now() < deadline) {
      await wait(POLL_EVERY_MS);
      if ((await reportReceivedAt(statusUrl)) !== before) {
        window.location.reload(); // the same fixed page, now with the fresh report
        return "reloading";
      }
    }
    ui.say("No answer from the agent yet.", true);
  }

  // Check all now: show the count, then refresh the overview once the agents had time to report.
  async function checkAll(form, ui) {
    const result = await send(form);
    if (!succeeded(result)) {
      ui.say(errorText(result), true);
      return;
    }
    const asked = Number.isInteger(result.data.asked) ? result.data.asked : 0;
    const offline = Number.isInteger(result.data.offline) ? result.data.offline : 0;
    ui.say("Asked " + plural(asked, "host") + " · " + offline + " offline", false);
    if (asked > 0) {
      await wait(OVERVIEW_REFRESH_MS);
      window.location.reload();
      return "reloading";
    }
  }

  async function testAlert(form, ui) {
    const result = await send(form);
    if (succeeded(result)) {
      ui.say("Sent", false);
    } else {
      ui.say(errorText(result), true);
    }
  }

  const HANDLERS = { "check-host": checkHost, "check-all": checkAll, "test-alert": testAlert };
  const running = new WeakSet();

  document.querySelectorAll("form.js-fetch-action").forEach((form) => {
    const handler = HANDLERS[form.dataset.action];
    if (!handler) {
      return; // unknown action: leave the plain form POST alone
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (running.has(form)) {
        return; // double-click protection
      }
      running.add(form);
      const ui = controls(form);
      ui.say("", false);
      ui.busy(true);
      let outcome;
      try {
        outcome = await handler(form, ui);
      } catch (e) {
        ui.say(NETWORK_ERROR, true);
      }
      // While the page reloads, keep the key disabled so it can't be sent twice.
      if (outcome !== "reloading") {
        ui.busy(false);
        running.delete(form);
      }
    });
  });
})();
