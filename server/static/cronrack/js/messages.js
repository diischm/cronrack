// PLACEHOLDER — Apo owns the final version.
// Toasts (handoff 5.0): one component for Django flash messages and fetch-action results.
//
// Flash messages are rendered by the server into #toasts (base.html). Other scripts show a
// toast by dispatching an event, so they never build toast markup themselves:
//
//   document.dispatchEvent(new CustomEvent("cronrack:toast",
//     { detail: { message: "Test alert sent.", error: false } }));
//
// New toasts are clones of <template id="toast-template"> (components/toast.html, the same
// markup as the server's), filled with textContent only. #toasts is the aria-live region,
// so each added toast is announced.
//
// Each toast closes with its × key, or by itself after 4s (errors: 8s). Hovering it or
// focusing inside it pauses that timer, which resumes with the time that was left (WCAG
// 2.2.1 timing adjustable). At most 3 are shown: a 4th removes the oldest. Loaded with defer.
(function () {
  "use strict";
  const SUCCESS_MS = 4000;
  const ERROR_MS = 8000;
  const MAX_VISIBLE = 3;

  const region = document.getElementById("toasts");
  const template = document.getElementById("toast-template");
  if (!region) {
    return;
  }

  function close(toast) {
    toast.remove();
  }

  function arm(toast) {
    let remaining = toast.classList.contains("toast--error") ? ERROR_MS : SUCCESS_MS;
    let startedAt = 0;
    let timer = null;
    let hovered = false;
    let focused = false;

    function run() {
      if (timer !== null || hovered || focused || !toast.isConnected) {
        return;
      }
      startedAt = Date.now();
      timer = window.setTimeout(() => close(toast), remaining);
    }

    function pause() {
      if (timer === null) {
        return;
      }
      window.clearTimeout(timer);
      timer = null;
      remaining = Math.max(0, remaining - (Date.now() - startedAt));
    }

    toast.addEventListener("mouseenter", () => {
      hovered = true;
      pause();
    });
    toast.addEventListener("mouseleave", () => {
      hovered = false;
      run();
    });
    toast.addEventListener("focusin", () => {
      focused = true;
      pause();
    });
    toast.addEventListener("focusout", (event) => {
      if (!toast.contains(event.relatedTarget)) {
        focused = false;
        run();
      }
    });
    const button = toast.querySelector(".toast__close");
    if (button) {
      button.addEventListener("click", () => {
        window.clearTimeout(timer);
        close(toast);
      });
    }
    run();
  }

  // Oldest first in the DOM, so the extra ones at the front go.
  function limit() {
    const toasts = region.querySelectorAll(".toast");
    for (let i = 0; i < toasts.length - MAX_VISIBLE; i += 1) {
      toasts[i].remove();
    }
  }

  function show(message, isError) {
    if (!template || typeof message !== "string" || !message) {
      return;
    }
    const toast = template.content.firstElementChild.cloneNode(true);
    toast.classList.toggle("toast--error", Boolean(isError));
    toast.querySelector(".toast__text").textContent = message;
    region.appendChild(toast);
    limit();
    arm(toast);
  }

  region.querySelectorAll(".toast").forEach(arm);
  limit();

  document.addEventListener("cronrack:toast", (event) => {
    const detail = event.detail || {};
    show(detail.message, detail.error);
  });
})();
