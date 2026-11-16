(() => {
  "use strict";
  const canvas = document.getElementById("graph");
  if (!canvas) return;
  const $ = id => document.getElementById(id);
  const status = $("graph-status");
  const style = getComputedStyle(document.documentElement);
  const color = name => style.getPropertyValue(name).trim();
  const colors = {"陌生": color("--warn"), "接触过": color("--accent"),
    "想回炉": "#9876d8", "扎实": "#4a9c70", "无": color("--muted")};
  fetch(canvas.dataset.src).then(response => {
    if (!response.ok) throw new Error("网络索引读取失败");
    return response.json();
  }).then(data => {
    const records = new Map(data.nodes.map(node => [node.id, node]));
    const neighbors = new Map(data.nodes.map(node => [node.id, new Set()]));
    data.edges.forEach(edge => {
      neighbors.get(edge.source).add(edge.target);
      neighbors.get(edge.target).add(edge.source);
    });
    const elements = data.nodes.map(node => ({data: {...node,
      size: 24 + 6 * Math.sqrt(node.degree), color: colors[node.mastery || "无"]}}));
    data.edges.forEach((edge, index) => elements.push({data: {...edge, id: `edge-${index}`}}));
    const cy = cytoscape({container: canvas, elements,
      layout: {name: "cose", animate: false, nodeRepulsion: 12000, padding: 40,
        nodeDimensionsIncludeLabels: true},
      minZoom: 0.15, maxZoom: 3,
      style: [
        {selector: "node", style: {"background-color": "data(color)", label: "data(label)",
          width: "data(size)", height: "data(size)", "font-size": 12, color: color("--fg"),
          "text-valign": "bottom", "text-margin-y": 5, "text-wrap": "wrap",
          "text-max-width": 150, "text-overflow-wrap": "anywhere",
          "text-background-color": color("--card"),
          "text-background-opacity": 0.9, "text-background-padding": "2px"}},
        {selector: "node[type = 'material'], node[type = 'paper']", style: {shape: "round-rectangle"}},
        {selector: "node[type = 'entity']", style: {shape: "diamond"}},
        {selector: "node[?due]", style: {"border-width": 4, "border-color": color("--warn")}},
        {selector: "node:selected", style: {"border-width": 4, "border-color": color("--accent")}},
        {selector: "edge", style: {width: 1.5, "line-color": color("--muted"),
          "curve-style": "bezier", opacity: 0.7}}
      ]});
    let selected = null;
    let root = null;
    let expanded = new Set();
    let visible = new Set();
    const picker = $("node-picker");

    function filterPicker() {
      const query = $("node-search").value.trim().toLocaleLowerCase();
      picker.replaceChildren();
      for (const node of data.nodes) {
        const terms = [node.label, node.id, ...(node.aliases || [])].join(" ").toLocaleLowerCase();
        if (!terms.includes(query)) continue;
        const option = document.createElement("option");
        option.value = node.id;
        option.textContent = `${node.label} · ${node.type} · ${node.id}`;
        option.selected = node.id === selected;
        picker.append(option);
      }
      if (!picker.options.length) {
        const option = document.createElement("option");
        option.textContent = "没有匹配名称 / 别名的页面";
        option.disabled = true;
        picker.append(option);
      }
    }

    function fitView() {
      if (!visible.size) return;
      const shown = cy.elements(":visible");
      cy.fit(shown, 45);
      if (cy.zoom() > 1.2) cy.zoom(1.2);
      cy.center(shown);
    }

    function updateVisibility(layout = false) {
      const types = new Set([...document.querySelectorAll(".f-type:checked")].map(el => el.value));
      const mastery = new Set([...document.querySelectorAll(".f-mastery:checked")].map(el => el.value));
      visible = new Set(data.nodes.filter(node => types.has(node.type) && mastery.has(node.mastery || "无")
        && (root === null || expanded.has(node.id))).map(node => node.id));
      cy.batch(() => {
        cy.nodes().forEach(node => node.style("display", visible.has(node.id()) ? "element" : "none"));
        cy.edges().forEach(edge => edge.style("display", visible.has(edge.source().id())
          && visible.has(edge.target().id()) ? "element" : "none"));
      });
      const edges = data.edges.filter(edge => visible.has(edge.source) && visible.has(edge.target)).length;
      const scope = root === null ? "全局" : `局部中心：${records.get(root).label}`;
      status.textContent = `${scope} · ${visible.size} 个节点 · ${edges} 条链接`;
      if (selected && !visible.has(selected)) status.textContent += " · 选中节点被当前筛选隐藏";
      $("graph-filter-empty").hidden = visible.size !== 0;
      if (layout && visible.size) {
        cy.elements(":visible").layout({name: "cose", animate: false, nodeRepulsion: 12000,
          fit: false, nodeDimensionsIncludeLabels: true, padding: 45}).run();
        fitView();
      }
    }

    function choose(id, center = true) {
      const node = records.get(id);
      if (!node) return;
      selected = id;
      $("node-detail").hidden = false;
      $("node-title").textContent = node.label;
      $("node-kind").textContent = `${node.type} · ${node.id} · 掌握度 ${node.mastery || "未设置"}`;
      $("node-open").href = node.url;
      $("node-neighbors").replaceChildren();
      for (const neighbor of neighbors.get(id)) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "fb neighbor";
        button.textContent = records.get(neighbor).label;
        button.addEventListener("click", () => choose(neighbor));
        $("node-neighbors").append(button);
      }
      if (!neighbors.get(id).size) $("node-neighbors").textContent = "孤立页面：暂无直接链接。";
      cy.nodes().unselect();
      cy.getElementById(id).select();
      filterPicker();
      updateVisibility();
      if (center && visible.has(id)) {
        cy.zoom(Math.min(1.2, Math.max(cy.zoom(), 0.9)));
        cy.center(cy.getElementById(id));
      }
    }

    function localView() {
      if (!selected) return;
      root = selected;
      expanded = new Set([root, ...neighbors.get(root)]);
      history.replaceState(null, "", `?focus=${encodeURIComponent(root)}`);
      updateVisibility(true);
    }

    cy.on("tap", "node", event => choose(event.target.id(), false));
    picker.addEventListener("change", () => choose(picker.value));
    $("node-search").addEventListener("input", filterPicker);
    $("node-search").addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        const option = [...picker.options].find(option => !option.disabled);
        if (option) choose(option.value);
      }
    });
    $("graph-local").addEventListener("click", localView);
    $("graph-expand").addEventListener("click", () => {
      if (!selected) return;
      if (root === null) return localView();
      expanded.add(selected);
      neighbors.get(selected).forEach(id => expanded.add(id));
      updateVisibility(true);
    });
    $("graph-global").addEventListener("click", () => {
      root = null;
      history.replaceState(null, "", location.pathname);
      updateVisibility(true);
    });
    $("graph-fit").addEventListener("click", fitView);
    document.querySelectorAll(".f-type, .f-mastery").forEach(el =>
      el.addEventListener("change", () => updateVisibility(true)));
    filterPicker();
    const initial = new URLSearchParams(location.search).get("focus");
    if (initial && records.has(initial)) {
      choose(initial, false);
      localView();
    } else {
      updateVisibility();
      fitView();
      if (initial) status.textContent += " · 请求的节点不存在，请按名称重新定位";
    }
  }).catch(error => {
    status.textContent = `${error.message}。请重新索引或刷新页面。`;
  });
})();
