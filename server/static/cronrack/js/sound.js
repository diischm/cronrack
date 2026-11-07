// In-app sound (Settings → Notifications → "Sounds in the app"). Loaded with defer on every
// logged-in page; does nothing unless <body> has data-sound-volume (only rendered when the
// user turned sounds on).
//
// A soft sine tone made with WebAudio (no audio files), played when the overview's live
// refresh (overview.js, "cronrack:overview-data") brings a "needs you" issue that the
// previous refresh didn't have: a new red issue on a host or cluster, or a host or cluster
// that went offline. The first refresh after the page opens only remembers what is there.
// Browsers only allow sound after the person interacted with the page, so the audio context
// is created on the first click or key press. The "Play" key on the settings page sends
// "cronrack:sound-test" with the slider's volume (that click unlocks it too).
(function () {
  "use strict";
  let context = null;
  let known = null; // keys of the "needs you" issues in the last refresh

  function unlock() {
    if (context) {
      return context;
    }
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) {
      return null;
    }
    try {
      context = new AudioCtx();
    } catch (error) {
      context = null;
    }
    return context;
  }

  ["pointerdown", "keydown"].forEach((type) => {
    document.addEventListener(type, unlock, { once: true, capture: true });
  });

  function tone(volume) {
    const ctx = unlock();
    if (!ctx || !(volume > 0)) {
      return;
    }
    if (ctx.state === "suspended") {
      ctx.resume();
    }
    const peak = Math.min(volume, 100) / 100 * 0.2; // soft: never louder than a fifth
    const now = ctx.currentTime;
    const oscillator = ctx.createOscillator();
    const gain = ctx.createGain();
    oscillator.type = "sine";
    oscillator.frequency.value = 660;
    oscillator.connect(gain);
    gain.connect(ctx.destination);
    gain.gain.setValueAtTime(0, now);
    gain.gain.linearRampToValueAtTime(peak, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.5);
    oscillator.start(now);
    oscillator.stop(now + 0.55);
  }

  function savedVolume() {
    const raw = document.body ? document.body.getAttribute("data-sound-volume") : null;
    if (raw === null) {
      return null;
    }
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  }

  function needsYou(data) {
    const keys = new Set();
    ["hosts", "clusters"].forEach((kind) => {
      const items = Array.isArray(data[kind]) ? data[kind] : [];
      items.forEach((item) => {
        if (item.status === "offline") {
          keys.add(kind + ":" + item.id + ":offline");
        }
        (Array.isArray(item.issues) ? item.issues : []).forEach((issue) => {
          if (issue.severity === "red" && typeof issue.fingerprint === "string") {
            keys.add(issue.fingerprint);
          }
        });
      });
    });
    return keys;
  }

  document.addEventListener("cronrack:overview-data", (event) => {
    const volume = savedVolume();
    const data = event.detail;
    if (volume === null || !data) {
      return;
    }
    const now = needsYou(data);
    const isNew = known !== null && Array.from(now).some((key) => !known.has(key));
    known = now;
    if (isNew) {
      tone(volume);
    }
  });

  document.addEventListener("cronrack:sound-test", (event) => {
    const volume = event.detail ? Number(event.detail.volume) : NaN;
    tone(Number.isFinite(volume) ? volume : 40);
  });
})();
