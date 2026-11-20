(() => {
  "use strict";
  const root = document.documentElement;
  root.classList.remove("no-js");
  const scheme = matchMedia("(prefers-color-scheme: dark)");
  const key = "secondbrain-theme";
  const valid = value => ["light", "dark", "system"].includes(value) ? value : "system";
  let preference = "system";
  try { preference = valid(localStorage.getItem(key)); } catch (_) { /* Storage may be disabled. */ }
  function applyTheme() {
    const theme = preference === "system" ? (scheme.matches ? "dark" : "light") : preference;
    root.dataset.theme = theme;
    root.dataset.themePreference = preference;
    const select = document.getElementById("theme-select");
    if (select) select.value = preference;
    document.dispatchEvent(new CustomEvent("secondbrain:themechange", {detail: {theme, preference}}));
  }
  applyTheme();
  scheme.addEventListener("change", () => { if (preference === "system") applyTheme(); });
  window.addEventListener("storage", event => {
    if (event.key === key || event.key === null) {
      preference = valid(event.key === null ? null : event.newValue);
      applyTheme();
    }
  });
  document.addEventListener("DOMContentLoaded", () => {
    const select = document.getElementById("theme-select");
    select.disabled = false;
    select.value = preference;
    select.addEventListener("change", () => {
      preference = valid(select.value);
      try { localStorage.setItem(key, preference); } catch (_) { /* Apply for this page even without storage. */ }
      applyTheme();
    });
    const sidebar = document.getElementById("app-sidebar");
    const frame = document.getElementById("app-frame");
    const trigger = document.getElementById("nav-toggle");
    const backdrop = document.getElementById("nav-backdrop");
    const mobile = matchMedia("(max-width: 760px)");
    let open = false;
    function closeNav(restore = true) {
      open = false;
      document.body.classList.remove("nav-open");
      frame.inert = false;
      backdrop.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      sidebar.removeAttribute("role");
      sidebar.removeAttribute("aria-modal");
      if (restore) trigger.focus();
    }
    trigger.addEventListener("click", () => {
      if (!mobile.matches) return;
      open = true;
      document.body.classList.add("nav-open");
      backdrop.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      sidebar.setAttribute("role", "dialog");
      sidebar.setAttribute("aria-modal", "true");
      frame.inert = true;
      (sidebar.querySelector('[aria-current="page"]') || document.getElementById("nav-close")).focus();
    });
    document.getElementById("nav-close").addEventListener("click", () => closeNav());
    backdrop.addEventListener("click", () => closeNav());
    mobile.addEventListener("change", () => { if (open) closeNav(false); });
    document.addEventListener("keydown", event => {
      if (!open) return;
      if (event.key === "Escape") { event.preventDefault(); closeNav(); }
      if (event.key !== "Tab") return;
      const focusable = Array.from(sidebar.querySelectorAll('a[href], button, select, summary, [tabindex="0"]'))
        .filter(element => !element.disabled && element.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    document.querySelectorAll("[data-list-filter]").forEach(input => {
      const list = document.getElementById(input.dataset.listFilter);
      const rows = Array.from(list.querySelectorAll("[data-filter-row]"));
      const empty = list.querySelector("[data-filter-empty]");
      const status = document.getElementById(input.dataset.filterStatus);
      const normalize = text => text.normalize("NFKC").toLocaleLowerCase();
      const entries = rows.map(row => ({row, text: normalize(row.dataset.filterText || row.textContent)}));
      input.addEventListener("input", () => {
        const query = normalize(input.value.trim());
        let count = 0;
        entries.forEach(({row, text}) => { row.hidden = !text.includes(query); if (!row.hidden) count++; });
        empty.hidden = count > 0 || !query;
        status.textContent = query ? `显示 ${count} / ${rows.length} 份材料` : `${rows.length} 份待读材料`;
      });
    });
    function openHashTarget() {
      let id;
      try { id = decodeURIComponent(location.hash.slice(1)); } catch (_) { return; }
      const target = document.getElementById(id);
      if (!target) return;
      let details = target.closest("details");
      while (details) { details.open = true; details = details.parentElement.closest("details"); }
      requestAnimationFrame(() => target.scrollIntoView({block: "start"}));
    }
    window.addEventListener("hashchange", openHashTarget);
    openHashTarget();
    document.addEventListener("click", event => {
      document.querySelectorAll(".row-menu[open]").forEach(menu => {
        if (!menu.contains(event.target)) menu.open = false;
      });
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape" || open) return;
      document.querySelectorAll(".row-menu[open]").forEach(menu => {
        menu.open = false;
        menu.querySelector("summary").focus();
      });
    });
  });
})();
