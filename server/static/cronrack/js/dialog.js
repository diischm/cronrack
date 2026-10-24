// PLACEHOLDER — Apo owns the final version.
// Confirm dialog (handoff 5.6) for forms marked class="js-confirm". The markup and the rules
// are in templates/components/confirm_dialog.html.
//
// Without this file (or without <dialog> support) the forms post as they are: the user ticks
// the visible "Yes, ..." box or types the host name, and the server checks it. With it:
//   1. the form's own confirmation (.js-confirm-nojs) is hidden, unless the server just
//      marked it invalid (then its error stays visible);
//   2. submit opens the dialog, filled with textContent from visible elements of the form
//      (.js-confirm-title, .js-confirm-text, the submit key's label, .js-confirm-name);
//   3. Confirm ticks the form's confirm box / copies the typed name, then submits the form.
// The typed-name check here is only so the key can't be pressed by mistake: the server
// decides. Focus starts on Cancel (never the destructive key), Tab stays inside, Esc, Cancel
// and a backdrop click close it, and focus goes back to the key that opened it.
// Motion: element.animate() (no style writes), <= 200ms fade + slight scale; nothing moves
// with prefers-reduced-motion. Loaded with defer.
(function () {
  "use strict";
  const OPEN_MS = 160;
  const CLOSE_MS = 120;

  const dialog = document.getElementById("confirm-dialog");
  const forms = document.querySelectorAll("form.js-confirm");
  if (!dialog || typeof dialog.showModal !== "function" || !forms.length) {
    return; // the plain forms keep their own confirmation
  }
  const title = dialog.querySelector(".confirm-dialog__title");
  const sentence = dialog.querySelector(".confirm-dialog__text");
  const field = dialog.querySelector(".confirm-dialog__field");
  const match = dialog.querySelector(".confirm-dialog__match");
  const input = dialog.querySelector("#confirm-dialog-input");
  const cancelKey = dialog.querySelector(".confirm-dialog__cancel");
  const actionKey = dialog.querySelector(".confirm-dialog__action");

  let current = null; // { form, opener, name } while open
  let closing = false;
  let sending = false;

  function textOf(el) {
    return el ? el.textContent.replace(/\s+/g, " ").trim() : "";
  }

  function moves() {
    return (
      typeof dialog.animate === "function" &&
      !window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function finished(animation) {
    return animation ? animation.finished.then(() => {}, () => {}) : Promise.resolve();
  }

  function submitKey(form) {
    return Array.from(form.elements).find((el) => el.type === "submit") || null;
  }

  // The form's own confirmation: the "Yes, ..." box or the typed-name input.
  function confirmBox(form) {
    return form.querySelector(".js-confirm-nojs input[type=checkbox][name=confirm]");
  }

  function typedInput(form) {
    return form.querySelector(".js-confirm-nojs input[type=text]");
  }

  function setReady(ready) {
    actionKey.setAttribute("aria-disabled", ready ? "false" : "true");
  }

  function matches() {
    return !current || !current.name || input.value === current.name;
  }

  function focusables() {
    return [input, cancelKey, actionKey].filter(
      (el) => !el.disabled && !el.closest("[hidden]") && el.getClientRects().length > 0
    );
  }

  function open(form, opener) {
    if (dialog.open || sending) {
      return;
    }
    const name = typedInput(form) ? textOf(form.querySelector(".js-confirm-name")) : "";
    current = { form, opener, name };
    title.textContent = textOf(form.querySelector(".js-confirm-title"));
    sentence.textContent = textOf(form.querySelector(".js-confirm-text"));
    actionKey.textContent = textOf(submitKey(form));
    field.hidden = !name;
    match.textContent = name;
    input.value = "";
    setReady(matches());
    dialog.showModal();
    cancelKey.focus();
    if (moves()) {
      dialog.animate(
        [
          { opacity: 0, transform: "scale(0.96)" },
          { opacity: 1, transform: "none" },
        ],
        { duration: OPEN_MS, easing: "ease" }
      );
      try {
        dialog.animate([{ opacity: 0 }, { opacity: 1 }], {
          duration: OPEN_MS,
          easing: "ease",
          pseudoElement: "::backdrop",
        });
      } catch (e) {
        // ::backdrop can't be animated here: it simply appears
      }
    }
  }

  async function close() {
    if (!dialog.open || closing || sending) {
      return;
    }
    closing = true;
    if (moves()) {
      const out = dialog.animate(
        [
          { opacity: 1, transform: "none" },
          { opacity: 0, transform: "scale(0.96)" },
        ],
        { duration: CLOSE_MS, easing: "ease", fill: "forwards" }
      );
      try {
        dialog.animate([{ opacity: 1 }, { opacity: 0 }], {
          duration: CLOSE_MS,
          easing: "ease",
          pseudoElement: "::backdrop",
          fill: "forwards",
        });
      } catch (e) {
        // no ::backdrop animation: it disappears with the dialog
      }
      await finished(out);
    }
    dialog.close(); // fires "close" (below)
  }

  function confirm() {
    if (!current || sending || actionKey.getAttribute("aria-disabled") === "true" || !matches()) {
      return;
    }
    const form = current.form;
    const box = confirmBox(form);
    if (box) {
      box.checked = true;
    }
    const typed = typedInput(form);
    if (typed) {
      typed.value = input.value;
    }
    sending = true;
    setReady(false); // no second press while the page loads
    form.submit(); // no submit event, so this doesn't open the dialog again
  }

  // Every close (animated, a second Esc, bfcache): tidy up and give focus back.
  dialog.addEventListener("close", () => {
    dialog.getAnimations({ subtree: true }).forEach((a) => a.cancel());
    const opener = current && current.opener;
    current = null;
    closing = false;
    input.value = "";
    if (opener && opener.isConnected) {
      opener.focus({ preventScroll: true });
    }
  });

  // Esc: animate out first. If the browser won't wait (not cancelable), it closes at once.
  dialog.addEventListener("cancel", (event) => {
    if (sending) {
      event.preventDefault();
      return;
    }
    if (event.cancelable) {
      event.preventDefault();
      close();
    }
  });

  // A click on the dimmed backdrop: its target is the dialog itself, outside the panel box.
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

  // Tab and Shift+Tab cycle through the dialog's own controls only.
  dialog.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") {
      return;
    }
    const items = focusables();
    if (!items.length) {
      return;
    }
    const at = items.indexOf(document.activeElement);
    event.preventDefault();
    const next = event.shiftKey
      ? items[at <= 0 ? items.length - 1 : at - 1]
      : items[at === -1 || at === items.length - 1 ? 0 : at + 1];
    next.focus();
  });

  cancelKey.addEventListener("click", close);
  actionKey.addEventListener("click", confirm);

  input.addEventListener("input", () => setReady(matches()));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault(); // Enter confirms only when the name matches
      confirm();
    }
  });

  forms.forEach((form) => {
    form.querySelectorAll(".js-confirm-nojs").forEach((el) => {
      if (!el.querySelector('[aria-invalid="true"]')) {
        el.hidden = true;
      }
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      open(form, event.submitter || submitKey(form));
    });
  });

  // Back to this page from the back/forward cache: closed dialog, nothing pre-confirmed.
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) {
      return;
    }
    sending = false;
    forms.forEach((form) => {
      const box = confirmBox(form);
      if (box) {
        box.checked = false;
      }
    });
    if (dialog.open) {
      dialog.close();
    }
  });
})();
