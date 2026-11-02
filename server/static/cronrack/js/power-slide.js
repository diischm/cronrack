// Power Off Slide, dev styleguide only. Adapted from SmoothUI (https://github.com/educlopez/smoothui),
// MIT License, Copyright (c) 2024 Eduardo Calvo; rebuilt in plain JS. See THIRD_PARTY_NOTICES.md.
//
// A deliberate "slide to confirm" for dangerous actions. CronRack has no such action (it never
// reboots or changes a host), so this only lives in /dev/styleguide/ and does nothing: it
// sends no request and changes no data. Loaded with defer by dev/styleguide.html.
//
// Markup (dev/_styleguide_glass.html): .g-slide > .g-slide__fill, .g-slide__label,
// button.g-slide__knob; a .g-slide__status (role="status") and a .g-slide__reset key in the
// same block (the track's parent).
// Pointer: drag the knob to the end (pointer capture, so it follows outside the track).
// Keyboard: focus the knob, Enter or End confirms; Escape or Home resets.
// Position, fill and label fade are CSS custom properties, set through the CSSOM.
(function () {
  "use strict";
  const KNOB = 52;
  const PAD = 3;
  const CONFIRM_AT = 0.92; // of the way

  document.querySelectorAll(".g-slide").forEach((track) => {
    const knob = track.querySelector(".g-slide__knob");
    const status = track.parentElement.querySelector(".g-slide__status");
    const reset = track.parentElement.querySelector(".g-slide__reset");
    if (!knob) {
      return;
    }
    let x = 0;
    let startX = 0;
    let dragging = false;
    let done = false;

    function max() {
      return Math.max(0, track.clientWidth - KNOB - PAD * 2);
    }

    function paint() {
      const m = max();
      track.style.setProperty("--g-slide-x", x + "px");
      track.style.setProperty("--g-slide-fill", x + PAD + KNOB / 2 + "px");
      track.style.setProperty("--g-slide-label", done ? "1" : String(m ? 1 - x / m : 1));
    }

    function say(text) {
      if (status) {
        status.textContent = text;
      }
    }

    function confirm() {
      done = true;
      dragging = false;
      x = max();
      track.classList.remove("is-dragging");
      track.classList.add("is-done");
      say("Confirmed. Demo only: nothing was sent.");
      paint();
    }

    function back() {
      done = false;
      dragging = false;
      x = 0;
      track.classList.remove("is-dragging", "is-done");
      say("");
      paint();
    }

    knob.addEventListener("pointerdown", (event) => {
      if (done) {
        return;
      }
      knob.setPointerCapture(event.pointerId);
      dragging = true;
      startX = event.clientX - x;
      track.classList.add("is-dragging");
    });
    knob.addEventListener("pointermove", (event) => {
      if (!dragging) {
        return;
      }
      x = Math.max(0, Math.min(max(), event.clientX - startX));
      paint();
    });
    function release() {
      if (!dragging) {
        return;
      }
      if (x >= max() * CONFIRM_AT) {
        confirm();
      } else {
        back();
      }
    }
    knob.addEventListener("pointerup", release);
    knob.addEventListener("pointercancel", () => {
      if (dragging) {
        back(); // a cancelled drag starts over; a confirmed slide stays confirmed
      }
    });
    knob.addEventListener("keydown", (event) => {
      if (!done && (event.key === "Enter" || event.key === "End")) {
        event.preventDefault();
        confirm();
      } else if (event.key === "Escape" || event.key === "Home") {
        event.preventDefault();
        back();
      }
    });
    if (reset) {
      reset.addEventListener("click", back);
    }
    paint();
  });
})();
