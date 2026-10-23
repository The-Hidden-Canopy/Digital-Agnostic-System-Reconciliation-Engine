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
async function viewImport(id, soft = false) {
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
      const a = s.latest_attempt || s.attempt || {};
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
  // A background refresh may have started before the user selected files or began a form.
  // Recheck after the asynchronous requests so that refresh cannot erase their input.
  if (soft && (($('#files') && $('#files').files.length) || document.activeElement.closest('form'))) return;
  put(main, h("h1", { text: "Import sources" }),
    caps.parser_isolation && caps.parser_isolation.mode === "portable" ? h("div", { class: "notice warn", role: "alert",
      text: "Portable parsing mode: OS isolation is disabled. Import trusted documents only." }) : null,
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
    const latest = s.latest_attempt || s.attempt;
    const reext = h("div", { class: "row" });
    const reextStatus = h("div", { role: "status" });
    const retry = async (button, body) => {
      button.disabled = true;
      put(reextStatus);
      try {
        await api("POST", "sources/" + enc(id) + "/reextract", { body });
        pollJobs(true);
        announce("Re-extraction queued; previous extraction stays in history.");
      } catch (err) {
        put(reextStatus, errorBox(err));
      } finally {
        button.disabled = false;
      }
    };
    if (sqlSource) {
      const d = h("select", { "aria-label": "Dialect for re-extraction" }, ["generic", "postgresql", "mysql", "sqlite", "unresolved"].map((x) => h("option", { value: x, text: x, selected: x === (latest && latest.dialect) })));
      const button = h("button", { text: latest && latest.state === "FAILED" ? "Retry extraction" : "Re-extract with this dialect", onclick: () => retry(button, { dialect: d.value }) });
      reext.append(d, button);
    } else {
      const button = h("button", { text: latest && latest.state === "FAILED" ? "Retry extraction" : "Re-extract", onclick: () => retry(button, {}) });
      reext.append(button);
    }
    put(box, h("div", { class: "card" },
      h("h2", { id: "src-h", tabIndex: -1, text: "Source: " + s.name }),
      h("dl", { class: "kv" }, h("dt", { text: "Digest" }), h("dd", { class: "mono", text: s.digest }),
        h("dt", { text: "Format" }), h("dd", { text: s.format }),
        h("dt", { text: "Extraction" }), h("dd", { text: latest ? `${latest.state} - coverage ${latest.coverage || "n/a"} - ${s.fact_count ?? 0} facts` : "none" }),
        s.manifest ? [h("dt", { text: "Parser" }), h("dd", { text: `${s.manifest.parser} ${s.manifest.parser_version} (adapter v${s.manifest.adapter_version})` })] : null),
      latest && latest.error ? h("div", { class: "notice bad", text: "Extraction failed: " + latest.error.code }) : null, reext, reextStatus,
      h("h3", { text: "Coverage by region" }),
      h("p", { class: "muted", text: "Syntax coverage and claim interpretation are separate: a region can be parsed yet yield no claim." }),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Kind", "Bytes", "Status", "Interpretation", "Reasons"].map((x) => h("th", { text: x })))),
        h("tbody", {}, regions.filter((r) => r.kind !== "structure").map((r) => h("tr", {}, h("td", { text: r.kind }), h("td", { class: "mono", text: `${r.byte_start}-${r.byte_end}` }),
          h("td", {}, badge(r.status, COVERAGE[r.status] || "b-neutral")), h("td", { text: r.interpretation }), h("td", { text: (r.reason_codes || []).join(", ") })))))),
      (s.diagnostics || []).length ? h("details", {}, h("summary", { text: `Diagnostics (${s.diagnostics.length})` }), h("pre", { class: "src", text: JSON.stringify(s.diagnostics, null, 2) })) : null,
      (s.blocked_keys || []).length ? h("div", { class: "notice warn" }, h("strong", { text: "Withheld keys (duplicate definitions are never resolved silently): " }), s.blocked_keys.map((b) => b.key).join(", ")) : null,
      h("h3", { text: "Text rendition (inert - never rendered as markup)" }),
      t.truncated ? h("p", { class: "muted", text: "Showing the first part only." }) : null,
      h("pre", { class: "src", role: "region", tabIndex: 0, "aria-label": "Source text", text: t.text || t.note }),
      s.attempt && s.attempt.state === "EXTRACTED" && !s.superseded_by && t.text ? authorForm(s, t.text) : null));
    $("#src-h").focus();
  } catch (e) { put(box, errorBox(e)); }
}

function authorForm(source, text) {
  const passage = h("textarea", { id: "a-source", readOnly: true, rows: 8, value: text });
  const selected = h("blockquote", { id: "a-selected", text: "Select a passage above, then use it below." });
  let span = null;
  const subject = h("input", { id: "a-subject", required: true, maxLength: 500 });
  const kind = h("select", { id: "a-kind" }, ["string", "integer", "decimal", "boolean", "null"].map((k) => h("option", { value: k, text: k })));
  const value = h("input", { id: "a-value" });
  const reason = h("textarea", { id: "a-reason", required: true, maxLength: 4000 });
  const status = h("div", { role: "status" });
  const submit = h("button", { type: "submit", text: "Create candidate for review", disabled: true });
  const useSelection = h("button", { type: "button", text: "Use selected passage", onclick: () => {
    const start = passage.selectionStart, end = passage.selectionEnd;
    const bytes = new TextEncoder();
    const snippet = text.slice(start, end);
    if (!snippet.trim() || bytes.encode(snippet).length > 16000) {
      span = null; submit.disabled = true; put(status, errorBox(new Error("Select a nonempty passage of at most 16000 bytes."))); return;
    }
    span = { byte_start: bytes.encode(text.slice(0, start)).length, byte_end: bytes.encode(text.slice(0, end)).length };
    selected.textContent = snippet; submit.disabled = false; put(status, );
  } });
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault(); if (!span) return; submit.disabled = true;
    try {
      let v = value.value;
      if (kind.value === "boolean") {
        if (!["true", "false"].includes(v.trim())) throw new Error("Boolean values must be true or false.");
        v = v.trim() === "true";
      } else if (kind.value === "null") v = null;
      const result = await api("POST", "candidates", { body: { source_id: source.id, attempt_id: source.attempt.id, ...span,
        interpretation: { predicate: "fact.equals", subject: subject.value, arguments: { expected: { kind: kind.value, value: v } } },
        reason: reason.value }, headers: { "Idempotency-Key": crypto.randomUUID() } });
      state.filters = { source: "", status: "PENDING", q: "" };
      location.hash = "#/review/" + enc(result.id); announce("Human interpretation created. Review and approval are still required.");
    } catch (err) { put(status, errorBox(err)); submit.disabled = false; }
  } }, h("label", { for: "a-source", text: "Select the source passage (read only)" }), passage, useSelection, selected,
    h("label", { for: "a-subject", text: "Fact key to check in the evidence" }), subject,
    h("label", { for: "a-kind", text: "Expected value type" }), kind,
    h("label", { for: "a-value", text: "Expected value" }), value,
    h("label", { for: "a-reason", text: "Why does this passage express the claim?" }), reason, submit, status);
  return h("details", { class: "card" }, h("summary", { text: "Create a claim from this text" }),
    h("p", { class: "notice", text: "Your interpretation stays separate from extracted facts. Approval records what to test; the application does not prove that the passage entails your interpretation." }), form);
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
    h("div", { class: "notice", text: "A candidate is an unreviewed suggestion or human interpretation. Approving records what to test - it does not mean the statement is true. Editing changes the interpretation, never the source." }),
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
      h("p", {}, "Proposed claim: ", h("code", { text: c.predicate.predicate }), " - ", h("code", { text: c.fact_key }),
        exp ? [" equals ", h("code", { text: fmtVal(exp) }), ` (${exp.kind})`] : h("code", { text: JSON.stringify(c.predicate.arguments) }),
        " ", badge(c.status, c.status === "APPROVED" ? "b-ok" : "b-neutral")),
      c.authored ? h("div", { class: "notice warn" }, h("strong", { text: "Human interpretation. " }),
        c.author_reason, " Semantic entailment from this passage is not machine-verified.") : null,
      !c.current ? h("div", { class: "notice warn", text: "This candidate belongs to a superseded extraction and can no longer be decided." }) : null,
      (c.qualifiers || []).length ? h("div", { class: "notice warn" }, h("strong", { text: "Unresolved qualifiers: " }), c.qualifiers.join(", ")) : null,
      h("h3", { text: "Source evidence" }),
      sp ? [h("pre", { class: "src", role: "region", tabIndex: 0, "aria-label": "Source evidence with highlighted token" }, sp.before, h("mark", { text: sp.text }), sp.after),
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
  const exp = c.predicate.arguments.expected || { kind: "string", value: "" };
  const status = h("div", { role: "status" });
  const action = h("select", { id: "d-action" }, [["APPROVE", "Approve as proposed"], ["APPROVE_WITH_EDIT", "Edit and approve"], ["REJECT", "Reject"], ["DEFER", "Defer"]].map(([v, t]) => h("option", { value: v, text: t })));
  const pred = h("select", { id: "e-pred" }, [["fact.equals", "equals a value"], ["fact.numeric_bound", "numeric bound"], ["fact.present", "is present"], ["interval.relation", "interval relation between two activities"], ["composition.valid", "a pipeline description composes (tags, generations, units)"]].map(([v, t]) => h("option", { value: v, text: t })));
  const subject = h("input", { id: "e-subj", value: c.predicate.subject, size: 40 });
  const kind = h("select", { id: "e-kind" }, ["string", "integer", "decimal", "boolean", "null"].map((k) => h("option", { value: k, selected: k === exp.kind, text: k })));
  const val = h("input", { id: "e-val", value: exp.kind === "null" ? "" : String(exp.value) });
  const op = h("select", { id: "e-op" }, ["lte", "lt", "gte", "gt"].map((o) => h("option", { value: o, text: o })));
  const bound = h("input", { id: "e-bound", value: exp.kind === "integer" || exp.kind === "decimal" ? exp.value : "" });
  const ileft = h("input", { id: "e-left" }), iright = h("input", { id: "e-right" }), icoord = h("input", { id: "e-coord", value: "seconds" });
  const irel = h("select", { id: "e-rel" }, ["precedes", "no_overlap", "overlaps"].map((r) => h("option", { value: r, text: r })));
  const cptarget = h("input", { id: "e-target", placeholder: "activity id (optional: only its ancestors are evaluated)" });
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
    h("div", { id: "e-cp", hidden: true },
      h("p", { class: "muted", text: "Set the subject above to the key that holds the pipeline object in the evidence document (e.g. pipeline). Overall support needs every obligation; assumptions stay visible." }),
      h("label", { for: "e-target", text: "Target activity (optional)" }), cptarget),
    h("label", { for: "e-app", text: "Applicability" }), app,
    h("div", { id: "e-na", hidden: true }, h("label", { for: "e-rat", text: "Rationale" }), rat, h("label", { for: "e-scope", text: "Scope of the decision" }), scope));
  const sync = () => {
    edit.hidden = action.value !== "APPROVE_WITH_EDIT";
    $("#e-eq", edit).hidden = pred.value !== "fact.equals"; $("#e-nb", edit).hidden = pred.value !== "fact.numeric_bound";
    $("#e-iv", edit).hidden = pred.value !== "interval.relation";
    $("#e-cp", edit).hidden = pred.value !== "composition.valid";
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
      if (pred.value === "composition.valid" && cptarget.value.trim()) args.target = cptarget.value.trim();
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
  const [claims, sources, runs, packs, purposeList] = await Promise.all([api("GET", "claims"), api("GET", "sources"), api("GET", "runs"), api("GET", "packs"), api("GET", "purposes")]);
  const purposes = purposeList.items;
  const cur = claims.items.filter((c) => c.status === "CURRENT");
  const ready = sources.items.filter((s) => s.attempt && s.attempt.state === "EXTRACTED" && !s.superseded_by);
  const preview = h("div", { role: "status", "aria-live": "polite" });
  const startBtn = h("button", { class: "primary", text: "Start run" });
  const packBoxes = new Map();
  const ctxArea = h("textarea", { id: "ctx", rows: 6, spellcheck: false, placeholder: "service.kind = web\nservice.accepts_uploads = true\ndata.is_sensitive = null   (null = explicitly none; leave out what you do not know)" });
  const packHint = h("p", { class: "muted" });
  const packPanel = h("details", { class: "card" }, h("summary", { text: "Risk packs (optional)" }),
    h("p", { class: "muted", text: "A pack maps stated context to obligations and the evidence they need. It is data, not code. Results are evidence assessments under the pack and the facts you state - not certification. Facts you leave out stay UNKNOWN; they are never assumed false." }),
    packs.items.length ? packs.items.map((p) => { const cb = h("input", { type: "checkbox", onchange: () => { packHint.textContent = "Context facts these packs look at: " + [...new Set([...packBoxes].filter(([, c]) => c.checked).flatMap(([id]) => packs.items.find((x) => x.pack_id === id).applicability_facts))].join(", "); } });
      packBoxes.set(p.pack_id, cb);
      return h("div", {}, h("label", { class: "inline" }, cb, h("span", {}, h("strong", { text: p.title }), ` v${p.version}, ${p.obligations} obligations`)), h("div", { class: "muted mono", text: p.digest })); })
      : h("p", { class: "muted", text: "No packs installed." }),
    packHint, h("label", { for: "ctx", text: "Context facts (one per line: key = value; true, false, null, integers or text)" }), ctxArea);
  const purposeBoxes = new Map(), roleInputs = new Map();
  const purposePanel = h("details", { class: "card" }, h("summary", { text: "Purpose (optional): is this evidence enough for what you want to do?" }),
    h("p", { class: "muted", text: "Completeness is relative to a purpose. A package can be enough to inspect something and not enough to resume from it. Bind each role by naming the fact key that states it - roles are never guessed from file names." }),
    purposes.map((pr) => {
      const inputs = pr.roles.map((r) => { const inp = h("input", { id: `role-${pr.profile_id}-${r.role}`, size: 38, placeholder: "fact key, e.g. checkpoint.allocator_next (comma-separate several)" }); roleInputs.set(pr.profile_id + "/" + r.role, inp);
        return h("div", {}, h("label", { for: inp.id, text: `${r.role}${r.optional ? " (optional)" : ""} - ${r.value_kind}, ${r.min}..${r.max === null ? "any" : r.max}` }), r.description ? h("div", { class: "muted", text: r.description }) : null, inp); });
      const box = h("div", { hidden: true, style: "margin:.4rem 0 .8rem 1.4rem" }, inputs,
        pr.coherence.length ? h("p", { class: "muted", text: "Coherence: " + pr.coherence.map((c) => `sources must agree on ${c.fact}`).join("; ") }) : null,
        pr.lifecycle ? h("p", { class: "muted", text: `Lifecycle: needs recorded history events up to '${pr.lifecycle.target}' (a boolean saying so is not a record)` }) : null);
      const cb = h("input", { type: "checkbox", id: "pu-" + pr.profile_id, onchange: () => { box.hidden = !cb.checked; upd(); } });
      purposeBoxes.set(pr.profile_id, cb);
      return h("div", {}, h("label", { class: "inline", for: cb.id }, cb, h("span", {}, h("strong", { text: pr.title }), ` v${pr.version}`)), h("div", { class: "muted", text: pr.description }), box);
    }));
  const purposeRequests = () => [...purposeBoxes].filter(([, c]) => c.checked).map(([id]) => {
    const pr = purposes.find((x) => x.profile_id === id), bindings = {};
    for (const r of pr.roles) { const v = roleInputs.get(id + "/" + r.role).value.split(",").map((x) => x.trim()).filter(Boolean); if (v.length) bindings[r.role] = v; }
    return { profile_id: id, version: pr.version, bindings };
  });
  const parseContext = () => {
    const out = {};
    for (const raw of ctxArea.value.split("\n")) {
      const line = raw.replace(/\s+\(.*\)\s*$/, "").trim(); if (!line) continue;
      const i = line.indexOf("="); if (i < 1) throw new Error(`Context line needs key = value: ${line}`);
      const k = line.slice(0, i).trim(), v = line.slice(i + 1).trim();
      out[k] = v === "true" ? true : v === "false" ? false : v === "null" ? null : /^-?\d+$/.test(v) ? parseInt(v, 10) : v;
    }
    return out;
  };
  const upd = () => {
    const nc = state.runSel.claims.size, ns = state.runSel.sources.size;
    const warnings = [];
    for (const cid of state.runSel.claims) {
      const c = cur.find((x) => x.id === cid);
      if (c && ns && [...state.runSel.sources].every((sid) => sid === c.origin_source_id)) warnings.push(`${c.subject}: the only selected evidence is the document it came from (no independent evidence).`);
    }
    put(preview, h("p", { text: `${nc} claim${nc === 1 ? "" : "s"} × ${ns} evidence source${ns === 1 ? "" : "s"}.` }),
      ...warnings.map((w) => h("div", { class: "notice warn", text: w })));
    startBtn.disabled = !(ns && (nc || purposeRequests().length));
  };
  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    try {
      const r = await api("POST", "runs", { body: { claim_revision_ids: [...state.runSel.claims], evidence_source_ids: [...state.runSel.sources],
        pack_ids: [...packBoxes].filter(([, c]) => c.checked).map(([id]) => id), context: parseContext(), purposes: purposeRequests() }, headers: { "Idempotency-Key": crypto.randomUUID() } });
      announce("Run started."); location.hash = "#/run/" + enc(r.run_id); pollJobs(true);
    } catch (e) { preview.append(errorBox(e)); startBtn.disabled = false; }
  });
  const claimBoxes = [];
  const cbox = (set, id, label, extra) => { const inp = h("input", { type: "checkbox", checked: set.has(id), onchange: (e) => { e.target.checked ? set.add(id) : set.delete(id); upd(); } });
    if (set === state.runSel.claims) claimBoxes.push([inp, id]);
    return h("li", {}, h("label", { class: "inline" }, inp, label), extra); };
  if (!live()) return;
  put(main, h("h1", { text: "Run checks" }),
    h("div", { class: "grid2" },
      h("div", { class: "card" }, h("h2", { text: "Claims to test" }),
        cur.length ? h("ul", { class: "plain", style: "list-style:none;padding:0" }, cur.map((c) => cbox(state.runSel.claims, c.id, h("span", {}, h("code", { text: c.subject }), ` ${c.predicate} ${c.arguments.expected ? fmtVal(c.arguments.expected) : c.arguments.op ? c.arguments.op + " " + c.arguments.bound.value : ""}`)))) :
          h("p", { class: "muted" }, "No approved claims yet. ", h("a", { href: "#/review", text: "Review candidates" }), "."),
        cur.length ? h("button", { text: "Select all", onclick: () => { cur.forEach((c) => state.runSel.claims.add(c.id)); claimBoxes.forEach(([inp]) => (inp.checked = true)); upd(); } }) : null),
      h("div", { class: "card" }, h("h2", { text: "Evidence to test against" }),
        ready.length ? h("ul", { style: "list-style:none;padding:0" }, ready.map((s) => cbox(state.runSel.sources, s.id, `${s.name} (${s.format}, coverage ${s.attempt.coverage})`))) : h("p", { class: "muted", text: "No completed extractions yet." }))),
    purposePanel, packPanel,
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
    (run.purposes || []).length ? purposesCard(run) : null,
    (run.obligations || []).length ? obligationsCard(run, id) : null,
    run.findings.length ? h("div", { class: "card" }, h("div", { class: "row" }, h("label", { class: "inline" }, "Show ", filter)),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Verdict", "Subject", "Predicate", "Reason", ""].map((t) => h("th", { text: t })))), tbody))) : h("p", { class: "muted", text: "This run evaluated no claims (purpose assessment only)." }), detail);
  draw();
  if (claimId) {
    const f = run.findings.find((x) => x.claim_id === claimId);
    if (f) findingDetail(detail, f, run);
  }
}

function compositionObligations(an) {
  return [h("h3", { text: "Composition obligations (each keeps its own verdict)" }), h("p", { class: "muted", text: an.scope.statement }),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Obligation", "Verdict", "Reason", "Witness"].map((t) => h("th", { text: t })))),
      h("tbody", {}, an.obligations.map((o) => h("tr", {}, h("td", {}, h("code", { text: o.id })), h("td", {}, verdictBadge(o.verdict)), h("td", { text: o.reason.replace(/_/g, " ") }),
        h("td", { class: "mono", text: o.witness.mismatches ? o.witness.mismatches.map((m) => `${o.witness.producer} gives ${m.tag}=${JSON.stringify(m.guarantee)}, ${o.witness.consumer} requires ${JSON.stringify(m.requirement)}`).join("; ")
          : o.witness.cycle ? o.witness.cycle.join(" → ") : o.witness.generations ? JSON.stringify(o.witness.generations) : o.witness.missing ? JSON.stringify(o.witness.missing) : "" })))))),
    an.conditional_on.length ? h("div", { class: "notice warn" }, h("strong", { text: "Conditional on: " }), h("ul", {}, an.conditional_on.map((c) => h("li", { text: c })))) : null];
}

function purposesCard(run) {
  return h("div", { class: "card" }, h("h2", { text: "Purpose assessments" }),
    h("div", { class: "notice", text: "Each purpose states what this evidence suffices for. It is never a statement that a package is simply 'complete'. Every obligation keeps its own verdict." }),
    run.purposes.map((p) => h("div", { style: "margin-bottom:1.2rem" }, h("h3", {}, p.profile.profile_id + " v" + p.profile.profile_version + "  ", verdictBadge(p.overall)),
      p.newer_version ? h("div", { class: "notice warn" }, `A newer version (v${p.newer_version}) is installed. This report keeps the version it was produced with; `, h("a", { href: "#/run", text: "evaluate again with the newer one" }), ".") : null,
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Obligation", "Verdict", "Reason", "What could resolve it"].map((t) => h("th", { text: t })))),
        h("tbody", {}, p.obligations.map((o) => h("tr", {}, h("td", {}, h("code", { text: o.id })), h("td", {}, verdictBadge(o.verdict)), h("td", { text: o.reason.replace(/_/g, " ") }),
          h("td", {}, o.would_resolve || "-", o.witness && o.witness.values && o.verdict === "CONTRADICTED" ? h("div", { class: "mono muted", text: "saw: " + o.witness.values.map((v) => `${v.source}=${JSON.stringify(v.value.value)}`).join(" vs ") }) : null)))))),
      h("p", { class: "muted", text: p.scope.statement + " Evaluated: " + (p.scope.evaluated_sources.join(", ") || "none") + (p.scope.sources_outside_evaluation_scope.length ? ". Outside this purpose's scope: " + p.scope.sources_outside_evaluation_scope.join(", ") : "") }),
      h("details", {}, h("summary", { text: "Pinned profile and bindings" }), h("pre", { class: "src", text: JSON.stringify({ profile: p.profile, bindings: p.bindings }, null, 2) })))));
}

function obligationsCard(run, runId) {
  const byPack = {};
  run.obligations.forEach((o) => (byPack[o.pack_id] = byPack[o.pack_id] || []).push(o));
  const appBadge = (a) => badge("applicability " + a.result, a.result === "TRUE" ? "b-ok" : a.result === "FALSE" ? "b-neutral" : "b-warn");
  return h("div", { class: "card" }, h("h2", { text: "Risk-pack obligations" }),
    h("div", { class: "notice", text: "Evidence assessments under the pinned pack and the context facts you stated. They are not legal, safety or compliance certification, and a remediation note is a proposal - closing it requires a new verified run." }),
    Object.entries(byPack).map(([pid, list]) => h("div", {}, h("h3", { text: `${pid} v${list[0].pack_version}` }), h("p", { class: "mono muted", text: list[0].pack_digest }),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Obligation", "Verdict", "Applicability", "Why", "Proposed next step"].map((t) => h("th", { text: t })))),
        h("tbody", {}, list.map((o) => h("tr", {}, h("td", {}, h("code", { text: o.obligation_id }), o.claim_ids.length ? h("div", {}, h("a", { href: `#/findings/${enc(runId)}/${enc(o.claim_ids[0])}`, text: "member finding" })) : null),
          h("td", {}, verdictBadge(o.verdict)),
          h("td", {}, appBadge(o.applicability), h("div", { class: "muted mono", text: Object.entries(o.applicability.facts).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ") || "no facts stated" })),
          h("td", { text: o.reason.replace(/_/g, " ") }),
          h("td", {}, o.remediation.length ? h("ul", {}, o.remediation.map((r) => h("li", { text: r }))) : "-")))))))));
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
    w.analysis ? compositionObligations(w.analysis) : null,
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

/* ---------------------------------------------------------------- advanced */
async function viewAdvanced() {
  setNav("");
  const live = begin();
  const DEFECTS = { store: ["omit_random_cursor", "reset_next_id", "double_cursor_after_restore", "flatten_txn", "sort_replay"],
    pipeline: ["stale_cache", "drop_versions"], scheduler: ["reverse_ties", "drop_random_cursor"] };
  const out = h("div", { role: "status", "aria-live": "polite" });
  const seed = h("input", { id: "x-seed", value: "seed-1" }), ops = h("input", { id: "x-ops", type: "number", min: 0, max: 400, value: 120 });
  const system = h("select", { id: "x-system" }, [["store", "Transactional key-value store"], ["pipeline", "Document transformation pipeline"],
    ["scheduler", "Seeded job scheduler"]].map(([value, text]) => h("option", { value, text })));
  const defect = h("select", { id: "x-defect" });
  const updateDefects = () => put(defect, h("option", { value: "", text: "none (the correct candidate)" }),
    DEFECTS[system.value].map((d) => h("option", { value: d, text: d })));
  system.addEventListener("change", updateDefects); updateDefects();
  const body = () => ({ system: system.value, seed: seed.value, ops: parseInt(ops.value, 10), defects: defect.value ? [defect.value] : [] });
  const run = async (path, payload, render) => {
    put(out, h("p", { class: "muted", text: "Running…" }));
    try { render(await api("POST", "experiments/" + path, { body: payload })); } catch (e) { put(out, errorBox(e)); }
  };
  const showCheckpoint = (r) => put(out, h("div", { class: "notice " + (r.passed ? "ok" : "bad") }, h("strong", { text: r.passed ? "No divergence" : `${r.divergences.length} divergence(s)` }),
    ` - case ${r.case.slice(0, 20)}…, paths: ${Object.entries(r.coverage.steps_compared_per_path || {}).map(([k, v]) => `${k} ${v} steps`).join(", ")}. This is a bounded statement about this subject over these operations, not universal correctness.`),
    r.divergences.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Path", "Operation", "Field", "Expected", "Actual"].map((t) => h("th", { text: t })))),
      h("tbody", {}, r.divergences.slice(0, 20).map((d) => h("tr", {}, h("td", { text: d.path }), h("td", { text: String(d.op_index) }), h("td", { class: "mono", text: d.field }), h("td", { class: "mono", text: JSON.stringify(d.expected) }), h("td", { class: "mono", text: JSON.stringify(d.actual) })))))) : null,
    r.errors.length ? errorBox(new Error(JSON.stringify(r.errors))) : null);
  const showMin = (r) => put(out, r.reduced_units == null
    ? h("div", { class: "notice warn", text: `Nothing to reduce: ${r.status} - ${r.stop_reason}` })
    : [h("div", { class: "notice " + (r.fresh_process_verification === "VERIFIED" ? "ok" : "bad") }, h("strong", { text: `${r.original_units} → ${r.reduced_units} operations` }),
        ` in ${r.oracle_calls} oracle calls. Fresh-process reproduction: ${r.fresh_process_verification}.`),
      h("p", { text: r.scope }), h("pre", { class: "src", role: "region", tabIndex: 0, "aria-label": "Reduced case", text: JSON.stringify(r.reduced_case, null, 2) }),
      h("p", { class: "muted", text: `Failure signature: ${r.signature.witness_class} (${r.signature.reason_code}). The package contains the full reduction log.` }),
      r.package ? h("button", { text: "Download counterexample", onclick: () => {
        const url = URL.createObjectURL(new Blob([JSON.stringify(r.package)], { type: "application/json" }));
        const link = h("a", { href: url, download: "counterexample.json" }); document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } }) : null]);
  const crashSel = h("select", { id: "c-w" }, h("option", { value: "", text: "correct writer" }), h("option", { value: "marker_before_flush", text: "marker_before_flush" }));
  const crashRec = h("select", { id: "c-r" }, h("option", { value: "", text: "correct recovery" }), ["join_members", "silent_fallback"].map((d) => h("option", { value: d, text: d })));
  const showCrash = (r) => put(out,
    h("div", { class: "notice " + (r.passed ? "ok" : "bad") }, h("strong", { text: r.passed ? "All recoveries valid" : `${r.violations.length} violation(s)` }),
      ` - coverage: ${r.coverage.mode}, ${r.coverage.explored.length} of ${r.coverage.positions_total} positions. Model: ${r.coverage.model} (not a proof for any real filesystem).`),
    r.violations.length ? h("ul", {}, r.violations.map((v) => h("li", { text: `position ${v.position}: ${v.class}` }))) : null);
  const crashBtn = h("button", { class: "primary", text: "Enumerate every crash position",
    onclick: () => run("crash", { writer_defect: crashSel.value || null, recovery_defects: crashRec.value ? [crashRec.value] : [] }, showCrash) });
  if (!live()) return;
  put(main, h("h1", { text: "Advanced: experiments" }),
    h("p", {}, h("a", { href: "#/campaign", text: "Mutation and metamorphic campaigns →" }), " - does the verifier itself catch defects?"),
    h("p", {}, h("a", { href: "#/composition", text: "Composition analysis →" }), " - do valid parts make a valid whole (units, tags, generations)?"),
    h("div", { class: "notice", text: "Optional laboratory tools on three fictional systems. Defects are deliberately injected so you can see the laboratory catch them. None of this is needed for the Import → Export workflow." }),
    h("div", { class: "card" }, h("h2", { text: "Replay and checkpoint" }),
      h("label", { for: "x-system", text: "Synthetic system" }), system,
      h("label", { for: "x-seed", text: "Seed" }), seed, h("label", { for: "x-ops", text: "Operations (0-400)" }), ops, h("label", { for: "x-defect", text: "Injected defect" }), defect,
      h("div", { class: "row", style: "margin-top:.6rem" }, h("button", { class: "primary", text: "Run three-way experiment", onclick: () => run(system.value === "store" ? "checkpoint" : system.value, body(), showCheckpoint) }),
        h("button", { text: "Minimize the failure", onclick: () => run(system.value === "store" ? "minimize" : "laboratory-minimize", body(), showMin) }))),
    h("div", { class: "card" }, h("h2", { text: "Test your own program" }),
      h("p", {}, "The same experiments and minimizer can run against a program you supply, speaking a small JSON-lines protocol over stdin/stdout. Because that executes software, it is configured on the command line only - this page never accepts a command, and a counterexample package never causes one to run."),
      h("pre", { class: "src", role: "region", tabIndex: 0, "aria-label": "Command line example", text: [
        "verifier-lab experiment checkpoint --subject-cmd \"python3 examples/reference_subject.py\" --ops 120",
        "verifier-lab minimize --subject-cmd \"python3 my_subject.py\" --output case.json",
        "verifier-lab verify-counterexample case.json --subject-cmd \"python3 my_subject.py\"   # identity must match" ].join("\n") }),
      h("p", { class: "muted", text: "Each instance is a fresh process with an empty working directory, a minimal environment and resource limits. That contains accidents; it is not a security boundary for hostile code. Subject crashes, hangs and protocol violations are reported as execution errors, never as divergences or passes." })),
    h("div", { class: "card" }, h("h2", { text: "Crash boundaries (virtual durable store)" }),
      h("label", { for: "c-w", text: "Writer" }), crashSel, h("label", { for: "c-r", text: "Recovery" }), crashRec,
      h("div", { class: "row", style: "margin-top:.6rem" }, crashBtn)),
    out);
}

/* -------------------------------------------------------------- composition */
async function viewComposition(sourceId) {
  setNav("");
  const live = begin();
  const sources = (await api("GET", "sources")).items.filter((x) => x.format === "json" && x.attempt && x.attempt.state === "EXTRACTED" && !x.superseded_by);
  const sel = h("select", { id: "c-src", onchange: () => (location.hash = "#/composition/" + enc(sel.value)) }, h("option", { value: "", text: "Choose a JSON source…" }),
    sources.map((x) => h("option", { value: x.id, selected: x.id === sourceId, text: x.name })));
  const subject = h("input", { id: "c-subj", value: "pipeline", size: 14 });
  const out = h("div", { id: "c-out", role: "status", "aria-live": "polite" });
  if (!live()) return;
  put(main, h("h1", { text: "Advanced: does the whole compose?" }),
    h("div", { class: "notice", text: "Parts can each be valid while their composition is not: units, offsets or generations can disagree across an edge. This page reads a pipeline description (data only), keeps every obligation separate, and never infers a unit conversion - one must be declared and match an exact registered factor." }),
    h("div", { class: "card" }, h("label", { for: "c-src", text: "Pipeline source" }), sel, h("label", { for: "c-subj", text: "Key holding the pipeline object" }), subject,
      h("p", { class: "muted" }, "Import a JSON file from ", h("a", { href: "#/import", text: "Import" }), ", e.g. fixtures/composition/units-mismatch.json.")), out);
  if (sourceId) await analyzeComposition(out, sourceId, subject.value, "", sources);
}

async function analyzeComposition(out, sourceId, subject, target, sources) {
  put(out, h("p", { class: "muted", text: "Analyzing…" }));
  let r;
  try { r = await api("GET", `composition/${enc(sourceId)}?subject=${enc(subject)}` + (target ? "&target=" + enc(target) : "")); } catch (e) { put(out, errorBox(e)); return; }
  const an = r.analysis, V = { SUPPORTED: "ok", CONTRADICTED: "bad", INSUFFICIENT_EVIDENCE: "warn", INVALID_EVIDENCE: "bad" };
  const tsel = h("select", { id: "c-target", "aria-label": "Target activity", onchange: () => analyzeComposition(out, sourceId, subject, tsel.value, sources) },
    h("option", { value: "", text: "Whole pipeline" }), r.activities.map((a) => h("option", { value: a.id, selected: a.id === target, text: "only ancestors of " + a.id })));
  // layered SVG; the tables below carry exactly the same information
  const depths = {}; r.activities.forEach((a) => { if (a.depth != null) (depths[a.depth] = depths[a.depth] || []).push(a); });
  const W = 150, H = 40, GX = 60, GY = 22, cols = Object.keys(depths).length, rows = Math.max(1, ...Object.values(depths).map((x) => x.length));
  const pos = {}; Object.entries(depths).forEach(([d, list]) => list.forEach((a, i) => (pos[a.id] = { x: 10 + d * (W + GX), y: 10 + i * (H + GY) })));
  const ns = "http://www.w3.org/2000/svg", mk = (t, at) => { const e = document.createElementNS(ns, t); Object.entries(at).forEach(([k, v]) => e.setAttribute(k, v)); return e; };
  const svg = mk("svg", { width: 20 + cols * (W + GX), height: 20 + rows * (H + GY), "aria-hidden": "true", role: "img" });
  const colour = { ok: "var(--ok)", bad: "var(--bad)", warn: "var(--warn)", n: "var(--neutral)" };
  an.edges.forEach(([u, v]) => { if (!pos[u] || !pos[v]) return;
    const ob = an.obligations.find((o) => o.id.startsWith("input:" + v + ":") && o.witness.producer === u); const c = colour[ob ? V[ob.verdict] : "n"];
    svg.append(mk("line", { x1: pos[u].x + W, y1: pos[u].y + H / 2, x2: pos[v].x, y2: pos[v].y + H / 2, stroke: c, "stroke-width": 2.5 })); });
  r.activities.forEach((a) => { const p = pos[a.id]; if (!p) return; const g = mk("g", { opacity: a.in_scope ? 1 : 0.4 });
    g.append(mk("rect", { x: p.x, y: p.y, width: W, height: H, rx: 6, fill: "var(--card)", stroke: "var(--line)", "stroke-width": 1.5 }));
    const t = mk("text", { x: p.x + 8, y: p.y + 25, fill: "var(--fg)", "font-size": 13 }); t.textContent = a.id + (a.conversion ? "  ⇄ unit conversion" : ""); g.append(t); svg.append(g); });
  const edgeRows = an.obligations.filter((o) => o.type === "edge");
  const impactSel = h("select", { id: "c-cmp", "aria-label": "Compare with another version" }, h("option", { value: "", text: "Compare with another version…" }), sources.filter((x) => x.id !== sourceId).map((x) => h("option", { value: x.id, text: x.name })));
  const impactOut = h("div", {});
  impactSel.addEventListener("change", async () => { if (!impactSel.value) return put(impactOut);
    try { const im = await api("POST", "composition/impact", { body: { old: impactSel.value, new: sourceId, subject } });
      put(impactOut, h("h3", { text: "What a change reaches" }), h("p", { class: "muted", text: im.note }),
        h("p", {}, `changed: ${im.changed.join(", ") || "none"} · added: ${im.added.join(", ") || "none"} · removed: ${im.removed.join(", ") || "none"} · external changed: ${im.external_changed.join(", ") || "none"}`),
        h("ul", {}, Object.entries(im.affected).map(([k, path]) => h("li", {}, h("code", { text: k }), " ← ", path.join(" → ")))), h("p", { class: "muted", text: "Unaffected: " + (im.unaffected.join(", ") || "none") }));
    } catch (e) { put(impactOut, errorBox(e)); } });
  put(out, h("div", { class: "notice " + V[an.overall], role: "alert" }, h("strong", { text: an.overall.replace(/_/g, " ") }), ` - ${r.source.name}. `, an.scope.statement),
    h("div", { class: "card" }, h("div", { class: "row" }, h("label", { class: "inline", text: "Scope " }, tsel), impactSel),
      h("h2", { text: "Pipeline (dependency order)" }), h("div", { style: "overflow:auto" }, svg),
      h("p", { class: "muted", text: an.order.length ? "Order: " + an.order.join(" → ") : "No valid order exists (see the cycle below)." })),
    h("div", { class: "card" }, h("h2", { text: "Edges: what each producer guarantees vs what each consumer requires" }),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Edge", "Artifact", "Verdict", "Reason", "Detail"].map((t) => h("th", { text: t })))),
        h("tbody", {}, edgeRows.length ? edgeRows.map((o) => h("tr", {}, h("td", { class: "mono", text: `${o.witness.producer || "?"} → ${o.witness.consumer || o.id.split(":")[1]}` }), h("td", { class: "mono", text: o.witness.artifact || o.id.split(":")[2] }), h("td", {}, verdictBadge(o.verdict)),
          h("td", { text: o.reason.replace(/_/g, " ") }), h("td", { class: "mono", text: o.witness.mismatches ? o.witness.mismatches.map((m) => `${m.tag}: gives ${JSON.stringify(m.guarantee)}, needs ${JSON.stringify(m.requirement)}`).join("; ") : o.witness.not_established ? "not established: " + o.witness.not_established.map((m) => m.tag).join(", ") : o.witness.source === "external" ? "external input (assumed)" : "" })))
          : h("tr", {}, h("td", { colSpan: 5, class: "muted", text: "No consumed inputs in scope." })))))),
    h("div", { class: "card" }, h("h2", { text: "Every obligation" }), compositionObligations(an), impactOut));
}

/* ---------------------------------------------------------------- campaign */
const KERNELS = [["real", "Real verifier (the production rules)"], ["always_supported", "Negative control: always SUPPORTED"],
  ["always_contradicted", "Negative control: always CONTRADICTED"], ["last_key_wins", "Negative control: duplicate keys / conflicts resolved silently"],
  ["self_reference_allowed", "Negative control: a claim's own source counts as evidence"]];

function downloadJson(name, obj) {
  const a = h("a", { href: URL.createObjectURL(new Blob([JSON.stringify(obj, null, 1)], { type: "application/json" })), download: name });
  document.body.append(a); a.click(); a.remove();
}

async function viewCampaign() {
  setNav("");
  const live = begin();
  const kernel = h("select", { id: "k-kernel" }, KERNELS.map(([v, t]) => h("option", { value: v, text: t })));
  const seed = h("input", { id: "k-seed", value: "campaign-1" });
  const out = h("div", { id: "k-out", role: "status", "aria-live": "polite" });
  const go = h("button", { class: "primary", text: "Run campaign", onclick: async () => {
    go.disabled = true; put(out, h("p", { class: "muted", text: "Running every case…" }));
    try { renderCampaign(out, await api("POST", "experiments/campaign", { body: { kernel: kernel.value, seed: seed.value } })); announce("Campaign finished."); }
    catch (e) { put(out, errorBox(e)); }
    go.disabled = false;
  } });
  if (!live()) return;
  put(main, h("h1", { text: "Advanced: does the verifier catch defects?" }),
    h("div", { class: "notice", text: "A passing test suite does not prove a verifier is right: a verifier can be consistently wrong. A campaign applies known defects (mutations) and meaning-preserving changes (metamorphic relations) to hand-written synthetic cases whose expected answers were committed in advance, then checks the verifier's answers. Negative controls are verifiers with a deliberate bug - a campaign that stays green against one would mean the harness itself is broken." }),
    h("div", { class: "card" }, h("label", { for: "k-kernel", text: "Verifier under test" }), kernel, h("label", { for: "k-seed", text: "Campaign seed (part of the pinned case manifest)" }), seed,
      h("div", { class: "row", style: "margin-top:.6rem" }, go, h("a", { href: "#/advanced", text: "Back to experiments" }))),
    out);
}

function renderCampaign(out, res) {
  const r = res.report, sum = r.summary, c = sum.counts;
  const executed = c.PASSED + c.FAILED;
  const failedRun = r.survivors.length > 0 || c.FAILED > 0 || c.ERROR > 0;
  const stat = (label, n, cls, note) => h("div", { class: "card", style: "flex:1;min-width:150px;margin:0" }, h("div", { class: "badge " + cls, text: label }), h("div", { style: "font-size:1.8rem;font-weight:700", text: String(n) }), h("div", { class: "muted", text: note }));
  const ver = res.verification;
  const verBox = h("div", {});
  const showVer = (v, title) => put(verBox, h("div", { class: "notice " + (v.status === "ACCEPTED" ? "ok" : "bad"), role: "alert" }, h("strong", { text: `${title}: ${v.status}` }), " - ", v.conclusion),
    v.status === "REJECTED" ? h("ul", {}, v.checks.filter((x) => !x.ok).map((x) => h("li", {}, h("code", { text: x.name }), x.detail ? ` - ${x.detail}` : ""))) : null);
  showVer(ver, "Independent checker (anchored to the trusted manifest digest)");
  const forge = h("button", { text: "Forge the passing total and re-verify", onclick: async () => {
    const bad = JSON.parse(JSON.stringify(r)); bad.summary.counts.PASSED += bad.summary.counts.FAILED; bad.summary.counts.FAILED = 0;
    showVer(await api("POST", "experiments/campaign-verify", { body: { report: bad, manifest: res.manifest, trusted_manifest_digest: res.trusted_manifest_digest } }), "Forged report");
  } });
  const covTable = (title, rows, withLayer) => h("div", {}, h("h3", { text: title }), h("div", { class: "tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, [title.split(" ")[0], withLayer ? "Layer" : "", "Cases", "Executed", "Passed", "Failed", "Skipped (precondition)"].filter((x, i) => x || i !== 1).map((t) => h("th", { text: t })))),
    h("tbody", {}, Object.entries(rows).map(([k, v]) => h("tr", {}, h("td", {}, h("code", { text: k })),
      withLayer ? h("td", {}, badge(r.cases.find((x) => x.operator === k).layer, r.cases.find((x) => x.operator === k).layer === "byte" ? "b-warn" : "b-info")) : null,
      h("td", { text: v.cases }), h("td", { text: v.executed }), h("td", { text: v.passed }), h("td", { class: v.failed ? "mono" : "", text: v.failed ? "✕ " + v.failed : "0" }), h("td", { text: v.skipped })))))));
  const filter = h("select", { "aria-label": "Filter cases", onchange: () => drawCases() }, ["", "FAILED", "PASSED", "SKIPPED_PRECONDITION", "ERROR"].map((x) => h("option", { value: x, text: x || "All statuses" })));
  const tbody = h("tbody");
  const drawCases = () => put(tbody, r.cases.filter((x) => !filter.value || x.status === filter.value).map((x) => h("tr", {},
    h("td", {}, badge(x.status.replace("_PRECONDITION", ""), x.status === "PASSED" ? "b-ok" : x.status === "FAILED" ? "b-bad" : x.status === "ERROR" ? "b-bad" : "b-neutral")),
    h("td", {}, badge(x.layer, x.layer === "metamorphic" ? "b-info" : x.layer === "byte" ? "b-warn" : "b-neutral")),
    h("td", {}, h("code", { text: x.id })),
    h("td", {}, x.comparison && x.comparison.target ? `${x.comparison.target.expected[0]} expected, ${x.comparison.target.actual[0] || "-"} actual`
      : x.comparison && x.comparison.relation ? (x.comparison.relation.ok ? "relation holds" : x.comparison.relation.detail.slice(0, 80)) : x.status === "SKIPPED_PRECONDITION" ? "precondition not met - not a pass" : x.error || "")))); drawCases();
  put(out,
    h("div", { class: "notice " + (failedRun ? "bad" : "ok"), role: "alert" }, h("strong", { text: failedRun ? `Campaign FAILED against "${r.kernel.name}"` : `Campaign passed against "${r.kernel.name}"` }),
      failedRun ? ` - ${r.survivors.length} mutation(s) survived undetected and ${c.FAILED} case(s) did not behave as expected. This is what a defective verifier looks like.` : ` - every executed case behaved as committed in advance. This is a bounded statement about these synthetic cases, not proof of correctness.`),
    h("div", { class: "row", style: "align-items:stretch" }, stat("Passed", c.PASSED, "b-ok", `of ${executed} executed`), stat("Failed", c.FAILED, c.FAILED ? "b-bad" : "b-neutral", "expectation not met"),
      stat("Skipped", c.SKIPPED_PRECONDITION, "b-neutral", "precondition not met; never counted as passes"), stat("Errors", c.ERROR, c.ERROR ? "b-bad" : "b-neutral", "execution problems")),
    h("p", {}, h("strong", { text: `${sum.mutation_detection.detected} of ${sum.mutation_detection.executed_mutations}` }), ` executed mutations were detected (${sum.mutation_detection.scope}). `,
      sum.never_executed.length ? badge("never executed: " + sum.never_executed.join(", "), "b-warn") : badge("every operator and relation ran at least once", "b-ok")),
    h("div", { class: "card" }, h("h2", { text: "Is this report trustworthy?" }), verBox, h("div", { class: "row", style: "margin-top:.5rem" }, forge,
      h("button", { text: "Download report", onclick: () => downloadJson("campaign-report.json", r) }), h("button", { text: "Download manifest", onclick: () => downloadJson("campaign-manifest.json", res.manifest) })),
      h("p", { class: "mono muted", text: "trusted manifest digest: " + res.trusted_manifest_digest }),
      h("p", { class: "muted", text: "Without a digest from a trusted channel, a checker can only say the report is consistent with the manifest it was given; an attacker could drop a failing case and recompute both." })),
    r.survivors.length ? h("div", { class: "card" }, h("h2", { text: `Surviving mutations (${r.survivors.length})` }), h("ul", {}, r.survivors.map((id) => {
      const x = r.cases.find((y) => y.id === id); const t = x.comparison.target;
      return h("li", {}, h("code", { text: id }), ` - expected ${t.expected[0]} (${t.expected[1]}), got ${t.actual[0]} (${t.actual[1]})`); }))) : null,
    h("div", { class: "card" }, h("h2", { text: "Coverage" }), covTable("Operator coverage", sum.operators, true), covTable("Rule coverage (predicate under mutation)", sum.rules, false),
      covTable("Metamorphic relation coverage", sum.relations, false)),
    h("div", { class: "card" }, h("h2", { text: "Every case" }), h("div", { class: "row" }, h("label", { class: "inline" }, "Show ", filter)),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Status", "Layer", "Case", "Outcome"].map((t) => h("th", { text: t })))), tbody))));
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
    if (changed) {
      refreshOverview();
      /* Never re-render over a form the user is filling in: the Run page keeps its selections and context text,
         and the Import page refreshes only while no file is chosen. Run progress has its own targeted refresh below. */
      const filesChosen = document.querySelector("#files") && document.querySelector("#files").files.length;
      if (route[0] === "import" && !filesChosen && !document.activeElement.closest("form")) render(true);
    }
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
      case "import": await viewImport(dec(route[1]), soft); break;
      case "review": await viewReview(dec(route[1]), params); break;
      case "run": await viewRun(dec(route[1])); break;
      case "findings": await viewFindings(dec(route[1]), dec(route[2])); break;
      case "export": await viewExport(); break;
      case "advanced": await viewAdvanced(); break;
      case "campaign": await viewCampaign(); break;
      case "composition": await viewComposition(dec(route[1])); break;
      default: location.hash = "#/import";
    }
  } catch (e) {
    put(main, h("h1", { text: "This screen could not be loaded" }), errorBox(e),
      h("p", {}, h("button", { type: "button", text: "Try again", onclick: () => render(false) })));
  }
  if (!soft) { document.title = "Verification Workbench"; if (!document.activeElement || document.activeElement === document.body) main.focus({ preventScroll: true }); }
}
window.addEventListener("hashchange", () => render(false));
refreshOverview(); pollJobs(true); render(true); /* initial load keeps focus at the document start so the skip link is the first Tab stop */
