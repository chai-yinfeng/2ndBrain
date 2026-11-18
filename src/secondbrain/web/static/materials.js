(() => {
  const form = document.getElementById("material-actions");
  if (!form) return;
  const boxes = [...document.querySelectorAll('input[name="item_ids"][form="material-actions"]')];
  const all = document.querySelector("[data-select-materials]");
  const status = document.getElementById("material-selection");
  function update() {
    const count = boxes.filter(box => box.checked).length;
    status.textContent = `已选择 ${count} 份材料`;
    all.checked = boxes.length > 0 && count === boxes.length;
    all.indeterminate = count > 0 && count < boxes.length;
  }
  all.disabled = boxes.length === 0;
  all.addEventListener("change", () => {
    boxes.forEach(box => { box.checked = all.checked; });
    update();
  });
  boxes.forEach(box => box.addEventListener("change", update));
  form.addEventListener("submit", event => {
    if (!boxes.some(box => box.checked)) {
      event.preventDefault();
      status.textContent = "请先选择要处理的材料。";
      boxes[0]?.focus();
    }
  });
  update();
})();
