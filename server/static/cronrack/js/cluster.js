// PLACEHOLDER — Apo owns the final version.
// Cluster page auto-refresh. Loaded with defer, on the cluster page only.
//
// Every 60s while the tab is visible, and once when it becomes visible again, fetch the
// cluster's detail.json and update what changes most: the header status (lamp, status icon,
// meta line), the summary readouts and "Needs your attention". The long sections (nodes,
// pods, workloads ...) are not rebuilt here: when a newer snapshot arrived, a "New data ·
// Show latest" key appears instead, which reloads this same fixed page.
//
// All cluster data is untrusted. Findings are clones of <template id="finding-template">,
// filled with textContent / setAttribute / className only, so no HTML string is ever parsed.
// The fetch URL and the page link must be this cluster's own fixed paths, and a fix's id
// comes from the finding's hex fingerprint only. Nothing is logged.
//
// If a refresh fails, the "Offline — retrying" line appears and the page keeps what it shows.
(function () {
  "use strict";
  const REFRESH_EVERY_MS = 60000;
  const JSON_PATH = /^\/api\/v1\/clusters\/\d+\/detail\.json$/;
  const PAGE_PATH = /^\/clusters\/\d+\/$/;
  const FINGERPRINT = /^[0-9a-f]{12,64}$/;

  const live = document.getElementById("cluster-live");
  const findingTemplate = document.getElementById("finding-template");
  const jsonUrl = live ? live.getAttribute("data-json-url") : null;
  if (!live || !findingTemplate || !jsonUrl || !JSON_PATH.test(jsonUrl)) {
    return;
  }
  const shownSnapshot = live.getAttribute("data-snapshot") || "";

  // Lamps like lamp.html: offline and waiting get the unlit one.
  const LAMPS = {
    red: "lamp--red",
    yellow: "lamp--amber",
    green: "lamp--green",
    offline: "lamp--off",
    waiting: "lamp--off",
  };
  // Status icons are clones from <template id="status-icons"> (base.html).
  const ICON_KINDS = { red: "bad", yellow: "warn", green: "good", waiting: "waiting" };
  const SEVERITIES = { red: "red", yellow: "yellow" };

  function byId(id) {
    return document.getElementById(id);
  }

  function text(value) {
    return typeof value === "string" ? value : "";
  }

  function count(value) {
    return Number.isInteger(value) && value >= 0 ? value : 0;
  }

  function setText(id, value) {
    const el = byId(id);
    if (el) {
      el.textContent = value;
    }
  }

  // A status icon; `words` replaces its default words (visually hidden + title) if given.
  function statusIcon(status, words) {
    const icons = byId("status-icons");
    const proto = icons && icons.content.querySelector(".status-icon--" + (ICON_KINDS[status] || "info"));
    if (!proto) {
      return null;
    }
    const icon = proto.cloneNode(true);
    if (words) {
      icon.setAttribute("title", words);
      icon.querySelector(".visually-hidden").textContent = words;
    }
    return icon;
  }

  function renderHeader(cluster) {
    const status = text(cluster.status);
    const offline = status === "offline";

    const lampHolder = byId("cluster-lamp");
    if (lampHolder) {
      const lamp = document.createElement("span");
      lamp.className = "lamp " + (LAMPS[status] || "lamp--off");
      lamp.setAttribute("aria-hidden", "true");
      lampHolder.replaceChildren(lamp);
    }

    const title = byId("cluster-title");
    if (title) {
      const old = title.querySelector(".status-icon");
      const icon = offline ? null : statusIcon(status, text(cluster.status_label));
      if (old) {
        old.remove();
      }
      if (icon) {
        title.appendChild(icon); // offline: the meta line says it in words instead
      }
    }

    const meta = byId("cluster-meta");
    if (meta) {
      const parts = [];
      if (offline) {
        // Like the template: Glass hides "Offline" + " · " visually (labels off).
        const strong = document.createElement("strong");
        strong.textContent = "Offline";
        const sep = document.createElement("span");
        sep.className = "offline-note__sep";
        sep.textContent = " · ";
        parts.push(strong, sep, "last data " + text(cluster.last_seen_human));
      } else {
        parts.push(cluster.has_snapshot ? "Updated " + text(cluster.last_seen_human) : "No report yet");
      }
      if (text(cluster.reporter)) {
        parts.push(" · via " + text(cluster.reporter));
      }
      if (text(cluster.version)) {
        parts.push(" · " + text(cluster.version));
      }
      meta.replaceChildren(...parts);
      meta.classList.toggle("offline-note", offline);
    }
  }

  // Same wording as the readouts in clusters/detail.html.
  function renderTiles(tiles) {
    if (!tiles || typeof tiles !== "object") {
      return;
    }
    const pods = tiles.pods || {};
    const problems = count(pods.problem);
    setText("tile-nodes", count(tiles.nodes_ready) + "/" + count(tiles.nodes_total));
    setText("tile-pods", count(pods.running) + " running · " + problems + " problem" + (problems === 1 ? "" : "s"));
    setText("tile-workloads", count(tiles.workloads_ok) + "/" + count(tiles.workloads_total));
    setText("tile-argo", count(tiles.argo) ? count(tiles.argo_synced) + "/" + count(tiles.argo) : "—");
    setText("tile-updates", Number.isInteger(tiles.updates) ? String(count(tiles.updates)) : "Off");
  }

  // One finding: a clone of the template, filled like components/finding_card.html.
  function buildFinding(issue) {
    const fragment = findingTemplate.content.cloneNode(true);
    const li = fragment.firstElementChild;
    const severity = SEVERITIES[text(issue.severity)] || "info";
    li.className = "finding finding--" + severity;

    const line = li.querySelector(".issue-line");
    line.className = "issue-line issue-line--" + severity;
    const icon = statusIcon(severity === "info" ? "info" : severity, null);
    const placeholder = line.querySelector(".status-icon");
    if (icon && placeholder) {
      placeholder.replaceWith(icon);
    }
    line.querySelector(".issue-line__text").textContent = text(issue.text);

    const why = li.querySelector(".finding__why");
    if (text(issue.why)) {
      why.textContent = text(issue.why);
    } else {
      why.remove();
    }

    const head = li.querySelector(".code-head");
    const code = li.querySelector("pre.code");
    const fingerprint = text(issue.fingerprint);
    if (text(issue.fix) && FINGERPRINT.test(fingerprint)) {
      const id = "fix-" + fingerprint.slice(0, 12);
      code.id = id;
      code.textContent = text(issue.fix);
      head.querySelector(".js-copy").setAttribute("aria-controls", id);
    } else {
      head.remove();
      code.remove();
    }
    return li;
  }

  function renderFindings(data) {
    const list = byId("cluster-issues");
    if (!list) {
      return;
    }
    // Don't pull the rug from under a keyboard user (e.g. on a Copy key): next time.
    if (list.contains(document.activeElement)) {
      return;
    }
    const issues = (Array.isArray(data.issues) ? data.issues : []).filter((i) => i && typeof i === "object");
    list.replaceChildren(...issues.map(buildFinding));
    const cluster = data.cluster;
    const none = byId("cluster-no-issues");
    if (none) {
      none.hidden = issues.length > 0 || text(cluster.status) === "offline" || !cluster.has_snapshot;
    }
  }

  // The long sections are server-rendered: offer the same fixed page when they're stale.
  function renderNewData(cluster, wasOffline) {
    const note = byId("cluster-new");
    if (!note) {
      return;
    }
    const link = note.querySelector("a");
    const url = text(cluster.url);
    if (link && PAGE_PATH.test(url)) {
      link.setAttribute("href", url);
    }
    const changed =
      text(cluster.snapshot_received_at) !== shownSnapshot || (text(cluster.status) === "offline") !== wasOffline;
    note.hidden = !changed;
  }

  function showFailed(on) {
    const line = byId("refresh-failed");
    if (line) {
      line.hidden = !on;
    }
  }

  // Whether the server rendered this page for an offline cluster (its fixed offline block).
  const renderedOffline = byId("cluster-agent-check") !== null;
  let inFlight = false;

  async function refresh() {
    if (inFlight) {
      return;
    }
    inFlight = true;
    try {
      const response = await fetch(jsonUrl, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      // A logged-out session gets the login page (HTML), which is not a cluster.
      const data = response.ok ? await response.json() : null;
      if (!data || !data.cluster || typeof data.cluster !== "object") {
        throw new Error("bad response");
      }
      showFailed(false);
      renderHeader(data.cluster);
      renderTiles(data.tiles);
      renderFindings(data);
      renderNewData(data.cluster, renderedOffline);
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

  if (document.visibilityState === "visible") {
    start();
  }
})();
