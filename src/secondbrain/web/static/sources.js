(() => {
  "use strict";
  const list = document.getElementById("following-list");
  if (!list) return;
  const sort = document.getElementById("source-sort");
  const rows = Array.from(list.children).filter(row => row.matches("[data-source]"));
  const collator = new Intl.Collator("zh-CN", {
    usage: "sort", sensitivity: "base", numeric: true, ignorePunctuation: true,
  });
  const orderKey = "secondbrain-source-sort";
  let preference = "recent";
  try { preference = localStorage.getItem(orderKey) || preference; } catch (_) { /* Optional storage. */ }
  function sortRows(value) {
    const mode = value === "name" ? "name" : "recent";
    const recent = (left, right) => Number(right.dataset.importOrder) - Number(left.dataset.importOrder);
    const ordered = [...rows].sort(mode === "name"
      ? (left, right) => collator.compare(left.dataset.sourceName, right.dataset.sourceName) || recent(left, right)
      : recent);
    const fragment = document.createDocumentFragment();
    ordered.forEach(row => fragment.append(row));
    list.append(fragment);
    sort.value = mode;
  }
  sortRows(preference);
  sort.disabled = false;
  sort.addEventListener("change", () => {
    sortRows(sort.value);
    try { localStorage.setItem(orderKey, sort.value); } catch (_) { /* This page still sorts. */ }
  });

  const dialogs = Array.from(document.querySelectorAll("dialog.source-dialog"));
  const openers = new WeakMap();
  function taskTo(host) {
    const task = document.getElementById("task-manual_add");
    if (task && host) host.append(task);
  }
  function openerFor(dialog, trigger) {
    if (trigger && !trigger.closest("dialog")) return trigger;
    return Array.from(document.querySelectorAll("[data-dialog-open]")).find(element =>
      element.dataset.dialogOpen === dialog.id && !element.closest("dialog"));
  }
  function openDialog(dialog, trigger) {
    if (!dialog) return;
    const position = {x: window.scrollX, y: window.scrollY};
    dialogs.filter(other => other !== dialog && other.open).forEach(other => other.close());
    openers.set(dialog, openerFor(dialog, trigger));
    if (!dialog.open) {
      dialog.showModal();
      window.scrollTo(position.x, position.y);
    }
    const host = dialog.querySelector("[data-source-task-host]");
    if (host) taskTo(host);
  }
  document.querySelectorAll("[data-dialog-open]").forEach(trigger => {
    trigger.addEventListener("click", event => {
      if (trigger.tagName === "A") event.preventDefault();
      openDialog(document.getElementById(trigger.dataset.dialogOpen), trigger);
    });
  });
  document.querySelectorAll("form[data-preview-dialog]").forEach(form => {
    form.addEventListener("submit", () => {
      openDialog(document.getElementById(form.dataset.previewDialog), form.querySelector("button"));
    });
  });
  dialogs.forEach(dialog => {
    dialog.querySelectorAll("[data-dialog-close]").forEach(button => {
      button.addEventListener("click", () => dialog.close());
    });
    dialog.addEventListener("click", event => {
      const box = dialog.getBoundingClientRect();
      if (event.target === dialog && (event.clientX < box.left || event.clientX > box.right
          || event.clientY < box.top || event.clientY > box.bottom)) dialog.close();
    });
    dialog.addEventListener("close", () => {
      if (!dialogs.some(other => other.open)) {
        const opener = openers.get(dialog);
        if (opener && opener.isConnected && opener.getClientRects().length) {
          opener.focus({preventScroll: true});
        }
      }
    });
  });
  // Native dialogs own focus trapping and Escape. Responses only update their body;
  // closing an in-flight preview must never reopen it when HTMX finishes.
})();
