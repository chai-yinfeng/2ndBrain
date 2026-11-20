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
  const labelFont = 12, labelGap = 5, labelMaxWidth = 144;
  const markerScale = zoom => Math.max(0.44, Math.min(1, zoom / 0.8));

  function graphStyle() {
    const style = getComputedStyle(document.documentElement);
    const color = name => style.getPropertyValue(name).trim();
    return [
      {selector: "node", style: {"background-color": color("--graph-concept"), label: "",
        width: "data(size)", height: "data(size)", "font-size": labelFont,
        "font-family": "sans-serif", color: color("--fg"),
        "text-valign": "bottom", "text-margin-y": labelGap, "text-wrap": "ellipsis",
        "text-max-width": labelMaxWidth, "border-width": 1, "border-color": color("--card"),
        "overlay-opacity": 0}},
      {selector: "node[type = 'material'], node[type = 'article'], node[type = 'video'], node[type = 'other']", style: {shape: "round-rectangle",
        "background-color": color("--graph-material")}},
      {selector: "node[type = 'paper']", style: {shape: "round-rectangle",
        "background-color": color("--graph-paper")}},
      {selector: "node[type = 'entity']", style: {shape: "diamond",
        "background-color": color("--graph-entity")}},
      {selector: "node.label-visible", style: {label: "data(label)"}},
      {selector: "edge", style: {width: 1.1, "line-color": color("--graph-edge"),
        // Nodes cover the endpoints, even when their screen-space markers overlap.
        "source-endpoint": "0px 0px", "target-endpoint": "0px 0px", "edge-distances": "node-position",
        "curve-style": "unbundled-bezier", "control-point-distances": 12,
        "control-point-weights": 0.5, opacity: 0.74, "overlay-opacity": 0}},
      {selector: "edge.local-edge", style: {"curve-style": "straight"}},
      {selector: "edge.overlapping-markers", style: {"curve-style": "haystack", "haystack-radius": 0}},
      {selector: ".dimmed", style: {opacity: 0.15, "text-opacity": 0}},
      {selector: "edge.dimmed", style: {opacity: 0.07}},
      {selector: "node.one-hop, node:selected", style: {
        "underlay-color": color("--accent"), "underlay-opacity": 0.14,
        "underlay-shape": "ellipse", "underlay-padding": 5, "border-color": color("--accent")}},
      {selector: "edge.focus-edge", style: {width: 1.8,
        "line-color": color("--accent"), opacity: 0.86}},
      {selector: "node:selected", style: {"underlay-opacity": 0.25,
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
    // Give the renderer distinct initial positions before running CoSE.
    const radius = Math.max(25, data.nodes.length * 28 / (2 * Math.PI));
    const elements = data.nodes.map((node, index) => {
      const angle = 2 * Math.PI * index / data.nodes.length;
      return {data: {...node, size: 14 + Math.min(8, 2.5 * Math.sqrt(node.degree))},
        position: {x: radius * Math.cos(angle), y: radius * Math.sin(angle)}};
    });
    data.edges.forEach((edge, index) => elements.push({data: {...edge, id: `edge-${index}`}}));
    const cy = cytoscape({container: canvas, elements, style: graphStyle(),
      layout: {name: "preset"},
      minZoom: 0.04, maxZoom: 3, wheelSensitivity: 0.2, boxSelectionEnabled: false,
      selectionType: "single"});
    if (data.nodes.length > 1) {
      cy.layout({name: "cose", animate: false, fit: false, randomize: false,
        idealEdgeLength: 115, nodeOverlap: 20, componentSpacing: 100}).run();
    }
    const initialPositions = new Map(cy.nodes().map(node => [node.id(), {...node.position()}]));
    let selected = null;
    let root = null;
    let expanded = new Set();
    let visible = new Set();
    const picker = $("node-picker");

    function arrangeLocal() {
      // Measure shortest paths in the expanded subgraph, independent of type filters.
      const visited = new Set([root]);
      cy.getElementById(root).position({x: 0, y: 0});
      let ring = [root];
      let radius = 0;
      const spacing = 110;
      while (ring.length) {
        const next = [];
        for (const id of ring) {
          for (const neighbor of neighbors.get(id)) {
            if (!expanded.has(neighbor) || visited.has(neighbor)) continue;
            visited.add(neighbor);
            next.push(neighbor);
          }
        }
        if (!next.length) break;
        // Leave at least one node spacing between rings and between adjacent nodes.
        radius = Math.max(radius + spacing,
          next.length > 1 ? spacing / (2 * Math.sin(Math.PI / next.length)) : 0);
        next.forEach((id, index) => {
          const angle = 2 * Math.PI * index / next.length - Math.PI / 2;
          cy.getElementById(id).position({x: radius * Math.cos(angle), y: radius * Math.sin(angle)});
        });
        ring = next;
      }
    }

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
    const labelMeasure = document.createElement("canvas").getContext("2d");
    // Use the selected font weight as a conservative bound for regular labels too.
    labelMeasure.font = `600 ${labelFont}px sans-serif`;
    const labelWidths = new Map();

    function updateEdgeClearance(edges, zoom) {
      const scale = markerScale(zoom);
      edges.forEach(edge => {
        const source = edge.source(), target = edge.target();
        const a = source.position(), b = target.position();
        const dx = (a.x - b.x) * zoom, dy = (a.y - b.y) * zoom;
        const clearance = (source.data("size") + target.data("size")) * scale * Math.SQRT1_2 + 2;
        // When markers overlap, draw a straight center-to-center line behind them.
        edge.toggleClass("overlapping-markers", dx * dx + dy * dy <= clearance * clearance);
      });
    }

    let markerZoom = 0;
    function updateMarkerScale(repositioned = false) {
      const zoom = cy.zoom();
      if (markerZoom === zoom) {
        if (repositioned) cy.batch(() => updateEdgeClearance(cy.edges(), zoom));
        return;
      }
      markerZoom = zoom;
      const scale = markerScale(zoom);
      // Cytoscape styles use model units; keep labels fixed and shrink distant markers.
      cy.batch(() => {
        const nodes = cy.nodes();
        nodes.style({
          "font-size": labelFont / zoom, "text-margin-y": labelGap / zoom,
          "text-max-width": labelMaxWidth / zoom, "border-width": 1 / zoom,
          "underlay-padding": 5 / zoom,
        });
        nodes.forEach(node => {
          const size = node.data("size") * scale / zoom;
          node.style({width: size, height: size});
        });
        updateEdgeClearance(cy.edges(), zoom);
      });
    }

    function updateLabels() {
      const mode = $("graph-labels").value;
      const labeled = mode === "all" ? visible : new Set();
      if (mode === "auto") {
        const zoom = cy.zoom(), scale = markerScale(zoom);
        const limit = zoom < 0.45 ? 6 : zoom < 1.15 ? 20 : Infinity;
        // Cytoscape adds model-space bounds around labels; include that at high zoom too.
        const labelPadding = 2 * zoom + 2;
        const markers = new Map();
        cy.nodes(":visible").forEach(node => {
          const {x, y} = node.renderedPosition();
          const half = node.data("size") * scale / 2 + 1;
          markers.set(node.id(), {left: x - half, right: x + half, top: y - half, bottom: y + half});
        });
        const occupied = [...markers.values()];
        const adjacent = markers.has(selected) ? neighbors.get(selected) : null;
        function place(id) {
          const marker = markers.get(id);
          if (!marker || labeled.size >= limit) return;
          let width = labelWidths.get(id);
          if (width === undefined) {
            width = Math.min(labelMaxWidth, labelMeasure.measureText(records.get(id).label).width);
            labelWidths.set(id, width);
          }
          const x = (marker.left + marker.right) / 2;
          const y = marker.bottom + labelGap - 1;
          const left = x - width / 2 - labelPadding, right = x + width / 2 + labelPadding;
          const top = y - labelPadding, bottom = y + labelFont + labelPadding;
          if (occupied.some(box => box !== marker && left < box.right && right > box.left
            && top < box.bottom && bottom > box.top)) return;
          occupied.push({left, right, top, bottom});
          labeled.add(id);
        }
        if (adjacent) place(selected);
        for (const {id} of ranked) {
          if (labeled.size >= limit) break;
          // Off-neighborhood nodes have transparent text while a node is selected.
          if (!adjacent || adjacent.has(id)) place(id);
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
      // Fit the node centers, leaving screen-space padding for markers and labels.
      let left = Infinity, right = -Infinity, top = Infinity, bottom = -Infinity;
      cy.nodes(":visible").forEach(node => {
        const {x, y} = node.position();
        left = Math.min(left, x);
        right = Math.max(right, x);
        top = Math.min(top, y);
        bottom = Math.max(bottom, y);
      });
      const width = cy.width(), height = cy.height();
      const zoom = Math.max(cy.minZoom(), Math.min(1.25,
        (width - 2 * Math.min(86, width / 4)) / Math.max(1, right - left),
        (height - 2 * Math.min(48, height / 4)) / Math.max(1, bottom - top)));
      cy.viewport({zoom, pan: {
        x: (width - (left + right) * zoom) / 2,
        y: (height - (top + bottom) * zoom) / 2,
      }});
      updateMarkerScale(true);
    }

    function updateVisibility(fit = false) {
      const types = new Set(typeFilters.filter(el => el.checked).map(el => el.value));
      visible = new Set(data.nodes.filter(node => types.has(node.type)
        && (root === null || expanded.has(node.id))).map(node => node.id));
      cy.batch(() => {
        cy.nodes().forEach(node => node.style("display", visible.has(node.id()) ? "element" : "none"));
        cy.edges().forEach(edge => edge.style("display", visible.has(edge.source().id())
          && visible.has(edge.target().id()) ? "element" : "none"));
        cy.edges().toggleClass("local-edge", root !== null);
      });
      const edges = data.edges.filter(edge => visible.has(edge.source) && visible.has(edge.target)).length;
      const scope = root === null ? "全局概览" : `局部中心：${records.get(root).label}`;
      status.textContent = `${scope} · ${visible.size} / ${records.size} 个节点 · ${edges} 条链接`;
      if (selected && !visible.has(selected)) status.textContent += " · 选中节点被当前范围或筛选隐藏";
      $("graph-filter-empty").hidden = visible.size !== 0;
      $("graph-global").setAttribute("aria-pressed", String(root === null));
      $("graph-local").setAttribute("aria-pressed", String(root !== null && root === selected));
      $("graph-expand").disabled = !selected || (root !== null && !expanded.has(selected));
      $("graph-layout-hint").textContent = root === null
        ? "位置由页面链接布局，不代表语义距离"
        : "中心外第 1、2… 圈分别对应 1、2… 跳";
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
      cy.autoungrabify(true);
      cy.batch(arrangeLocal);
      updateLocation(root);
      updateVisibility(true);
    }

    cy.on("drag", "node", event => {
      cy.batch(() => updateEdgeClearance(event.target.connectedEdges(), cy.zoom()));
    });
    cy.on("dragfree", "node", updateLabels);
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
      if (!expanded.has(selected)) return;
      neighbors.get(selected).forEach(id => expanded.add(id));
      cy.batch(arrangeLocal);
      updateVisibility(true);
    });
    $("graph-global").addEventListener("click", () => {
      root = null;
      cy.autoungrabify(false);
      cy.nodes().positions(node => initialPositions.get(node.id()));
      updateLocation(null);
      updateVisibility(true);
    });
    $("graph-fit").addEventListener("click", fitView);
    $("graph-reset").addEventListener("click", () => {
      root = null;
      expanded.clear();
      cy.autoungrabify(false);
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
      zoomFrame = requestAnimationFrame(() => {
        zoomFrame = null;
        updateMarkerScale();
        updateLabels();
      });
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
    updateMarkerScale();
    $("graph-zoom-level").textContent = `${Math.round(cy.zoom() * 100)}%`;
  }).catch(error => {
    status.textContent = `${error.message}。请重新索引或刷新页面。`;
  });
})();
