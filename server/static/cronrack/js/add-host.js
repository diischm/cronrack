// PLACEHOLDER — Apo owns the final version.
// Add host, step 3 "Connect" (handoff 5.5), on the page that shows the token once
// (hosts/add_done.html). Also loaded by hosts/token_rotated.html for the token guard below;
// everything after it needs the Connect section and does nothing without it.
//
// Token guard (both pages): if the page comes back from the back/forward cache
// (pageshow with event.persisted), the token is replaced with a note and its Copy key
// removed. Cache-Control: no-store should keep these pages out of that cache; this covers a
// browser that restores them anyway. The page is never reloaded instead: it is the answer
// to a POST, and a reload would send it again (a second host, or yet another token).
//
// - Polls /api/v1/hosts/<id>/status.json every 3s, only while the tab is visible; a tab
//   that becomes visible again checks at once and goes on polling.
// - First report (status no longer "waiting", or report_received_at set): the loader
//   becomes the host's lamp + "Connected", steps 2 and 3 are marked done, "Go to host"
//   appears and a toast says "<name> is connected." (messages.js, textContent only).
// - 10 minutes without a report: polling stops and the agent commands + "Keep waiting"
//   appear; "Keep waiting" polls for another 10 minutes.
// - Until the agent connects, leaving the page asks first (native beforeunload prompt),
//   because the token can't be shown again. The prompt is removed once connected.
//
// The token is never read, copied into a variable, stored or logged here. The host id
// comes from data-host-id (digits only, checked); the host name (untrusted) is read from
// the page's text and only ever written back with textContent. The status value picks
// from fixed tables, so it never becomes a class name or text by itself. Loaded with
// defer. Never logs anything.
(function () {
  "use strict";
  const POLL_MS = 3000;
  const WAIT_MS = 10 * 60 * 1000;
  const HOST_PATH = /^\/hosts\/\d+\/$/;

  // Lamp class, chip class and chip text per status (chip text = presenters.STATUS_LABELS).
  const LOOKS = {
    red: ["lamp--red", "chip--red", "Needs you"],
    yellow: ["lamp--amber", "chip--amber", "Heads up"],
    green: ["lamp--green", "chip--green", "All good"],
    offline: ["lamp--off", "chip--red", "Offline"],
    maintenance: ["lamp--off", "chip--neutral", "Maintenance"],
  };

  const TOKEN_HIDDEN = "Token hidden. If you still need it, rotate the token in Host settings.";

  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) {
      return;
    }
    const well = document.getElementById("agent-token");
    if (well) {
      well.textContent = TOKEN_HIDDEN;
    }
    // Removed, not aria-disabled: copy.js would still copy on click.
    document.querySelectorAll('.js-copy[aria-controls="agent-token"]').forEach((key) => key.remove());
  });

  const section = document.getElementById("connect");
  if (!section) {
    return;
  }
  const hostId = section.getAttribute("data-host-id") || "";
  if (!/^\d+$/.test(hostId)) {
    return;
  }
  const statusUrl = "/api/v1/hosts/" + hostId + "/status.json";
  const hostPath = "/hosts/" + hostId + "/";

  const loader = document.getElementById("connect-loader");
  const lampSlot = document.getElementById("connect-lamp");
  const line = document.getElementById("connect-line");
  const sub = document.getElementById("connect-sub");
  const nameEl = document.getElementById("connect-name");
  const stuck = document.getElementById("connect-stuck");
  const keepWaiting = document.getElementById("connect-keep-waiting");
  const go = document.getElementById("connect-go");
  const title = document.getElementById("connect-title");
  const hostName = nameEl ? nameEl.textContent : "";
  const waitingSub = sub ? sub.textContent : "";

  let deadline = Date.now() + WAIT_MS;
  let timer = null;
  let inFlight = false;
  let connected = false;
  let gaveUp = false;

  // --- leaving before the agent connected -------------------------------------------
  function confirmLeave(event) {
    event.preventDefault();
    event.returnValue = ""; // older browsers need it set to show the prompt
  }
  window.addEventListener("beforeunload", confirmLeave);

  // --- polling --------------------------------------------------------------------------
  function polling() {
    return !connected && !gaveUp && document.visibilityState === "visible";
  }

  function schedule(delay) {
    window.clearTimeout(timer);
    timer = null;
    if (polling()) {
      timer = window.setTimeout(poll, delay);
    }
  }

  function hasReported(data) {
    if (!data || typeof data !== "object") {
      return false;
    }
    if (typeof data.report_received_at === "string" && data.report_received_at) {
      return true;
    }
    return typeof data.status === "string" && data.status !== "waiting";
  }

  async function poll() {
    timer = null;
    if (!polling() || inFlight) {
      return;
    }
    if (Date.now() >= deadline) {
      giveUp();
      return;
    }
    inFlight = true;
    let data = null;
    try {
      const response = await fetch(statusUrl, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (response.ok) {
        data = await response.json();
      }
    } catch (e) {
      data = null; // offline for a moment, or not JSON (e.g. the login page): try again
    }
    inFlight = false;
    if (hasReported(data)) {
      showConnected(typeof data.status === "string" ? data.status : "");
    } else if (Date.now() >= deadline) {
      giveUp();
    } else {
      schedule(POLL_MS);
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      schedule(0);
    } else {
      window.clearTimeout(timer);
      timer = null;
    }
  });

  // --- states ----------------------------------------------------------------------------
  // The loader's slot shows a lamp instead: lit in the host's status colour, or unlit.
  function showLamp(lampClass) {
    if (loader) {
      loader.hidden = true;
    }
    if (lampSlot) {
      const lamp = document.createElement("span");
      lamp.className = "lamp " + lampClass;
      lamp.setAttribute("aria-hidden", "true");
      lampSlot.replaceChildren(lamp);
      lampSlot.hidden = false;
    }
  }

  function markDone(n) {
    const step = document.getElementById("step-" + n);
    if (!step) {
      return;
    }
    step.classList.remove("step--current");
    step.classList.add("step--done");
    step.removeAttribute("aria-current");
    const num = step.querySelector(".step__num");
    const check = step.querySelector(".step__check");
    const state = step.querySelector(".step__state");
    if (num) {
      num.hidden = true;
    }
    if (check) {
      check.removeAttribute("hidden");
    }
    if (state) {
      state.textContent = "Done";
    }
  }

  function showConnected(status) {
    connected = true;
    window.clearTimeout(timer);
    timer = null;
    window.removeEventListener("beforeunload", confirmLeave);

    const look = Object.prototype.hasOwnProperty.call(LOOKS, status) ? LOOKS[status] : null;
    showLamp(look ? look[0] : "lamp--off");
    if (line) {
      line.textContent = "Connected";
      if (look) {
        const chip = document.createElement("span");
        chip.className = "chip " + look[1];
        chip.textContent = look[2];
        line.append(" ", chip);
      }
    }
    if (sub) {
      sub.textContent = hostName + " sent its first report.";
    }
    if (stuck) {
      stuck.hidden = true;
    }
    markDone(2);
    markDone(3);
    if (go && HOST_PATH.test(hostPath)) {
      go.setAttribute("href", hostPath);
      go.hidden = false;
    }
    document.dispatchEvent(
      new CustomEvent("cronrack:toast", {
        detail: { message: hostName + " is connected.", error: false },
      })
    );
  }

  function giveUp() {
    gaveUp = true;
    window.clearTimeout(timer);
    timer = null;
    showLamp("lamp--off"); // unlit: not connected, and no longer checking
    if (sub) {
      sub.textContent = "Stopped checking after 10 minutes.";
    }
    if (stuck) {
      stuck.hidden = false;
    }
  }

  if (keepWaiting) {
    keepWaiting.addEventListener("click", () => {
      gaveUp = false;
      deadline = Date.now() + WAIT_MS;
      if (stuck) {
        stuck.hidden = true;
      }
      if (lampSlot) {
        lampSlot.hidden = true;
      }
      if (loader) {
        loader.hidden = false;
      }
      if (sub) {
        sub.textContent = waitingSub;
      }
      // The key just disappeared: put focus on the section heading, not on <body>.
      if (title) {
        title.focus();
      }
      schedule(0);
    });
  }

  schedule(POLL_MS);
})();
