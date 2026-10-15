// PLACEHOLDER — Apo owns the final version.
// Host preview on the overview (handoff 5.3). Loaded with defer, on the overview page only.
//
// The rack units stay plain <a href="/hosts/<id>/"> links. This script takes over only a
// plain primary click or Enter on a real host's unit (not ctrl/cmd/shift/alt, not a middle
// click, never the Welcome example) and opens <dialog id="host-preview"> instead:
//   1. the unit slides 6px out of the rack (120ms)
//   2. the dialog opens with showModal() and grows out of the unit (FLIP, 200ms; on phones a
//      bottom sheet slides up instead)
//   3. /api/v1/hosts/<id>/summary.json is fetched; the small loader shows only after 300ms;
//      on failure: "Couldn't load this host" + Retry
//   4. the body is a clone of <template id="host-preview-template">, filled with
//      textContent / setAttribute only. No HTML string is ever parsed, so hostile text
//      (container names, mounts...) stays text. Nothing is logged or stored in the page.
// Close (Esc, Close key, a click on the dimmed page) plays the opening backwards, then
// closes; focus goes back to the unit. "Open host page" grows the panel over the viewport,
// then goes to the unit's own /hosts/<id>/ link (never a URL from the JSON or the query).
//
// Motion uses element.animate() (Web Animations), so no style="" is ever written. With
// prefers-reduced-motion nothing moves or scales: open, close and navigate are instant.
// overview.js pauses its auto-refresh between "cronrack:preview-open" and
// "cronrack:preview-closed", so the unit that opened the preview stays the same element.
(function () {
  "use strict";
  const HOST_PATH = /^\/hosts\/(\d+)\/$/;
  const LOADER_DELAY_MS = 300;
  const SLIDE_MS = 120;
  const SLIDE_PX = 6;
  const MORPH_MS = 200;

  function byId(id) {
    return document.getElementById(id);
  }

  const dialog = byId("host-preview");
  const rack = byId("rack");
  const template = byId("host-preview-template");
  if (!dialog || !rack || !template || typeof dialog.showModal !== "function") {
    return; // no <dialog> support: the units keep working as plain links
  }
  const inner = byId("host-preview-inner");
  const nameEl = byId("host-preview-name");
  const lamp = byId("host-preview-lamp");
  const chip = byId("host-preview-chip");
  const offlineLine = byId("host-preview-offline");
  const loading = byId("host-preview-loading");
  const loader = byId("host-preview-loader");
  const errorBox = byId("host-preview-error");
  const errorText = byId("host-preview-error-text");
  const retry = byId("host-preview-retry");
  const body = byId("host-preview-body");
  const openLink = byId("host-preview-open");
  const closeKey = byId("host-preview-close");

  // Same lamps and chips as the rack (overview.js / rack_unit.html).
  const LAMPS = {
    red: "lamp--red",
    yellow: "lamp--amber",
    green: "lamp--green",
    offline: "lamp--off",
    waiting: "lamp--off",
  };
  const CHIPS = { red: "chip--red", yellow: "chip--amber", green: "chip--green" };
  // Issues are never colour-only: screen readers hear the severity in words first.
  const SEVERITY_WORDS = { red: "Needs you: ", yellow: "Heads up: ", info: "Info: " };

  const canAnimate = typeof Element.prototype.animate === "function";

  let unit = null; // the rack unit that opened the preview
  let hostUrl = null; // its own href, checked against HOST_PATH
  let hostId = null; // the digits from that href
  let busy = false; // an animation is running: ignore clicks and Esc
  let closing = false;
  let slide = null; // the unit's slide-out Animation
  let opened = Promise.resolve(); // resolves when the open animation is done
  let request = null; // AbortController of the running fetch
  let loadSeq = 0;
  let loaderTimer = 0;

  function moves() {
    return canAnimate && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function isPhone() {
    return window.matchMedia("(max-width: 599.98px)").matches;
  }

  // An Animation's finished promise rejects when it's cancelled; either way we go on.
  function finished(animation) {
    return animation ? animation.finished.then(() => {}, () => {}) : Promise.resolve();
  }

  function text(value) {
    return typeof value === "string" ? value : "";
  }

  function count(value) {
    return Number.isInteger(value) && value >= 0 ? value : 0;
  }

  function percent(value) {
    return Number.isFinite(value) ? Math.min(100, Math.max(0, Math.round(value))) : null;
  }

  function plainClick(event) {
    return (
      event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey
    );
  }

  // The transform that makes a box at `to` look like it sits at `from` (the "Invert" in FLIP).
  function invert(from, to) {
    const dx = from.left - to.left;
    const dy = from.top - to.top;
    const sx = from.width / to.width;
    const sy = from.height / to.height;
    return "translate(" + dx + "px, " + dy + "px) scale(" + sx + ", " + sy + ")";
  }

  function fadeBackdrop(show) {
    try {
      dialog.animate(show ? [{ opacity: 0 }, { opacity: 1 }] : [{ opacity: 1 }, { opacity: 0 }], {
        duration: MORPH_MS,
        pseudoElement: "::backdrop",
        fill: show ? "none" : "forwards",
      });
    } catch (e) {
      // ::backdrop can't be animated in this browser: it simply appears and disappears
    }
  }

  // The unit that opened the preview. Auto-refresh is paused while the preview is open, so
  // normally it's the same element; if it was replaced anyway, find it again by its link.
  function currentUnit() {
    if (unit && unit.isConnected) {
      return unit;
    }
    if (!hostUrl) {
      return null;
    }
    const units = rack.querySelectorAll("a.rack-unit");
    return Array.prototype.find.call(units, (a) => a.getAttribute("href") === hostUrl) || null;
  }

  // --- header ----------------------------------------------------------------------

  function setHeader(status, label, name, lastSeen) {
    nameEl.textContent = name;
    lamp.hidden = !LAMPS[status];
    lamp.className = "lamp " + (LAMPS[status] || "lamp--off");
    const offline = status === "offline";
    chip.hidden = offline || !label;
    chip.className = "chip " + (CHIPS[status] || "chip--neutral");
    chip.textContent = label;
    offlineLine.hidden = !offline;
    if (offline) {
      const strong = document.createElement("strong");
      strong.textContent = "Offline";
      offlineLine.replaceChildren(strong, " · last report " + lastSeen);
    }
  }

  // Until the data arrives, the header shows what the unit already shows: its name, and its
  // lamp and chip if it has them (matched by known class names, never by copying markup).
  // Offline and waiting units have the unlit lamp; offline ones have no chip, so none shows.
  function headerFromUnit(link) {
    const name = link.querySelector(".rack-unit__name");
    const unitLamp = link.querySelector(".rack-unit__lamp .lamp");
    const unitChip = link.querySelector(".rack-unit__head .chip");
    let status = "waiting";
    ["red", "yellow", "green"].forEach((key) => {
      if (unitLamp && unitLamp.classList.contains(LAMPS[key])) {
        status = key;
      }
    });
    setHeader(status, unitChip ? unitChip.textContent : "", name ? name.textContent : "", "");
  }

  // --- states ----------------------------------------------------------------------

  function showLoading() {
    window.clearTimeout(loaderTimer);
    loading.hidden = false;
    loader.hidden = true;
    errorBox.hidden = true;
    body.replaceChildren();
  }

  function showError() {
    loading.hidden = true;
    loader.hidden = true;
    errorBox.hidden = false;
    errorText.textContent = "Couldn't load this host";
    body.replaceChildren();
  }

  // --- body ------------------------------------------------------------------------

  function fillIssues(list, issues, none) {
    const proto = list.querySelector(".hp-issue");
    proto.remove();
    if (!issues.length) {
      list.replaceWith(none);
      return;
    }
    none.remove();
    issues.forEach((issue) => {
      const li = proto.cloneNode(true);
      const severity = text(issue && issue.severity);
      if (severity !== "red") {
        li.querySelector(".issue-icon").remove();
      }
      li.querySelector(".hp-issue__severity").textContent = SEVERITY_WORDS[severity] || "";
      li.querySelector(".hp-issue__text").textContent = text(issue && issue.text);
      list.appendChild(li);
    });
  }

  function setReadout(well, value, note) {
    well.querySelector(".hp-readout__value").textContent = value;
    well.querySelector(".hp-readout__note").textContent = note;
  }

  function fillReadouts(row, data, disks) {
    const updates = row.querySelector(".hp-readout--updates");
    const containers = row.querySelector(".hp-readout--containers");
    const disk = row.querySelector(".hp-readout--disk");

    const u = data.updates && typeof data.updates === "object" ? data.updates : null;
    if (u) {
      const notes = [];
      if (count(u.security)) {
        notes.push(count(u.security) + " security");
      }
      if (u.reboot_required === true) {
        notes.push("reboot required");
      }
      setReadout(updates, String(count(u.total)), notes.join(" · "));
    } else {
      updates.remove();
    }

    const c = data.containers && typeof data.containers === "object" ? data.containers : null;
    if (c) {
      const failing = Array.isArray(c.problems) ? c.problems.length : 0;
      const running = count(c.running) + " running";
      setReadout(containers, failing ? failing + " failing" : running, failing ? running : "");
    } else {
      containers.remove();
    }

    let fullest = null;
    disks.forEach((d) => {
      const p = percent(d.percent);
      if (p !== null && (fullest === null || p > fullest.p)) {
        fullest = { p, mount: text(d.mount) };
      }
    });
    if (fullest) {
      setReadout(disk, fullest.p + "%", "on " + fullest.mount);
    } else {
      disk.remove();
    }

    if (!row.children.length) {
      row.remove();
    }
  }

  function fillList(section, items, fill) {
    const list = section.querySelector("ul");
    const proto = list.firstElementChild;
    proto.remove();
    items.forEach((item) => {
      const li = proto.cloneNode(true);
      fill(li, item);
      list.appendChild(li);
    });
  }

  function fillDisk(li, d) {
    const p = percent(d.percent);
    const mount = text(d.mount);
    li.querySelector(".disk-row__mount").textContent = mount;
    const meter = li.querySelector("meter");
    meter.setAttribute("value", String(p === null ? 0 : p));
    meter.setAttribute("aria-label", mount);
    meter.textContent = (p === null ? 0 : p) + "%";
    meter.classList.toggle("meter--red", p !== null && p >= 95);
    meter.classList.toggle("meter--amber", p !== null && p >= 85 && p < 95);
    li.querySelector(".disk-row__fixed").textContent = p === null ? "n/a" : p + "%";
  }

  function buildBody(data, status) {
    const root = template.content.cloneNode(true).firstElementChild;
    const part = (selector) => root.querySelector(selector);
    const lastSeen = text(data.last_seen_human);

    if (status === "waiting") {
      root.replaceChildren(part(".hp-waiting"));
      return root;
    }
    part(".hp-waiting").remove();

    if (status === "offline") {
      part(".hp-offline__text").textContent =
        "This host stopped reporting. Last report " + lastSeen + ".";
      // The id comes from the host id (digits from the unit's href), never from host text.
      const wellId = "host-preview-check-" + hostId;
      part(".hp-offline pre").setAttribute("id", wellId);
      part(".hp-offline .js-copy").setAttribute("aria-controls", wellId);
      part(".hp-last").remove();
    } else {
      part(".hp-offline").remove();
      part(".hp-last__ago").textContent = lastSeen;
    }

    const issues = Array.isArray(data.issues) ? data.issues : [];
    const none = document.createElement("p");
    none.className = "muted";
    none.textContent = "Nothing needs you.";
    if (status === "offline" && !issues.length) {
      part(".hp-issues").remove(); // the "stopped reporting" row says it
    } else {
      fillIssues(part(".hp-issues"), issues, none);
    }

    const disks = Array.isArray(data.disks) ? data.disks.filter((d) => d && typeof d === "object") : [];
    fillReadouts(part(".hp-readouts"), data, disks);

    const c = data.containers && typeof data.containers === "object" ? data.containers : null;
    const problems = c && Array.isArray(c.problems) ? c.problems.filter((p) => p && typeof p === "object") : [];
    if (problems.length) {
      fillList(part(".hp-containers"), problems, (li, p) => {
        li.querySelector(".hp-container__name").textContent = text(p.name);
        li.querySelector(".hp-container__state").textContent = text(p.state);
      });
    } else {
      part(".hp-containers").remove();
    }

    if (!Array.isArray(data.failed_units)) {
      part(".hp-units").remove();
    } else if (data.failed_units.length) {
      part(".hp-units__none").remove();
      fillList(part(".hp-units"), data.failed_units, (li, name) => {
        li.textContent = text(name);
      });
    } else {
      part(".hp-units ul").remove();
    }

    if (disks.length) {
      fillList(part(".hp-disks"), disks, fillDisk);
    } else {
      part(".hp-disks").remove();
    }
    return root;
  }

  function render(data) {
    const status = text(data.status);
    setHeader(status, text(data.status_label), text(data.name), text(data.last_seen_human));
    loading.hidden = true;
    loader.hidden = true;
    errorBox.hidden = true;
    body.replaceChildren(buildBody(data, status));
  }

  // Swap the content, then let the panel's height follow smoothly instead of jumping.
  async function resize(change) {
    const before = dialog.getBoundingClientRect().height;
    change();
    const after = dialog.getBoundingClientRect().height;
    if (!moves() || Math.abs(after - before) < 1) {
      return;
    }
    busy = true;
    dialog.classList.add("is-resizing");
    await finished(
      dialog.animate([{ height: before + "px" }, { height: after + "px" }], {
        duration: MORPH_MS,
        easing: "ease-out",
      })
    );
    dialog.classList.remove("is-resizing");
    busy = closing;
  }

  // --- data ------------------------------------------------------------------------

  function abortLoad() {
    loadSeq += 1;
    window.clearTimeout(loaderTimer);
    if (request) {
      request.abort();
      request = null;
    }
  }

  async function load() {
    abortLoad();
    const seq = loadSeq;
    const controller = new AbortController();
    request = controller;
    showLoading();
    loaderTimer = window.setTimeout(() => {
      if (seq === loadSeq) {
        loader.hidden = false;
      }
    }, LOADER_DELAY_MS);

    let data = null;
    try {
      const response = await fetch("/api/v1/hosts/" + hostId + "/summary.json", {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      // A logged-out session gets the login page (HTML): json() throws, that's an error too.
      const json = response.ok ? await response.json() : null;
      if (json && typeof json === "object" && json.id === Number(hostId)) {
        data = json;
      }
    } catch (e) {
      data = null;
    }
    // Render after the open animation, so the panel doesn't change size mid-flight.
    await opened;
    if (seq !== loadSeq || !dialog.open || closing) {
      return; // closed, retried or replaced meanwhile
    }
    window.clearTimeout(loaderTimer);
    request = null;
    await resize(() => (data ? render(data) : showError()));
  }

  // --- open ------------------------------------------------------------------------

  function playOpen(first) {
    const last = dialog.getBoundingClientRect();
    fadeBackdrop(true);
    if (isPhone()) {
      return dialog.animate([{ transform: "translateY(100%)" }, { transform: "translateY(0)" }], {
        duration: MORPH_MS,
        easing: "ease-out",
      });
    }
    // The content would look squashed while the panel is still unit-shaped: fade it in late.
    inner.animate([{ opacity: 0 }, { opacity: 0, offset: 0.4 }, { opacity: 1 }], {
      duration: MORPH_MS,
      easing: "ease-out",
    });
    return dialog.animate(
      [
        { transformOrigin: "top left", transform: invert(first, last) },
        { transformOrigin: "top left", transform: "none" },
      ],
      { duration: MORPH_MS, easing: "ease-out" }
    );
  }

  async function open(link, href, id) {
    busy = true;
    unit = link;
    hostUrl = href;
    hostId = id;
    document.dispatchEvent(new CustomEvent("cronrack:preview-open"));
    const animate = moves();
    if (animate) {
      slide = link.animate(
        [{ transform: "translateX(0)" }, { transform: "translateX(" + SLIDE_PX + "px)" }],
        { duration: SLIDE_MS, easing: "ease-out", fill: "forwards" }
      );
      await finished(slide);
    }
    headerFromUnit(link);
    openLink.setAttribute("href", href);
    showLoading();
    const first = link.getBoundingClientRect(); // First: where the unit is now
    dialog.showModal(); // Last: where the dialog's CSS puts it
    nameEl.focus({ preventScroll: true });
    opened = animate ? finished(playOpen(first)) : Promise.resolve();
    load();
    await opened;
    busy = closing;
  }

  rack.addEventListener("click", (event) => {
    if (event.defaultPrevented || !plainClick(event)) {
      return; // ctrl/cmd/shift-click etc.: the browser opens the link as usual
    }
    const link = event.target instanceof Element ? event.target.closest("a.rack-unit") : null;
    if (!link || link.classList.contains("rack-unit--example")) {
      return; // the Welcome example keeps its own page
    }
    const href = link.getAttribute("href") || "";
    const match = HOST_PATH.exec(href);
    if (!match) {
      return;
    }
    event.preventDefault();
    if (busy || closing || dialog.open) {
      return; // one preview at a time, and never while something is moving
    }
    open(link, href, match[1]);
  });

  // --- close -----------------------------------------------------------------------

  function playClose() {
    fadeBackdrop(false);
    const from = dialog.getBoundingClientRect();
    const target = currentUnit();
    const opts = { duration: MORPH_MS, easing: "ease-in", fill: "forwards" };
    if (isPhone()) {
      return dialog.animate([{ transform: "translateY(0)" }, { transform: "translateY(100%)" }], opts);
    }
    inner.animate([{ opacity: 1 }, { opacity: 0 }], { duration: MORPH_MS * 0.6, fill: "forwards" });
    if (!target) {
      return dialog.animate([{ opacity: 1 }, { opacity: 0 }], opts);
    }
    return dialog.animate(
      [
        { transformOrigin: "top left", transform: "none" },
        { transformOrigin: "top left", transform: invert(target.getBoundingClientRect(), from) },
      ],
      opts
    );
  }

  async function close() {
    if (busy || closing || !dialog.open) {
      return;
    }
    closing = true;
    busy = true;
    abortLoad();
    if (moves()) {
      await finished(playClose());
    }
    dialog.close(); // fires "close", which tidies up (below)
  }

  // Runs after every close: the animated one above, or an instant one (e.g. the browser
  // closed the dialog itself on a second Esc, or the page came back from the back button).
  async function tidyUp() {
    busy = true;
    abortLoad();
    dialog.getAnimations({ subtree: true }).forEach((a) => a.cancel());
    const target = currentUnit();
    if (target) {
      target.focus({ preventScroll: true });
    }
    if (slide) {
      if (moves()) {
        slide.reverse(); // back into the rack
        await finished(slide);
      }
      slide.cancel();
      slide = null;
    }
    body.replaceChildren();
    unit = null;
    hostUrl = null;
    hostId = null;
    closing = false;
    busy = false;
    document.dispatchEvent(new CustomEvent("cronrack:preview-closed"));
  }

  dialog.addEventListener("close", tidyUp);

  // Esc: run the close animation first. If the browser doesn't let us wait (cancelable is
  // false, e.g. Esc pressed twice quickly), it closes at once and tidyUp() still runs.
  dialog.addEventListener("cancel", (event) => {
    if (event.cancelable) {
      event.preventDefault();
      close();
    }
  });

  closeKey.addEventListener("click", close);

  // A click on the dimmed page around the panel. Clicks inside the panel have a child as
  // their target; the backdrop belongs to the dialog itself, so check the position too.
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) {
      return;
    }
    const r = dialog.getBoundingClientRect();
    const inside =
      event.clientX >= r.left &&
      event.clientX <= r.right &&
      event.clientY >= r.top &&
      event.clientY <= r.bottom;
    if (!inside) {
      close();
    }
  });

  retry.addEventListener("click", () => {
    if (!busy && !closing) {
      nameEl.focus({ preventScroll: true }); // the Retry key is about to disappear
      load();
    }
  });

  // --- open host page --------------------------------------------------------------

  openLink.addEventListener("click", (event) => {
    if (!plainClick(event)) {
      return; // new tab / window: the link's href is the unit's own /hosts/<id>/
    }
    event.preventDefault();
    const url = hostUrl;
    if (busy || closing || !url || !HOST_PATH.test(url)) {
      return;
    }
    navigate(url);
  });

  async function navigate(url) {
    busy = true;
    abortLoad();
    if (moves()) {
      const from = dialog.getBoundingClientRect();
      const viewport = { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
      inner.animate([{ opacity: 1 }, { opacity: 0 }], { duration: MORPH_MS * 0.6, fill: "forwards" });
      await finished(
        dialog.animate(
          [
            { transformOrigin: "top left", transform: "none" },
            { transformOrigin: "top left", transform: invert(viewport, from), borderRadius: "0px" },
          ],
          { duration: MORPH_MS, easing: "ease-in-out", fill: "forwards" }
        )
      );
    }
    window.location.assign(url); // a fixed same-site path from the unit's own link
  }

  // Back button from the host page: the browser may restore this page from its cache with
  // the panel still covering everything. Close it at once.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && dialog.open) {
      dialog.getAnimations({ subtree: true }).forEach((a) => a.cancel());
      busy = false;
      closing = true;
      dialog.close();
    }
  });
})();
