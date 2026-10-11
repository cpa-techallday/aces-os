// Evidence plate: the golden runs of the desktop's projects and one run's records (run.json, result.json,
// validation.json, compute_used.json, the sweep, the winner, the figures, report.html inside a sandboxed iframe through
// the console's file route, the report's provenance count, the events tail) with the grade line followed by the fixed
// sentence "no grade is a sign-off" and Verify (aces golden verify, a read); the desktop's operator ledger with Verify
// ledger (aces.ledger.verify); the rig-side SIMLAB evidence chain as the SIMLAB console reports and verifies it.
// Bindings: /api/golden/runs?project=, /api/golden/run/{p}/{id}, /api/golden/file/..., POST /api/golden/verify/{p}/{id},
// /api/ledger?n=, /api/ledger/verify, /api/ledger/simlab, /api/projects; stamps golden.runs, ledger.aces, sim.evidence,
// sim.evidence.verify (notices), console.health (inline: ledger_entries). Deep link ?plate=evidence&project=<name>&run=<id>.
const GRADE_SENTENCE = "no grade is a sign-off";
import { jobPanel, fileB64 } from "../lib/jobs.js";

let R = null;

export async function mount(root, ctx) {
  const { dom } = ctx; const { el, block } = dom;
  R = {
    ctx, root, res: {}, runs: null, projects: null, project: ctx.params.get("project") || "", run: ctx.params.get("run") || null, runProject: ctx.params.get("project") || null,
    detail: null, detailRes: null, verify: null, verifying: false, ledger: null, ledgerVerify: null, ledgerVerifying: false, chain: null, ledgerEntries: null, dead: false,
    b: { start: el("div"), runs: el("div"), detail: el("div"), ledger: el("div"), chain: el("div"), foot: el("div.plate-foot.mono") },
    startMode: "project", startJob: null, startResult: null, initResult: null,
  };
  root.append(
    block("start a golden run · aces golden run on this desktop (and the rig lane when configured)", [R.b.start], { span2: true, id: "evi-start" }),
    block("golden runs · $ACES_WORK/<project>/golden/<run>/run.json", [R.b.runs], { span2: true, id: "evi-runs" }),
    block("run", [R.b.detail], { span2: true, id: "evi-run", coord: GRADE_SENTENCE }),
    block("desktop ledger · $ACES_STATE/ledger.jsonl", [R.b.ledger], { span2: true, id: "evi-ledger" }),
    block("SIMLAB chain · the rig-side evidence ledger, as the SIMLAB console reports it", [R.b.chain], { span2: true, id: "evi-chain" }),
    R.b.foot,
  );
  ctx.events.on("golden.runs", (st) => { if (st.changed) loadRuns(); });
  ctx.events.on("ledger.aces", (st) => { if (st.changed) loadLedger(); });
  ctx.events.on("console.health", (st) => { const n = st.payload && st.payload.ledger_entries; if (Number.isFinite(n) && n !== R.ledgerEntries) { R.ledgerEntries = n; loadLedger(); } });
  ctx.events.on("sim.evidence", (st) => { if (st.changed) loadChain(); });
  ctx.events.on("sim.evidence.verify", (st) => { if (st.changed) loadChain(); });
  const pr = await R.ctx.api.get("/api/projects");
  if (R.dead) return;
  R.res.projects = pr; R.projects = pr.ok ? pr.data : null;
  await Promise.all([loadRuns(), loadLedger(), loadChain()]);
  if (R.dead) return;
  renderStart();
  if (R.run && R.runProject) await loadRun(R.runProject, R.run); else { renderDetail(); renderInspector(); }
  renderFoot();
}

export function unmount() { if (R) R.dead = true; R = null; }

// ---------- start a golden run: a project with golden/spec.json, the built-in demo, or a STEP file as a new project ----------
const GOLDEN_FIELDS = [
  ["n", "designs in the sweep", "number"], ["seed", "sampler seed", "number"], ["res", "FEA resolution mm", "number"], ["gen_res", "generator resolution mm", "number"],
  ["tol", "agreement tolerance (0-1)", "number"], ["rounds", "verification rounds", "number"], ["check", "finer independent check", ["", "auto", "off", "force"]],
  ["device", "where the solves run", ["", "auto", "cpu", "gpu"]], ["conforming", "CalculiX second discretisation", ["", "auto", "rig", "local", "off"]],
  ["lane", "rig lane (or none)", "text"], ["require_grade", "require at least", ["", "preview-grade", "verified-desktop", "rig-verified"]],
  ["rig_sweep", "also sweep on the rig", "bool"], ["strict", "fail unless verified-desktop or better", "bool"],
];
function renderStart() {
  const { dom } = R.ctx; const { el, clear, button, note } = dom;
  const box = R.b.start; clear(box);
  const projects = R.projects && Array.isArray(R.projects.projects) ? R.projects.projects.map((p) => p.name) : [];
  const modeSel = el("select", { "aria-label": "what to run", onchange: (e) => { R.startMode = e.target.value; renderStart(); } },
    [["project", "a project's golden/spec.json"], ["demo", "the built-in demo design space"], ["step", "a new project from a STEP file"]].map(([v, t]) => el("option", { value: v, text: t, selected: v === R.startMode ? "selected" : null })));
  box.append(el("div.btnrow", [el("label.k", { text: "run" }), modeSel]));
  let target = null; const getTarget = () => target && target();
  if (R.startMode === "project") {
    const sel = el("select", { "aria-label": "project" }, projects.map((n) => el("option", { value: n, text: n, selected: n === R.project ? "selected" : null })));
    box.append(el("div.btnrow", [el("label.k", { text: "project" }), sel]));
    target = () => ({ project: sel.value, confirm: sel.value });
  } else if (R.startMode === "demo") {
    const sel = el("select", { "aria-label": "demo" }, ["root-mount", "bone"].map((n) => el("option", { value: n, text: n })));
    box.append(el("div.btnrow", [el("label.k", { text: "demo" }), sel, el("span.dim", { text: "runs in the project golden-demo" })]));
    target = () => ({ demo: sel.value, confirm: "golden-demo" });
  } else {
    const name = el("input", { type: "text", placeholder: "new project name", "aria-label": "new project name", style: { maxWidth: "260px" } });
    const cls = el("select", { "aria-label": "project class" }, ["personal", "open", "commercial", "sovereign", "controlled"].map((c) => el("option", { value: c, text: c })));
    const hyp = el("input", { type: "text", placeholder: "hypothesis (optional)", "aria-label": "hypothesis" });
    const file = el("input", { type: "file", accept: ".step,.stp", "aria-label": "STEP file" });
    const create = button("Create the project from the STEP", { onclick: async () => {
      const f = file.files[0];
      if (!f || !name.value.trim()) { R.initResult = { cls: "act", text: "a project name and a STEP file are needed" }; renderStart(); return; }
      R.initResult = { cls: "", text: `uploading ${f.name} and running aces golden init` }; renderStart();
      const r = await R.ctx.api.post("/api/golden/init", { name: name.value.trim(), class: cls.value, hypothesis: hyp.value.trim() || null, step: { filename: f.name, data_b64: await fileB64(f) } });
      if (!R || R.dead) return;
      R.initResult = r.ok ? { cls: "", text: `project ${r.data.project} created with golden/spec.json from ${f.name} (ledger #${r.data.ledger_n}); pick it and start` } : { cls: r.status === 503 || r.status === 409 || r.status === 400 ? "act" : "fault", text: `${r.error} (HTTP ${r.status})` };
      if (r.ok) { R.startMode = "project"; R.project = r.data.project; const pr = await R.ctx.api.get("/api/projects"); if (R && !R.dead) { R.projects = pr.ok ? pr.data : R.projects; } }
      if (R && !R.dead) renderStart();
    } });
    box.append(el("div.toolform", [el("label", { text: "project name" }), name, el("label", { text: "class" }), cls, el("label", { text: "hypothesis" }), hyp, el("label", { text: "STEP file" }), file]), el("div.btnrow", [create]));
    if (R.initResult) box.append(el("div.result." + (R.initResult.cls || "ok"), { text: R.initResult.text }));
    return;
  }
  const opts = {};
  const form = el("div.toolform");
  for (const [key, label, kind] of GOLDEN_FIELDS) {
    let input;
    if (Array.isArray(kind)) input = el("select", { id: `gold-${key}` }, kind.map((v) => el("option", { value: v, text: v || "(default)" })));
    else if (kind === "bool") input = el("input", { type: "checkbox", id: `gold-${key}` });
    else input = el("input", { type: kind, id: `gold-${key}`, step: "any", placeholder: "default" });
    opts[key] = { input, kind };
    form.append(el("label", { for: `gold-${key}`, text: label }), input);
  }
  const note2 = el("input", { type: "text", placeholder: "a note carried into the evidence bundle (optional)", "aria-label": "note" });
  const typed = el("input.type.short", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": "type the project name to start" });
  const start = button("Start the golden run", { cls: "btn primary", disabled: true, onclick: async () => {
    const t = getTarget(); if (!t) return;
    const options = {};
    for (const [key, f] of Object.entries(opts)) {
      if (f.kind === "bool") { if (f.input.checked) options[key] = true; continue; }
      const v = f.input.value; if (v === "") continue;
      options[key] = f.kind === "number" ? Number(v) : v;
    }
    start.disabled = true;
    const r = await R.ctx.api.post("/api/golden/run", { ...t, confirm: typed.value, options, note: note2.value.trim() || null });
    if (!R || R.dead) return;
    R.startJob = r.ok ? r.data.job : null;
    R.startResult = r.ok ? null : { cls: r.status === 503 || r.status === 400 || r.status === 409 || r.status === 404 ? "act" : "fault", text: `${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})` };
    renderStart();
  } });
  const refresh = () => { const t = getTarget(); typed.placeholder = t ? `type ${t.confirm} to start` : ""; start.disabled = !t || typed.value !== t.confirm; };
  typed.addEventListener("input", refresh);
  box.querySelectorAll("select").forEach((s) => s.addEventListener("change", refresh));
  box.append(el("details", [el("summary", { text: "options (blank keeps the spec's own values)" }), form]), note2, el("div.actrow", [typed, start]));
  refresh();
  if (!projects.length && R.startMode === "project") box.append(note("no projects", "create one from a STEP file, or run the demo"));
  if (R.startResult) box.append(el("div.result." + R.startResult.cls, { text: R.startResult.text }));
  if (R.startJob) box.append(jobPanel(R.ctx, R.startJob, { onDone: (j) => {
    if (!R || R.dead) return;
    loadRuns().then(() => { const rid = j.result && j.result.run_id; if (rid && R && !R.dead) loadRun(j.project, rid); });
  } }));
}

// ---------- data ----------
async function loadRuns() {
  const r = await R.ctx.api.get(`/api/golden/runs${R.project ? "?project=" + encodeURIComponent(R.project) : ""}`);
  if (!R || R.dead) return;
  R.res.runs = r; R.runs = r.ok ? r.data : null;
  renderRuns(); renderFoot();
}

async function loadRun(project, runId) {
  R.runProject = project; R.run = runId; R.detail = null; R.detailRes = null; R.verify = null;
  renderRuns(); renderDetail();
  const r = await R.ctx.api.get(`/api/golden/run/${encodeURIComponent(project)}/${encodeURIComponent(runId)}`);
  if (!R || R.dead || R.run !== runId) return;
  R.detailRes = r; R.detail = r.ok ? r.data : null;
  renderRuns(); renderDetail(); renderInspector();
}

async function loadLedger() {
  const r = await R.ctx.api.get("/api/ledger?n=200");
  if (!R || R.dead) return;
  R.res.ledger = r; R.ledger = r.ok ? r.data : null;
  renderLedger(); renderFoot();
}

async function loadChain() {
  const r = await R.ctx.api.get("/api/ledger/simlab");
  if (!R || R.dead) return;
  R.res.chain = r; R.chain = r.ok ? r.data : null;
  renderChain(); renderFoot();
}

function setURL() {
  const u = new URL(location.href); u.searchParams.set("plate", "evidence");
  if (R.run && R.runProject) { u.searchParams.set("project", R.runProject); u.searchParams.set("run", R.run); }
  else { u.searchParams.delete("run"); if (R.project) u.searchParams.set("project", R.project); else u.searchParams.delete("project"); }
  history.replaceState(null, "", u);
}

function selectRun(project, runId) {
  if (R.run === runId && R.runProject === project) { R.run = null; R.runProject = null; R.detail = null; R.detailRes = null; R.verify = null; setURL(); renderRuns(); renderDetail(); renderInspector(); return; }
  R.runProject = project; R.run = runId; setURL();
  loadRun(project, runId);
}

function chip(vocabulary, word, title = null) {
  const { vocab, dom } = R.ctx;
  if (word === null || word === undefined || word === "") return dom.el("span.dim", { text: "not recorded" });
  return vocab.known(vocabulary, word) ? vocab.state(vocabulary, word, { title }) : dom.el("span", { text: String(word), title: `${word}: not a word of the ${vocabulary} vocabulary; shown as received` });
}

const fileURL = (rel) => `/api/golden/file/${encodeURIComponent(R.runProject)}/${encodeURIComponent(R.run)}/${rel.split("/").map(encodeURIComponent).join("/")}`;

// ---------- rendering ----------
function renderRuns() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table, button } = dom;
  const box = R.b.runs; clear(box);
  const names = (R.projects && Array.isArray(R.projects.projects) ? R.projects.projects : []).map((p) => p.name);
  const sel = el("select.sel#eviProject", { "aria-label": "project filter", onchange: (e) => { R.project = e.target.value; setURL(); loadRuns(); } });
  sel.append(el("option", { value: "", text: names.length ? `all projects (${names.length})` : "all projects" }));
  for (const n of names) sel.append(el("option", { value: n, text: n }));
  sel.value = names.includes(R.project) ? R.project : "";
  box.append(el("div.actrow", [el("label.lab", { for: "eviProject", text: "project" }), sel]));
  const abs = absence(R.res.runs, "golden runs");
  if (abs) box.append(abs);
  const d = R.runs;
  if (!d) { if (!abs) box.append(note("golden runs not loaded", "no reply yet")); R.ctx.coord(""); return; }
  const rows = Array.isArray(d.runs) ? d.runs : [];
  if (d.reason) box.append(note("no golden runs", d.reason, { act: true, detail: d.root || null }));
  box.append(table([
    { th: "run", render: (r) => button(r.run_id, { title: `open run ${r.run_id} of ${r.project}`, onclick: () => selectRun(r.project, r.run_id) }), cls: "nm" },
    { th: "project", key: "project" },
    { th: "created", render: (r) => format.utcShort(r.created_utc) }, { th: "finished", render: (r) => (r.finished_utc ? format.utcShort(r.finished_utc) : "not finished") },
    { th: "status", render: (r) => (r.unreadable ? el("span.ink-fault", { text: r.unreadable }) : chip("run", r.status)) },
    { th: "outcome", render: (r) => r.outcome || "not recorded" }, { th: "verdict", render: (r) => r.verdict || "not recorded" },
    { th: "grade", render: (r) => [el("b", { text: r.grade || "not recorded" }), el("span.sub", { text: GRADE_SENTENCE, style: { whiteSpace: "nowrap" } })] },
    { th: "exit", render: (r) => (r.exit_code === null || r.exit_code === undefined ? format.DASH : String(r.exit_code)), num: true },
    { th: "wall", render: (r) => format.dur(r.wall_s), num: true },
    { th: "result.json", render: (r) => (r.has_result ? "present" : "absent") },
  ], rows.map((r) => ({ ...r, __cls: R.run === r.run_id && R.runProject === r.project ? "eye" : null })), { empty: d.reason || `no golden runs under ${d.root || "$ACES_WORK"}`, caption: "golden runs" }));
  box.append(el("div.prose.dim", { text: `${GRADE_SENTENCE}; desktop grades are not rig results` }));
  R.ctx.coord(`${rows.length} golden run${rows.length === 1 ? "" : "s"}${R.project ? " · " + R.project : ""}`);
}

function renderDetail() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table, kv, button } = dom;
  const box = R.b.detail; clear(box);
  if (!R.run) { box.append(note("no run selected", "select a golden run to read its records, report and evidence")); return; }
  const abs = absence(R.detailRes, `run ${R.run}`);
  if (abs) box.append(abs);
  const d = R.detail;
  if (!d) { if (!abs) box.append(note(`run ${R.run}`, "loading")); return; }
  const run = d.run || {}, res = d.result || {}, val = d.validation || {};
  const k = (t) => el("span.k", { text: t }), b = (t, cls = "") => el("b" + (cls ? "." + cls : ""), { text: t === null || t === undefined || t === "" ? "not recorded" : String(t) });
  const grade = run.grade || res.grade || val.grade || null;
  // the grade line and, immediately after it, the fixed sentence (spec 4.3)
  box.append(el("div.line", [k("project"), b(run.project || R.runProject), k("run"), b(run.run_id || R.run), k("status"), chip("run", run.status), k("outcome"), b(run.outcome || res.outcome), k("verdict"), b(run.verdict || res.verdict), k("grade"), b(grade), el("span.ink-amber", { text: GRADE_SENTENCE })]));
  box.append(el("div.prose.dim", { text: (d.notes || ["no grade is a sign-off", "desktop grades are not rig results"]).join("; ") }));
  if (res.verdict_line) box.append(el("div.prose", [el("span.cap", { text: "verdict line (as written) " }), el("span", { text: `“${res.verdict_line}”` })]));
  if (d.result_absent_reason) box.append(note("result.json absent", d.result_absent_reason, { act: true }));
  box.append(el("div.line", [k("tool"), b(run.tool ? `${run.tool.name} ${run.tool.version} · python ${run.tool.python}` : null), k("created"), b(run.created_utc ? format.utc(run.created_utc) : null), k("finished"), b(run.finished_utc ? format.utc(run.finished_utc) : null), k("wall"), b(run.timings ? format.dur(run.timings.total_s) : null), k("exit"), b(run.exit_code), k("seed"), b(run.seed), k("demo"), b(run.demo || "none"), k("spec sha256"), b(format.shortHash(run.spec_sha256))]));
  if (run.resolution) box.append(el("div.line", [k("resolution"), b(`gen ${run.resolution.gen_mm} mm · fea ${run.resolution.fea_mm} mm · field ${run.resolution.field_h_mm} mm · check levels ${(run.resolution.check_levels || []).map((l) => `${l.h_mm} mm`).join(", ") || "none"}`)]));
  if (run.message) box.append(el("div.prose", [el("span.cap", { text: "message " }), el("span", { text: run.message })]));
  // stages
  const stages = Array.isArray(run.stages) ? run.stages : [];
  box.append(el("div.cap", { text: `stages · ${stages.length}` }));
  box.append(table([
    { th: "stage", key: "name", cls: "nm" }, { th: "status", render: (s) => chip("run", s.status) }, { th: "started", render: (s) => format.utcShort(s.started_utc) },
    { th: "wall", render: (s) => format.dur(s.wall_s), num: true }, { th: "note", render: (s) => s.note || format.DASH, cls: "body" },
  ], stages, { empty: "no stages recorded in run.json", caption: "stages of the run" }));
  // validation and grade reasons
  box.append(el("div.cap", { text: "validation · validation.json" }));
  if (d.validation) {
    const ic = val.independent_check || {}, conf = val.conforming || {};
    box.append(kv([["outcome", val.outcome], ["passed", val.passed === undefined ? null : String(val.passed)], ["tolerance", val.tol !== undefined ? String(val.tol) : null], ["rounds", val.final_round !== undefined ? `${val.final_round} (final)` : null], ["winner case", val.winner_case], ["winner params", val.winner_params ? Object.entries(val.winner_params).map(([key, v]) => `${key} ${format.num(v, 6)}`).join(" · ") : null],
      ["independent check", ic.ran === undefined ? null : `${ic.ran ? "ran" : "did not run"}${ic.passed === undefined ? "" : (ic.passed ? " · passed" : " · did not pass")}${ic.reason ? " · " + ic.reason : ""}`],
      ["conforming", conf.ran === undefined ? null : `${conf.ran ? "ran" : "did not run"} · ${conf.status || "status not recorded"}${conf.host ? " · " + conf.host : ""}${conf.reason ? " · " + conf.reason : ""}`],
      ["rig", val.rig ? JSON.stringify(val.rig).slice(0, 200) : "null: no rig work in this run"], ["grade", val.grade, "act"], ["grade reasons", Array.isArray(val.grade_reasons) ? val.grade_reasons.join("; ") : null], ["verdict reasons", Array.isArray(val.verdict_reasons) ? (val.verdict_reasons.length ? val.verdict_reasons.join("; ") : "none") : null]]));
    if (val.scope) box.append(el("div.prose.dim", { text: `scope: ${val.scope}` }));
  } else box.append(el("div.prose.dim", { text: "validation.json absent" }));
  // evidence
  const ev = res.evidence || {}; const lg = d.evidence_ledgers;
  box.append(el("div.cap", { text: "evidence · bundle and ledgers" }));
  box.append(kv([["bundle sha256", ev.bundle_sha256], ["simlab ledger entry", ev.simlab_ledger_entry !== undefined ? String(ev.simlab_ledger_entry) : null], ["aces ledger entry", ev.aces_ledger_entry !== undefined ? String(ev.aces_ledger_entry) : null], ["ledgers ok (as the run recorded)", ev.ledgers_ok === undefined ? null : String(ev.ledgers_ok)],
    ["simlab ledger", lg && lg.simlab ? `${lg.simlab.ledger} · entry ${lg.simlab.entry} of ${lg.simlab.entries} · verify ${lg.simlab.verify ? (lg.simlab.verify.ok ? "ok" : "not ok") : "not recorded"}` : "evidence/ledgers.json absent"],
    ["aces ledger", lg && lg.aces ? `${lg.aces.path || ""} · entry ${lg.aces.entry ?? "?"} of ${lg.aces.entries ?? "?"}${lg.aces.verify ? " · verify " + (lg.aces.verify.ok ? "ok" : "not ok") : ""}` : null]]));
  // compute used
  const cu = d.compute_used; const curows = Array.isArray(cu) ? cu : (cu && Array.isArray(cu.rows) ? cu.rows : []);
  box.append(el("div.cap", { text: `compute used · ${curows.length} rows${cu && !Array.isArray(cu) && cu.schema ? " · " + cu.schema : ""}` }));
  box.append(table([
    { th: "step", key: "step", cls: "nm" }, { th: "computation", key: "computation", cls: "body" }, { th: "solver", key: "solver", cls: "body" }, { th: "device", key: "device" },
    { th: "resolution mm", render: (r) => format.num(r.resolution_mm, 4), num: true }, { th: "size", key: "size", cls: "body" }, { th: "count", render: (r) => String(r.count ?? format.DASH), num: true },
    { th: "wall", render: (r) => format.dur(r.wall_s), num: true }, { th: "grade", key: "grade" }, { th: "role", render: (r) => r.role || "none" }, { th: "source", key: "source", cls: "body" }, { th: "note", render: (r) => r.note || format.DASH, cls: "body" },
  ], curows, { empty: d.compute_used ? "compute_used.json holds no rows" : "compute_used.json absent", caption: "compute used", cls: "wrapnames" }));
  // sweep
  const sw = d.sweep;
  box.append(el("div.cap", { text: `sweep · sweep.csv${sw ? ` · ${sw.rows.length} rows` : ""}` }));
  if (sw && Array.isArray(sw.columns) && sw.columns.length) {
    const cols = sw.columns.map((c, i) => ({ th: c, render: (row) => { const v = row[i]; if (c === "status") return chip("case", v); if (c === "design_status" || c === "grade" || c === "device") return v === null || v === undefined ? format.DASH : String(v); return typeof v === "number" ? format.num(v, 5) : (v === null || v === undefined ? format.DASH : String(v)); }, cls: i === 0 ? "nm" : null, num: typeof (sw.rows[0] || [])[i] === "number" }));
    box.append(table(cols, sw.rows, { empty: "no sweep rows", caption: "sweep table" }));
  } else box.append(el("div.prose.dim", { text: "sweep.csv absent" }));
  // winner and figures
  const w = d.winner || {}; const wj = w.json || {};
  box.append(el("div.cap", { text: "winner · winner/" }));
  if (w.json || (w.files && w.files.length)) {
    box.append(kv([["case", wj.case_id || (res.winner && res.winner.case_id)], ["params", wj.params ? Object.entries(wj.params).map(([key, v]) => `${key} ${format.num(v, 6)}`).join(" · ") : (res.winner && res.winner.params ? Object.entries(res.winner.params).map(([key, v]) => `${key} ${format.num(v, 6)}`).join(" · ") : null)],
      ["metrics", wj.metrics ? Object.entries(wj.metrics).map(([key, v]) => `${key} ${format.num(v, 6)}`).join(" · ") : (res.winner && res.winner.metrics ? Object.entries(res.winner.metrics).map(([key, v]) => `${key} ${format.num(v, 6)}`).join(" · ") : null)], ["stl sha256", res.winner ? format.shortHash(res.winner.stl_sha256) : null]]));
    box.append(el("div.line", [k("files"), ...(w.files || []).map((f) => { const nm = typeof f === "string" ? f : f.name; return el("a", { href: fileURL("winner/" + nm), target: "_blank", rel: "noopener", text: nm }); })]));
  } else if (d.candidate) box.append(el("div.prose.dim", { text: `no winner; candidate/ holds ${(d.candidate.files || []).length} files` }));
  else box.append(el("div.prose.dim", { text: "no winner/ and no candidate/ directory" }));
  const figs = Array.isArray(d.figures) ? d.figures : [];
  box.append(el("div.cap", { text: `figures · ${figs.length}` }));
  if (figs.length) box.append(el("div.cards", figs.map((f) => el("a.card", { href: fileURL("figures/" + f), target: "_blank", rel: "noopener", title: `open figures/${f}` }, [el("img", { src: fileURL("figures/" + f), alt: `figure ${f}`, style: { width: "100%", height: "auto", display: "block", borderRadius: "2px" } }), el("div.tag", { text: f })]))));
  else box.append(el("div.prose.dim", { text: "no figures/ directory" }));
  // report
  const rp = d.report;
  const prov = rp && Array.isArray(rp.provenance) ? rp.provenance.length : (rp && rp.provenance && typeof rp.provenance === "object" ? Object.keys(rp.provenance).length : null);
  const secs = rp && rp.sections ? (Array.isArray(rp.sections) ? rp.sections.length : Object.keys(rp.sections).length) : null;
  box.append(el("div.cap", { text: "report · report.json and report.html" }));
  box.append(el("div.line", [k("provenance pointers"), b(prov === null ? "report.json absent" : String(prov)), k("sections"), b(secs === null ? null : String(secs)), k("report verdict"), b(rp ? rp.verdict : null), k("report grade"), b(rp ? rp.grade : null)]));
  const hasHtml = (d.files || []).some((f) => f.rel === "report.html");
  if (hasHtml) {
    box.append(el("iframe", { src: fileURL("report.html"), sandbox: "", title: `report.html of golden run ${R.run} (sandboxed: no scripts, no origin)`, style: { width: "100%", height: "520px", border: "1px solid var(--line-2)", borderRadius: "2px", background: "var(--chalk)" } }));
    box.append(el("div.line", [k("report files"), ...["report.html", "report.md", "report.json"].filter((f) => (d.files || []).some((x) => x.rel === f)).map((f) => el("a", { href: fileURL(f), target: "_blank", rel: "noopener", text: f }))]));
  } else box.append(el("div.prose.dim", { text: "report.html absent from the run directory" }));
  // verify
  box.append(el("div.cap", { text: "verify · aces golden verify <run_dir> --json --no-model (a read, not ledgered)" }));
  box.append(el("div.actrow", [button("Verify", { cls: "btn primary", disabled: R.verifying, title: "runs aces golden verify on this desktop (up to 300 s); the tool's own checks are shown", onclick: verifyRun }), R.verify && R.verify.result ? el("span.result" + (R.verify.result.cls ? "." + R.verify.result.cls : ""), { text: R.verify.result.text }) : null]));
  if (R.verify && R.verify.data) {
    const v = R.verify.data;
    box.append(el("div.line", [k("ok"), b(String(v.ok)), k("exit"), b(v.exit), k("ran"), b(v.ran_at ? format.utc(v.ran_at) : null), k("checks"), b(String((v.checks || []).length))]));
    box.append(table([
      { th: "check", key: "name", cls: "nm" },
      { th: "result", render: (c) => (c.ok === true ? el("span", [dom.icon("check", "ic check"), " pass"]) : c.ok === false ? dom.ink("fault", "fail") : dom.ink("chalk", "not run")) },
      { th: "detail (the tool's own text)", render: (c) => c.detail || format.DASH, cls: "body" },
    ], v.checks || [], { empty: "the tool reported no checks", caption: "verify checks" }));
  }
  // files and events
  const files = Array.isArray(d.files) ? d.files : [];
  box.append(el("div.cap", { text: `files · ${files.length}${d.files_truncated ? " (listing capped)" : ""}` }));
  box.append(table([
    { th: "file", render: (f) => (/\.(joblib|pkl|npz|brep|3mf)$/i.test(f.rel) ? el("span", { text: f.rel, title: "streamed only as octet-stream, never loaded" }) : el("a", { href: fileURL(f.rel), target: "_blank", rel: "noopener", text: f.rel })), cls: "nm" },
    { th: "bytes", render: (f) => format.bytes(f.bytes), num: true },
  ], files.slice(0, 60), { empty: "no files listed", caption: `files of run ${R.run} (first 60)`, cls: "wrapnames" }));
  if (files.length > 60) box.append(el("div.prose.dim", { text: `${files.length - 60} more files not listed here` }));
  const ev2 = Array.isArray(d.events_tail) ? d.events_tail : null;
  box.append(el("div.cap", { text: `events · events.jsonl${ev2 ? ` · last ${ev2.length}` : ""}` }));
  if (ev2 && ev2.length) {
    const pre = el("pre.log", { "aria-label": "events.jsonl tail" });
    ev2.forEach((e, i) => { pre.append(el("span", { text: typeof e === "string" ? e : `${e.ts || ""} ${e.kind || ""} ${e.stage || ""} ${e.msg || ""}`.replace(/\s+/g, " ").trim() })); if (i < ev2.length - 1) pre.append("\n"); });
    box.append(pre);
  } else box.append(el("div.prose.dim", { text: "events.jsonl absent" }));
  if (Array.isArray(d.read_errors) && d.read_errors.length) box.append(note("records that could not be read", d.read_errors.map((e) => `${e.rel}: ${e.error}`).join("; "), { fault: true }));
}

async function verifyRun() {
  if (!R || R.verifying || !R.run) return;
  R.verifying = true; R.verify = { result: { cls: "live", text: "verifying…" } }; renderDetail();
  const r = await R.ctx.api.post(`/api/golden/verify/${encodeURIComponent(R.runProject)}/${encodeURIComponent(R.run)}`, {});
  if (!R || R.dead) return;
  R.verifying = false;
  if (r.ok && r.data) {
    const v = r.data; const n = (v.checks || []).length, pass = (v.checks || []).filter((c) => c.ok === true).length, notrun = (v.checks || []).filter((c) => c.ok === null || c.ok === undefined).length;
    R.verify = { data: v, result: { cls: v.ok ? "" : "fault", text: `${v.ok ? "verify ok" : "verify did not pass"} · ${pass} of ${n} checks pass, ${notrun} not run · exit ${v.exit}` } };
  } else R.verify = { result: { cls: r.status === 503 || r.status === 409 ? "act" : "fault", text: `${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})${r.body && r.body.stderr_tail ? " · " + String(r.body.stderr_tail).slice(-300) : ""}` } };
  renderDetail();
}

function renderLedger() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table, button } = dom;
  const box = R.b.ledger; clear(box);
  const abs = absence(R.res.ledger, "desktop ledger");
  if (abs) box.append(abs);
  const d = R.ledger;
  const k = (t) => el("span.k", { text: t }), b = (t, cls = "") => el("b" + (cls ? "." + cls : ""), { text: t === null || t === undefined ? "not reported" : String(t) });
  if (d) {
    const rows = Array.isArray(d.entries) ? d.entries : [];
    box.append(el("div.line", [k("path"), b(d.path), k("entries"), b(d.n_total), k("shown"), b(rows.length)]));
    box.append(el("div.actrow", [button("Verify ledger", { disabled: R.ledgerVerifying, title: "aces.ledger.verify: recomputes every hash and the chain (a read)", onclick: verifyLedger }), R.ledgerVerify ? el("span.result" + (R.ledgerVerify.cls ? "." + R.ledgerVerify.cls : ""), { text: R.ledgerVerify.text }) : null]));
    box.append(table([
      { th: "n", key: "n", num: true }, { th: "recorded", render: (e) => format.utc(e.ts) }, { th: "kind", key: "kind", cls: "nm" },
      { th: "payload", render: (e) => (e.payload && typeof e.payload === "object" ? Object.entries(e.payload).slice(0, 6).map(([key, v]) => `${key}: ${typeof v === "object" ? JSON.stringify(v).slice(0, 60) : String(v).slice(0, 60)}`).join(" · ") : format.DASH), cls: "body" },
      { th: "hash", render: (e) => format.shortHash(e.hash) }, { th: "prev", render: (e) => (e.prev && /^0+$/.test(e.prev) ? "genesis" : format.shortHash(e.prev)) },
    ], rows, { empty: "the ledger holds no entries", caption: "desktop operator ledger, newest first" }));
  } else if (!abs) box.append(note("ledger not loaded", "no reply yet"));
}

async function verifyLedger() {
  if (!R || R.ledgerVerifying) return;
  R.ledgerVerifying = true; R.ledgerVerify = { cls: "live", text: "verifying…" }; renderLedger();
  const r = await R.ctx.api.get("/api/ledger/verify");
  if (!R || R.dead) return;
  R.ledgerVerifying = false;
  if (r.ok && r.data) R.ledgerVerify = { cls: r.data.ok ? "" : "fault", text: `${r.data.ok ? "ok" : "FAIL"} · ${r.data.n} entries · ${r.data.message}${r.data.first_bad ? " · first bad entry " + r.data.first_bad : ""}` };
  else R.ledgerVerify = { cls: r.status === 404 || r.status === 503 ? "act" : "fault", text: `${(r.meta && r.meta.error) || r.error || "no reply"}${r.status ? ` (HTTP ${r.status}${r.code ? ", " + r.code : ""})` : ""}` };
  renderLedger();
}

function renderChain() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table } = dom;
  const box = R.b.chain; clear(box);
  const abs = absence(R.res.chain, "SIMLAB evidence chain");
  if (abs) box.append(abs);
  const d = R.chain;
  if (!d) { if (!abs) box.append(note("chain not loaded", "no reply yet")); return; }
  const rows = Array.isArray(d.entries) ? d.entries : [];
  const v = d.verify;
  const k = (t) => el("span.k", { text: t }), b = (t, cls = "") => el("b" + (cls ? "." + cls : ""), { text: t === null || t === undefined ? "not reported" : String(t) });
  box.append(el("div.line", [k("entries"), b(rows.length), k("console verify"), v ? b(`${v.ok ? "ok" : "not ok"} · ${v.entries} entries · ${(v.problems || []).length} problems · head ${format.shortHash(v.head)}`, v.ok ? "" : "ink-fault") : b(d.verify_note || "not reported")]));
  box.append(el("div.prose.dim", { text: d.note || "rig-side chain; verified by the SIMLAB console, not by this desktop" }));
  box.append(table([
    { th: "recorded", render: (e) => format.utc(e.ts) }, { th: "experiment", key: "experiment", cls: "nm" }, { th: "sha256", render: (e) => format.shortHash(e.sha256) },
    { th: "previous", render: (e) => (e.previous ? format.shortHash(e.previous) : "none (first entry)") }, { th: "cases", render: (e) => `${e.n_finished ?? "?"} of ${e.n_cases ?? "?"} finished` }, { th: "company", render: (e) => e.company || "not recorded" },
    { th: "bundle", render: (e) => e.path || format.DASH, cls: "body" },
  ], rows.slice().reverse(), { empty: "the console reports no evidence entries", caption: "rig-side SIMLAB evidence ledger, newest first", cls: "wrapnames" }));
}

function renderFoot() {
  const { dom, format } = R.ctx; const { clear } = dom;
  const items = [];
  for (const [label, name] of [["golden runs", "runs"], ["ledger", "ledger"], ["SIMLAB chain", "chain"]]) {
    const r = R.res[name]; const a = r && r.meta && r.meta.fetched_utc ? format.age(r.meta.fetched_utc) : null;
    items.push(`${label} ${a === null ? (r && r.ok === false ? "unavailable" : "not loaded") : format.ago(a)}`);
  }
  items.push(GRADE_SENTENCE);
  clear(R.b.foot); R.b.foot.append(dom.facts(items));
}

function renderInspector() {
  const { dom, format } = R.ctx; const { block, kv, el } = dom;
  const d = R.detail;
  if (d && d.run) {
    const run = d.run, res = d.result || {}, ev = res.evidence || {};
    R.ctx.inspector.show([
      block("run", kv([["status", chip("run", run.status)], ["verdict", run.verdict], ["grade", run.grade, "act"], ["", GRADE_SENTENCE, "dim"], ["wall", run.timings ? format.dur(run.timings.total_s) : null], ["stages", String((run.stages || []).length)]])),
      block("evidence", kv([["bundle", format.shortHash(ev.bundle_sha256)], ["simlab entry", ev.simlab_ledger_entry !== undefined ? String(ev.simlab_ledger_entry) : null], ["aces entry", ev.aces_ledger_entry !== undefined ? String(ev.aces_ledger_entry) : null], ["ledgers ok", ev.ledgers_ok === undefined ? null : String(ev.ledgers_ok)]])),
    ], { name: run.run_id || R.run, coord: `${run.project || R.runProject} · ${run.grade || "grade not recorded"} · ${GRADE_SENTENCE}` });
    return;
  }
  const rows = R.runs && Array.isArray(R.runs.runs) ? R.runs.runs : [];
  const lg = R.ledger;
  R.ctx.inspector.show([
    block("golden runs", kv([["runs", String(rows.length)], ["rig-verified", String(rows.filter((r) => r.grade === "rig-verified").length)], ["verified-desktop", String(rows.filter((r) => r.grade === "verified-desktop").length)], ["preview-grade", String(rows.filter((r) => r.grade === "preview-grade").length)], ["", GRADE_SENTENCE, "dim"]])),
    block("ledgers", kv([["desktop entries", lg ? String(lg.n_total) : "not loaded"], ["SIMLAB chain entries", R.chain ? String((R.chain.entries || []).length) : "not loaded"]])),
  ], { name: "evidence", coord: "select a golden run", ringed: false });
}
