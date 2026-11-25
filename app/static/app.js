/* Certificate Tracker: small progressive enhancements. Works without a build step. */
(function () {
  "use strict";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const csrf = () => ($('meta[name="csrf-token"]') || {}).content || "";

  // ---------- theme
  const stored = localStorage.getItem("theme");
  if (stored) document.documentElement.dataset.theme = stored;
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest("[data-theme-toggle]")) return;
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("theme", next);
  });

  // ---------- nav toggle
  document.addEventListener("click", (ev) => {
    const t = ev.target.closest("[data-toggle]");
    if (t) { const el = document.getElementById(t.dataset.toggle); if (el) el.classList.toggle("open"); }
  });

  // ---------- dates
  const pad = (n) => String(n).padStart(2, "0");
  const fmt = (d) => `${pad(d.getDate())}-${pad(d.getMonth() + 1)}-${d.getFullYear()}`;
  function addYears(iso, years) {
    const [y, m, d] = iso.split("-").map(Number);
    const last = new Date(y + years, m, 0).getDate(); // clamp 29 Feb
    return new Date(y + years, m - 1, Math.min(d, last));
  }
  $$("[data-utc]").forEach((el) => {
    const d = new Date(el.dataset.utc);
    if (!isNaN(d)) el.textContent = `${fmt(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  });

  // ---------- snooze & renew dialogs
  function openDialog(id, btn) {
    const dlg = document.getElementById(id);
    if (!dlg) return;
    const form = $("form", dlg);
    form.action = form.dataset.actionTemplate.replace("{id}", btn.dataset[id === "snooze-dialog" ? "snooze" : "renew"]);
    const title = $("[data-dialog-title]", dlg);
    if (title) title.textContent = btn.dataset.title || "";
    if (id === "renew-dialog") {
      const iso = btn.dataset.expiry;
      $("[data-old-expiry]", dlg).textContent = iso ? fmt(new Date(iso + "T00:00:00")) : "none";
      form.expiry.value = "";
      form.window_n.value = "";
      $$("[data-years]", dlg).forEach((b) => {
        b.disabled = !iso;
        b.onclick = () => { form.expiry.value = fmt(addYears(iso, Number(b.dataset.years))); };
      });
    }
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
  }
  document.addEventListener("click", (ev) => {
    const s = ev.target.closest("[data-snooze]");
    if (s) { ev.preventDefault(); openDialog("snooze-dialog", s); return; }
    const r = ev.target.closest("[data-renew]");
    if (r) { ev.preventDefault(); openDialog("renew-dialog", r); return; }
    const c = ev.target.closest("[data-close]");
    if (c) { const d = c.closest("dialog"); if (d) d.close(); }
  });

  // ---------- bulk selection
  const bulk = $("#bulk-form");
  function updateBulk() {
    if (!bulk) return;
    const ids = $$(".bulk-check:checked").map((c) => c.value);
    const unique = Array.from(new Set(ids));
    bulk.hidden = unique.length === 0;
    $("[data-bulk-count]", bulk).textContent = unique.length;
  }
  document.addEventListener("change", (ev) => {
    if (ev.target.matches("[data-bulk-all]")) {
      $$(".bulk-check").forEach((c) => { c.checked = ev.target.checked; });
    }
    if (ev.target.matches(".bulk-check, [data-bulk-all]")) updateBulk();
  });
  if (bulk) {
    bulk.addEventListener("submit", () => {
      $$("input[name=ids]", bulk).forEach((i) => i.remove());
      Array.from(new Set($$(".bulk-check:checked").map((c) => c.value))).forEach((id) => {
        const i = document.createElement("input");
        i.type = "hidden"; i.name = "ids"; i.value = id; bulk.appendChild(i);
      });
    });
    $("[data-bulk-clear]", bulk).addEventListener("click", () => {
      $$(".bulk-check, [data-bulk-all]").forEach((c) => { c.checked = false; });
      updateBulk();
    });
  }

  // ---------- table sorting
  $$("table[data-sortable]").forEach((table) => {
    $$("th[data-sort]", table).forEach((th, idx) => {
      th.addEventListener("click", () => {
        const col = th.cellIndex;
        const dir = th.dataset.dir === "asc" ? "desc" : "asc";
        $$("th", table).forEach((h) => delete h.dataset.dir);
        th.dataset.dir = dir;
        const numeric = ["number", "duration"].includes(th.dataset.type) || th.dataset.sort === "status";
        const rows = $$("tbody tr", table);
        rows.sort((a, b) => {
          let x = a.cells[col].dataset.sortval ?? a.cells[col].textContent;
          let y = b.cells[col].dataset.sortval ?? b.cells[col].textContent;
          if (numeric) { x = x === "" ? Infinity : Number(x); y = y === "" ? Infinity : Number(y); }
          else { x = String(x).toLowerCase(); y = String(y).toLowerCase();
            if (x === "") x = "￿"; if (y === "") y = "￿"; }
          return (x < y ? -1 : x > y ? 1 : 0) * (dir === "asc" ? 1 : -1);
        });
        const tbody = $("tbody", table);
        rows.forEach((r) => tbody.appendChild(r));
      });
    });
  });

  // ---------- inline editing
  function startEdit(td) {
    if (td.classList.contains("editing")) return;
    const type = td.dataset.type;
    const original = td.textContent.trim();
    if (type === "checkbox") { save(td, original !== "Yes", original); return; }
    td.classList.add("editing");
    let input;
    if (type === "longtext") {
      input = document.createElement("textarea"); input.rows = 3;
    } else {
      input = document.createElement("input");
      if (type === "date") { input.placeholder = "DD-MM-YYYY"; input.inputMode = "numeric"; }
      if (type === "number") input.inputMode = "decimal";
      if (type === "duration") input.placeholder = "e.g. 2 weeks";
      if (type === "select") {
        const listId = "dl-inline-" + td.dataset.col;
        if (!document.getElementById(listId)) {
          const dl = document.createElement("datalist"); dl.id = listId;
          JSON.parse(td.dataset.options || "[]").forEach((o) => { const op = document.createElement("option"); op.value = o; dl.appendChild(op); });
          document.body.appendChild(dl);
        }
        input.setAttribute("list", listId);
      }
    }
    input.value = original;
    td.textContent = "";
    td.appendChild(input);
    input.focus();
    let done = false;
    const finish = (commit) => {
      if (done) return; done = true;
      td.classList.remove("editing");
      if (commit && input.value.trim() !== original) save(td, input.value, original);
      else td.textContent = original;
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && (type !== "longtext" || ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); finish(true); }
      if (ev.key === "Escape") { ev.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  }
  async function save(td, value, original) {
    const tr = td.closest("tr");
    td.classList.add("saving");
    td.textContent = typeof value === "boolean" ? (value ? "Yes" : "No") : value;
    try {
      const resp = await fetch(`/entries/${tr.dataset.entry}/field`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify({ col: Number(td.dataset.col), value }),
        credentials: "same-origin",
      });
      const data = await resp.json().catch(() => ({ ok: false, error: `HTTP ${resp.status}` }));
      if (!resp.ok || !data.ok) throw new Error(data.error || `HTTP ${resp.status}`);
      td.textContent = data.display;
      td.dataset.sortval = data.display;
      const badge = tr.querySelector(".badge");
      if (badge) { badge.className = `badge st-${data.status}`; badge.textContent = data.status_label; }
    } catch (err) {
      td.textContent = original;
      alert("Not saved: " + err.message);
    } finally {
      td.classList.remove("saving");
    }
  }
  document.addEventListener("click", (ev) => {
    const td = ev.target.closest("td.editable");
    if (td && !ev.target.closest("input, textarea")) startEdit(td);
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && ev.target.matches && ev.target.matches("td.editable")) { ev.preventDefault(); startEdit(ev.target); }
  });

  // ---------- PWA
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
  }
})();
