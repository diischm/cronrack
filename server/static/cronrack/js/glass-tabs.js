// Glass host-detail tabs. Tabs adapted from SmoothUI (https://github.com/educlopez/smoothui),
// MIT License, Copyright (c) 2024 Eduardo Calvo; rebuilt in plain JS. See THIRD_PARTY_NOTICES.md.
//
// Loaded with defer on the host page only (hosts/detail.html). Glass only: in Legacy (or
// without JS) the page stays one long column of panels, as it always was.
//
// Groups the page's existing sections into tabs (WAI-ARIA tabs pattern):
//   Overview    "Needs your attention" + System + Disks
//   Docker / Systemd / Kubernetes   that app's panel, when the host has it
// Fewer than two groups: no tabs. Each group's sections move into one role="tabpanel"; the
// tabs are buttons in a role="tablist" with a roving tabindex (Left / Right / Home / End,
// automatic activation). A sliding indicator follows the selected tab (--g-ind-x / --g-ind-w
// set through the CSSOM). The last tab chosen for a host is remembered for
// this browser session, so "Check now" (which reloads the page) keeps it. Labels are fixed
// words; no host data is read or written. If the skin switches to Legacy while the page is
// open (from another tab), it reloads once, so the Legacy layout comes back untouched.
(function () {
  "use strict";
  const root = document.documentElement;
  if (root.getAttribute("data-skin") !== "glass") {
    return;
  }
  const attention = document.querySelector(".attention");
  const panels = document.querySelector(".app-panels");
  if (!attention || !panels) {
    return; // no report yet: nothing to tab between
  }

  function panel(app) {
    return panels.querySelector('section[aria-labelledby="panel-' + app + '"]');
  }

  const groups = [
    { key: "overview", label: "Overview", sections: [attention, panel("system"), panel("disks")] },
    { key: "docker", label: "Docker", sections: [panel("docker")] },
    { key: "systemd", label: "Systemd", sections: [panel("systemd")] },
    { key: "kubernetes", label: "Kubernetes", sections: [panel("kubernetes")] },
  ]
    .map((group) => Object.assign(group, { sections: group.sections.filter(Boolean) }))
    .filter((group) => group.sections.length > 0);
  if (groups.length < 2) {
    return;
  }

  const STORE_KEY = "cronrack-host-tab:" + window.location.pathname;
  const list = document.createElement("div");
  list.className = "g-tabs";
  list.setAttribute("role", "tablist");
  list.setAttribute("aria-label", "Host sections");
  const indicator = document.createElement("span");
  indicator.className = "g-tabs__ind";
  indicator.setAttribute("aria-hidden", "true");
  list.appendChild(indicator);

  const tabs = [];
  const tabpanels = [];
  groups.forEach((group) => {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "g-tab";
    tab.id = "g-tab-" + group.key;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", "g-tabpanel-" + group.key);
    tab.textContent = group.label;
    list.appendChild(tab);
    tabs.push(tab);

    const box = document.createElement("div");
    box.className = "g-tabpanel";
    box.id = "g-tabpanel-" + group.key;
    box.setAttribute("role", "tabpanel");
    box.setAttribute("aria-labelledby", tab.id);
    box.tabIndex = 0;
    tabpanels.push(box);
  });

  // Tabs + panels go where "Needs your attention" was; the sections move into their panel.
  attention.before(list);
  let anchor = list;
  tabpanels.forEach((box, i) => {
    anchor.after(box);
    anchor = box;
    groups[i].sections.forEach((section) => box.appendChild(section));
  });
  panels.hidden = true; // now empty

  function moveIndicator(tab) {
    // offsetLeft is measured from the list's padding edge, the same origin as left: 0.
    indicator.style.setProperty("--g-ind-x", tab.offsetLeft + "px");
    indicator.style.setProperty("--g-ind-w", tab.offsetWidth + "px");
  }

  function select(index, focus) {
    tabs.forEach((tab, i) => {
      const on = i === index;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      tabpanels[i].hidden = !on;
    });
    moveIndicator(tabs[index]);
    if (focus) {
      tabs[index].focus();
    }
    try {
      window.sessionStorage.setItem(STORE_KEY, groups[index].key);
    } catch (e) {
      // storage blocked: the choice lasts for this page only
    }
  }

  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(i, false));
    tab.addEventListener("keydown", (event) => {
      let next = null;
      if (event.key === "ArrowRight") {
        next = (i + 1) % tabs.length;
      } else if (event.key === "ArrowLeft") {
        next = (i - 1 + tabs.length) % tabs.length;
      } else if (event.key === "Home") {
        next = 0;
      } else if (event.key === "End") {
        next = tabs.length - 1;
      }
      if (next !== null) {
        event.preventDefault();
        select(next, true);
      }
    });
  });

  let start = 0;
  try {
    const saved = window.sessionStorage.getItem(STORE_KEY);
    const found = groups.findIndex((group) => group.key === saved);
    start = found >= 0 ? found : 0;
  } catch (e) {
    start = 0;
  }
  select(start, false);
  // From now on the indicator slides; on load it is simply in place.
  window.requestAnimationFrame(() => list.classList.add("g-tabs--live"));

  // The indicator follows layout changes (rotation, font swap, window resize).
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      const current = tabs.find((tab) => tab.getAttribute("aria-selected") === "true");
      if (current) {
        moveIndicator(current);
      }
    }).observe(list);
  }

  document.addEventListener("cronrack:appearance", () => {
    if (root.getAttribute("data-skin") !== "glass") {
      window.location.reload();
    }
  });
})();
