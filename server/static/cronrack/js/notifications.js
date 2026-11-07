// Settings → Notifications (templates/prefs/notifications.html). Loaded with defer on that
// page only. Without it the page is a normal form with a Save key; this adds:
//   - autosave: every change posts the whole form with fetch (same URL, same fields), then a
//     "Saved" toast; the server answers which rules now differ from the preset;
//   - presets: switch at once (fetch), set every rule back to that preset's defaults;
//   - the expand arrow per rule (Counts as / Limit / Applies to), with the grid-rows
//     animation from notifications.css; closed panels are inert, so Tab skips them;
//   - the search box, which opens every group that has a match;
//   - dimming the controls next to a switch that is off, and the "Play" key for sounds.
// Rules from the house style: never build markup from strings (the DOM is the server's),
// read attributes with getAttribute, no inline styles, no logging.
(function () {
  "use strict";
  const form = document.getElementById("notify-form");
  if (!form) {
    return;
  }
  const presetForm = document.getElementById("notify-preset-form");
  const search = document.getElementById("notify-search");
  const SAVE_DELAY_MS = 400;
  const SAVE_ERROR = "Couldn't save. Check your connection and try again.";
  const PRESET_ERROR = "Couldn't switch the preset. Try again.";
  const PRESET_NAMES = { quiet: "Quiet", balanced: "Balanced", everything: "Everything" };

  form.classList.add("notify--js");
  const saveKeys = document.getElementById("notify-save");
  if (saveKeys) {
    saveKeys.hidden = true;
  }
  const autosaveNote = document.getElementById("notify-autosave");
  if (autosaveNote) {
    autosaveNote.hidden = false; // the Save key is gone: say why
  }

  function toast(message, isError) {
    document.dispatchEvent(
      new CustomEvent("cronrack:toast", { detail: { message, error: Boolean(isError) } })
    );
  }

  function csrf(fromForm) {
    const field = fromForm.querySelector("input[name=csrfmiddlewaretoken]");
    return field ? field.value : "";
  }

  async function post(target, body) {
    const response = await fetch(target.action, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-CSRFToken": csrf(target) },
      body,
    });
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    return { ok: response.ok && data !== null && data.ok === true, data };
  }

  const rules = Array.from(form.querySelectorAll(".notify-rule"));

  function markChanged(changedIds) {
    const changed = new Set(changedIds);
    rules.forEach((rule) => {
      const label = rule.querySelector(".notify-rule__changed");
      if (label) {
        label.hidden = !changed.has(rule.getAttribute("data-rule"));
      }
    });
  }

  // --- autosave -----------------------------------------------------------------------------

  let saveTimer = null;
  let saving = Promise.resolve();

  function save() {
    saving = saving.then(async () => {
      try {
        const result = await post(form, new FormData(form));
        if (result.ok) {
          markChanged(Array.isArray(result.data.changed) ? result.data.changed : []);
          toast("Saved", false);
        } else {
          const error = result.data && typeof result.data.error === "string" ? result.data.error : "";
          toast(error || SAVE_ERROR, true);
        }
      } catch (error) {
        toast(SAVE_ERROR, true);
      }
    });
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault(); // Enter in a field: save now instead of reloading the page
    window.clearTimeout(saveTimer);
    save();
  });

  function scheduleSave() {
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(save, SAVE_DELAY_MS);
  }

  form.addEventListener("change", (event) => {
    // Time fields fire "change" per typed part (hours, then minutes): save when the field
    // is left instead, so a half-typed time is never stored.
    if (event.target === search || event.target.type === "time") {
      return;
    }
    scheduleSave();
  });

  form.querySelectorAll('input[type="time"]').forEach((input) => {
    let saved = input.value;
    input.addEventListener("focusout", () => {
      if (input.value !== saved && input.checkValidity()) {
        saved = input.value;
        scheduleSave();
      }
    });
  });

  // --- switches dim their controls ----------------------------------------------------------

  function syncSwitch(input) {
    const group = form.querySelector('[data-switch-for="' + input.id + '"]');
    if (group) {
      group.classList.toggle("is-dim", !input.checked);
    }
  }
  form.querySelectorAll('input[role="switch"]').forEach((input) => {
    syncSwitch(input);
    input.addEventListener("change", () => syncSwitch(input));
  });

  // --- expand arrows and "Applies to" ------------------------------------------------------

  function setOpen(rule, open) {
    const button = rule.querySelector(".notify-rule__more");
    const panel = rule.querySelector(".notify-more");
    rule.classList.toggle("is-open", open);
    if (button) {
      button.setAttribute("aria-expanded", open ? "true" : "false");
    }
    if (panel) {
      panel.inert = !open;
    }
  }

  function syncTargets(rule) {
    const applies = rule.querySelector(".notify-applies");
    const targets = rule.querySelector(".notify-targets");
    if (applies && targets) {
      targets.hidden = applies.value !== "selected";
    }
  }

  rules.forEach((rule) => {
    const button = rule.querySelector(".notify-rule__more");
    if (button) {
      button.hidden = false;
      button.addEventListener("click", () => setOpen(rule, !rule.classList.contains("is-open")));
    }
    setOpen(rule, false);
    syncTargets(rule);
    const applies = rule.querySelector(".notify-applies");
    if (applies) {
      applies.addEventListener("change", () => syncTargets(rule));
    }
  });

  // Panels start closed without animating; from the next frame on, opening is animated.
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => form.classList.add("notify--ready"));
  });

  // --- presets ------------------------------------------------------------------------------

  function applyDefaults(preset) {
    rules.forEach((rule) => {
      const id = rule.getAttribute("data-rule");
      const level = rule.getAttribute("data-" + preset);
      rule.querySelectorAll('input[type="radio"]').forEach((radio) => {
        radio.checked = radio.value === level;
      });
      const counts = rule.querySelector('select[name="counts:' + id + '"]');
      if (counts) {
        counts.value = rule.getAttribute("data-counts");
      }
      const limit = rule.querySelector('select[name="limit:' + id + '"]');
      if (limit && rule.getAttribute("data-limit")) {
        limit.value = rule.getAttribute("data-limit");
      }
      const applies = rule.querySelector(".notify-applies");
      if (applies) {
        applies.value = "all";
      }
      rule.querySelectorAll('.notify-targets input[type="checkbox"]').forEach((box) => {
        box.checked = false;
      });
      syncTargets(rule);
    });
    markChanged([]);
  }

  const presetKeys = Array.from(document.querySelectorAll(".notify-preset"));
  presetKeys.forEach((key) => {
    key.addEventListener("click", async (event) => {
      if (!presetForm) {
        return;
      }
      event.preventDefault();
      const preset = key.value;
      const body = new FormData(presetForm);
      body.set("preset", preset);
      window.clearTimeout(saveTimer); // a pending autosave would bring the old rules back
      try {
        await saving;
        const result = await post(presetForm, body);
        if (!result.ok) {
          toast(PRESET_ERROR, true);
          return;
        }
        presetKeys.forEach((other) => {
          other.setAttribute("aria-pressed", other === key ? "true" : "false");
        });
        applyDefaults(preset);
        toast((PRESET_NAMES[preset] || "Preset") + " applied", false);
      } catch (error) {
        toast(PRESET_ERROR, true);
      }
    });
  });

  // --- search -------------------------------------------------------------------------------

  const tools = document.getElementById("notify-tools");
  const result = document.getElementById("notify-search-result");
  const groups = Array.from(form.querySelectorAll(".notify-group"));
  const openAtStart = new Map(groups.map((group) => [group, group.open]));
  if (tools) {
    tools.hidden = false;
  }

  function filter() {
    const query = search.value.trim().toLowerCase();
    let total = 0;
    groups.forEach((group) => {
      let shown = 0;
      group.querySelectorAll(".notify-rule").forEach((rule) => {
        const match = !query || (rule.getAttribute("data-search") || "").includes(query);
        rule.hidden = !match;
        if (match) {
          shown += 1;
        }
      });
      total += shown;
      group.hidden = Boolean(query) && shown === 0;
      group.open = query ? shown > 0 : openAtStart.get(group);
    });
    if (result) {
      if (!query) {
        result.textContent = "";
      } else if (total === 0) {
        result.textContent = "No rule matches.";
      } else {
        result.textContent = total + (total === 1 ? " rule" : " rules") + " found.";
      }
    }
  }

  if (search) {
    let filterTimer = null;
    search.addEventListener("input", () => {
      window.clearTimeout(filterTimer);
      filterTimer = window.setTimeout(filter, 250); // and one announcement, not one per key
    });
  }

  // --- sound preview ------------------------------------------------------------------------

  const play = document.getElementById("notify-play");
  const volume = document.getElementById("notify-volume");
  if (play && volume) {
    play.hidden = false;
    play.addEventListener("click", () => {
      document.dispatchEvent(
        new CustomEvent("cronrack:sound-test", { detail: { volume: Number(volume.value) } })
      );
    });
  }
})();
