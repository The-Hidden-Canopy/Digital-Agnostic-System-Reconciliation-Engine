/* Verification Workbench - vanilla JS, no remote resources. All source text is rendered with textContent. */
"use strict";
const TOKEN = document.querySelector('meta[name="vl-token"]').content;
const $ = (sel, root = document) => root.querySelector(sel);
const main = $("#main");

const VERDICTS = {
  SUPPORTED: ["✓", "b-ok", "Supported"],
  CONTRADICTED: ["✕", "b-bad", "Contradicted"],
  INSUFFICIENT_EVIDENCE: ["?", "b-warn", "Insufficient evidence"],
  INVALID_EVIDENCE: ["!", "b-bad", "Invalid evidence"],
  NOT_APPLICABLE: ["–", "b-neutral", "Not applicable"],
  HUMAN_REVIEW_REQUIRED: ["☐", "b-info", "Human review required"],
};
const COVERAGE = { PARSED: "b-ok", PARTIALLY_PARSED: "b-warn", UNSUPPORTED: "b-warn", CORRUPT: "b-bad",
  ENCRYPTED: "b-bad", RESOURCE_LIMIT: "b-bad", NOT_ATTEMPTED: "b-neutral" };

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else if (k === "class") el.className = v;
    else if (k in el && k !== "list" && k !== "form") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function put(el, ...kids) {
  const flat = kids.flat(Infinity).filter((k) => k != null && k !== false);
  el.replaceChildren(...flat.map((k) => (k.nodeType ? k : document.createTextNode(String(k)))));
}
const badge = (text, cls) => h("span", { class: "badge " + cls, text });
function verdictBadge(v) {
  const [icon, cls, label] = VERDICTS[v] || ["?", "b-neutral", v];
  return h("span", { class: "badge " + cls }, h("span", { "aria-hidden": "true" }, icon), label);
}
function announce(msg) { $("#live").textContent = ""; setTimeout(() => ($("#live").textContent = msg), 30); }
function fmtVal(v) {
  if (!v) return "";
  return v.kind === "string" ? JSON.stringify(v.value) : v.kind === "null" ? "null" : String(v.value);
}
function fmtBytes(n) { return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KiB" : (n / 1048576).toFixed(1) + " MiB"; }

async function api(method, path, { body, headers = {}, raw = false, blob = false } = {}) {
  const opts = { method, headers: { "X-Session-Token": TOKEN, ...headers } };
  if (body !== undefined) {
    if (raw) opts.body = body;
    else { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  }
  const res = await fetch("/api/v1/" + path, opts);
  if (blob && res.ok) return res.blob();
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON error */ }
  if (!res.ok) {
    const err = new Error((data && data.error && (data.error.message || data.error.code)) || res.statusText);
    err.status = res.status; err.data = data && data.error; err.etag = res.headers.get("ETag");
    throw err;
  }
  return data;
}
const enc = encodeURIComponent;

/* ------------------------------------------------------------------ state */
const state = { candSel: new Set(), filters: { source: "", status: "PENDING", q: "" }, runSel: { claims: new Set(), sources: new Set() }, lastJobStates: {} };
let route = [];
let renderSeq = 0;
/* Out-of-order async renders must never overwrite a newer view: each view takes a ticket and checks it after every await. */
const begin = () => { const mine = ++renderSeq; return () => mine === renderSeq; };

function setNav(step) {
  document.querySelectorAll("#steps a").forEach((a) => {
    if (a.dataset.step === step) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
}
function errorBox(err) {
  return h("div", { class: "notice bad", role: "alert" }, h("strong", { text: "Something went wrong: " }), err.message || String(err));
}

/* ---------------------------------------------------------------- overview */
async function refreshOverview() {
  try {
    const o = await api("GET", "overview");
    const c = o.candidates || {};
    const el = $("#overview");
    put(el, 
      h("span", { text: `${o.sources} source${o.sources === 1 ? "" : "s"}` }),
      h("span", { text: `${c.PENDING || 0} candidate claims awaiting review` }),
      h("span", { text: `${o.claims} approved claim${o.claims === 1 ? "" : "s"}` }),
      o.latest_run
        ? h("span", {}, "latest run: ", badge(o.latest_run.freshness.state, o.latest_run.freshness.state === "CURRENT" ? "b-ok" : "b-warn"),
            " ", Object.entries(o.latest_run.counts).map(([k, v]) => `${v} ${k.toLowerCase().replace(/_/g, " ")}`).join(", "))
        : h("span", { class: "muted", text: "no runs yet" }));
    window.__caps = o.capabilities;
  } catch (e) { /* overview is best-effort */ }
}

/* ------------------------------------------------------------------ import */
async function viewImport(id) {
  const live = begin();
  setNav("import");
  const caps = (await api("GET", "capabilities"));
  const { items: sources } = await api("GET", "sources");
  const fileInput = h("input", { type: "file", id: "files", multiple: true, accept: ".pdf,.json,.md,.markdown,.sql" });
  const dialect = h("select", { id: "dialect" }, ["unresolved", "generic", "postgresql", "mysql", "sqlite"].map((d) => h("option", { value: d, text: d === "unresolved" ? "unresolved (flag for review)" : d })));
  const replaces = h("select", { id: "replaces" }, h("option", { value: "", text: "none - this is a new source" }),
    sources.filter((s) => !s.superseded_by).map((s) => h("option", { value: s.id, text: s.name })));
  const preview = h("div", { "aria-live": "polite" });
  const status = h("p", { role: "status" });
  const describe = () => {
    put(preview, ...[...fileInput.files].map((f) => {
      const ext = (f.name.split(".").pop() || "").toLowerCase();
      const type = { pdf: "pdf", json: "json", md: "markdown", markdown: "markdown", sql: "sql" }[ext];
      return h("p", {}, h("strong", { text: f.name }), ` - ${fmtBytes(f.size)} - detected by extension: ${type || "unsupported"}`,
        type === "pdf" && !caps.pdf.available ? badge("PDF parser not installed - will not be extracted", "b-warn") : null,
        f.size > caps.max_upload_bytes ? badge("exceeds upload limit", "b-bad") : null);
    }));
  };
  fileInput.addEventListener("change", describe);
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    for (const f of fileInput.files) {
      try {
        const r = await api("POST", "sources", { raw: true, body: await f.arrayBuffer(), headers: {
          "X-Filename": enc(f.name), "X-Dialect": dialect.value, "X-Replaces": replaces.value } });
        status.textContent = r.deduplicated ? `${f.name}: already imported (same bytes) - no new extraction.` : `${f.name}: admitted, extraction queued.`;
        announce(status.textContent);
      } catch (err) { put(status, errorBox(new Error(`${f.name}: ${err.message}`))); }
    }
    fileInput.value = ""; put(preview, ); pollJobs(true); viewImport();
  } },
    h("label", { for: "files", text: "Files (PDF, JSON, Markdown, SQL)" }), fileInput, preview,
    h("label", { for: "dialect", text: "SQL dialect (applies to .sql files; must be declared, never guessed)" }), dialect,
    h("label", { for: "replaces", text: "Replaces an earlier source (marks dependent results stale)" }), replaces,
    h("p", { class: "muted", text: `Extraction is local and deterministic: no network fetches, no code execution, no OCR or AI. Upload limit ${fmtBytes(caps.max_upload_bytes)}. PDF text extraction ${caps.pdf.available ? "available (" + caps.pdf.parser + " " + caps.pdf.version + ")" : "NOT available - install the pdf extra"}.` }),
    h("button", { class: "primary", type: "submit", text: "Import" }), status);

  const table = h("div", { class: "tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Source", "Type", "Size", "Extraction", "Candidates", ""].map((t) => h("th", { text: t })))),
    h("tbody", {}, sources.length ? sources.map((s) => {
      const a = s.attempt || {};
      const cc = s.candidate_counts || {};
      return h("tr", {},
        h("td", {}, h("a", { href: "#/import/" + enc(s.id), text: s.name }), s.superseded_by ? h("div", {}, badge("superseded", "b-warn")) : null),
        h("td", { text: s.format + (a.dialect ? " / " + a.dialect : "") }), h("td", { text: fmtBytes(s.size) }),
        h("td", {}, a.state ? [badge(a.state, a.state === "EXTRACTED" ? "b-ok" : a.state === "FAILED" ? "b-bad" : "b-info"), " ",
          a.coverage ? badge(a.coverage, COVERAGE[a.coverage] || "b-neutral") : null,
          a.error ? h("div", { class: "muted", text: a.error.code }) : null] : "-"),
        h("td", { text: `${cc.PENDING || 0} pending · ${cc.APPROVED || 0} approved · ${cc.REJECTED || 0} rejected` }),
        h("td", {}, h("a", { href: "#/review?source=" + enc(s.id), text: "Review claims" })));
    }) : h("tr", {}, h("td", { colSpan: 6, class: "muted", text: "Nothing imported yet. Choose a file above to begin." })))));
  if (!live()) return;
  put(main, h("h1", { text: "Import sources" }),
    h("div", { class: "card" }, form), h("div", { class: "card" }, h("h2", { text: "Sources" }), table),
    id ? h("div", { id: "detail" }) : null);
  if (id && live()) await sourceDetail(id);
}

async function sourceDetail(id) {
  const box = $("#detail");
  try {
    const s = await api("GET", "sources/" + enc(id));
    const t = await api("GET", "sources/" + enc(id) + "/text");
    const regions = s.coverage_regions || [];
    const sqlSource = s.format === "sql";
    const reext = h("div", { class: "row" });
    if (sqlSource) {
      const d = h("select", { "aria-label": "Dialect for re-extraction" }, ["generic", "postgresql", "mysql", "sqlite", "unresolved"].map((x) => h("option", { value: x, text: x })));
      reext.append(d, h("button", { text: "Re-extract with this dialect", onclick: async () => {
        await api("POST", "sources/" + enc(id) + "/reextract", { body: { dialect: d.value } }); pollJobs(true);
        announce("Re-extraction queued; previous extraction stays in history."); } }));
    }
    put(box, h("div", { class: "card" },
      h("h2", { id: "src-h", tabIndex: -1, text: "Source: " + s.name }),
      h("dl", { class: "kv" }, h("dt", { text: "Digest" }), h("dd", { class: "mono", text: s.digest }),
        h("dt", { text: "Format" }), h("dd", { text: s.format }),
        h("dt", { text: "Extraction" }), h("dd", { text: s.attempt ? `${s.attempt.state} - coverage ${s.attempt.coverage || "n/a"} - ${s.fact_count ?? 0} facts` : "none" }),
        s.manifest ? [h("dt", { text: "Parser" }), h("dd", { text: `${s.manifest.parser} ${s.manifest.parser_version} (adapter v${s.manifest.adapter_version})` })] : null),
      s.attempt && s.attempt.error ? h("div", { class: "notice bad", text: "Extraction failed: " + s.attempt.error.code }) : null, reext,
      h("h3", { text: "Coverage by region" }),
      h("p", { class: "muted", text: "Syntax coverage and claim interpretation are separate: a region can be parsed yet yield no claim." }),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Kind", "Bytes", "Status", "Interpretation", "Reasons"].map((x) => h("th", { text: x })))),
        h("tbody", {}, regions.filter((r) => r.kind !== "structure").map((r) => h("tr", {}, h("td", { text: r.kind }), h("td", { class: "mono", text: `${r.byte_start}-${r.byte_end}` }),
          h("td", {}, badge(r.status, COVERAGE[r.status] || "b-neutral")), h("td", { text: r.interpretation }), h("td", { text: (r.reason_codes || []).join(", ") })))))),
      (s.diagnostics || []).length ? h("details", {}, h("summary", { text: `Diagnostics (${s.diagnostics.length})` }), h("pre", { class: "src", text: JSON.stringify(s.diagnostics, null, 2) })) : null,
      (s.blocked_keys || []).length ? h("div", { class: "notice warn" }, h("strong", { text: "Withheld keys (duplicate definitions are never resolved silently): " }), s.blocked_keys.map((b) => b.key).join(", ")) : null,
      h("h3", { text: "Text rendition (inert - never rendered as markup)" }),
      t.truncated ? h("p", { class: "muted", text: "Showing the first part only." }) : null,
      h("pre", { class: "src", tabIndex: 0, "aria-label": "Source text", text: t.text || t.note })));
    $("#src-h").focus();
  } catch (e) { put(box, errorBox(e)); }
}

/* ------------------------------------------------------------------ review */
async function viewReview(selected, params) {
  const live = begin();
  setNav("review");
  if (params.get("source")) state.filters.source = params.get("source");
  const { items: sources } = await api("GET", "sources");
  const f = state.filters;
  const qs = new URLSearchParams({ limit: "200" });
  if (f.source) qs.set("source_id", f.source); if (f.status) qs.set("status", f.status); if (f.q) qs.set("q", f.q);
  const list = await api("GET", "candidates?" + qs);
  const left = h("div", {},
    h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); } },
      h("label", { class: "sr-only", for: "f-src", text: "Source" }),
      h("select", { id: "f-src", onchange: (e) => { f.source = e.target.value; viewReview(selected, new URLSearchParams()); } },
        h("option", { value: "", text: "All sources" }), sources.map((s) => h("option", { value: s.id, selected: s.id === f.source, text: s.name }))),
      h("label", { class: "sr-only", for: "f-st", text: "Status" }),
      h("select", { id: "f-st", onchange: (e) => { f.status = e.target.value; viewReview(selected, new URLSearchParams()); } },
        ["PENDING", "DEFERRED", "APPROVED", "REJECTED", ""].map((s) => h("option", { value: s, selected: s === f.status, text: s || "Any status" }))),
      h("label", { class: "sr-only", for: "f-q", text: "Search keys" }),
      h("input", { id: "f-q", type: "search", placeholder: "Search keys", value: f.q, onchange: (e) => { f.q = e.target.value; viewReview(selected, new URLSearchParams()); } })),
    h("p", { class: "muted", role: "status", text: `Showing ${list.items.length} of ${list.total} (denominator: current extractions only)` }),
    h("ul", { class: "list", "aria-label": "Candidate claims" }, list.items.map((c) => {
      const eligible = c.status === "PENDING" && !(c.qualifiers || []).length;
      return h("li", { "aria-current": c.id === selected ? "true" : null },
        h("input", { type: "checkbox", "aria-label": "Select for bulk approval: " + c.fact_key, disabled: !eligible, checked: state.candSel.has(c.id),
          title: eligible ? "" : "Only pending candidates without extraction qualifiers can be bulk-approved",
          onchange: (e) => { e.target.checked ? state.candSel.add(c.id) : state.candSel.delete(c.id); renderBulk(); } }),
        h("button", { class: "item", onclick: () => (location.hash = "#/review/" + enc(c.id)) },
          h("div", {}, h("strong", { class: "mono", text: c.fact_key })),
          h("div", { class: "muted" }, `${c.source_name} = ${fmtVal(c.predicate.arguments.expected)} `, badge(c.status, c.status === "APPROVED" ? "b-ok" : c.status === "REJECTED" ? "b-bad" : "b-neutral"),
            (c.qualifiers || []).length ? badge("qualifiers", "b-warn") : null)));
    })),
    h("div", { id: "bulk" }));
  const right = h("div", { id: "cand", class: "card" }, h("p", { class: "muted", text: list.items.length ? "Select a candidate to see its exact source evidence." : "No candidates match. Import a source or change the filters." }));
  if (!live()) return;
  put(main, h("h1", { text: "Review claims" }),
    h("div", { class: "notice", text: "A candidate is an unreviewed machine suggestion. Approving records what to test - it does not mean the statement is true. Editing changes the interpretation, never the source." }),
    h("div", { class: "grid2" }, left, right));
  renderBulk();
  if (selected && live()) await candidateDetail(selected);
}

function renderBulk() {
  const box = $("#bulk"); if (!box) return;
  put(box, );
  if (!state.candSel.size) return;
  box.append(h("div", { class: "card" }, h("p", { text: `${state.candSel.size} selected for bulk approval.` }),
    h("button", { class: "primary", text: "Preview bulk approval", onclick: async () => {
      const rows = []; for (const id of state.candSel) rows.push(await api("GET", "candidates/" + enc(id)));
      const dlg = h("dialog", { "aria-labelledby": "bulk-h" }, h("h2", { id: "bulk-h", text: "Confirm exact resulting claims" }),
        h("ul", {}, rows.map((c) => h("li", {}, h("code", { text: c.fact_key }), ` equals ${fmtVal(c.predicate.arguments.expected)} (from ${c.source_name})`))),
        h("div", { class: "row" }, h("button", { class: "primary", text: `Approve these ${rows.length}`, onclick: async () => {
          for (const c of rows) { try { await api("POST", "candidates/" + enc(c.id) + "/decisions", { body: { action: "APPROVE" }, headers: { "If-Match": c.etag } }); } catch (e) { announce("Skipped " + c.fact_key + ": " + e.message); } }
          state.candSel.clear(); dlg.close(); dlg.remove(); announce("Bulk approval recorded."); refreshOverview(); viewReview(null, new URLSearchParams()); } }),
          h("button", { text: "Cancel", onclick: () => { dlg.close(); dlg.remove(); } })));
      document.body.append(dlg); dlg.showModal(); } })));
}

async function candidateDetail(id) {
  const box = $("#cand");
  try {
    const c = await api("GET", "candidates/" + enc(id));
    const sp = c.span;
    const exp = c.predicate.arguments.expected;
    const form = decisionForm(c);
    put(box, 
      h("h2", { id: "cand-h", tabIndex: -1, text: c.fact_key }),
      h("p", {}, "Proposed claim: ", h("code", { text: c.predicate.predicate }), " - ", h("code", { text: c.fact_key }), " equals ", h("code", { text: fmtVal(exp) }), ` (${exp.kind})`, " ", badge(c.status, c.status === "APPROVED" ? "b-ok" : "b-neutral")),
      !c.current ? h("div", { class: "notice warn", text: "This candidate belongs to a superseded extraction and can no longer be decided." }) : null,
      (c.qualifiers || []).length ? h("div", { class: "notice warn" }, h("strong", { text: "Unresolved qualifiers: " }), c.qualifiers.join(", ")) : null,
      h("h3", { text: "Source evidence" }),
      sp ? [h("pre", { class: "src", tabIndex: 0, "aria-label": "Source evidence with highlighted token" }, sp.before, h("mark", { text: sp.text }), sp.after),
        h("p", { class: "muted" }, `Extraction: ${sp.extraction_method}. Extraction confidence: ${sp.extraction_confidence ?? "not provided by this extractor"} (extraction confidence is not confidence in truth).`),
        h("details", {}, h("summary", { text: "Exact locator" }), h("pre", { class: "src", text: JSON.stringify({ span: sp.span_id, artifact: sp.artifact_hash, rendition: sp.rendition_hash, locator: sp.locator }, null, 2) }))]
        : h("p", { class: "muted", text: "The span is unavailable." }),
      c.related.length ? [h("h3", { text: "Reconciliation proposals" }), h("ul", {}, c.related.map((r) => h("li", {}, badge(r.relation, r.relation === "DUPLICATE" ? "b-info" : "b-warn"), ` ${r.source_name} says ${fmtVal(r.value)}. `, h("span", { class: "muted", text: r.note }))))] : null,
      c.decisions.length ? [h("h3", { text: "Decision history (append-only)" }), h("ol", {}, c.decisions.map((d) => h("li", {}, `${d.action} - ${d.created_at}`, d.reason ? ` - reason: ${d.reason}` : "")))] : null,
      c.current ? form : null);
    $("#cand-h").focus();
  } catch (e) { put(box, errorBox(e)); }
}

function decisionForm(c) {
  const exp = c.predicate.arguments.expected;
  const status = h("div", { role: "status" });
  const action = h("select", { id: "d-action" }, [["APPROVE", "Approve as proposed"], ["APPROVE_WITH_EDIT", "Edit and approve"], ["REJECT", "Reject"], ["DEFER", "Defer"]].map(([v, t]) => h("option", { value: v, text: t })));
  const pred = h("select", { id: "e-pred" }, [["fact.equals", "equals a value"], ["fact.numeric_bound", "numeric bound"], ["fact.present", "is present"], ["interval.relation", "interval relation between two activities"]].map(([v, t]) => h("option", { value: v, text: t })));
  const subject = h("input", { id: "e-subj", value: c.predicate.subject, size: 40 });
  const kind = h("select", { id: "e-kind" }, ["string", "integer", "decimal", "boolean", "null"].map((k) => h("option", { value: k, selected: k === exp.kind, text: k })));
  const val = h("input", { id: "e-val", value: exp.kind === "null" ? "" : String(exp.value) });
  const op = h("select", { id: "e-op" }, ["lte", "lt", "gte", "gt"].map((o) => h("option", { value: o, text: o })));
  const bound = h("input", { id: "e-bound", value: exp.kind === "integer" || exp.kind === "decimal" ? exp.value : "" });
  const ileft = h("input", { id: "e-left" }), iright = h("input", { id: "e-right" }), icoord = h("input", { id: "e-coord", value: "seconds" });
  const irel = h("select", { id: "e-rel" }, ["precedes", "no_overlap", "overlaps"].map((r) => h("option", { value: r, text: r })));
  const app = h("select", { id: "e-app" }, h("option", { value: "APPLICABLE", text: "Applicable (default)" }), h("option", { value: "NOT_APPLICABLE", text: "Not applicable (requires rationale and scope)" }));
  const rat = h("input", { id: "e-rat", size: 50 }), scope = h("input", { id: "e-scope", size: 50 });
  const reason = h("textarea", { id: "d-reason" });
  const edit = h("fieldset", { hidden: true }, h("legend", { text: "Edit interpretation" }),
    h("label", { for: "e-pred", text: "Predicate" }), pred, h("label", { for: "e-subj", text: "Subject (fact key to look for in the evidence)" }), subject,
    h("div", { id: "e-eq" }, h("label", { for: "e-kind", text: "Expected value type" }), kind, h("label", { for: "e-val", text: "Expected value (exact; changes in scope, units, negation or dates need a reason)" }), val),
    h("div", { id: "e-nb", hidden: true }, h("label", { for: "e-op", text: "Operator" }), op, h("label", { for: "e-bound", text: "Bound (exact decimal)" }), bound),
    h("div", { id: "e-iv", hidden: true },
      h("p", { class: "muted", text: "Evidence must state <name>.start and <name>.end (optionally _min/_max for uncertainty) in one coordinate. Overlapping uncertainty gives insufficient evidence, never a guess." }),
      h("label", { for: "e-left", text: "Left activity name" }), ileft, h("label", { for: "e-right", text: "Right activity name" }), iright,
      h("label", { for: "e-rel", text: "Relation" }), irel, h("label", { for: "e-coord", text: "Shared coordinate (stated assumption, e.g. seconds since one clock)" }), icoord),
    h("label", { for: "e-app", text: "Applicability" }), app,
    h("div", { id: "e-na", hidden: true }, h("label", { for: "e-rat", text: "Rationale" }), rat, h("label", { for: "e-scope", text: "Scope of the decision" }), scope));
  const sync = () => {
    edit.hidden = action.value !== "APPROVE_WITH_EDIT";
    $("#e-eq", edit).hidden = pred.value !== "fact.equals"; $("#e-nb", edit).hidden = pred.value !== "fact.numeric_bound";
    $("#e-iv", edit).hidden = pred.value !== "interval.relation";
    $("#e-na", edit).hidden = app.value !== "NOT_APPLICABLE";
  };
  [action, pred, app].forEach((e) => e.addEventListener("change", sync));
  const submit = h("button", { class: "primary", type: "submit", text: "Record decision" });
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault(); submit.disabled = true; put(status, );
    const body = { action: action.value, reason: reason.value };
    if (action.value === "APPROVE_WITH_EDIT") {
      const args = {};
      if (pred.value === "fact.equals") {
        let v = val.value; const k = kind.value;
        if (k === "boolean") v = v.trim() === "true"; else if (k === "null") v = null;
        args.expected = { kind: k, value: v };
      } else if (pred.value === "fact.numeric_bound") {
        const t = bound.value.trim(); args.op = op.value; args.bound = { kind: /^-?\d+$/.test(t) ? "integer" : "decimal", value: t };
      }
      if (pred.value === "interval.relation") Object.assign(args, { left: ileft.value, right: iright.value, relation: irel.value, coord: icoord.value });
      if (app.value === "NOT_APPLICABLE") args.applicability = { decision: "NOT_APPLICABLE", rationale: rat.value, scope: scope.value };
      body.edit = { predicate: pred.value, subject: subject.value, arguments: args };
    }
    try {
      const out = await api("POST", "candidates/" + enc(c.id) + "/decisions", { body, headers: { "If-Match": c.etag, "Idempotency-Key": crypto.randomUUID() } });
      announce(`Decision recorded: ${out.status}.`); refreshOverview();
      await viewReview(c.id, new URLSearchParams());
    } catch (err) {
      submit.disabled = false;
      if (err.status === 412) put(status, h("div", { class: "notice warn", role: "alert" }, "This candidate was changed elsewhere. Your draft is kept below. ",
        h("button", { type: "button", text: "Reload candidate", onclick: () => candidateDetail(c.id) })));
      else put(status, errorBox(err));
    }
  } },
    h("h3", { text: "Your decision" }),
    h("label", { for: "d-action", text: "Action" }), action, edit,
    h("label", { for: "d-reason", text: "Reason (required to reject or edit)" }), reason, h("div", { class: "row", style: "margin-top:.6rem" }, submit), status);
  return form;
}

/* --------------------------------------------------------------------- run */
async function viewRun(runId) {
  const live = begin();
  setNav("run");
  const [claims, sources, runs] = await Promise.all([api("GET", "claims"), api("GET", "sources"), api("GET", "runs")]);
  const cur = claims.items.filter((c) => c.status === "CURRENT");
  const ready = sources.items.filter((s) => s.attempt && s.attempt.state === "EXTRACTED" && !s.superseded_by);
  const preview = h("div", { role: "status", "aria-live": "polite" });
  const startBtn = h("button", { class: "primary", text: "Start run" });
  const upd = () => {
    const nc = state.runSel.claims.size, ns = state.runSel.sources.size;
    const warnings = [];
    for (const cid of state.runSel.claims) {
      const c = cur.find((x) => x.id === cid);
      if (c && ns && [...state.runSel.sources].every((sid) => sid === c.origin_source_id)) warnings.push(`${c.subject}: the only selected evidence is the document it came from (no independent evidence).`);
    }
    put(preview, h("p", { text: `${nc} claim${nc === 1 ? "" : "s"} × ${ns} evidence source${ns === 1 ? "" : "s"}.` }),
      ...warnings.map((w) => h("div", { class: "notice warn", text: w })));
    startBtn.disabled = !(nc && ns);
  };
  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    try {
      const r = await api("POST", "runs", { body: { claim_revision_ids: [...state.runSel.claims], evidence_source_ids: [...state.runSel.sources] }, headers: { "Idempotency-Key": crypto.randomUUID() } });
      announce("Run started."); location.hash = "#/run/" + enc(r.run_id); pollJobs(true);
    } catch (e) { preview.append(errorBox(e)); startBtn.disabled = false; }
  });
  const cbox = (set, id, label, extra) => h("li", {}, h("label", { class: "inline" }, h("input", { type: "checkbox", checked: set.has(id), onchange: (e) => { e.target.checked ? set.add(id) : set.delete(id); upd(); } }), label), extra);
  if (!live()) return;
  put(main, h("h1", { text: "Run checks" }),
    h("div", { class: "grid2" },
      h("div", { class: "card" }, h("h2", { text: "Claims to test" }),
        cur.length ? h("ul", { class: "plain", style: "list-style:none;padding:0" }, cur.map((c) => cbox(state.runSel.claims, c.id, h("span", {}, h("code", { text: c.subject }), ` ${c.predicate} ${c.arguments.expected ? fmtVal(c.arguments.expected) : c.arguments.op ? c.arguments.op + " " + c.arguments.bound.value : ""}`)))) :
          h("p", { class: "muted" }, "No approved claims yet. ", h("a", { href: "#/review", text: "Review candidates" }), "."),
        cur.length ? h("button", { text: "Select all", onclick: () => { cur.forEach((c) => state.runSel.claims.add(c.id)); viewRun(runId); } }) : null),
      h("div", { class: "card" }, h("h2", { text: "Evidence to test against" }),
        ready.length ? h("ul", { style: "list-style:none;padding:0" }, ready.map((s) => cbox(state.runSel.sources, s.id, `${s.name} (${s.format}, coverage ${s.attempt.coverage})`))) : h("p", { class: "muted", text: "No completed extractions yet." }))),
    h("div", { class: "card" }, preview, startBtn,
      h("p", { class: "muted", text: "A run records verdicts for these exact claim revisions and this evidence snapshot. Closing this tab does not cancel it." })),
    h("div", { id: "rundetail" }),
    h("h2", { text: "Earlier runs" }),
    h("ul", {}, runs.items.map((r) => h("li", {}, h("a", { href: "#/run/" + enc(r.id), text: r.created_at }), " ", badge(r.job ? r.job.state : r.state, r.state === "SUCCEEDED" ? "b-ok" : r.state === "FAILED" ? "b-bad" : "b-info"), " ", r.state === "SUCCEEDED" ? h("a", { href: "#/findings/" + enc(r.id), text: "findings" }) : null))));
  upd();
  if (runId && live()) await runDetail(runId);
}

async function runDetail(runId) {
  const box = $("#rundetail"); if (!box) return;
  const r = await api("GET", "runs/" + enc(runId));
  const j = r.job || {};
  const active = ["QUEUED", "RUNNING", "CANCELLING"].includes(j.state);
  put(box, h("div", { class: "card" }, h("h2", { text: "Run progress" }),
    h("p", {}, badge(j.state || r.state, j.state === "SUCCEEDED" ? "b-ok" : j.state === "FAILED" ? "b-bad" : "b-info"), ` stage: ${j.stage || "-"} `,
      j.total != null ? h("span", {}, h("progress", { value: j.done || 0, max: j.total || 1, "aria-label": "Run progress" }), ` ${j.done || 0} of ${j.total}`) : j.done != null ? ` ${j.done} done, total unknown` : null),
    j.error ? h("div", { class: "notice bad", role: "alert" }, `${j.error.code}: ${j.error.message}`) : null,
    active ? h("button", { class: "danger", text: "Cancel run", onclick: async () => { await api("POST", "jobs/" + enc(j.id) + "/cancel", { body: {} }); announce("Cancellation requested."); pollJobs(true); } }) : null,
    r.state === "SUCCEEDED" ? h("p", {}, h("a", { href: "#/findings/" + enc(r.id), text: "Open findings" })) : null,
    r.state === "FAILED" && r.errors.length ? h("div", { class: "notice bad" }, h("strong", { text: "Execution errors (not verdicts):" }), h("ul", {}, r.errors.map((e) => h("li", { text: `${e.code || e.error?.code}: ${e.message || e.error?.message}` })))) : null));
}

/* ---------------------------------------------------------------- findings */
async function viewFindings(runId, claimId) {
  const live = begin();
  setNav("findings");
  const runs = (await api("GET", "runs")).items.filter((r) => r.state === "SUCCEEDED");
  if (!live()) return;
  if (!runs.length) { put(main, h("h1", { text: "Findings" }), h("div", { class: "card" }, h("p", { class: "muted" }, "No completed runs. ", h("a", { href: "#/run", text: "Run checks" }), "."))); return; }
  const id = runId || runs[0].id;
  const run = await api("GET", "runs/" + enc(id));
  const counts = {}; run.findings.forEach((f) => (counts[f.verdict] = (counts[f.verdict] || 0) + 1));
  const sel = h("select", { "aria-label": "Run", onchange: (e) => (location.hash = "#/findings/" + enc(e.target.value)) }, runs.map((r) => h("option", { value: r.id, selected: r.id === id, text: r.created_at })));
  const filter = h("select", { "aria-label": "Filter verdict", onchange: () => draw() }, h("option", { value: "", text: "All verdicts" }), Object.keys(VERDICTS).map((v) => h("option", { value: v, text: VERDICTS[v][2] })));
  const tbody = h("tbody");
  const detail = h("div", { id: "fdetail" });
  const draw = () => {
    put(tbody, ...run.findings.filter((f) => !filter.value || f.verdict === filter.value).map((f) =>
      h("tr", { "aria-current": f.claim_id === claimId ? "true" : null }, h("td", {}, verdictBadge(f.verdict)), h("td", {}, h("code", { text: f.claim ? f.claim.subject : "?" })),
        h("td", { text: f.claim ? f.claim.predicate : "" }), h("td", { text: f.reason_code }),
        h("td", {}, h("a", { href: `#/findings/${enc(id)}/${enc(f.claim_id)}`, text: "Inspect" })))));
  };
  if (!live()) return;
  put(main, h("h1", { text: "Findings" }),
    h("div", { class: "card" }, h("div", { class: "row" }, h("label", { class: "inline", text: "Run " }, sel),
      badge(run.freshness.state, run.freshness.state === "CURRENT" ? "b-ok" : "b-warn")),
      run.freshness.state === "STALE" ? h("div", { class: "notice warn" }, h("strong", { text: "This result is stale: " }), run.freshness.causes.map((c) => c.kind.replace(/_/g, " ")).join(", "), ". The original run is still a valid statement about its original inputs. ", h("a", { href: "#/run", text: "Rerun" }), ".") : null,
      h("h2", { text: "Verdict counts (all six shown)" }),
      h("div", { class: "row" }, Object.keys(VERDICTS).map((v) => h("span", {}, verdictBadge(v), ` ${counts[v] || 0}`))),
      run.errors.length ? h("div", { class: "notice bad" }, h("strong", { text: "Execution errors (separate from verdicts): " }), run.errors.length) : null),
    h("div", { class: "card" }, h("div", { class: "row" }, h("label", { class: "inline" }, "Show ", filter)),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Verdict", "Subject", "Predicate", "Reason", ""].map((t) => h("th", { text: t })))), tbody))), detail);
  draw();
  if (claimId) {
    const f = run.findings.find((x) => x.claim_id === claimId);
    if (f) findingDetail(detail, f, run);
  }
}

function findingDetail(box, f, run) {
  const w = f.witness || {};
  const ev = f.evidence_view || [];
  put(box, h("div", { class: "card" }, h("h2", { id: "f-h", tabIndex: -1, text: "Finding: " + (f.claim ? f.claim.subject : f.claim_id) }),
    h("p", {}, verdictBadge(f.verdict), " ", h("code", { text: f.reason_code })),
    w.expected ? h("p", {}, "Claim expects ", h("code", { text: fmtVal(w.expected) })) : null,
    w.op ? h("p", {}, "Claim expects value ", h("code", { text: `${w.op} ${w.bound.value}` })) : null,
    f.verdict === "NOT_APPLICABLE" ? h("div", { class: "notice" }, `Reviewer decision - rationale: ${w.rationale}; scope: ${w.scope}`) : null,
    f.verdict === "INSUFFICIENT_EVIDENCE" ? h("div", { class: "notice warn", text: "Not stated is not the same as false: no usable evidence was found for this claim." }) : null,
    f.verdict === "HUMAN_REVIEW_REQUIRED" ? h("div", { class: "notice", text: "The machine could not decide. A human determination is needed; this is not a machine-proven finding." }) : null,
    ev.length ? [h("h3", { text: "Evidence examined" }), ev.map((e) => h("div", {}, h("p", {}, h("strong", { text: e.source_name }), " observed ", h("code", { text: fmtVal(e.value) }), (e.qualifiers || []).length ? " " : null, (e.qualifiers || []).map((q) => badge(q, "b-warn"))),
      h("pre", { class: "src", tabIndex: 0 }, e.before, h("mark", { text: e.token }), e.after)))] : null,
    (w.unusable_sources || []).length ? h("div", { class: "notice warn", text: "Some selected evidence could not be read: " + w.unusable_sources.map((u) => u.coverage).join(", ") }) : null,
    h("details", {}, h("summary", { text: "Advanced: rule, identities, raw witness" }),
      h("dl", { class: "kv" }, h("dt", { text: "Rule" }), h("dd", { text: `${f.rule_id} v${f.rule_version}` }), h("dt", { text: "Finding id" }), h("dd", { class: "mono", text: f.id }),
        h("dt", { text: "Claim id" }), h("dd", { class: "mono", text: f.claim_id }), h("dt", { text: "Bundle id" }), h("dd", { class: "mono", text: run.bundle_id })),
      h("pre", { class: "src", text: JSON.stringify(f.witness, null, 2) })),
    h("p", { class: "muted", text: "Findings never become a compliance badge. Remediating means changing the source or claim and re-running; the old finding stays inspectable." })));
  $("#f-h").focus();
}

/* ------------------------------------------------------------------ export */
async function viewExport() {
  const live = begin();
  setNav("export");
  const runs = (await api("GET", "runs")).items.filter((r) => r.state === "SUCCEEDED");
  if (!live()) return;
  if (!runs.length) { put(main, h("h1", { text: "Export" }), h("div", { class: "card" }, h("p", { class: "muted" }, "Complete a run first. ", h("a", { href: "#/run", text: "Run checks" }), "."))); return; }
  const sel = h("select", { id: "x-run" }, runs.map((r) => h("option", { value: r.id, text: `${r.created_at}  (${r.freshness.state})` })));
  const out = h("div", { role: "status", "aria-live": "polite" });
  const dl = h("button", { class: "primary", text: "Download package (.zip)", onclick: async () => {
    try {
      const blob = await api("GET", "runs/" + enc(sel.value) + "/export", { blob: true });
      const a = h("a", { href: URL.createObjectURL(blob), download: "verification-package.zip" }); document.body.append(a); a.click(); a.remove();
      put(out, h("div", { class: "notice ok", text: "Package downloaded. It was generated from the pinned snapshot of that run." }));
    } catch (e) { put(out, errorBox(e)); }
  } });
  const ver = h("button", { text: "Verify with the independent checker", onclick: async () => {
    ver.disabled = true;
    try {
      const r = await api("POST", "runs/" + enc(sel.value) + "/verify", { body: {} });
      const bad = r.checks.filter((c) => !c.ok); const badF = r.findings.filter((f) => !f.accepted);
      put(out, h("div", { class: "notice " + (r.status === "VERIFIED" ? "ok" : "bad"), role: "alert" }, h("strong", { text: r.status }), ` - ${r.checks.length} package checks, ${r.findings.length} findings re-derived. ${r.note}`),
        bad.concat(badF).length ? h("pre", { class: "src", text: JSON.stringify({ failed_checks: bad, rejected_findings: badF }, null, 2) }) : null,
        r.unsupported.length ? h("div", { class: "notice warn", text: `${r.unsupported.length} finding(s) use rule families this checker does not cover.` }) : null,
        h("details", {}, h("summary", { text: "Checker limitations" }), h("ul", {}, r.limitations.map((l) => h("li", { text: l })))));
    } catch (e) { put(out, errorBox(e)); }
    ver.disabled = false;
  } });
  if (!live()) return;
  put(main, h("h1", { text: "Export" }),
    h("div", { class: "card" }, h("label", { for: "x-run", text: "Run to export" }), sel,
      h("div", { class: "notice warn", text: "The package includes the original source files and their text renditions. Excluding confidential originals would reduce independent verifiability; this version always includes them." }),
      h("p", { class: "muted", text: "Contains: bundle.json, per-artifact files, extraction records (facts + coverage), claim provenance with review decisions, result.json, a human report and a hash manifest. Hashes prove byte consistency, not authorship or truth." }),
      h("div", { class: "row" }, dl, ver), out));
}

/* -------------------------------------------------------------- job tray */
let pollTimer = null;
async function pollJobs(force) {
  clearTimeout(pollTimer);
  let active = false;
  try {
    const { items } = await api("GET", "jobs");
    const recent = items.filter((j) => ['QUEUED', 'RUNNING', 'CANCELLING'].includes(j.state)).concat(items.filter((j) => !['QUEUED', 'RUNNING', 'CANCELLING'].includes(j.state)).slice(0, 2)).slice(0, 4);
    active = recent.some((j) => ["QUEUED", "RUNNING", "CANCELLING"].includes(j.state));
    put($("#tray"), ...(recent.length ? [h("strong", { text: "Jobs " }), h("span", { class: "muted", text: "(closing this tab does not cancel them)" })] : []),
      ...recent.map((j) => h("div", { class: "row" }, badge(j.state, j.state === "SUCCEEDED" ? "b-ok" : j.state === "FAILED" ? "b-bad" : j.state === "CANCELLED" ? "b-neutral" : "b-info"),
        h("span", { text: `${j.kind} - ${j.stage || ""}` }), j.total != null ? h("span", { class: "muted", text: `${j.done || 0}/${j.total}` }) : j.done != null ? h("span", { class: "muted", text: `${j.done} done, total unknown` }) : null,
        j.error ? h("span", { class: "muted", text: j.error.code }) : null,
        ["QUEUED", "RUNNING"].includes(j.state) ? h("button", { text: "Cancel", "aria-label": `Cancel ${j.kind} job`, onclick: async () => { await api("POST", "jobs/" + enc(j.id) + "/cancel", { body: {} }); pollJobs(true); } }) : null)));
    let changed = false;
    for (const j of items) {
      if (state.lastJobStates[j.id] && state.lastJobStates[j.id] !== j.state && ["SUCCEEDED", "FAILED", "CANCELLED"].includes(j.state)) {
        changed = true; announce(`${j.kind} job ${j.state.toLowerCase()}.`);
      }
      state.lastJobStates[j.id] = j.state;
    }
    if (changed) { refreshOverview(); if (["import", "run"].includes(route[0]) && !document.activeElement.closest("form")) render(true); }
    else if (route[0] === "run" && route[1]) runDetail(decodeURIComponent(route[1])).catch(() => {});
  } catch (e) { put($("#tray"), h("span", { class: "muted", text: "Reconnecting… last known state shown." })); active = true; }
  pollTimer = setTimeout(() => pollJobs(), active ? 700 : 4000);
}

/* ------------------------------------------------------------------ router */
async function render(soft) {
  const hash = location.hash.replace(/^#\/?/, "");
  const [path, query] = hash.split("?");
  route = path.split("/").filter(Boolean);
  const params = new URLSearchParams(query || "");
  const dec = (s) => (s ? decodeURIComponent(s) : undefined);
  try {
    switch (route[0] || "import") {
      case "import": await viewImport(dec(route[1])); break;
      case "review": await viewReview(dec(route[1]), params); break;
      case "run": await viewRun(dec(route[1])); break;
      case "findings": await viewFindings(dec(route[1]), dec(route[2])); break;
      case "export": await viewExport(); break;
      default: location.hash = "#/import";
    }
  } catch (e) { put(main, errorBox(e)); }
  if (!soft) { document.title = "Verification Workbench"; if (!document.activeElement || document.activeElement === document.body) main.focus({ preventScroll: true }); }
}
window.addEventListener("hashchange", () => render(false));
refreshOverview(); pollJobs(true); render(true); /* initial load keeps focus at the document start so the skip link is the first Tab stop */
