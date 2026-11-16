// PLACEHOLDER — Apo owns the final version.
// Install page (templates/pwa/install.html): shows the one block that fits this browser.
// It only toggles the `hidden` attribute on blocks that are already in the page: no HTML is
// built from text, nothing is stored or logged. Without JS the page keeps its first block
// (use the browser's menu), which is always correct.
(() => {
  "use strict";
  const fallback = document.getElementById("install-fallback");
  const android = document.getElementById("install-android");
  const ios = document.getElementById("install-ios");
  const done = document.getElementById("install-done");
  const button = document.getElementById("install-button");
  const status = document.getElementById("install-status");
  if (!fallback || !android || !ios || !done || !button) return;

  // Show one block, hide the rest.
  function show(block) {
    for (const item of [fallback, android, ios, done]) item.hidden = item !== block;
  }

  function say(text) {
    if (status) status.textContent = text;
  }

  // After a swap the user caused, put focus on the new block's first link or button, so
  // keyboard and switch users don't lose their place.
  function focusIn(block) {
    const target = block.querySelector("a, button:not([hidden])");
    if (target) target.focus();
  }

  // 1. Already running as the installed app: nothing to install.
  const standalone =
    (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
    navigator.standalone === true;
  if (standalone) {
    show(done);
    return;
  }

  // 2. Browsers only install over HTTPS (or localhost): keep the plain instructions.
  if (!window.isSecureContext) return;

  // 3. iPhone and iPad have no install button: Share, then Add to Home Screen. Newer iPads
  // call themselves a Mac, so a "Mac" with a touch screen counts as an iPad. Chrome, Firefox
  // and Edge on iPhone have different menus, so they keep the generic instructions.
  const ua = navigator.userAgent;
  const isIos =
    /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  if (isIos && !/CriOS|FxiOS|EdgiOS/.test(ua)) {
    show(ios);
    return;
  }

  // 4. Chrome on Android offers its own prompt: keep it, and show our button.
  let offered = null;
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    offered = event;
    show(android);
    button.hidden = false;
  });

  button.addEventListener("click", async () => {
    if (!offered) return;
    const offer = offered;
    // The offer works once: drop it whatever the answer.
    offered = null;
    try {
      await offer.prompt();
      const choice = await offer.userChoice;
      // "accepted": appinstalled (below) shows the done block.
      if (choice.outcome === "accepted") return;
    } catch (e) {
      // The browser refused to show its dialog: same as "not now".
    }
    // Dismissed: no button any more, so bring back the plain instructions.
    show(fallback);
    focusIn(fallback);
  });

  // 5. Installed (from our button or the browser's menu).
  window.addEventListener("appinstalled", () => {
    offered = null;
    show(done);
    say("CronRack is installed.");
    if (android.contains(document.activeElement) || document.activeElement === document.body) {
      focusIn(done);
    }
  });
})();
