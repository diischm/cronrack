// PLACEHOLDER — Apo owns the final version.
// Django messages as toasts: dismiss button, and auto-dismiss after 4 seconds.
(function () {
  "use strict";
  const AUTO_DISMISS_MS = 4000;

  document.querySelectorAll("[data-toast]").forEach((toast) => {
    const dismiss = () => toast.remove();
    const button = toast.querySelector("[data-toast-dismiss]");
    if (button) {
      button.addEventListener("click", dismiss);
    }
    window.setTimeout(dismiss, AUTO_DISMISS_MS);
  });
})();
