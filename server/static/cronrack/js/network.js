// PLACEHOLDER — Apo owns the final version.
// The Network page's 60 s refresh (visible tabs only): /api/v1/network.json updates the
// sentence, each row's status word, latency and loss, and the facts. Everything goes through
// textContent and classes, so an address or name from a report can't become markup. The
// trends are drawn by the server: they change on the next page load. Rows are matched by
// their point name; a different set of points reloads the page once.
(function () {
  "use strict";
  const rows = document.getElementById("net-rows");
  if (!rows) {
    return;
  }
  const STATUSES = ["ok", "down", "slow", "untested"];

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) {
      el.textContent = typeof value === "string" ? value : "";
    }
  }

  function fillRow(tr, row) {
    const word = tr.querySelector(".net-word");
    const status = STATUSES.includes(row.status) ? row.status : "untested";
    word.className = "net-word net-word--" + status;
    word.textContent = typeof row.word === "string" ? row.word : "";
    tr.querySelector(".net-cell--latency").textContent = typeof row.latency === "string" ? row.latency : "";
    tr.querySelector(".net-cell--loss").textContent = typeof row.loss === "string" ? row.loss : "";
    tr.querySelector(".net-cell--address").textContent = typeof row.address === "string" && row.address ? row.address : "—";
  }

  async function refresh() {
    let data;
    try {
      const response = await fetch("/api/v1/network.json", {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      data = response.ok ? await response.json() : null;
    } catch (e) {
      return; // keep what is shown; try again on the next tick
    }
    const network = data && data.network;
    if (!network || !Array.isArray(network.rows)) {
      return;
    }
    const trs = Array.from(rows.querySelectorAll("tr[data-point]"));
    const same =
      trs.length === network.rows.length &&
      trs.every((tr, i) => tr.getAttribute("data-point") === network.rows[i].name);
    if (!same) {
      window.location.reload(); // the same fixed page, with the new set of points
      return;
    }
    trs.forEach((tr, i) => fillRow(tr, network.rows[i]));
    const title = document.getElementById("net-title");
    if (title && title.textContent !== network.text) {
      setText("net-page-announce", network.text); // once: only when the state changed
    }
    setText("net-title", network.text);
    setText("net-sentence", network.sentence);
    const facts = network.facts || {};
    setText("net-dns", facts.dns);
    setText("net-public-ip", facts.public_ip);
    setText("net-reboot", facts.last_reboot);
    setText("net-method", facts.method);
    setText("net-checked", facts.checked);
  }

  window.setInterval(() => {
    if (document.visibilityState === "visible") {
      refresh();
    }
  }, 60000);
})();
