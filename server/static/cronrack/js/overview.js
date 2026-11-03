// PLACEHOLDER — Apo owns the final version.
// Overview auto-refresh (handoff 5.3). Loaded with defer, on the overview page only.
//
// Every 60s while the tab is visible, and once when it becomes visible again, fetch
// /api/v1/overview.json and update the headline, the subline, the summary tiles, the racks
// and the sections around them. "Check all now" / "Check now" (actions.js) ask for one
// refresh with a "cronrack:refresh" event; this file puts a promise on event.detail.promise
// that resolves once the page shows the new data (after the view transition, if any).
//
// All host data is untrusted. Everything is built from clones of the page's <template>s and
// filled with textContent / setAttribute / className only; CSS custom properties go through
// element.style.setProperty. No HTML string is ever parsed, so text like <script> is shown as
// text and can't become markup. Nothing is logged.
//
// What a refresh renders (the same as the server's templates):
//   - each host / cluster unit (components/rack_unit.html, cluster_unit.html): BOTH controls,
//     the Legacy link and the Glass button with its expand region, from one data row, so the
//     two skins never disagree. Units the visitor opened (Glass) stay open, and focus moves
//     to the same control of the rebuilt unit.
//   - Glass: "Needs attention" (one card per host and cluster that is red, yellow or offline,
//     the set the headline counts) and "While you were away" (newest 10), side by side in
//     #g-duo. A section with nothing in it is removed, and added back from its <template>.
//   - Legacy: "While you were away" with the newest 3, right of the rack.
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
//   - the headline changes word by word (.g-word), a changed status sends its dot's ring
//     (.g-ring) and crossfades its colour, new activity rows slide in (.g-new) while the
//     others move down (FLIP, transform only).
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
    if (LAMPS[status]) {
      face.querySelector(".rack-unit__lamp").appendChild(lamp(status));
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

  function attentionMeta(kind, row) {
    if (kind === "cluster") {
      const nodes = row.nodes || {};
      return (
        "cluster · nodes " + count(nodes.ready) + "/" + count(nodes.total) + " ready · " +
        plural(count(row.problem_pods), "problem pod")
      );
    }
    const parts = [];
    const role = ROLE_WORDS[text(row.k8s_role)];
    if (role) {
      parts.push(role);
    }
    if (row.containers) {
      parts.push(plural(count(row.containers.total), "container"));
    }
    if (Number.isFinite(row.disk_max_percent)) {
      parts.push("disk " + Math.round(row.disk_max_percent) + "%");
    }
    parts.push("report " + text(row.last_seen_human));
    return parts.join(" · ");
  }

  function buildAttentionCard(kind, row) {
    const li = clone("attention-card-template");
    const status = text(row.status);
    li.setAttribute("data-unit", kind);
    li.setAttribute("data-id", String(row.id));
    li.style.setProperty("--g-vt", "g-attn-" + (kind === "cluster" ? "c" : "h") + row.id);
    li.querySelector(".g-attn").setAttribute("href", kind === "cluster" ? clusterUrl(row) : hostUrl(row));
    li.querySelector(".g-attn__name").textContent = text(row.name);
    const placeholder = li.querySelector(".g-attn__head .status-icon");
    if (status === "offline") {
      const offline = document.createElement("span");
      offline.className = "g-attn__offline";
      const words = document.createElement("span");
      words.className = "visually-hidden";
      words.textContent = "Offline";
      offline.append(lamp("offline"), words);
      placeholder.replaceWith(offline);
    } else {
      const icon = statusIcon(status, text(row.status_label));
      if (icon) {
        placeholder.replaceWith(icon);
      } else {
        placeholder.remove();
      }
    }
    let issue = issueList(row, 2)
      .map((item) => text(item.text))
      .join(" · ");
    if (!issue && status === "offline") {
      issue = kind === "cluster" ? "No current data" : "Offline · last report " + text(row.last_seen_human);
    }
    li.querySelector(".g-attn__issue-text").textContent = issue;
    li.querySelector(".g-attn__meta-text").textContent = attentionMeta(kind, row);
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

  // Per Word Crossfade: each word its own span, coming in one after another (glass.css).
  function setTitle(value, animate) {
    const title = byId("overview-title");
    if (!title || title.textContent === value) {
      return;
    }
    if (!animate) {
      title.textContent = value;
      return;
    }
    const parts = [];
    value.split(" ").forEach((word, i) => {
      if (i) {
        parts.push(" ");
      }
      const span = document.createElement("span");
      span.className = "g-word";
      span.style.setProperty("--g-i", String(i));
      span.textContent = word;
      parts.push(span);
    });
    title.replaceChildren(...parts);
  }

  function renderHead(data, hostCount, animate) {
    setTitle(headline(data, hostCount), animate);
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
    // Glass's fourth tile (hidden in Legacy) counts the offline hosts.
    setText("tile-offline-count", String(offline));
    setText("tile-offline-count-unit", offline === 1 ? "host" : "hosts");
    const note = byId("tile-offline");
    const noteText = byId("tile-offline-text");
    if (note && noteText) {
      const strong = document.createElement("strong");
      strong.textContent = "Offline";
      noteText.replaceChildren(strong, " · " + plural(offline, "host"));
      note.hidden = offline === 0;
    }
    setText("hosts-count", " " + hostCount);
  }

  // --- Sections that come and go -------------------------------------------------------------------

  // Glass's row: [Needs attention | While you were away], right after the tiles.
  function renderDuo(cards, rows) {
    let duo = byId("g-duo");
    if (!cards.length && !rows.length) {
      if (duo) {
        duo.remove();
      }
      return;
    }
    if (!duo) {
      duo = clone("g-duo-template");
      byId("overview-body").before(duo);
    }
    let attention = byId("g-attention");
    if (cards.length) {
      if (!attention) {
        attention = clone("g-attention-template");
        duo.prepend(attention);
      }
      setText("g-attention-count", " " + cards.length);
      replaceKeepingFocus(byId("g-attention-list"), cards);
    } else if (attention) {
      attention.remove();
    }
    let away = byId("g-away");
    if (rows.length) {
      if (!away) {
        away = clone("g-away-template");
        duo.append(away);
      }
      byId("g-away-list").replaceChildren(...rows);
    } else if (away) {
      away.remove();
    }
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

  function slotKey(li) {
    return li.getAttribute("data-unit") + ":" + li.getAttribute("data-id");
  }

  // Units the visitor opened: their keys, so the rebuilt units open again.
  function openKeys() {
    return new Set(Array.from(document.querySelectorAll(".rack__slot.is-open[data-unit]")).map(slotKey));
  }

  function reopen(units, keys) {
    units.forEach((li) => {
      if (keys.has(slotKey(li))) {
        li.classList.add("is-open");
        li.querySelector("button.g-unit").setAttribute("aria-expanded", "true");
      }
    });
  }

  // Replace a list's items; if one held keyboard focus, focus the same control (by its
  // position among the item's links and buttons) in the rebuilt item with the same key.
  function replaceKeepingFocus(list, items) {
    const focused = document.activeElement;
    const old = focused && list.contains(focused) ? focused.closest("li[data-unit]") : null;
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

  // Lamp per unit on the page, to find the dots whose status changed (ring()).
  function pageLamps() {
    const lamps = new Map();
    document.querySelectorAll(".rack__slot[data-unit]").forEach((li) => {
      lamps.set(slotKey(li), lampClass(li.querySelector(".rack-unit__lamp .lamp")));
    });
    return lamps;
  }

  // What a set of units, cards and rows shows, in one string: the headline, each unit's
  // key + lamp + issue line, each attention card's key and the activity rows. The same
  // function describes the page now and the nodes a refresh built, so equal strings mean
  // nothing visible changed (the "… ago" texts are left out on purpose).
  function viewState(title, unitItems, clusterItems, cardItems, rowItems) {
    const unit = (li) => {
      const line = li.querySelector("a.rack-unit .rack-unit__issues");
      return [slotKey(li), lampClass(li.querySelector(".rack-unit__lamp .lamp")), line ? line.textContent : ""];
    };
    return JSON.stringify([
      title,
      Array.from(unitItems, unit),
      Array.from(clusterItems, unit),
      Array.from(cardItems, slotKey),
      Array.from(rowItems, rowKey),
    ]);
  }

  function children(id) {
    const el = byId(id);
    return el ? el.children : [];
  }

  // Status ring: the changed units' dots crossfade from the old colour and send one ring.
  function ring(before) {
    pageLamps().forEach((now, key) => {
      const old = before.get(key);
      if (!old || !now || old === now) {
        return;
      }
      const [kind, id] = key.split(":"); // id: digits only (validId)
      document.querySelectorAll('[data-unit="' + kind + '"][data-id="' + id + '"]').forEach((el) => {
        el.querySelectorAll(".rack-unit__lamp .lamp, .g-attn__head .lamp").forEach((dot) => {
          dot.classList.replace(now, old);
          dot.classList.add("g-dot-fade", "g-ring");
          window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => dot.classList.replace(old, now));
          });
        });
        el.querySelectorAll(".g-attn__head > .status-icon").forEach((icon) => icon.classList.add("g-ring"));
      });
    });
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
  document.querySelectorAll("li[data-unit][data-id]").forEach((li) => {
    const kind = li.getAttribute("data-unit") === "cluster" ? "cluster" : "host";
    const id = li.getAttribute("data-id");
    if (!/^\d+$/.test(id)) {
      return;
    }
    let name = (kind === "cluster" ? "g-cl-" : "g-host-") + id;
    if (li.classList.contains("g-attention__slot")) {
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
    const units = showsExample ? null : hosts.map((h) => buildUnit(h, !seen.has("host:" + h.id)));
    const clusterUnits = clustersShown ? clusters.map(buildClusterUnit) : null;
    if (units) {
      reopen(units, keepOpen);
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
    const glassRows = activity.slice(0, GLASS_ACTIVITY).map(buildActivityRow);
    const legacyRows = activity.slice(0, LEGACY_ACTIVITY).map(buildActivityRow);

    const title = byId("overview-title");
    const stateBefore = viewState(
      title ? title.textContent : "",
      rack.children,
      children("cluster-rack"),
      children("g-attention-list"),
      children("g-away-list")
    );
    const stateAfter = viewState(
      headline(data, hosts.length),
      units || rack.children,
      clusterUnits || [],
      cards,
      glassRows
    );
    const lampsBefore = pageLamps();
    const allowed = motionOK();
    const awayBefore = allowed ? rowTops(byId("g-away-list")) : new Map();
    let animate = false;

    function swap() {
      renderHead(data, hosts.length, animate);
      if (units) {
        replaceKeepingFocus(rack, units);
      }
      if (clusterUnits) {
        replaceKeepingFocus(clusterRack, clusterUnits);
        setText("clusters-count", " " + clusters.length);
      }
      renderDuo(cards, glassRows);
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
      ring(lampsBefore);
      try {
        await transition.finished;
      } catch (e) {
        // skipped (e.g. the tab was hidden): the page already shows the new state
      }
    } else {
      swap();
      if (animate) {
        playList(byId("g-away-list"), awayBefore);
        ring(lampsBefore);
      }
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
