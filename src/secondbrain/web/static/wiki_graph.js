(() => {
  "use strict";
  const canvas = document.getElementById("graph");
  if (!canvas) return;
  const $ = id => document.getElementById(id);
  const status = $("graph-status");
  const typeFilters = [...document.querySelectorAll(".f-type")];
  const kinds = {concept: "概念", entity: "实体", paper: "论文", article: "文章与网页", video: "视频", other: "其他资料"};
  const knowledgeStatus = {recorded: "已登记阅读后的知识", draft: "机器草稿（未登记阅读）",
    legacy: "历史知识页（未记录登记状态）"};

  function graphStyle() {
    const style = getComputedStyle(document.documentElement);
    const color = name => style.getPropertyValue(name).trim();
    return [
      {selector: "node", style: {"background-color": color("--graph-concept"), label: "",
        width: "data(size)", height: "data(size)", "font-size": 11, color: color("--fg"),
        "text-valign": "bottom", "text-margin-y": 7, "text-wrap": "ellipsis",
        "text-max-width": 140, "min-zoomed-font-size": 8,
        "text-background-color": color("--bg"), "text-background-opacity": 0.9,
        "text-background-padding": "3px", "border-width": 2, "border-color": color("--card"),
        "overlay-opacity": 0}},
      {selector: "node[type = 'material'], node[type = 'article'], node[type = 'video'], node[type = 'other']", style: {shape: "round-rectangle",
        "background-color": color("--graph-material")}},
      {selector: "node[type = 'paper']", style: {shape: "round-rectangle",
        "background-color": color("--graph-paper")}},
      {selector: "node[type = 'entity']", style: {shape: "diamond",
        "background-color": color("--graph-entity")}},
      {selector: "node.label-visible", style: {label: "data(label)"}},
      {selector: "edge", style: {width: 1.2, "line-color": color("--graph-edge"),
        "curve-style": "haystack", opacity: 0.5, "overlay-opacity": 0}},
      {selector: ".dimmed", style: {opacity: 0.15, "text-opacity": 0}},
      {selector: "edge.dimmed", style: {opacity: 0.07}},
      {selector: "node.one-hop", style: {"border-width": 3, "border-color": color("--accent")}},
      {selector: "edge.focus-edge", style: {width: 2.4,
        "line-color": color("--accent"), opacity: 0.9}},
      {selector: "node:selected", style: {"border-width": 4, "border-color": color("--accent"),
        "font-weight": 600, "z-index": 10}}
    ];
  }

  fetch(canvas.dataset.src).then(response => {
    if (!response.ok) throw new Error("网络索引读取失败");
    return response.json();
  }).then(data => {
    const records = new Map(data.nodes.map(node => [node.id, node]));
    const neighbors = new Map(data.nodes.map(node => [node.id, new Set()]));
    const searchTerms = new Map(data.nodes.map(node => [node.id,
      [node.label, node.id, ...(node.aliases || [])].join(" ").toLocaleLowerCase()]));
    const ranked = [...data.nodes].sort((a, b) => b.degree - a.degree || a.id.localeCompare(b.id));
    data.edges.forEach(edge => {
      neighbors.get(edge.source).add(edge.target);
      neighbors.get(edge.target).add(edge.source);
    });
    const elements = data.nodes.map(node => ({data: {...node,
      size: 22 + Math.min(16, 4 * Math.sqrt(node.degree))}}));
    data.edges.forEach((edge, index) => elements.push({data: {...edge, id: `edge-${index}`}}));
    const cy = cytoscape({container: canvas, elements, style: graphStyle(),
      layout: {name: "preset"},
      minZoom: 0.04, maxZoom: 3, wheelSensitivity: 0.2, boxSelectionEnabled: false,
      selectionType: "single"});
    // Log-degree bands keep sparse networks readable without a force simulation.
    // Only explicit scope changes arrange nodes; selection, themes and filters do not.
    function arrange(collection) {
      if (collection.nodes().length < 2) return;
      collection.layout({name: "concentric", animate: false, fit: false, minNodeSpacing: 85,
        nodeDimensionsIncludeLabels: true, avoidOverlap: true, spacingFactor: 1.25,
        concentric: node => Math.floor(Math.log2(node.degree() + 1)), levelWidth: () => 1}).run();
    }
    arrange(cy.elements());
    const initialPositions = new Map(cy.nodes().map(node => [node.id(), {...node.position()}]));
    let selected = null;
    let root = null;
    let expanded = new Set();
    let visible = new Set();
    const picker = $("node-picker");

    function updateLocation(focus) {
      const url = new URL(location.href);
      if (focus) url.searchParams.set("focus", focus);
      else url.searchParams.delete("focus");
      // In particular, retain drafts=1 when changing scope.
      history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }

    function filterPicker() {
      const query = $("node-search").value.trim().toLocaleLowerCase();
      const fragment = document.createDocumentFragment();
      let matches = 0;
      for (const node of data.nodes) {
        if (!searchTerms.get(node.id).includes(query)) continue;
        const option = document.createElement("option");
        option.value = node.id;
        option.textContent = `${node.label} · ${kinds[node.type] || node.type}`;
        option.title = option.textContent;
        option.selected = node.id === selected;
        fragment.append(option);
        matches++;
      }
      if (!matches) {
        const option = document.createElement("option");
        option.textContent = "没有匹配名称 / 别名的页面";
        option.disabled = true;
        fragment.append(option);
      }
      picker.replaceChildren(fragment);
      if (![...picker.options].some(option => option.value === selected)) picker.selectedIndex = -1;
      $("node-results").textContent = `${matches} 个匹配 · 列表包含筛选隐藏的页面，同名可打开页面核对来源。`;
    }

    function updateLabels() {
      const mode = $("graph-labels").value;
      const labeled = new Set();
      if (mode === "all" || (mode === "auto" && visible.size <= 45 && cy.zoom() >= 0.45)) {
        visible.forEach(id => labeled.add(id));
      } else if (mode === "auto") {
        ranked.filter(node => visible.has(node.id)).slice(0, cy.zoom() >= 0.65 ? 24 : 10)
          .forEach(node => labeled.add(node.id));
      }
      if (mode !== "none" && selected && visible.has(selected)) {
        labeled.add(selected);
        let count = 0;
        for (const id of neighbors.get(selected)) {
          if (visible.has(id) && count++ < 14) labeled.add(id);
        }
      }
      cy.batch(() => cy.nodes().forEach(node => node.toggleClass("label-visible", labeled.has(node.id()))));
    }

    function highlight() {
      cy.batch(() => {
        cy.elements().removeClass("dimmed one-hop focus-edge");
        if (selected && visible.has(selected)) {
          const node = cy.getElementById(selected);
          const adjacent = node.closedNeighborhood();
          cy.elements().difference(adjacent).addClass("dimmed");
          adjacent.nodes().not(node).addClass("one-hop");
          node.connectedEdges().addClass("focus-edge");
        }
      });
      updateLabels();
    }

    function fitView() {
      if (!visible.size) return;
      const shown = cy.elements(":visible");
      cy.fit(shown, 55);
      if (cy.zoom() > 1.25) cy.zoom(1.25);
      cy.center(shown);
    }

    function updateVisibility(fit = false) {
      const types = new Set(typeFilters.filter(el => el.checked).map(el => el.value));
      visible = new Set(data.nodes.filter(node => types.has(node.type)
        && (root === null || expanded.has(node.id))).map(node => node.id));
      cy.batch(() => {
        cy.nodes().forEach(node => node.style("display", visible.has(node.id()) ? "element" : "none"));
        cy.edges().forEach(edge => edge.style("display", visible.has(edge.source().id())
          && visible.has(edge.target().id()) ? "element" : "none"));
      });
      const edges = data.edges.filter(edge => visible.has(edge.source) && visible.has(edge.target)).length;
      const scope = root === null ? "全局概览" : `局部中心：${records.get(root).label}`;
      status.textContent = `${scope} · ${visible.size} / ${records.size} 个节点 · ${edges} 条链接`;
      if (selected && !visible.has(selected)) status.textContent += " · 选中节点被当前范围或筛选隐藏";
      $("graph-filter-empty").hidden = visible.size !== 0;
      $("graph-global").setAttribute("aria-pressed", String(root === null));
      $("graph-local").setAttribute("aria-pressed", String(root !== null && root === selected));
      highlight();
      if (fit) fitView();
    }

    function clearSelection() {
      selected = null;
      cy.nodes().unselect();
      $("node-detail").hidden = true;
      $("node-placeholder").hidden = false;
      picker.selectedIndex = -1;
      updateVisibility();
    }

    function choose(id, center = true) {
      const node = records.get(id);
      if (!node) return;
      selected = id;
      $("node-detail").hidden = false;
      $("node-placeholder").hidden = true;
      $("node-title").textContent = node.label;
      $("node-kind").textContent = kinds[node.type] || node.type;
      $("node-aliases").textContent = node.aliases.length ? `别名：${node.aliases.join("、")}` : "暂无别名";
      const syncStatus = {withdrawn: "来源材料已回收", unsupported: "无有效来源，待复核", review: "部分来源待复核"}[node.material_sync_status];
      $("node-state").textContent = `${syncStatus ? syncStatus + " · " : ""}${knowledgeStatus[node.knowledge_status] || "知识页"}`;
      $("node-open").href = node.url;
      const sources = $("node-sources");
      sources.replaceChildren();
      const sourceIds = new Set(node.sources);
      if (node.material_id) sourceIds.add(node.material_id);
      for (const sid of sourceIds) {
        const link = document.createElement("a");
        link.className = "fb";
        link.href = `/item/${encodeURIComponent(sid)}`;
        link.textContent = (node.source_titles || {})[sid] || "来源材料";
        sources.append(link);
      }
      if (!sourceIds.size) sources.textContent = "未记录来源材料。";
      $("node-neighbor-count").textContent = neighbors.get(id).size;
      const links = document.createDocumentFragment();
      for (const neighbor of neighbors.get(id)) {
        const record = records.get(neighbor);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "fb neighbor";
        const title = document.createElement("span");
        title.className = "neighbor-title";
        title.textContent = record.label;
        const kind = document.createElement("span");
        kind.className = "neighbor-kind";
        kind.textContent = kinds[record.type] || record.type;
        button.append(title, kind);
        button.addEventListener("click", () => choose(neighbor));
        links.append(button);
      }
      $("node-neighbors").replaceChildren(links);
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
      updateLocation(root);
      updateVisibility();
      arrange(cy.elements(":visible"));
      fitView();
    }

    cy.on("tap", "node", event => choose(event.target.id(), false));
    cy.on("tap", event => { if (event.target === cy) clearSelection(); });
    picker.addEventListener("change", () => choose(picker.value));
    $("node-search").addEventListener("input", filterPicker);
    $("node-search").addEventListener("keydown", event => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const option = [...picker.options].find(option => !option.disabled);
      if (option) choose(option.value);
    });
    $("graph-local").addEventListener("click", localView);
    $("graph-expand").addEventListener("click", () => {
      if (!selected) return;
      if (root === null) return localView();
      expanded.add(selected);
      neighbors.get(selected).forEach(id => expanded.add(id));
      updateVisibility();
      arrange(cy.elements(":visible"));
      fitView();
    });
    $("graph-global").addEventListener("click", () => {
      root = null;
      cy.nodes().positions(node => initialPositions.get(node.id()));
      updateLocation(null);
      updateVisibility(true);
    });
    $("graph-fit").addEventListener("click", fitView);
    $("graph-reset").addEventListener("click", () => {
      root = null;
      expanded.clear();
      typeFilters.forEach(el => { el.checked = true; });
      $("node-search").value = "";
      $("graph-labels").value = "auto";
      cy.nodes().positions(node => initialPositions.get(node.id()));
      updateLocation(null);
      clearSelection();
      filterPicker();
      fitView();
    });
    const zoomBy = factor => cy.zoom({level: Math.max(cy.minZoom(),
      Math.min(cy.maxZoom(), cy.zoom() * factor)),
      renderedPosition: {x: cy.width() / 2, y: cy.height() / 2}});
    $("graph-zoom-in").addEventListener("click", () => zoomBy(1.3));
    $("graph-zoom-out").addEventListener("click", () => zoomBy(1 / 1.3));
    let zoomFrame = null;
    cy.on("zoom", () => {
      $("graph-zoom-level").textContent = `${Math.round(cy.zoom() * 100)}%`;
      if (zoomFrame !== null) return;
      zoomFrame = requestAnimationFrame(() => { zoomFrame = null; updateLabels(); });
    });
    $("graph-labels").addEventListener("change", updateLabels);
    typeFilters.forEach(el =>
      el.addEventListener("change", () => updateVisibility(true)));
    document.addEventListener("secondbrain:themechange", () => {
      cy.style(graphStyle());
      // Reapply display bypasses and label/highlight classes without moving nodes.
      updateVisibility();
    });
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => cy.resize()).observe(canvas);
    }
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
    $("graph-zoom-level").textContent = `${Math.round(cy.zoom() * 100)}%`;
  }).catch(error => {
    status.textContent = `${error.message}。请重新索引或刷新页面。`;
  });
})();
