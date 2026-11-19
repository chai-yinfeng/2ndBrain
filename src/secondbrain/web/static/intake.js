(() => {
  function syncReadingForms() {
    for (const current of document.querySelectorAll("form")) {
      const selected = current.querySelector('[name="state"]:checked');
      const learning = current.querySelector("[data-intake-read]");
      if (!selected || !learning) continue;
      learning.hidden = selected.value !== "read";
      for (const field of learning.querySelectorAll("input, textarea")) {
        field.disabled = !!field.closest("[hidden]");
      }
    }
  }
  document.addEventListener("change", syncReadingForms);
  document.addEventListener("htmx:afterSwap", syncReadingForms);
  syncReadingForms();
  const root = document.querySelector("[data-intake]");
  if (!root) return;
  const tabs = [...root.querySelectorAll("[data-intake-type]")];
  const panels = [...root.querySelectorAll("[data-intake-panel]")];
  const form = root.querySelector("[data-intake-form]");
  const fields = [...form.querySelectorAll("input, textarea, select")];
  const forms = [...root.querySelectorAll("form")];
  const content = form.querySelector("[data-intake-content]");
  const contentLabel = form.querySelector("[data-intake-content-label]");
  const headings = {link: "添加链接", file: "上传 PDF", text: "粘贴文本"};
  const capture = () => fields.map(field => ({
    value: field.value,
    checked: field.checked,
    files: field.type === "file" ? field.files : null,
  }));
  const emptyDraft = capture();
  const drafts = new Map();
  let active = "link";

  function sync() {
    for (const panel of panels) {
      panel.hidden = !panel.dataset.intakePanel.split(" ").includes(active);
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", `intake-tab-${active}`);
    }
    for (const wrapper of form.querySelectorAll("[data-intake-field]")) {
      wrapper.hidden = !wrapper.dataset.intakeField.split(" ").includes(active);
    }
    syncReadingForms();
    for (const field of root.querySelectorAll("form input, form textarea, form select, form button")) {
      field.disabled = !!field.closest("[hidden]");
    }
    for (const tab of tabs) {
      const selected = tab.dataset.intakeType === active;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      tab.classList.toggle("primary", selected);
    }
    if (active !== "paper") root.querySelector("[data-intake-heading]").textContent = headings[active];
  }

  function select(type, focus = false) {
    if (type !== active) {
      if (active !== "paper") drafts.set(active, capture());
      if (type !== "paper") {
        const draft = drafts.get(type) || emptyDraft;
        fields.forEach((field, index) => {
          const saved = draft[index];
          if (field.type === "file") field.files = saved.files;
          else if (field.type === "checkbox" || field.type === "radio") field.checked = saved.checked;
          else field.value = saved.value;
        });
      }
      active = type;
    }
    content.open = active !== "link" || root.dataset.intakeRemedy === "true";
    contentLabel.hidden = active !== "link";
    contentLabel.textContent = "补充正文或 PDF（可选，单个链接；二选一）";
    sync();
    if (focus) tabs.find(tab => tab.dataset.intakeType === active).focus();
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab.dataset.intakeType));
    tab.addEventListener("keydown", event => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault();
      select(tabs[next].dataset.intakeType, true);
    });
  });
  forms.forEach(current => current.addEventListener("change", sync));
  window.addEventListener("hashchange", () => {
    if (location.hash === "#paper-name") select("paper");
  });
  root.querySelector("[role=tablist]").hidden = false;
  select(location.hash === "#paper-name" ? "paper" : "link");
})();
