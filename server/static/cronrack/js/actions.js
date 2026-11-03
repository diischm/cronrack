// PLACEHOLDER — Apo owns the final version.
// Fetch actions (handoff 5.4, 5.7): "Check now", "Check all now" and "Send test alert".
//
// Each is a real <form method="post" class="js-fetch-action"> with {% csrf_token %}, so it
// works without JavaScript (the server redirects back with a flash message, shown as a
// toast). With JS, this file sends the same POST with fetch (X-CSRFToken header, Accept:
// application/json) and shows the result as the same kind of toast (messages.js, via the
// "cronrack:toast" event), so the browser never lands on a raw JSON page.
//
// Form attributes (fixed values from the template, no host data), read with getAttribute:
//   data-action      "check-host" | "check-all" | "test-alert"
//   data-loader      id of the small logo-bar loader (optional)
//   data-status-url  status.json URL to poll (check-host only)
//
// messages.js puts the text in with textContent only. Error text is shown exactly as the
// backend sent it. Nothing is logged. Loaded with defer.
//
// One delegated submit listener, so forms that overview.js builds on a refresh (the Glass
// units' "Check now") work the same as the server-rendered ones.
//
// "Check now" on the overview: once the new report is in, the overview refreshes in place
// (overview.js) instead of reloading; elsewhere (the host page) the page reloads.
//
// "Check all now" is honest about what happened: before asking, it notes each online host's
// latest report time (status.json, the same as "Check now"), then polls them for up to 15 s,
// at most 4 requests at a time. Offline hosts aren't asked and count as "not reported". Then
// it waits for the overview refresh to finish. Its progress goes out as "cronrack:progress"
// events (phases start { total }, done { reported, total }, error; counts only), which the
// Glass theme's Dynamic Island shows (glass.js): "All 5 hosts reported" / "4 of 5 hosts
// reported". The key stays disabled for the whole round.
(function () {
  "use strict";
  const LOADER_DELAY_MS = 300; // fast answers never flash the loader
  const POLL_EVERY_MS = 1000;
  const POLL_MAX_MS = 15000;
  const OVERVIEW_REFRESH_MS = 5000; // check-all without any host to poll: wait, then refresh
  const MAX_IN_FLIGHT = 4;
  const STATUS_URL = /^\/api\/v1\/hosts\/\d+\/status\.json$/; // the only URL ever polled
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

  // UI helpers for one form: result toast, loader (after 300ms), disabled key.
  function controls(form) {
    const loader = document.getElementById(form.getAttribute("data-loader") || "");
    // form.elements also has keys outside the form that point to it with form="..." (the
    // Settings page's "Send test alert" sits in the Alerts panel, outside its own form).
    // "Check all now" exists twice (phone top bar, desktop header): both stay disabled.
    const forms =
      form.getAttribute("data-action") === "check-all"
        ? Array.from(document.querySelectorAll('form.js-fetch-action[data-action="check-all"]'))
        : [form];
    const buttons = forms.flatMap((f) => Array.from(f.elements).filter((el) => el.type === "submit"));
    let loaderTimer = null;

    return {
      say(message, isError) {
        document.dispatchEvent(
          new CustomEvent("cronrack:toast", { detail: { message, error: Boolean(isError) } })
        );
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

  // Run fn over items, at most MAX_IN_FLIGHT at a time. Resolves to the results in order.
  async function pool(items, fn) {
    const results = new Array(items.length);
    let next = 0;
    async function worker() {
      while (next < items.length) {
        const i = next;
        next += 1;
        results[i] = await fn(items[i]);
      }
    }
    await Promise.all(Array.from({ length: Math.min(MAX_IN_FLIGHT, items.length) }, worker));
    return results;
  }

  // The latest report time, or undefined when status.json can't be read right now.
  function receivedOrUnknown(url) {
    return reportReceivedAt(url).catch(() => undefined);
  }

  // Ask overview.js for one refresh. It calls preventDefault() on the event and puts its
  // promise on detail.promise. Without overview.js on the page nobody does, and the page
  // reloads.
  function refreshOverview() {
    const event = new CustomEvent("cronrack:refresh", { cancelable: true, detail: {} });
    if (document.dispatchEvent(event)) {
      window.location.reload();
      return Promise.resolve("reloading");
    }
    return Promise.resolve(event.detail.promise).then(() => undefined);
  }

  // Check now: note the latest report time, ask, then poll status.json until a newer
  // report is saved (max 15s). last_seen_human ("40s ago") is never compared: it changes
  // every second on its own.
  async function checkHost(form, ui) {
    const statusUrl = form.getAttribute("data-status-url");
    if (!STATUS_URL.test(statusUrl || "")) {
      ui.say(NETWORK_ERROR, true);
      return;
    }
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
        if (document.getElementById("rack")) {
          return refreshOverview(); // the overview: update in place
        }
        window.location.reload(); // the same fixed page, now with the fresh report
        return "reloading";
      }
    }
    ui.say("No answer from the agent yet.", true);
  }

  // Progress of "Check all now" for anyone who wants to show it (glass.js: the Dynamic
  // Island). Only counts, never text from the server.
  function progress(phase, detail) {
    document.dispatchEvent(
      new CustomEvent("cronrack:progress", { detail: Object.assign({ action: "check-all", phase }, detail) })
    );
  }

  // The overview's hosts: the status.json URL of each online one (its "Check now" form) and
  // the number of offline ones. Fixed same-site paths from the templates / overview.js.
  function overviewHosts() {
    const urls = new Set();
    document.querySelectorAll('#rack form[data-action="check-host"][data-status-url]').forEach((f) => {
      const url = f.getAttribute("data-status-url");
      if (STATUS_URL.test(url)) {
        urls.add(url);
      }
    });
    const offline = document.querySelectorAll("#rack .g-unit-more__check-off").length;
    return { urls: Array.from(urls), offline };
  }

  // Check all now: ask every host, wait for the reports (or 15 s), refresh, then report.
  async function checkAll(form, ui) {
    const hosts = overviewHosts();
    const total = hosts.urls.length + hosts.offline;
    progress("start", { total });
    let result;
    let before;
    try {
      before = await pool(hosts.urls, receivedOrUnknown);
      result = await send(form);
    } catch (e) {
      progress("error");
      throw e; // the submit handler shows the network error toast
    }
    if (!succeeded(result)) {
      progress("error");
      ui.say(errorText(result), true);
      return;
    }
    const asked = Number.isInteger(result.data.asked) ? result.data.asked : 0;
    const offline = Number.isInteger(result.data.offline) ? result.data.offline : 0;
    // Same wording as the no-JS flash message (hosts.views.host_collect_all).
    ui.say("Asked " + plural(asked, "host") + " · " + offline + " offline.", false);

    let reported = 0;
    if (asked > 0 && hosts.urls.length) {
      // Hosts whose report time couldn't be read first can't be told apart: not polled.
      const waiting = new Set(hosts.urls.map((_, i) => i).filter((i) => before[i] !== undefined));
      const deadline = Date.now() + POLL_MAX_MS;
      while (waiting.size && Date.now() < deadline) {
        await wait(POLL_EVERY_MS);
        const ids = Array.from(waiting);
        const now = await pool(
          ids.map((i) => hosts.urls[i]),
          receivedOrUnknown
        );
        ids.forEach((i, k) => {
          if (now[k] !== undefined && now[k] !== before[i]) {
            waiting.delete(i);
            reported += 1;
          }
        });
      }
    } else if (asked > 0) {
      await wait(OVERVIEW_REFRESH_MS); // no units to poll: give the agents a moment
    }
    if (asked === 0 && !total) {
      progress("done", { reported: 0, total: 0 });
      return;
    }
    const outcome = await refreshOverview();
    progress("done", { reported, total });
    return outcome;
  }

  async function testAlert(form, ui) {
    const result = await send(form);
    if (succeeded(result)) {
      ui.say("Test alert sent.", false); // same as the no-JS flash message
    } else {
      ui.say(errorText(result), true);
    }
  }

  // Focus back on the key that was used, or on the same key of the rebuilt unit; only if
  // focus was dropped meanwhile (the visitor didn't move it somewhere else).
  function restoreFocus(focused, slotSelector) {
    const active = document.activeElement;
    if (active && active !== document.body && active !== focused) {
      return;
    }
    let target = focused.isConnected ? focused : null;
    if (!target && slotSelector && /^li\[data-unit="(host|cluster)"\]\[data-id="\d+"\]$/.test(slotSelector)) {
      const slot = document.querySelector(slotSelector);
      target = slot && slot.querySelector(".g-unit-more__check button[type=submit]");
    }
    if (target) {
      target.focus({ preventScroll: true });
    }
  }

  const HANDLERS = { "check-host": checkHost, "check-all": checkAll, "test-alert": testAlert };
  const running = new Set(); // forms, or "check-all" for that round

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("form.js-fetch-action")) {
      return;
    }
    const handler = HANDLERS[form.getAttribute("data-action")];
    if (!handler) {
      return; // unknown action: leave the plain form POST alone
    }
    event.preventDefault();
    // Double-click protection; one "Check all now" round at a time, from either key.
    const key = form.getAttribute("data-action") === "check-all" ? "check-all" : form;
    if (running.has(key)) {
      return;
    }
    running.add(key);
    // Disabling the focused key drops keyboard focus to <body>, and on the overview a refresh
    // may rebuild the unit the form was in: remember where focus was, to put it back.
    const focused = form.contains(document.activeElement) ? document.activeElement : null;
    const slot = form.closest("li[data-unit][data-id]");
    const slotSelector = slot
      ? 'li[data-unit="' + slot.getAttribute("data-unit") + '"][data-id="' + slot.getAttribute("data-id") + '"]'
      : null;
    const ui = controls(form);
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
      running.delete(key);
      if (focused) {
        restoreFocus(focused, slotSelector);
      }
    }
  });
})();
