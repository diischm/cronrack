// PLACEHOLDER — Apo owns the final version.
// Overview auto-refresh (handoff 5.3). Loaded with defer, on the overview page only.
//
// Every 60s while the tab is visible, and once when it becomes visible again, fetch
// /api/v1/overview.json and update the headline, the subline, the summary tiles and the
// rack. "Check all now" (actions.js) asks for one refresh with a "cronrack:refresh" event.
//
// All host data is untrusted. Units are clones of <template id="rack-unit-template">,
// filled with textContent / setAttribute / className only. No HTML string is ever parsed,
// so text like <script> is shown as text and can't become markup. Nothing is logged.
//
// If a refresh fails, a small "Offline — retrying" line appears and the page keeps what
// it shows. The Welcome example (0 hosts) is rendered by the server only: when hosts
// appear (or all disappear), the page reloads once so the server renders the right one.
//
// While the host preview (host-preview.js) is open, refreshes wait: the unit that opened it
// must stay the same element for the close animation and for focus to return to it. One
// refresh runs right after the preview closes if any came due meanwhile.
(function () {
  "use strict";
  const OVERVIEW_URL = "/api/v1/overview.json";
  const REFRESH_EVERY_MS = 60000;
  const RELOAD_KEY = "cronrack-overview-reloaded";

  const rack = document.getElementById("rack");
  const unitTemplate = document.getElementById("rack-unit-template");
  if (!rack || !unitTemplate) {
    return;
  }

  // Offline and waiting (no report yet) get the unlit lamp, like rack_unit.html.
  const LAMPS = {
    red: "lamp--red",
    yellow: "lamp--amber",
    green: "lamp--green",
    offline: "lamp--off",
    waiting: "lamp--off",
  };
  // Status icon per status: clones from <template id="status-icons"> (base.html), whose
  // shape differs per status (triangle, exclamation, check, hourglass, "i").
  const ICON_KINDS = { red: "bad", yellow: "warn", green: "good", waiting: "waiting" };

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    const el = byId(id);
    if (el) {
      el.textContent = text;
    }
  }

  // A status icon with its words (visually hidden + title), or null if there are no words.
  function statusIcon(status, words) {
    const icons = byId("status-icons");
    const proto = icons && icons.content.querySelector(".status-icon--" + (ICON_KINDS[status] || "info"));
    if (!proto || !words) {
      return null;
    }
    const icon = proto.cloneNode(true);
    icon.setAttribute("title", words);
    icon.querySelector(".visually-hidden").textContent = words;
    return icon;
  }

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  function count(value) {
    return Number.isInteger(value) && value >= 0 ? value : 0;
  }

  function text(value) {
    return typeof value === "string" ? value : "";
  }

  // The unit links to the host page. Only a same-site /hosts/<id>/ path is accepted, so a
  // bad value can never become a javascript: or off-site link.
  function hostUrl(host) {
    if (typeof host.url === "string" && /^\/hosts\/\d+\/$/.test(host.url)) {
      return host.url;
    }
    return Number.isInteger(host.id) ? "/hosts/" + host.id + "/" : "/";
  }

  // Same wording as rack_unit.html.
  function updatesText(u) {
    if (!u) {
      return "n/a";
    }
    const security = count(u.security);
    return count(u.total) + (security ? " (" + security + " security)" : "");
  }

  function containersText(c) {
    if (!c) {
      return "n/a";
    }
    const failing = count(c.failing);
    return failing ? count(c.total) + " · " + failing + " failing" : count(c.running) + " running";
  }

  // Disk: "62%" plus the small meter (neutral, amber from 85%, red from 95%, like
  // meter.html). No number: "n/a" and no meter.
  function fillDisk(li, percent) {
    const value = li.querySelector(".rack-unit__disk");
    const holder = li.querySelector(".readout__meter");
    if (!Number.isFinite(percent)) {
      value.textContent = "n/a";
      holder.remove();
      return;
    }
    const p = Math.min(100, Math.max(0, Math.round(percent)));
    value.textContent = p + "%";
    const meter = holder.querySelector("meter");
    meter.setAttribute("value", String(p));
    meter.textContent = p + "%";
    meter.classList.toggle("meter--red", p >= 95);
    meter.classList.toggle("meter--amber", p >= 85 && p < 95);
  }

  // One <li> for one host: a clone of the template, filled like rack_unit.html.
  function buildUnit(host) {
    const fragment = unitTemplate.content.cloneNode(true);
    const li = fragment.firstElementChild;
    const link = li.querySelector(".rack-unit");
    const status = text(host.status);
    const offline = status === "offline";
    const issues = Array.isArray(host.issues) ? host.issues.slice(0, 3) : [];

    link.setAttribute("href", hostUrl(host));
    link.classList.toggle("rack-unit--offline", offline);

    if (LAMPS[status]) {
      const lamp = document.createElement("span");
      lamp.className = "lamp " + LAMPS[status];
      lamp.setAttribute("aria-hidden", "true");
      li.querySelector(".rack-unit__lamp").appendChild(lamp);
    }

    li.querySelector(".rack-unit__name").textContent = text(host.name);

    const placeholder = li.querySelector(".rack-unit__head .status-icon");
    const note = li.querySelector(".rack-unit__offline");
    const icon = offline ? null : statusIcon(status, text(host.status_label));
    if (icon) {
      placeholder.replaceWith(icon);
    } else {
      placeholder.remove(); // offline: "Offline · last report …" is the text label
    }
    if (offline) {
      const strong = document.createElement("strong");
      strong.textContent = "Offline";
      note.replaceChildren(strong, " · last report " + text(host.last_seen_human));
    } else {
      note.remove();
    }

    const issueLine = li.querySelector(".rack-unit__issues");
    let line = issues.map((issue) => text(issue && issue.text)).join(" · ");
    if (!issues.length && !offline) {
      line = status === "waiting" ? "Waiting for the first report" : "Nothing needs you";
    }
    if (line) {
      issueLine.querySelector(".rack-unit__issue-text").textContent = line;
    } else {
      issueLine.remove(); // offline with no issues: the offline line says it
    }

    li.querySelector(".rack-unit__updates").textContent = updatesText(host.updates);
    li.querySelector(".rack-unit__containers").textContent = containersText(host.containers);
    fillDisk(li, host.disk_max_percent);
    return li;
  }

  function renderHead(data, hostCount) {
    const attention = count(data.attention_count);
    let title = "All good";
    if (!hostCount) {
      title = "No hosts yet";
    } else if (attention) {
      title = plural(attention, "thing") + (attention === 1 ? " needs you" : " need you");
    }
    setText("overview-title", title);
    setText(
      "overview-sub",
      plural(hostCount, "host") + (hostCount ? " · updated " + text(data.updated_human) : "")
    );

    const counts = data.status_counts || {};
    ["red", "yellow", "green"].forEach((key) => {
      const n = count(counts[key]);
      setText("tile-" + key, String(n));
      setText("tile-" + key + "-unit", n === 1 ? "host" : "hosts");
    });
    setText("tile-online", String(count(data.hosts_online)));
    setText("tile-online-unit", "of " + hostCount);

    const offline = count(counts.offline);
    const note = byId("tile-offline");
    const noteText = byId("tile-offline-text");
    if (note && noteText) {
      const strong = document.createElement("strong");
      strong.textContent = "Offline";
      noteText.replaceChildren(strong, " · " + plural(offline, "host"));
      note.hidden = offline === 0;
    }
  }

  // Reload at most once in a row: if the page still disagrees after reloading (e.g. the
  // dev preview, which always shows sample data), stay on it instead of reloading again.
  function reloadOnce() {
    try {
      if (window.sessionStorage.getItem(RELOAD_KEY)) {
        return false;
      }
      window.sessionStorage.setItem(RELOAD_KEY, "1");
    } catch (e) {
      return false; // storage blocked: never risk a reload loop
    }
    window.location.reload(); // the same fixed page
    return true;
  }

  function clearReloadMark() {
    try {
      window.sessionStorage.removeItem(RELOAD_KEY);
    } catch (e) {
      // storage blocked: nothing to clear
    }
  }

  function showFailed(on) {
    const line = byId("refresh-failed");
    if (line) {
      line.hidden = !on;
    }
  }

  let inFlight = false;
  let previewOpen = false;
  let refreshAfterPreview = false;

  // Re-rendering replaces every unit. If one had keyboard focus, give it to the new unit
  // with the same link, so focus isn't dropped back to the top of the page.
  function replaceUnits(units) {
    const focused = document.activeElement;
    const href = focused && rack.contains(focused) ? focused.getAttribute("href") : null;
    rack.replaceChildren(...units);
    if (href) {
      const again = units.map((li) => li.querySelector(".rack-unit")).find((a) => a && a.getAttribute("href") === href);
      if (again) {
        again.focus({ preventScroll: true });
      }
    }
  }

  async function refresh() {
    if (previewOpen) {
      refreshAfterPreview = true;
      return;
    }
    if (inFlight) {
      return;
    }
    inFlight = true;
    try {
      const response = await fetch(OVERVIEW_URL, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      // A logged-out session gets the login page (HTML), which is not an overview.
      const data = response.ok ? await response.json() : null;
      if (!data || !Array.isArray(data.hosts)) {
        throw new Error("bad response");
      }
      showFailed(false);
      if (previewOpen) {
        refreshAfterPreview = true; // the preview opened while this request was running
        return;
      }

      const showsExample = rack.querySelector(".rack-unit--example") !== null;
      const hosts = data.hosts.filter((h) => h && typeof h === "object");
      if (showsExample !== (hosts.length === 0)) {
        // Example shown but hosts exist, or units shown but none left. If the reload was
        // already tried, keep the page as it is rather than show a mix (or an empty rack).
        reloadOnce();
        return;
      }
      clearReloadMark();
      renderHead(data, hosts.length);
      if (!showsExample) {
        replaceUnits(hosts.map(buildUnit));
      }
    } catch (e) {
      showFailed(true); // keep the current content, try again on the next tick
    } finally {
      inFlight = false;
    }
  }

  let timer = null;

  function start() {
    window.clearInterval(timer);
    timer = window.setInterval(refresh, REFRESH_EVERY_MS);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refresh();
      start();
    } else {
      window.clearInterval(timer);
      timer = null;
    }
  });

  // actions.js asks for a refresh after "Check all now". preventDefault() tells it the
  // event was handled, so it doesn't fall back to reloading the page.
  document.addEventListener("cronrack:refresh", (event) => {
    event.preventDefault();
    refresh();
  });

  document.addEventListener("cronrack:preview-open", () => {
    previewOpen = true;
  });
  document.addEventListener("cronrack:preview-closed", () => {
    previewOpen = false;
    if (refreshAfterPreview) {
      refreshAfterPreview = false;
      refresh();
    }
  });

  if (document.visibilityState === "visible") {
    start();
  }
})();
