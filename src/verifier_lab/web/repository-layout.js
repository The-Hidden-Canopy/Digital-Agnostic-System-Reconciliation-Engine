/* Deterministic, bounded repository-map selection and spatial arrangement. */
(() => {
  const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
  const groupKey = (node, grouping) => grouping === "kind" ? node.kind :
    node.kind === "repository" ? "Repository" : node.path || "Other items";

  function choose(nodes, edges, selectedId, routeIds, limit, scope) {
    const byId = new Map(nodes.map(n => [n.id, n]));
    const chosen = [], seen = new Set();
    const add = id => { if (byId.has(id) && !seen.has(id)) { seen.add(id); chosen.push(id); } };
    add(selectedId);
    routeIds.forEach(add); // Never hide a current guided step when density is reduced.
    const cap = Math.max(limit, chosen.length);
    if (scope === "overview") {
      if (chosen.length < cap) add("repo");
      const groups = new Map();
      nodes.forEach(n => {
        if (seen.has(n.id)) return;
        const key = groupKey(n, "file");
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(n);
      });
      const ordered = [...groups].sort((a, b) => compare(a[0], b[0])).map(([, group]) => group.sort((a, b) =>
        Number(!a.label) - Number(!b.label) || Number(a.kind !== "file") - Number(b.kind !== "file") || compare(a.id, b.id)));
      for (let round = 0; chosen.length < cap; round++) {
        let found = false;
        for (const group of ordered) {
          if (group[round]) { found = true; add(group[round].id); }
          if (chosen.length >= cap) break;
        }
        if (!found) break;
      }
    } else {
      const adjacency = new Map(nodes.map(n => [n.id, []]));
      edges.forEach(e => {
        if (adjacency.has(e.source) && adjacency.has(e.target)) {
          adjacency.get(e.source).push(e.target); adjacency.get(e.target).push(e.source);
        }
      });
      adjacency.forEach(items => items.sort(compare));
      for (let i = 0; i < chosen.length && chosen.length < cap; i++) {
        for (const id of adjacency.get(chosen[i]) || []) { add(id); if (chosen.length >= cap) break; }
      }
    }
    return chosen.map(id => byId.get(id));
  }

  function arrange(nodes, grouping) {
    const positions = new Map(), captions = [];
    if (grouping === "none") {
      nodes.forEach((node, i) => {
        const angle = i * 2.399963, radius = i ? Math.min(220, 22 * Math.sqrt(i)) : 0;
        positions.set(node.id, [450 + Math.cos(angle) * radius * 1.6, 285 + Math.sin(angle) * radius]);
      });
      return { positions, captions };
    }
    const groups = new Map();
    nodes.forEach(n => { const key = groupKey(n, grouping); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(n); });
    const ordered = [...groups].sort((a, b) => compare(a[0], b[0]));
    const columns = Math.max(1, Math.ceil(Math.sqrt(ordered.length * 1.58)));
    const rows = Math.max(1, Math.ceil(ordered.length / columns)), cellWidth = 840 / columns, cellHeight = 510 / rows;
    ordered.forEach(([key, group], index) => {
      const x = 30 + cellWidth * (index % columns + .5), y = 30 + cellHeight * (Math.floor(index / columns) + .5);
      const radius = Math.max(0, Math.min(cellWidth, cellHeight) * .38 - 5);
      group.forEach((node, i) => {
        const angle = i * 2.399963, r = group.length === 1 ? 0 : radius * Math.sqrt(i / (group.length - 1));
        positions.set(node.id, [x + Math.cos(angle) * r, y + Math.sin(angle) * r]);
      });
      if (ordered.length <= 12) captions.push({ key, x, y: Math.max(14, y - cellHeight * .42) });
    });
    return { positions, captions };
  }

  function labels(nodes, positions, selectedId, routeIds, limit = 12) {
    const route = new Set(routeIds), boxes = [], out = [];
    const ordered = [...nodes].sort((a, b) => {
      const rank = n => n.id === selectedId ? 0 : route.has(n.id) ? 1 : n.label ? 2 : n.kind === "file" ? 3 : 4;
      return rank(a) - rank(b) || compare(a.id, b.id);
    });
    for (const node of ordered) {
      if (out.length >= limit) break;
      const point = positions.get(node.id); if (!point) continue;
      const full = node.label || node.name || node.kind;
      const text = full.length > 26 ? full.slice(0, 23) + "…" : full;
      const width = Math.min(190, text.length * 7 + 4);
      const x = Math.max(5, Math.min(895 - width, point[0] + 10));
      const y = Math.max(16, Math.min(560, point[1] - 8));
      const box = { left: x, right: x + width, top: y - 13, bottom: y + 3 };
      if (boxes.some(b => box.left < b.right && box.right > b.left && box.top < b.bottom && box.bottom > b.top)) continue;
      boxes.push(box); out.push({ id: node.id, text, full, x, y });
    }
    return out;
  }
  const api = { choose, arrange, labels, groupKey };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof globalThis !== "undefined") globalThis.ElucidaryLayout = api;
})();
