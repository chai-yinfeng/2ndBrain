(() => {
  "use strict";
  function renderMath() {
    if (!window.katex) return;
    document.querySelectorAll(".wiki-body .math:not([data-rendered])").forEach(element => {
      const source = element.textContent;
      try {
        katex.render(source, element, {
          displayMode: element.dataset.display === "true",
          trust: false,
          throwOnError: false,
          strict: "warn",
          maxExpand: 1000
        });
        element.dataset.rendered = "true";
      } catch (_) {
        element.textContent = source;
        element.title = "公式无法渲染，显示原始 TeX";
      }
    });
  }
  document.addEventListener("DOMContentLoaded", renderMath);
  document.addEventListener("htmx:afterSwap", renderMath);
})();
