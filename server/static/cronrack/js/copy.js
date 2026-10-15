// PLACEHOLDER — Apo owns the final version.
// Copy keys (handoff 5.5): <button class="js-copy" aria-controls="ID">Copy</button> copies the
// visible text of the code well with that id. Nothing is stored in the page for later: the text
// is read from the code well at click time. Loaded with defer. Never logs anything.
// Works for keys that exist at load time and for ones added later.
(function () {
  "use strict";
  const FEEDBACK_MS = 2000;
  const HINT_MS = 6000;
  // One shared, visually hidden aria-live region (base.html) announces the result.
  const status = document.getElementById("copy-status");

  function announce(message) {
    if (!status) {
      return;
    }
    // Clear first, so the same message twice in a row is announced again.
    status.textContent = "";
    window.setTimeout(() => {
      status.textContent = message;
    }, 50);
  }

  // Fallback when the clipboard API is missing or refused (e.g. plain HTTP on a LAN IP):
  // select the code well's text so the user can press Ctrl+C themselves.
  function selectText(target) {
    const selection = window.getSelection();
    if (!selection) {
      return;
    }
    const range = document.createRange();
    range.selectNodeContents(target);
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function showHint(button, message) {
    let hint = button.nextElementSibling;
    if (!hint || !hint.classList.contains("copy-hint")) {
      hint = document.createElement("span");
      hint.className = "copy-hint";
      button.after(hint);
    }
    hint.textContent = message;
    hint.hidden = false;
    window.clearTimeout(hint.hideTimer);
    hint.hideTimer = window.setTimeout(() => {
      hint.hidden = true;
    }, HINT_MS);
  }

  function showCopied(button) {
    const label = button.textContent;
    if (button.copyTimer) {
      window.clearTimeout(button.copyTimer);
    } else {
      button.originalLabel = label;
    }
    button.textContent = "Copied";
    announce("Copied");
    button.copyTimer = window.setTimeout(() => {
      button.textContent = button.originalLabel;
      button.copyTimer = null;
    }, FEEDBACK_MS);
  }

  async function copy(button) {
    const target = document.getElementById(button.getAttribute("aria-controls") || "");
    if (!target) {
      return;
    }
    const text = target.textContent.trim();
    try {
      if (!navigator.clipboard || !window.isSecureContext) {
        throw new Error("clipboard unavailable");
      }
      await navigator.clipboard.writeText(text);
      showCopied(button);
    } catch (e) {
      selectText(target);
      showHint(button, "Press Ctrl+C to copy");
      announce("Press Ctrl+C to copy");
    }
  }

  // One listener on the document, so Copy keys added later (the overview's host preview
  // clones its body from a <template>) work the same as the ones in the page source.
  document.addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest(".js-copy") : null;
    if (button) {
      copy(button);
    }
  });
})();
