// PLACEHOLDER — Apo owns the final version.
// Overview auto-refresh (handoff 5.3). Loaded with defer, on the overview page only.
//
// Every 60s while the tab is visible, and once when it becomes visible again, fetch
// /api/v1/overview.json and update the headline, the subline, the summary tiles (Legacy), the
// filter chips (Glass), the racks and the sections around them. "Check all now" / "Check now" (actions.js) ask for one
// refresh with a "cronrack:refresh" event; this file puts a promise on event.detail.promise
// that resolves once the page shows the new data (after the view transition, if any).
//
// All host data is untrusted. Everything is built from clones of the page's <template>s and
// filled with textContent / setAttribute / className only; CSS custom properties go through
// element.style.setProperty. No HTML string is ever parsed, so text like <script> is shown as
// text and can't become markup. Nothing is logged.
//
// The network strip (components/network_strip.html, the first thing on the page): overview.json's
// "network", or null (no strip). Its points update in place; a change of state is announced
// once, politely (#net-announce), and "updated …" in Glass's top bar (#overview-updated).
//
// What a refresh renders (the same as the server's templates):
//   - each host / cluster unit (components/rack_unit.html, cluster_unit.html): BOTH controls,
//     the Legacy link and the Glass button with its expand region, from one data row, so the
//     two skins never disagree. Units the visitor opened (Glass) stay open, and focus moves
//     to the same control of the rebuilt unit.
//   - Glass: "Needs attention" (one short card per host and cluster that is red, yellow or
//     offline, the set the headline counts; full width above the Hosts row) and "While you
//     were away" (newest 10, right of the Hosts rack). A section with nothing in it is
//     removed, and added back from its <template>. Cards the visitor opened stay open.
//   - Legacy: "While you were away" with the newest 3, right of the rack.
// Every summary line (cards and Glass units) is the server's "summary" field
// (hosts.presenters.short_summary), never worked out here, so the two can't disagree.
//
// Glass filter chips (#host-filter): All / Needs you / Heads up / All good / Offline. A click
// shows only the units with that status (.is-filtered-out on the others, Glass CSS only, so
// Legacy never hides a unit), keeps focus on the chip and announces "Showing 1 of 3 hosts".
// The choice is kept for this tab only (sessionStorage, default All). A chip whose count is 0
// is aria-disabled; if the chosen one drops to 0 on a refresh, the filter goes back to All.
// The Welcome example (0 hosts) is rendered by the server only: when hosts appear (or all
// disappear), or the first cluster appears / the last one goes, the page reloads once so the
// server renders the right sections.
//
// Glass motion (only when allowed: Glass, Reduce animations off, no prefers-reduced-motion):
//   - a refresh that changed something runs inside document.startViewTransition (feature-
//     detected; otherwise the change is applied at once). Units and cards carry stable
//     view-transition names (--g-vt: g-host-<id>, g-cl-<id>, g-attn-h<id> / g-attn-c<id>), so
//     they morph instead of jumping. A refresh where nothing visible changed (the usual
//     case: only "updated 30s ago") never starts a transition.
//   - a rack unit's status gradient crossfades (its old rack__slot--st-* class first, then
//     the new one: glass.css transitions --g-st), the network strip's icon and line colours
//     crossfade (classes swapped in place), new activity rows slide in (.g-new) while the
//     others move down (FLIP, transform only).
//   - filtering runs in a view transition too: hidden units slide out, shown ones slide in.
// The first-load stagger is CSS (data-entrance, glass.js), so a refresh never plays it.
//
// While the host preview (host-preview.js, Legacy) is open, refreshes wait: the unit that
// opened it must stay the same element for the close animation and for focus to return to
// it. One refresh runs right after the preview closes if any came due meanwhile.
(function () {
  "use strict";
  const OVERVIEW_URL = "/api/v1/overview.json";
  const REFRESH_EVERY_MS = 60000;
  const RELOAD_KEY = "cronrack-overview-reloaded";
  const GLASS_ACTIVITY = 10;
  const LEGACY_ACTIVITY = 3;
  const NEEDS_ATTENTION = ["red", "yellow", "offline"];
  // rack__slot--st-<status>: only these (glass.css maps offline, waiting, maintenance to grey).
  const STATUSES = ["red", "yellow", "green", "offline", "waiting", "maintenance"];
  const FILTER_KEY = "cronrack-host-filter";
  const FILTERS = ["all", "red", "yellow", "green", "offline"];
  const NET_STATUSES = ["ok", "down", "slow", "untested"];
  const NET_LINES = ["ok", "slow", "off"];
  const NET_KINDS = ["you", "router", "box", "internet"];

  const root = document.documentElement;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const rack = document.getElementById("rack");
  const unitTemplate = document.getElementById("rack-unit-template");
  const clusterRack = document.getElementById("cluster-rack");
  const clusterTemplate = document.getElementById("cluster-unit-template");
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
  const LAMP_CLASSES = ["lamp--red", "lamp--amber", "lamp--green", "lamp--off"];
  // Status icon per status: clones from <template id="status-icons"> (base.html), whose
  // shape differs per status (triangle, exclamation, check, hourglass, "i").
  const ICON_KINDS = { red: "bad", yellow: "warn", green: "good", waiting: "waiting" };
  // Kubernetes role words, like role_label.html. Unknown roles get no label.
  const ROLE_WORDS = { server: "k8s server", agent: "k8s worker", node: "k8s node" };

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, value) {
    const el = byId(id);
    if (el) {
      el.textContent = value;
    }
  }

  function motionOK() {
    return (
      root.getAttribute("data-skin") === "glass" &&
      root.getAttribute("data-motion") !== "off" &&
      !reducedMotion.matches
    );
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

  function validId(value) {
    return Number.isInteger(value) && value > 0;
  }

  function statusClass(status) {
    return STATUSES.includes(status) ? "rack__slot--st-" + status : "";
  }

  function slotStatus(li) {
    return Array.from(li.classList).find((c) => c.startsWith("rack__slot--st-")) || "";
  }

  // One row per id, so ids built from it (aria-controls, region ids) are never duplicated.
  function unique(rows) {
    const ids = new Set();
    return rows.filter((row) => !ids.has(row.id) && ids.add(row.id));
  }

  function clone(templateId) {
    const template = byId(templateId);
    return template ? template.content.firstElementChild.cloneNode(true) : null;
  }

  // A status icon with its words (visually hidden + title), or null if there are no words.
  // words === undefined keeps the template's default words (issue lines).
  function statusIcon(status, words) {
    const icons = byId("status-icons");
    const proto = icons && icons.content.querySelector(".status-icon--" + (ICON_KINDS[status] || "info"));
    if (!proto || words === "") {
      return null;
    }
    const icon = proto.cloneNode(true);
    if (words !== undefined) {
      icon.setAttribute("title", words);
      icon.querySelector(".visually-hidden").textContent = words;
    }
    return icon;
  }

  function lamp(status) {
    const el = document.createElement("span");
    el.className = "lamp " + LAMPS[status];
    el.setAttribute("aria-hidden", "true");
    return el;
  }

  function lampClass(el) {
    return el ? LAMP_CLASSES.find((c) => el.classList.contains(c)) || "" : "";
  }

  // Links: only a same-site /hosts/<id>/ or /clusters/<id>/ path is accepted, so a bad
  // value can never become a javascript: or off-site link.
  function hostUrl(host) {
    if (typeof host.url === "string" && /^\/hosts\/\d+\/$/.test(host.url)) {
      return host.url;
    }
    return "/hosts/" + host.id + "/";
  }

  function clusterUrl(cluster) {
    if (typeof cluster.url === "string" && /^\/clusters\/\d+\/$/.test(cluster.url)) {
      return cluster.url;
    }
    return "/clusters/" + cluster.id + "/";
  }

  function issueList(row, max) {
    return (Array.isArray(row.issues) ? row.issues : [])
      .filter((issue) => issue && typeof issue === "object")
      .slice(0, max);
  }

  // Same wording as rack_unit_face.html.
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

  // "Offline" + its " · " separator, like the templates: <strong>Offline</strong><span
  // class="offline-note__sep"> · </span>. Glass hides both visually (labels off).
  function offlineWords() {
    const strong = document.createElement("strong");
    strong.textContent = "Offline";
    const sep = document.createElement("span");
    sep.className = "offline-note__sep";
    sep.textContent = " · ";
    return [strong, sep];
  }

  // --- One unit face (rack_unit_face.html), filled in place -----------------------------------
  // face = the Legacy <a> or the Glass <button>; both hold the same pieces.

  function fillFaceStatus(face, status, label, offlineText) {
    const offline = status === "offline";
    const holder = face.querySelector(".rack-unit__lamp"); // Legacy's face only
    if (LAMPS[status] && holder) {
      holder.appendChild(lamp(status));
    }
    const placeholder = face.querySelector(".rack-unit__head .status-icon");
    const note = face.querySelector(".rack-unit__offline");
    const icon = offline ? null : statusIcon(status, label);
    if (icon) {
      placeholder.replaceWith(icon);
    } else {
      placeholder.remove(); // offline: the "Offline · last …" line is the text label
    }
    if (offline) {
      note.replaceChildren(...offlineWords(), offlineText);
    } else {
      note.remove();
    }
  }

  function fillDisk(face, percent, fill) {
    const value = face.querySelector(".rack-unit__disk");
    const holder = face.querySelector(".readout__meter");
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
    const bar = holder.querySelector(".g-meter");
    if (bar) {
      bar.setAttribute("data-percent", String(p));
      bar.style.setProperty("--g-v", String(p));
      bar.classList.toggle("g-meter--hot", p >= 85);
      bar.classList.toggle("g-meter--fill", fill);
    }
  }

  function fillHostFace(face, host, isNew) {
    const status = text(host.status);
    const offline = status === "offline";
    const issues = issueList(host, 3);
    face.classList.toggle("rack-unit--offline", offline);
    face.querySelector(".rack-unit__name").textContent = text(host.name);
    fillFaceStatus(face, status, text(host.status_label), "last report " + text(host.last_seen_human));

    // Kubernetes role label: relabel the template's copy, or drop it with its ", " separator.
    const label = face.querySelector(".rack-unit__role");
    const role = ROLE_WORDS[text(host.k8s_role)];
    if (role) {
      label.querySelector(".role-label__text").textContent = role;
    } else {
      label.previousElementSibling.remove();
      label.remove();
    }

    const issueLine = face.querySelector(".rack-unit__issues");
    let line = issues.map((issue) => text(issue.text)).join(" · ");
    if (!issues.length && !offline) {
      line = status === "waiting" ? "Waiting for the first report" : "Nothing needs you";
    }
    if (line) {
      issueLine.querySelector(".rack-unit__issue-text").textContent = line;
    } else {
      issueLine.remove(); // offline with no issues: the offline line says it
    }
    face.querySelector(".rack-unit__summary-text").textContent = text(host.summary);
    face.querySelector(".rack-unit__updates").textContent = updatesText(host.updates);
    face.querySelector(".rack-unit__containers").textContent = containersText(host.containers);
    fillDisk(face, host.disk_max_percent, isNew && motionOK());
  }

  function fillClusterFace(face, cluster) {
    const status = text(cluster.status);
    const issues = issueList(cluster, 3);
    const nodes = cluster.nodes || {};
    const problems = count(cluster.problem_pods);
    const nodesText = count(nodes.ready) + "/" + count(nodes.total) + " ready";
    face.classList.toggle("rack-unit--offline", status === "offline");
    face.querySelector(".rack-unit__name").textContent = text(cluster.name);
    fillFaceStatus(face, status, text(cluster.status_label), "last data " + text(cluster.last_seen_human));

    let line = issues.map((issue) => text(issue.text)).join(" · ");
    if (!line) {
      line = { waiting: "Waiting for the first report", offline: "No current data" }[status] || "Nothing needs you";
    }
    face.querySelector(".cluster-unit__issues").textContent = line;
    face.querySelector(".rack-unit__summary-text").textContent = text(cluster.summary);
    face.querySelector(".cluster-unit__facts").textContent =
      "Nodes " + nodesText + " · " + plural(problems, "problem pod");
    face.querySelector(".cluster-unit__nodes").textContent = nodesText;
    face.querySelector(".cluster-unit__pods").textContent = problems + " problem" + (problems === 1 ? "" : "s");
    face.querySelector(".cluster-unit__version").textContent = text(cluster.version) || "n/a";
  }

  // --- The Glass expand region (.g-unit-more) ----------------------------------------------------

  function issueLine(issue) {
    const severity = issue.severity === "red" || issue.severity === "yellow" ? issue.severity : "info";
    const li = document.createElement("li");
    li.className = "issue-line issue-line--" + severity;
    const icon = statusIcon(severity, undefined);
    if (icon) {
      li.appendChild(icon);
    }
    const words = document.createElement("span");
    words.className = "issue-line__text";
    words.textContent = text(issue.text);
    li.appendChild(words);
    return li;
  }

  // Ids for aria-controls, the issues (or the note when there are none), "last report".
  function fillMore(li, kind, id, row, noteText) {
    const button = li.querySelector("button.g-unit");
    const more = li.querySelector(".g-unit-more");
    const regionId = "unit-more-" + kind + "-" + id;
    button.setAttribute("aria-controls", regionId);
    more.id = regionId;
    const list = more.querySelector(".g-unit-more__issues");
    const note = more.querySelector(".g-unit-more__note");
    const issues = issueList(row, 50);
    if (issues.length) {
      list.replaceChildren(...issues.map(issueLine));
      note.remove();
    } else {
      list.remove();
      note.textContent = noteText;
    }
    more.querySelector(".g-unit-more__seen").textContent = text(row.last_seen_human);
    more.querySelector(".g-unit-more__open").setAttribute("href", kind === "cluster" ? clusterUrl(row) : hostUrl(row));
    return more;
  }

  function moreNote(status, missing) {
    if (status === "offline") {
      return missing;
    }
    return status === "waiting" ? "Waiting for the first report." : "No issues. Every check passed.";
  }

  // One <li> for one host: a clone of the template, both controls filled from one row.
  function buildUnit(host, isNew) {
    const li = unitTemplate.content.firstElementChild.cloneNode(true);
    const status = text(host.status);
    const id = host.id;
    li.setAttribute("data-unit", "host");
    li.setAttribute("data-id", String(id));
    if (statusClass(status)) {
      li.classList.add(statusClass(status));
    }
    li.style.setProperty("--g-vt", "g-host-" + id);
    li.querySelectorAll(".rack-unit").forEach((face) => fillHostFace(face, host, isNew));
    li.querySelector("a.rack-unit").setAttribute("href", hostUrl(host));

    const more = fillMore(li, "host", id, host, moreNote(status, "No report since " + text(host.last_seen_human) + "."));
    const form = more.querySelector(".g-unit-more__check");
    const off = more.querySelector(".g-unit-more__check-off");
    const offNote = more.querySelector(".g-unit-more__off-note");
    if (status === "offline") {
      form.remove();
      offNote.id = "unit-off-host-" + id;
      off.setAttribute("aria-describedby", offNote.id);
    } else {
      form.setAttribute("action", "/hosts/" + id + "/collect/");
      form.setAttribute("data-status-url", "/api/v1/hosts/" + id + "/status.json");
      off.remove();
      offNote.remove();
    }
    return li;
  }

  function buildClusterUnit(cluster) {
    const li = clusterTemplate.content.firstElementChild.cloneNode(true);
    const status = text(cluster.status);
    const id = cluster.id;
    const nodes = cluster.nodes || {};
    li.setAttribute("data-unit", "cluster");
    li.setAttribute("data-id", String(id));
    if (statusClass(status)) {
      li.classList.add(statusClass(status));
    }
    li.style.setProperty("--g-vt", "g-cl-" + id);
    li.querySelectorAll(".rack-unit").forEach((face) => fillClusterFace(face, cluster));
    li.querySelector("a.rack-unit").setAttribute("href", clusterUrl(cluster));

    const more = fillMore(
      li, "cluster", id, cluster, moreNote(status, "No current data since " + text(cluster.last_seen_human) + ".")
    );
    more.querySelector(".cluster-more__nodes").textContent = count(nodes.ready) + "/" + count(nodes.total) + " ready";
    more.querySelector(".cluster-more__pods").textContent = String(count(cluster.problem_pods));
    more.querySelector(".cluster-more__version").textContent = text(cluster.version) || "n/a";
    return li;
  }

  // --- Glass "Needs attention" cards (components/attention_card.html) ----------------------------
  // Closed: name + dot, the summary line, the type. The expand region: every issue, facts,
  // Check now (online hosts) or its offline stand-in, Open host / Open cluster.

  function cardType(kind, row) {
    return kind === "cluster" ? "cluster" : ROLE_WORDS[text(row.k8s_role)] || "host";
  }

  function fact(dl, selector, value) {
    dl.querySelector(selector).textContent = value;
  }

  function buildAttentionCard(kind, row) {
    const li = clone("attention-card-template");
    const status = text(row.status);
    const id = row.id;
    const regionId = "attn-more-" + kind + "-" + id;
    li.setAttribute("data-card", kind);
    li.setAttribute("data-id", String(id));
    li.style.setProperty("--g-vt", "g-attn-" + (kind === "cluster" ? "c" : "h") + id);
    li.querySelector(".g-attn__btn").setAttribute("aria-controls", regionId);
    li.querySelector(".g-attn__name").textContent = text(row.name);
    const placeholder = li.querySelector(".g-attn__head .status-icon");
    if (status === "offline") {
      const offline = document.createElement("span");
      offline.className = "g-attn__offline";
      const words = document.createElement("span");
      words.className = "visually-hidden";
      words.textContent = "Offline";
      offline.append(words);
      placeholder.replaceWith(offline);
    } else {
      const icon = statusIcon(status, text(row.status_label));
      if (icon) {
        placeholder.replaceWith(icon);
      } else {
        placeholder.remove();
      }
    }
    li.querySelector(".g-attn__summary-text").textContent = text(row.summary);
    li.querySelector(".g-attn__type").textContent = cardType(kind, row);

    const more = li.querySelector(".g-attn-more");
    more.id = regionId;
    const list = more.querySelector(".g-attn-more__issues");
    const note = more.querySelector(".g-attn-more__note");
    const issues = issueList(row, 50);
    if (issues.length) {
      list.replaceChildren(...issues.map(issueLine));
      note.remove();
    } else {
      list.remove();
      const missing = kind === "cluster" ? "No current data since " : "No report since ";
      note.textContent = moreNote(status, missing + text(row.last_seen_human) + ".");
    }
    const hostFacts = more.querySelector(".g-attn-more__facts--host");
    const clusterFacts = more.querySelector(".g-attn-more__facts--cluster");
    const form = more.querySelector(".g-attn-more__check");
    const off = more.querySelector(".g-attn-more__check-off");
    const offNote = more.querySelector(".g-attn-more__off-note");
    const open = more.querySelector(".g-attn-more__open");
    if (kind === "cluster") {
      const nodes = row.nodes || {};
      const problems = count(row.problem_pods);
      hostFacts.remove();
      fact(clusterFacts, ".g-attn-more__nodes", count(nodes.ready) + "/" + count(nodes.total) + " ready");
      fact(clusterFacts, ".g-attn-more__pods", problems + " problem" + (problems === 1 ? "" : "s"));
      fact(clusterFacts, ".g-attn-more__version", text(row.version) || "n/a");
      form.remove();
      off.remove();
      offNote.remove();
      open.textContent = "Open cluster";
      open.setAttribute("href", clusterUrl(row));
      return li;
    }
    clusterFacts.remove();
    const disk = row.disk_max_percent;
    fact(hostFacts, ".g-attn-more__disk", Number.isFinite(disk) ? Math.round(disk) + "%" : "n/a");
    fact(hostFacts, ".g-attn-more__updates", updatesText(row.updates));
    fact(hostFacts, ".g-attn-more__seen", text(row.last_seen_human));
    if (status === "offline") {
      form.remove();
      offNote.id = "attn-off-host-" + id;
      off.setAttribute("aria-describedby", offNote.id);
    } else {
      form.setAttribute("action", "/hosts/" + id + "/collect/");
      form.setAttribute("data-status-url", "/api/v1/hosts/" + id + "/status.json");
      off.remove();
      offNote.remove();
    }
    open.textContent = "Open host";
    open.setAttribute("href", hostUrl(row));
    return li;
  }

  // --- "While you were away" rows (components/activity_row.html) ----------------------------------
  // There is no entry id, so a row is known by its time + status + text.

  function activityKey(entry) {
    return text(entry.time_human) + "|" + text(entry.status) + "|" + text(entry.text);
  }

  // The same key for a row already on the page (server-rendered rows carry no data-key).
  const LAMP_STATUS = { "lamp--red": "red", "lamp--amber": "yellow", "lamp--green": "green", "lamp--off": "offline" };

  function rowKey(row) {
    if (row.hasAttribute("data-key")) {
      return row.getAttribute("data-key");
    }
    const words = row.querySelector(".activity__words");
    const time = row.querySelector(".activity__time");
    return (
      (time ? time.textContent : "") + "|" + (LAMP_STATUS[lampClass(row.querySelector(".lamp"))] || "") + "|" +
      (words ? words.textContent : "")
    );
  }

  function buildActivityRow(entry) {
    const li = clone("activity-row-template");
    const status = text(entry.status);
    if (LAMPS[status]) {
      li.querySelector(".activity__lamp").appendChild(lamp(status));
    }
    li.querySelector(".activity__words").textContent = text(entry.text);
    li.querySelector(".activity__time").textContent = text(entry.time_human);
    li.setAttribute("data-key", activityKey(entry));
    return li;
  }

  // --- Headline, subline, tiles ---------------------------------------------------------------------

  function headline(data, hostCount) {
    const attention = count(data.attention_count);
    if (!hostCount) {
      return "No hosts yet";
    }
    return attention ? plural(attention, "thing") + (attention === 1 ? " needs you" : " need you") : "All good";
  }

  function renderHead(data, hostCount) {
    // Legacy's header: the headline and the subline.
    setText("overview-title", headline(data, hostCount));
    setText(
      "overview-sub",
      plural(hostCount, "host") + (hostCount ? " · updated " + text(data.updated_human) : "")
    );
    // Glass's top bar.
    setText("overview-updated", "updated " + text(data.updated_human));

    // Legacy's tiles.
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
    setText("hosts-count", " " + hostCount);
    renderChips(chipCounts(data, hostCount));
  }

  // --- Glass filter chips -----------------------------------------------------------------------

  const filterBox = byId("host-filter");
  let filter = "all";

  function chipCounts(data, hostCount) {
    const counts = data.status_counts || {};
    return {
      all: hostCount,
      red: count(counts.red),
      yellow: count(counts.yellow),
      green: count(counts.green),
      offline: count(counts.offline),
    };
  }

  function chips() {
    return filterBox ? Array.from(filterBox.querySelectorAll("button.g-chip[data-filter]")) : [];
  }

  // The counts (glass.js rolls .g-flow), and aria-disabled on the empty ones.
  function renderChips(counts) {
    chips().forEach((chip) => {
      const key = chip.getAttribute("data-filter");
      if (!FILTERS.includes(key)) {
        return;
      }
      setText("chip-" + key, String(counts[key]));
      if (key !== "all" && counts[key] === 0) {
        chip.setAttribute("aria-disabled", "true");
      } else {
        chip.removeAttribute("aria-disabled");
      }
    });
  }

  function pressChips() {
    chips().forEach((chip) => {
      chip.setAttribute("aria-pressed", chip.getAttribute("data-filter") === filter ? "true" : "false");
    });
  }

  // Units that don't match the filter get .is-filtered-out (hidden in Glass only).
  function applyFilter(units) {
    Array.from(units).forEach((li) => {
      const out = filter !== "all" && li.hasAttribute("data-unit") && slotStatus(li) !== "rack__slot--st-" + filter;
      li.classList.toggle("is-filtered-out", out);
    });
  }

  function readFilter() {
    try {
      const value = window.sessionStorage.getItem(FILTER_KEY);
      return FILTERS.includes(value) ? value : "all";
    } catch (e) {
      return "all"; // storage blocked: the default
    }
  }

  function saveFilter() {
    try {
      if (filter === "all") {
        window.sessionStorage.removeItem(FILTER_KEY);
      } else {
        window.sessionStorage.setItem(FILTER_KEY, filter);
      }
    } catch (e) {
      // storage blocked: the filter lasts until the page reloads
    }
  }

  // "Showing 1 of 3 hosts" / "Showing all 3 hosts", politely (#host-filter-status).
  function announceFilter() {
    const units = Array.from(rack.children).filter((li) => li.hasAttribute("data-unit"));
    const shown = units.filter((li) => !li.classList.contains("is-filtered-out")).length;
    setText(
      "host-filter-status",
      filter === "all" ? "Showing all " + plural(units.length, "host") : "Showing " + shown + " of " + plural(units.length, "host")
    );
  }

  function chipCount(key) {
    const el = byId("chip-" + key);
    const n = el ? Number(el.textContent) : 0;
    return Number.isInteger(n) ? n : 0;
  }

  // A filter whose chip is empty goes back to All (returns true when it did).
  function settleFilter(counts) {
    if (filter !== "all" && !counts[filter]) {
      filter = "all";
      saveFilter();
      return true;
    }
    return false;
  }

  function setFilter(next) {
    filter = next;
    saveFilter();
    // The announcement counts the units after the change, so it runs inside the update
    // (a view transition calls it later, asynchronously).
    const apply = () => {
      pressChips();
      applyFilter(rack.children);
      announceFilter();
    };
    if (motionOK() && typeof document.startViewTransition === "function" && document.visibilityState === "visible") {
      document.startViewTransition(apply);
    } else {
      apply();
    }
  }

  if (filterBox) {
    filterBox.addEventListener("click", (event) => {
      const chip = event.target instanceof Element ? event.target.closest("button.g-chip[data-filter]") : null;
      if (!chip || chip.getAttribute("aria-disabled") === "true") {
        return; // an empty chip: nothing to show (it stays focusable and readable)
      }
      const next = chip.getAttribute("data-filter");
      if (FILTERS.includes(next) && next !== filter) {
        setFilter(next);
      }
    });
    // A filter chosen earlier in this tab, if its chip still has hosts.
    filter = readFilter();
    const counts = {};
    FILTERS.forEach((key) => {
      counts[key] = chipCount(key);
    });
    settleFilter(counts);
    pressChips();
    applyFilter(rack.children);
  }

  // --- The network strip ---------------------------------------------------------------------------

  function netClass(list, value, fallback) {
    return list.includes(value) ? value : fallback;
  }

  function netLine(point) {
    const li = document.createElement("li");
    li.className = "net-ln net-ln--" + netClass(NET_LINES, point.line, "off");
    li.setAttribute("aria-hidden", "true");
    return li;
  }

  function fillNetPoint(li, point) {
    li.className = "net-pt net-pt--" + netClass(NET_STATUSES, point.status, "untested");
    li.querySelector(".net-pt__label").textContent = text(point.name);
    li.querySelector(".net-pt__word").textContent = ": " + text(point.word);
  }

  function buildNetPoint(point) {
    const li = clone("net-point-template");
    const kind = netClass(NET_KINDS, point.kind, "internet");
    li.setAttribute("data-kind", kind);
    const icons = byId("net-icons");
    const icon = icons && icons.content.querySelector('[data-kind="' + kind + '"] svg');
    if (icon) {
      li.querySelector(".net-pt__icon").replaceChildren(icon.cloneNode(true));
    }
    fillNetPoint(li, point);
    return li;
  }

  function renderNetwork(net) {
    let strip = byId("net-strip");
    if (!net || typeof net !== "object") {
      if (strip) {
        strip.remove();
      }
      return;
    }
    if (!strip) {
      strip = clone("net-strip-template");
      byId("net-strip-template").before(strip);
    }
    const status = byId("net-status");
    const before = status.textContent;
    const now = text(net.text);
    status.textContent = now;
    byId("net-more").setAttribute("href", "/network/"); // a fixed path, never from data
    const chain = byId("net-chain");
    const points = (Array.isArray(net.points) ? net.points : []).filter((p) => p && typeof p === "object");
    const shown = Array.from(chain.querySelectorAll(".net-pt"));
    const same =
      shown.length === points.length &&
      shown.every((li, i) => li.getAttribute("data-kind") === netClass(NET_KINDS, points[i].kind, "internet"));
    if (same) {
      // In place: the colours crossfade (glass.css).
      shown.forEach((li, i) => fillNetPoint(li, points[i]));
      Array.from(chain.querySelectorAll(".net-ln")).forEach((line, i) => {
        line.className = "net-ln net-ln--" + netClass(NET_LINES, points[i + 1].line, "off");
      });
    } else {
      const items = [];
      points.forEach((point, i) => {
        if (i) {
          items.push(netLine(point));
        }
        items.push(buildNetPoint(point));
      });
      chain.replaceChildren(...items);
    }
    if (before && before !== now) {
      setText("net-announce", now); // once: only when the state changed
    }
  }

  function netState(net) {
    return net && typeof net === "object"
      ? [text(net.text), (Array.isArray(net.points) ? net.points : []).map((p) => p && text(p.status))]
      : null;
  }

  function pageNetState() {
    const status = byId("net-status");
    if (!status) {
      return null;
    }
    const statuses = Array.from(document.querySelectorAll("#net-chain .net-pt")).map((li) =>
      (Array.from(li.classList).find((c) => c.startsWith("net-pt--")) || "").slice(8)
    );
    return [status.textContent, statuses];
  }

  // --- Sections that come and go -------------------------------------------------------------------

  // Glass's "Needs attention": full width, before the Hosts row.
  function renderAttention(cards) {
    let attention = byId("g-attention");
    if (!cards.length) {
      if (attention) {
        attention.remove();
      }
      return;
    }
    if (!attention) {
      attention = clone("g-attention-template");
      byId("overview-body").before(attention);
    }
    setText("g-attention-count", " " + cards.length);
    replaceKeepingFocus(byId("g-attention-list"), cards);
  }

  // Glass's "While you were away": inside the Hosts section, after the rack (before Clusters).
  function renderAway(rows) {
    let away = byId("g-away");
    if (!rows.length) {
      if (away) {
        away.remove();
      }
      return;
    }
    if (!away) {
      away = clone("g-away-template");
      const clustersHead = byId("clusters-head");
      if (clustersHead) {
        clustersHead.before(away);
      } else {
        byId("overview-hosts").append(away);
      }
    }
    byId("g-away-list").replaceChildren(...rows);
  }

  // Legacy's "While you were away": the newest 3, right of the rack (.overview-body--split).
  function renderLegacyAway(rows) {
    const body = byId("overview-body");
    let away = byId("away");
    body.classList.toggle("overview-body--split", rows.length > 0);
    if (!rows.length) {
      if (away) {
        away.remove();
      }
      return;
    }
    if (!away) {
      away = clone("away-template");
      body.append(away);
    }
    byId("away-list").replaceChildren(...rows);
  }

  // --- Keeping state across a rebuild ---------------------------------------------------------------

  // "host:3" for a rack unit, "card-host:3" for that host's attention card.
  function slotKey(li) {
    const card = li.getAttribute("data-card");
    return (card ? "card-" + card : li.getAttribute("data-unit")) + ":" + li.getAttribute("data-id");
  }

  // Units and cards the visitor opened: their keys, so the rebuilt ones open again.
  function openKeys() {
    const open = document.querySelectorAll(".rack__slot.is-open[data-unit], .g-attention__slot.is-open[data-card]");
    return new Set(Array.from(open).map(slotKey));
  }

  function reopen(items, keys) {
    items.forEach((li) => {
      if (keys.has(slotKey(li))) {
        li.classList.add("is-open");
        li.querySelector("button.g-unit, button.g-attn__btn").setAttribute("aria-expanded", "true");
      }
    });
  }

  // Replace a list's items; if one held keyboard focus, focus the same control (by its
  // position among the item's links and buttons) in the rebuilt item with the same key.
  function replaceKeepingFocus(list, items) {
    const focused = document.activeElement;
    const old = focused && list.contains(focused) ? focused.closest("li[data-unit], li[data-card]") : null;
    let index = -1;
    let key = null;
    if (old) {
      key = slotKey(old);
      index = Array.from(old.querySelectorAll("a, button")).indexOf(focused);
    }
    list.replaceChildren(...items);
    if (key && index >= 0) {
      const again = items.find((li) => slotKey(li) === key);
      const control = again && again.querySelectorAll("a, button")[index];
      if (control) {
        control.focus({ preventScroll: true });
      }
    }
  }

  // What a set of units, cards and rows shows, in one string: the headline, each unit's
  // key + lamp + issue line, each attention card's key and the activity rows. The same
  // function describes the page now and the nodes a refresh built, so equal strings mean
  // nothing visible changed (the "… ago" texts are left out on purpose).
  function viewState(title, unitItems, clusterItems, cardItems, rowItems) {
    const unit = (li) => {
      const line = li.querySelector("a.rack-unit .rack-unit__issues");
      const summary = li.querySelector(".g-unit .rack-unit__summary-text");
      return [
        slotKey(li),
        lampClass(li.querySelector(".rack-unit__lamp .lamp")),
        slotStatus(li),
        line ? line.textContent : "",
        summary ? summary.textContent : "",
      ];
    };
    const card = (li) => {
      const summary = li.querySelector(".g-attn__summary-text");
      return [slotKey(li), summary ? summary.textContent : ""];
    };
    return JSON.stringify([
      title,
      Array.from(unitItems, unit),
      Array.from(clusterItems, unit),
      Array.from(cardItems, card),
      Array.from(rowItems, rowKey),
    ]);
  }

  // Each rack unit's status class, to crossfade the gradients that changed (fadeStatus()).
  function pageStatuses() {
    const statuses = new Map();
    document.querySelectorAll(".rack__slot[data-unit]").forEach((li) => statuses.set(slotKey(li), slotStatus(li)));
    return statuses;
  }

  // The rebuilt unit starts with its old status class and switches to the new one a frame
  // later, so glass.css's --g-st transition crossfades the gradient.
  function fadeStatus(before) {
    pageStatuses().forEach((now, key) => {
      const old = before.get(key);
      if (!old || !now || old === now) {
        return;
      }
      const [kind, id] = key.split(":"); // id: digits only (validId)
      document.querySelectorAll('.rack__slot[data-unit="' + kind + '"][data-id="' + id + '"]').forEach((li) => {
        li.classList.replace(now, old);
        window.getComputedStyle(li).getPropertyValue("--g-st"); // the old colour is the start
        window.requestAnimationFrame(() => li.classList.replace(old, now));
      });
    });
  }

  function children(id) {
    const el = byId(id);
    return el ? el.children : [];
  }

  // Animated List: rows already there glide from where they were (FLIP, transform only);
  // rows that weren't there slide in (.g-new).
  function rowTops(list) {
    const tops = new Map();
    if (list) {
      Array.from(list.children).forEach((row) => tops.set(rowKey(row), row.getBoundingClientRect().top));
    }
    return tops;
  }

  function playList(list, before) {
    if (!list || !before.size) {
      return;
    }
    const moved = [];
    Array.from(list.children).forEach((row) => {
      const key = rowKey(row);
      if (!before.has(key)) {
        row.classList.add("g-new");
        return;
      }
      const dy = before.get(key) - row.getBoundingClientRect().top;
      if (Math.abs(dy) > 0.5) {
        row.style.setProperty("--g-flip-y", dy.toFixed(1) + "px");
        row.classList.add("g-flip-from");
        moved.push(row);
      }
    });
    if (moved.length) {
      window.requestAnimationFrame(() => {
        moved.forEach((row) => {
          row.classList.add("g-flip");
          row.classList.remove("g-flip-from");
        });
      });
    }
  }

  // --- Reload rules ---------------------------------------------------------------------------------

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

  // --- The refresh ----------------------------------------------------------------------------------

  // Stable view-transition names on the server-rendered units and cards (CSSOM).
  document.querySelectorAll("li[data-unit][data-id], li[data-card][data-id]").forEach((li) => {
    const card = li.hasAttribute("data-card");
    const kind = li.getAttribute(card ? "data-card" : "data-unit") === "cluster" ? "cluster" : "host";
    const id = li.getAttribute("data-id");
    if (!/^\d+$/.test(id)) {
      return;
    }
    let name = (kind === "cluster" ? "g-cl-" : "g-host-") + id;
    if (card) {
      name = "g-attn-" + (kind === "cluster" ? "c" : "h") + id;
    }
    li.style.setProperty("--g-vt", name);
  });

  let seen = new Set(Array.from(document.querySelectorAll(".rack__slot[data-unit]")).map(slotKey));
  let running = null; // the refresh in progress
  let previewOpen = false;
  let refreshAfterPreview = false;

  async function load() {
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
    return data;
  }

  async function update() {
    let data;
    try {
      data = await load();
    } catch (e) {
      showFailed(true); // keep the current content, try again on the next tick
      return;
    }
    showFailed(false);
    if (previewOpen) {
      refreshAfterPreview = true; // the preview opened while this request was running
      return;
    }

    const showsExample = rack.querySelector(".rack-unit--example") !== null;
    const hosts = unique(data.hosts.filter((h) => h && typeof h === "object" && validId(h.id)));
    const clusters = unique(
      (Array.isArray(data.clusters) ? data.clusters : []).filter((c) => c && typeof c === "object" && validId(c.id))
    );
    const activity = (Array.isArray(data.activity) ? data.activity : []).filter((a) => a && typeof a === "object");
    const clustersShown = clusterRack !== null && clusterTemplate !== null;
    if (showsExample !== (hosts.length === 0) || clustersShown !== (clusters.length > 0)) {
      // Example shown but hosts exist, or units shown but none left; or the Clusters rack
      // should appear or go. If the reload was already tried, keep the page as it is
      // rather than show a mix (or an empty rack).
      reloadOnce();
      return;
    }
    clearReloadMark();

    // Build everything off the page first (no layout yet).
    const keepOpen = openKeys();
    const counts = chipCounts(data, hosts.length);
    const filterReset = filterBox !== null && settleFilter(counts);
    const units = showsExample ? null : hosts.map((h) => buildUnit(h, !seen.has("host:" + h.id)));
    const clusterUnits = clustersShown ? clusters.map(buildClusterUnit) : null;
    if (units) {
      reopen(units, keepOpen);
      applyFilter(units);
    }
    if (clusterUnits) {
      reopen(clusterUnits, keepOpen);
    }
    const cards = showsExample
      ? []
      : hosts
          .filter((h) => NEEDS_ATTENTION.includes(h.status))
          .map((h) => buildAttentionCard("host", h))
          .concat(
            clusters.filter((c) => NEEDS_ATTENTION.includes(c.status)).map((c) => buildAttentionCard("cluster", c))
          );
    reopen(cards, keepOpen);
    const glassRows = activity.slice(0, GLASS_ACTIVITY).map(buildActivityRow);
    const legacyRows = activity.slice(0, LEGACY_ACTIVITY).map(buildActivityRow);

    const title = byId("overview-title");
    const stateBefore = viewState(
      JSON.stringify([title ? title.textContent : "", pageNetState()]),
      rack.children,
      children("cluster-rack"),
      children("g-attention-list"),
      children("g-away-list")
    );
    const stateAfter = viewState(
      JSON.stringify([headline(data, hosts.length), netState(data.network)]),
      units || rack.children,
      clusterUnits || [],
      cards,
      glassRows
    );
    const statusesBefore = pageStatuses();
    const allowed = motionOK();
    const awayBefore = allowed ? rowTops(byId("g-away-list")) : new Map();
    let animate = false;

    function swap() {
      renderHead(data, hosts.length);
      renderNetwork(data.network);
      pressChips();
      if (units) {
        replaceKeepingFocus(rack, units);
      }
      if (clusterUnits) {
        replaceKeepingFocus(clusterRack, clusterUnits);
        setText("clusters-count", " " + clusters.length);
      }
      renderAttention(cards);
      renderAway(glassRows);
      renderLegacyAway(legacyRows);
    }

    animate = allowed && stateBefore !== stateAfter;

    if (animate && typeof document.startViewTransition === "function" && document.visibilityState === "visible") {
      const transition = document.startViewTransition(swap);
      try {
        await transition.updateCallbackDone;
      } catch (e) {
        swap(); // the transition could not run the swap: apply it directly
      }
      playList(byId("g-away-list"), awayBefore);
      fadeStatus(statusesBefore);
      try {
        await transition.finished;
      } catch (e) {
        // skipped (e.g. the tab was hidden): the page already shows the new state
      }
    } else {
      swap();
      if (animate) {
        playList(byId("g-away-list"), awayBefore);
        fadeStatus(statusesBefore);
      }
    }
    if (filterReset) {
      announceFilter(); // the chosen chip went empty: everything shows again
    }
    seen = new Set(Array.from(document.querySelectorAll(".rack__slot[data-unit]")).map(slotKey));
  }

  // One refresh at a time: a request while one runs gets the running one's promise.
  function refresh() {
    if (previewOpen) {
      refreshAfterPreview = true;
      return Promise.resolve();
    }
    if (!running) {
      running = update().finally(() => {
        running = null;
      });
    }
    return running;
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

  // actions.js asks for a refresh after "Check all now" / "Check now". preventDefault() tells
  // it the event was handled (so it doesn't reload the page); detail.promise resolves once
  // the page shows the new data.
  document.addEventListener("cronrack:refresh", (event) => {
    event.preventDefault();
    const promise = refresh();
    if (event.detail && typeof event.detail === "object") {
      event.detail.promise = promise;
    }
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
