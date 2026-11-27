// Settings → General (templates/prefs/index.html). Loaded with defer on that page only.
// Without it the page is a normal form with Save keys. This saves every change as it happens,
// the same way Notifications does: the same POST (same URL, same fields) sent with fetch, then
// a "Saved" toast. The view answers a redirect when it saved, and 400 with the page when a
// field is wrong; then the form is submitted for real, so the server shows the error in place.
// "Send test alert" waits for a pending save, so it tests the URL that was just typed.
// The password form keeps its own key: a password is never saved on the fly.
// House rules: no markup from strings, no inline styles, no logging.
(function () {
  "use strict";
  const form = document.getElementById("prefs-form");
  if (!form || typeof window.fetch !== "function") {
    return;
  }
  const SAVE_DELAY_MS = 600;
  const SAVE_ERROR = "Couldn't save. Check your connection and try again.";

  form.querySelectorAll(".js-save-key, .js-save-row").forEach((el) => {
    el.hidden = true;
  });
  const note = document.getElementById("prefs-autosave");
  if (note) {
    note.hidden = false; // the Save keys are gone: say why
  }

  function toast(message, isError) {
    document.dispatchEvent(
      new CustomEvent("cronrack:toast", { detail: { message, error: Boolean(isError) } })
    );
  }

  function snapshot() {
    return new URLSearchParams(new FormData(form)).toString();
  }

  let lastSaved = snapshot();
  let timer = null;
  let saving = Promise.resolve();

  function save() {
    window.clearTimeout(timer);
    timer = null;
    saving = saving.then(async () => {
      const body = snapshot();
      if (body === lastSaved) {
        return;
      }
      const invalid = form.querySelector(":invalid");
      if (invalid) {
        toast("Not saved: " + invalid.validationMessage, true);
        return;
      }
      const token = form.querySelector("input[name=csrfmiddlewaretoken]");
      try {
        const response = await fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          headers: { "X-CSRFToken": token ? token.value : "" },
          body: new FormData(form),
        });
        if (response.ok && response.redirected) {
          lastSaved = body;
          toast("Saved", false);
        } else if (response.status === 400) {
          HTMLFormElement.prototype.submit.call(form); // the server shows what's wrong
        } else {
          toast(SAVE_ERROR, true);
        }
      } catch (error) {
        toast(SAVE_ERROR, true);
      }
    });
    return saving;
  }

  function scheduleSave() {
    window.clearTimeout(timer);
    timer = window.setTimeout(save, SAVE_DELAY_MS);
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault(); // Enter in a field: save now instead of reloading the page
    save();
  });
  form.addEventListener("change", scheduleSave);
  form.addEventListener("input", (event) => {
    if (event.target.type === "url") {
      scheduleSave(); // typing a URL: save shortly after the last key
    }
  });

  // "Send test alert" (actions.js listens on document): first finish a pending save.
  const testForm = document.getElementById("test-alert-form");
  let resent = false;
  if (testForm) {
    testForm.addEventListener("submit", async (event) => {
      if (resent) {
        resent = false;
        return;
      }
      if (timer === null && snapshot() === lastSaved) {
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      await save();
      if (snapshot() === lastSaved) {
        resent = true;
        testForm.requestSubmit();
      }
    });
  }
})();
