// PLACEHOLDER — Apo owns the final version.
// Appearance: the top-bar light/dark key (Legacy only; Glass hides it) and Settings >
// Appearance. Every control shows the same state and stays in sync, also across tabs.
//   Theme      Glass | Legacy, saved as "cronrack-skin" (Glass is the default).
//   Legacy     Light / Dark saved as "cronrack-theme"; System removes the key and the page
//              follows prefers-color-scheme, also when the device switches while it's open.
//              Shown only while Legacy is chosen. Kept as it is while Glass is on.
//   Moving background (Glass)  "cronrack-motion": "off" stops the glows drifting.
//   Show status labels (Glass) "cronrack-labels": "on" shows the word next to each dot.
// theme-init.js already set the <html> attributes before first paint; this file only reacts.
// Storage can be blocked (private mode): every access is in try/catch, and a choice then
// still works for this page. A "cronrack:appearance" event tells glass.js about changes.
// Loaded with defer.
(function () {
  "use strict";
  const root = document.documentElement;
  const KEYS = {
    skin: "cronrack-skin",
    theme: "cronrack-theme",
    motion: "cronrack-motion",
    labels: "cronrack-labels",
  };
  const override = document.querySelector('meta[name="cronrack-skin-override"]');
  const buttons = document.querySelectorAll("[data-theme-toggle]");
  const skinPicker = document.getElementById("skin-picker");
  const themePicker = document.getElementById("theme-picker");
  const glassPrefs = document.getElementById("glass-prefs");
  const skinRadios = skinPicker ? skinPicker.querySelectorAll("input[type=radio][name=skin]") : [];
  const themeRadios = themePicker ? themePicker.querySelectorAll("input[type=radio][name=theme]") : [];
  const motionSwitch = document.getElementById("pref-motion");
  const labelsSwitch = document.getElementById("pref-labels");
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const unsaved = {}; // the choices made while storage is blocked, for this page only

  function read(name) {
    let value = Object.prototype.hasOwnProperty.call(unsaved, name) ? unsaved[name] : null;
    try {
      value = window.localStorage.getItem(KEYS[name]);
    } catch (e) {
      // blocked: keep the in-page choice
    }
    return value;
  }

  function save(name, value) {
    unsaved[name] = value;
    try {
      if (value) {
        window.localStorage.setItem(KEYS[name], value);
      } else {
        window.localStorage.removeItem(KEYS[name]);
      }
    } catch (e) {
      // blocked: the choice works for this page only
    }
  }

  function savedSkin() {
    const value = override ? override.getAttribute("content") : read("skin");
    return value === "legacy" ? "legacy" : "glass";
  }

  function savedTheme() {
    const value = read("theme");
    return value === "light" || value === "dark" ? value : null;
  }

  function systemTheme() {
    return media && media.matches ? "dark" : "light";
  }

  // Apply the saved choices, then show them on every control.
  function apply() {
    const skin = savedSkin();
    const choice = savedTheme();
    const theme = skin === "glass" ? "dark" : choice || systemTheme();
    const motion = read("motion") === "off" ? "off" : "on";
    const labels = read("labels") === "on" ? "on" : "off";
    const changed = root.getAttribute("data-skin") !== skin
      || root.getAttribute("data-motion") !== motion
      || root.getAttribute("data-labels") !== labels;
    root.setAttribute("data-skin", skin);
    root.setAttribute("data-theme", theme);
    root.setAttribute("data-motion", motion);
    root.setAttribute("data-labels", labels);

    buttons.forEach((button) => {
      button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    });
    skinRadios.forEach((radio) => {
      radio.checked = radio.value === skin;
    });
    themeRadios.forEach((radio) => {
      radio.checked = radio.value === (choice || "system");
    });
    if (themePicker && skinPicker) {
      themePicker.hidden = skin !== "legacy";
    }
    if (glassPrefs) {
      glassPrefs.hidden = skin !== "glass";
    }
    if (motionSwitch) {
      motionSwitch.checked = motion === "on";
    }
    if (labelsSwitch) {
      labelsSwitch.checked = labels === "on";
    }
    if (changed) {
      document.dispatchEvent(new CustomEvent("cronrack:appearance"));
    }
  }

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      save("theme", root.getAttribute("data-theme") === "dark" ? "light" : "dark");
      apply();
    });
  });

  skinRadios.forEach((radio) => {
    radio.addEventListener("change", () => {
      if (radio.checked) {
        save("skin", radio.value === "legacy" ? "legacy" : "glass");
        apply();
      }
    });
  });

  themeRadios.forEach((radio) => {
    radio.addEventListener("change", () => {
      if (radio.checked) {
        save("theme", radio.value === "system" ? null : radio.value);
        apply();
      }
    });
  });

  if (motionSwitch) {
    motionSwitch.addEventListener("change", () => {
      save("motion", motionSwitch.checked ? null : "off");
      apply();
    });
  }
  if (labelsSwitch) {
    labelsSwitch.addEventListener("change", () => {
      save("labels", labelsSwitch.checked ? "on" : null);
      apply();
    });
  }

  if (media) {
    media.addEventListener("change", apply); // only changes the page while nothing is saved
  }
  // Another tab changed a choice.
  window.addEventListener("storage", (event) => {
    if (event.key === null || Object.values(KEYS).includes(event.key)) {
      apply();
    }
  });

  // Settings: the pickers need JS, so they stay hidden until here (apply() then decides
  // which of the Legacy picker and the Glass switches to show).
  if (skinPicker) {
    skinPicker.hidden = false;
  } else if (themePicker) {
    themePicker.hidden = false;
  }
  const note = document.getElementById("theme-nojs");
  if (note && (skinPicker || themePicker)) {
    note.hidden = true;
  }
  apply();
})();
