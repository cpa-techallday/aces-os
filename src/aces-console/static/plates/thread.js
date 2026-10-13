// Thread plate: the conversation is the home screen of the aces console. The composer talks to the loop
// (/api/loop/*): a message becomes a plan card (strike items, approve), an approved plan becomes editable
// spec drafts, a confirmed draft becomes a golden run whose job chip turns into a result link when the run
// lands. Nothing runs without the plan being visible and a spec being confirmed. Above the thread, one line
// of plain-words rig truth; absence is drawn with its reason, never guessed.
// Bindings: /api/loop/message, /api/loop/threads, /api/loop/thread/{id}, /api/loop/plan/approve,
// /api/loop/spec/confirm, /api/job/{id}; stamps rig.status, sim.capacity, jobs.
// Deep link: ?plate=thread&thread=<id>.

const $tid = () => { try { return localStorage.getItem("aces.loop.thread"); } catch { return null; } };
let R = null;

export async function mount(root, ctx) {
  const { el } = ctx.dom;
  R = {
    ctx, root, dead: false, pollers: new Map(),
    thread: null, threadId: num(ctx.params.get("thread")) !== null ? ctx.params.get("thread") : $tid(),
    state: "", busy: false,
    rigStatus: null, capacity: null, jobs: null,
    b: {
      truth: el("div.truth", { role: "status" }),
      log: el("div.t-log", { role: "log", "aria-live": "polite", "aria-label": "conversation" }),
    },
  };
  const ta = el("textarea", {
    rows: "3", "aria-label": "describe a part, a machine, or a change",
    placeholder: "Describe a part, a machine, or a change — e.g. “a bracket that holds 15 kg and survives 8 g shock, as light as possible”  (Ctrl+Enter sends)",
  });
  const send = ctx.dom.button("Send", { cls: "btn primary t-send", onclick: () => sendMessage(ta) });
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send.click(); });
  const newBtn = ctx.dom.button("New conversation", { cls: "btn", onclick: newThread, title: "start a fresh thread (this one stays listed)" });
  // attach: models (.step/.stp/.stl), photos, documents — uploaded first, then the message rides after
  const fileIn = el("input", { type: "file", multiple: "multiple", style: "display:none",
                               accept: ".step,.stp,.stl,.png,.jpg,.jpeg,.webp,.gif,.pdf,.txt,.md,.csv,.json",
                               "aria-label": "attach files" });
  R.pendingFiles = [];
  const chips = el("span.t-files");
  fileIn.addEventListener("change", () => {
    for (const f of fileIn.files || []) R.pendingFiles.push(f);
    fileIn.value = "";
    renderFileChips(chips);
  });
  const attach = ctx.dom.button("📎 Attach", {
    cls: "btn", title: "attach a STEP/STL model, a photo, or a document — it joins the thread before your message",
    onclick: () => fileIn.click(),
  });
  R.b.composer = el("div.t-composer", [
    ta,
    el("div.t-composer-row", [newBtn, attach, chips, el("span.t-state", { text: R.state }), send]),
    fileIn,
  ]);
  R.b.ta = ta;
  R.b.chips = chips;

  root.append(el("div.t-wrap", [
    el("div.t-head", [el("div.t-title", { text: "What are we building?" }), R.b.truth]),
    R.b.log,
    R.b.composer,
  ]));

  ctx.events.on("rig.status", (st) => { R.rigStatus = st; renderTruth(); });
  ctx.events.on("sim.capacity", (st) => { R.capacity = st; renderTruth(); });
  ctx.events.on("jobs", () => { pollRuns(true); });

  renderTruth();
  renderLog();
  if (R.threadId) await loadThread(R.threadId);
}

export function unmount() {
  if (!R) return;
  R.dead = true;
  for (const p of R.pollers.values()) clearInterval(p);
  R = null;
}

function num(v) { const n = Number(v); return Number.isFinite(n) && v !== null && v !== "" ? v : null; }

// ---------- plain-words rig truth ----------
function renderTruth() {
  const { el, clear } = R.ctx.dom;
  const box = R.b.truth; clear(box);
  const bits = [];
  const st = R.rigStatus;
  if (st && st.ok !== false && st.payload && st.payload.mode) bits.push([`rig is in ${String(st.payload.mode).toUpperCase()} mode`, st.payload.switching ? "warn" : "ok"]);
  else if (st && st.ok === false) bits.push([`rig status unreachable${st.error ? ": " + st.error : ""}`, "bad"]);
  else bits.push(["rig status not reported yet", "dim"]);
  const cap = R.capacity && R.capacity.ok !== false ? R.capacity.payload : null;
  if (cap) {
    const gpus = cap.gpus || cap.cards || [];
    const free = Array.isArray(gpus) ? gpus.filter((g) => !g.lease && !g.job).length : null;
    if (free !== null) bits.push([`${free} of ${gpus.length} GPUs free`, free > 0 ? "ok" : "warn"]);
    if (cap.queued !== undefined && cap.queued !== null) bits.push([`${cap.queued} job${cap.queued === 1 ? "" : "s"} queued`, cap.queued > 0 ? "warn" : "dim"]);
  }
  for (const [text, tone] of bits) box.append(el("span.t-bit." + tone, { text }));
}

// ---------- thread state ----------
async function loadThread(tid) {
  const r = await R.ctx.api.get(`/api/loop/thread/${tid}`);
  if (!R || R.dead) return;
  if (r.ok) { R.thread = r.data.thread; R.threadId = tid; storeTid(tid); renderLog(); pollRuns(false); }
  else {
    R.thread = null; R.threadId = null; storeTid(null);
    renderLog();
  }
}

function storeTid(tid) {
  try { if (tid === null) localStorage.removeItem("aces.loop.thread"); else localStorage.setItem("aces.loop.thread", tid); } catch { }
}

function newThread() {
  R.thread = null; R.threadId = null; storeTid(null);
  const u = new URL(location.href); u.searchParams.delete("thread"); history.replaceState(null, "", u);
  renderLog();
}

// ---------- rendering ----------
function renderLog() {
  const { el, clear } = R.ctx.dom;
  const box = R.b.log; clear(box);
  const th = R.thread;
  if (!th || !th.messages || !th.messages.length) {
    box.append(el("div.t-empty", [
      el("div.t-empty-title", { text: "Start with one sentence." }),
      el("div.t-empty-body", { text: "Say what the thing is and what it must do — load it carries, heat it sheds, speed it sees, space it fits. Attach a STEP, a photo, or a drawing with 📎 for context. A plan appears here before anything runs; nothing burns compute until you approve it." }),
    ]));
    return;
  }
  const lastOf = {};
  th.messages.forEach((m, i) => { if (m.card && m.card.plan) lastOf.plan = i; if (m.card && m.card.drafts) lastOf.drafts = i; });
  th.messages.forEach((m, i) => box.append(renderMessage(m, { plan: i === lastOf.plan, drafts: i === lastOf.drafts })));
  setTimeout(() => { box.scrollTop = box.scrollHeight; }, 0);
}

function renderMessage(m, live = { plan: false, drafts: false }) {
  const { el } = R.ctx.dom;
  const mine = m.role === "user";
  const body = el("div.t-body", { text: m.text });
  const kids = [el("div.t-who", { text: mine ? "you" : "aces loop" }), body];
  if (m.card && m.card.upload) kids.push(uploadCard(m.card.upload));
  if (!mine && m.card) {
    if (m.card.plan) kids.push(planCard(m.card.plan, live.plan));
    if (m.card.drafts) kids.push(draftsCard(m.card.drafts, live.drafts));
    if (m.card.run) kids.push(runChip(m.card.run));
  }
  return el("div.t-msg" + (mine ? ".user" : ".agent"), kids);
}

// an upload: photos render inline; models and documents are chips that download from the file route
function uploadCard(up) {
  const { el } = R.ctx.dom;
  const href = `/api/loop/file/${up.id}`;
  if (up.kind === "photo") {
    const img = el("img.t-render", { src: href, alt: up.name, loading: "lazy" });
    img.addEventListener("error", () => { img.remove(); });
    return el("div.t-card.t-upload", [img, el("div.t-plan-item-sub", { text: `${up.name} · ${fmtSize(up.size)}` })]);
  }
  const tag = up.kind === "model" ? (up.ext === ".stl" ? "mesh — CFD body, not a design space" : "model — usable as the design space") : "document";
  return el("div.t-card.t-upload", [
    el("a.t-dl", { href, text: `⭳ ${up.name}`, target: "_blank", rel: "noopener" }),
    el("div.t-plan-item-sub", { text: `${tag} · ${fmtSize(up.size)}` }),
  ]);
}

function fmtSize(n) {
  if (typeof n !== "number") return "";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

// a plan card: items with strike toggles, then approve
function planCard(plan, interactive) {
  const { el, button } = { el: R.ctx.dom.el, button: R.ctx.dom.button };
  const struck = new Set();
  const list = el("div.t-plan-items");
  for (const it of plan.items) {
    const row = el("div.t-plan-item" + (it.wired ? "" : ".unwired"));
    const strike = el("button.t-strike", { text: "✕", title: "strike this item from the plan", onclick: () => { struck.has(it.key) ? struck.delete(it.key) : struck.add(it.key); row.classList.toggle("struck"); } });
    const body = el("div.t-plan-item-body", [
      el("div.t-plan-item-title", { text: it.title }),
      el("div.t-plan-item-sub", { text: `${it.physics.join(" + ")} · answers: ${it.answers}` }),
      it.wired ? null : el("div.t-plan-item-reason", { text: it.reason || "not wired" }),
    ].filter(Boolean));
    row.append(it.wired && interactive ? strike : el("span.t-strike.dim", { text: it.wired ? "·" : "—", title: it.wired ? "from an earlier plan" : "not wired; runs nothing" }), body);
    list.append(row);
  }
  const note = plan.missing_geometry ? el("div.t-plan-note", { text: "Before anything runs: the design space — attach a STEP with the 📎 button below, pick a demo (root-mount, bone), or name an envelope like 200x150x40 mm in your next message." }) : null;
  if (!interactive) return el("div.t-card.t-plan.superseded", [
    el("div.t-card-title", { text: `Plan — ${plan.wired_count} wired item${plan.wired_count === 1 ? "" : "s"} (superseded)` }),
    list, note].filter(Boolean));
  const state = el("span.t-state");
  const approve = button("Approve plan → spec drafts", {
    cls: "btn primary",
    onclick: async () => {
      const keep = plan.items.filter((i) => i.wired && !struck.has(i.key)).map((i) => i.key);
      if (!keep.length) { state.textContent = "nothing left to approve — every wired item is struck"; return; }
      approve.disabled = true;
      const r = await R.ctx.api.post("/api/loop/plan/approve", { thread: R.threadId, keep });
      approve.disabled = false;
      if (!R || R.dead) return;
      if (r.ok) { await loadThread(R.threadId); }
      else state.textContent = r.error || `HTTP ${r.status}`;
    },
  });
  if (!plan.wired_count) approve.disabled = true;
  return el("div.t-card.t-plan", [
    el("div.t-card-title", { text: `Plan — ${plan.wired_count} wired item${plan.wired_count === 1 ? "" : "s"}` }),
    list,
    note,
    el("div.t-card-actions", [state, approve]),
  ].filter(Boolean));
}

// drafts card: one editable spec per item, with its missing list and a confirm row
function draftsCard(drafts, interactive) {
  const { el } = R.ctx.dom;
  const box = el("div.t-card.t-drafts" + (interactive ? "" : ".superseded"), [el("div.t-card-title", { text: interactive ? "Spec drafts — edit, then confirm to run" : "Spec drafts (superseded)" })]);
  for (const key of Object.keys(drafts)) box.append(draftEditor(key, interactive));
  return box;
}

function draftEditor(key, interactive) {
  const { el, button } = R.ctx.dom;
  const draft = (R.thread.drafts || {})[key];
  if (!draft) return el("div.t-plan-note", { text: `draft ${key} is not in this thread any more` });
  const missing = draft._missing || [];
  const ta = el("textarea.t-spec", {
    rows: "14", "aria-label": `spec draft for ${key}`,
    text: JSON.stringify(draft, null, 1),
    spellcheck: "false",
  });
  if (!interactive) ta.setAttribute("readonly", "readonly");
  const name = el("input.t-project", { type: "text", placeholder: "project name, e.g. radar-bracket", "aria-label": "project name", value: draft.name || "" });
  const state = el("span.t-state");
  const confirm = button("Run this spec", {
    cls: "btn primary",
    title: "aces golden run --spec (confirm is the project name); one golden run at a time",
    disabled: !interactive,
    onclick: async () => {
      let spec;
      try { spec = JSON.parse(ta.value); } catch (e) { state.textContent = `spec is not valid JSON: ${e.message}`; return; }
      const project = name.value.trim();
      if (!project) { state.textContent = "name the project first (lower case, digits, - and _)"; return; }
      confirm.disabled = true;
      state.textContent = "starting…";
      const r = await R.ctx.api.post("/api/loop/spec/confirm", { thread: R.threadId, item: key, spec, confirm: project });
      if (!R || R.dead) return;
      if (r.ok) { await loadThread(R.threadId); }
      else { state.textContent = r.error || `HTTP ${r.status}`; confirm.disabled = false; }
    },
  });
  return el("div.t-draft", [
    el("div.t-draft-head", [el("strong", { text: key })]),
    missing.length ? el("ul.t-missing", missing.map((x) => el("li", { text: x }))) : null,
    ta,
    el("div.t-card-actions", [name, state, confirm]),
  ].filter(Boolean));
}

// a run chip: job state, turning into a result link when the run lands
function runChip(run) {
  const { el } = R.ctx.dom;
  const st = el("span.t-run-state", { text: "queued…" });
  const link = el("a.t-run-link", { href: `?plate=evidence&project=${encodeURIComponent(run.project)}&run=latest`, text: "open in Evidence" });
  const chip = el("div.t-card.t-run", [
    el("div.t-card-title", { text: `golden run · ${run.project}` }),
    el("div.t-run-row", [el("span.mono", { text: `job ${run.job}` }), st, link]),
  ]);
  chip.dataset.job = String(run.job);
  chip.dataset.project = run.project;
  watchJob(chip, run.job);
  return chip;
}

async function watchJob(chip, jobId) {
  const r = await R.ctx.api.get(`/api/job/${jobId}`);
  if (!R || R.dead || !chip.isConnected) return;
  const st = chip.querySelector(".t-run-state");
  if (!r.ok) { st.textContent = `job state unavailable: ${r.error}`; return; }
  const j = r.data;
  const status = j.status || (j.exit !== null && j.exit !== undefined ? "exited" : "unknown");
  if (status === "running") { st.textContent = "running…"; st.className = "t-run-state live"; return; }
  if (j.exit === 0) { st.textContent = "finished — result below"; st.className = "t-run-state done"; }
  else if (j.exit !== null && j.exit !== undefined) { st.textContent = `exited ${j.exit} — see the job log`; st.className = "t-run-state fault"; }
  else return;
  if (!chip.dataset.resultLoaded) { chip.dataset.resultLoaded = "1"; loadResult(chip, chip.dataset.project); }
}

// ---------- result card: the run's own words and numbers, nothing invented ----------
const METRIC_LABELS = { mass_kg: "mass (kg)", mass_voxel_kg: "voxel mass (kg)", mesh_volume_mm3: "mesh volume (mm³)", voxel_volume_mm3: "voxel volume (mm³)", compliance_Nmm: "compliance (N·mm)", umax_mm: "max displacement (mm)", vm_max_MPa: "max von Mises (MPa)", vm_p99_MPa: "p99 von Mises (MPa)", vm_p95_MPa: "p95 von Mises (MPa)" };

async function latestRunId(project) {
  const r = await R.ctx.api.get(`/api/golden/runs?project=${encodeURIComponent(project)}`);
  if (!r.ok || !r.data || !Array.isArray(r.data.runs) || !r.data.runs.length) return null;
  return r.data.runs[0].run_id;   // rows are newest first
}

async function loadResult(chip, project) {
  const { el } = R.ctx.dom;
  const rid = await latestRunId(project);
  if (!R || R.dead || !chip.isConnected) return;
  if (!rid) { chip.append(el("div.t-plan-note", { text: "no golden runs found for this project yet" })); return; }
  const r = await R.ctx.api.get(`/api/golden/run/${encodeURIComponent(project)}/${rid}`);
  if (!R || R.dead || !chip.isConnected) return;
  if (!r.ok) {
    chip.append(el("div.t-plan-note", { text: `run detail unavailable: ${r.error || "HTTP " + r.status}` }));
    return;
  }
  const d = r.data;
  const res = d.result || {};
  const card = el("div.t-card.t-result");
  const grade = d.grade || res.grade || null;
  const verdictLine = res.verdict_line || d.verdict || null;
  card.append(el("div.t-result-head", [
    el("span.t-grade." + (grade === "rig-verified" ? "best" : grade === "verified-desktop" ? "good" : "plain"), { text: grade || (d.status || "status not reported") }),
    el("span.t-outcome." + ((d.outcome || res.outcome) === "pass" ? "pass" : "other"), { text: d.outcome || res.outcome || d.status || "unknown" }),
  ]));
  if (verdictLine) card.append(el("div.t-verdict", { text: verdictLine }));
  if (d.result_absent_reason) card.append(el("div.t-plan-note", { text: d.result_absent_reason }));
  // scorecard: the winner's metrics as the sheet rows
  const w = res.winner || {};
  const metrics = w.metrics && typeof w.metrics === "object" ? w.metrics : null;
  if (metrics) {
    const rows = Object.entries(metrics).filter(([k, v]) => typeof v === "number");
    if (rows.length) {
      const tbl = el("table.t-score");
      for (const [k, v] of rows) tbl.append(el("tr", [el("td", { text: METRIC_LABELS[k] || k }), el("td.mono", { text: fmtNum(v) })]));
      card.append(tbl);
    }
  }
  // downloads + renders: the file route serves the run's own files
  const base = `/api/golden/file/${encodeURIComponent(project)}/${rid}/`;
  const dl = el("div.t-downloads");
  const stl = w.stl || (d.run && d.run.paths && d.run.paths.stl) || null;
  if (stl) dl.append(fileLink(base + stl, "STL — the solved part"));
  if (res.report && res.report.html) dl.append(fileLink(base + res.report.html, "report (HTML)"));
  if (res.report && res.report.md) dl.append(fileLink(base + res.report.md, "report (markdown)"));
  if (w.json) dl.append(fileLink(base + w.json, "winner.json"));
  if (dl.children.length) card.append(dl);
  // spec sheet: edit the run's spec in the inspector and re-run as a sibling candidate
  const sheetBtn = R.ctx.dom.button("Spec sheet", {
    cls: "btn", title: "open this run's spec in the inspector: edit a value, re-run as a sibling candidate",
    onclick: () => openSpecSheet(project, rid, metrics),
  });
  card.append(el("div.t-card-actions", [sheetBtn]));
  // iso render if the run wrote one
  const figs = Array.isArray(d.figures) ? d.figures : [];
  const label = w.label || "winner";
  const iso = figs.find((f) => f.includes("iso")) || null;
  const winnerDir = stl ? stl.split("/")[0] : label;
  const isoRel = iso ? `figures/${iso}` : `${winnerDir}/${label}_iso.png`;
  const img = el("img.t-render", { src: base + isoRel, alt: "the part, isometric render", loading: "lazy" });
  img.addEventListener("error", () => { img.remove(); });
  card.append(img);
  chip.append(card);
  renderCompare();
}

function fileLink(href, text) {
  return R.ctx.dom.el("a.t-dl", { href, text: "⭳ " + text, target: "_blank", rel: "noopener" });
}

function fmtNum(v) {
  if (typeof v !== "number") return String(v);
  if (Math.abs(v) >= 1000) return v.toPrecision(5);
  if (Math.abs(v) >= 1) return v.toPrecision(4);
  return v.toExponential(2);
}

// ---------- the live spec sheet (inspector dock): edit → re-run → delta in the compare card ----------
// Editable rows are the decision knobs; everything else in the spec is carried through untouched.
const SHEET_FIELDS = [
  ["resolution.gen_mm", "generator resolution (mm)", "number"],
  ["resolution.fea_mm", "FEA resolution (mm)", "number"],
  ["sweep.n", "designs in the sweep", "number"],
  ["sweep.seed", "sampler seed", "number"],
  ["generator.iterations", "SIMP iterations", "number"],
  ["generator.variables.volfrac.lo", "volume fraction, low", "number"],
  ["generator.variables.volfrac.hi", "volume fraction, high", "number"],
  ["generator.variables.rmin_mm.lo", "min feature radius, low (mm)", "number"],
  ["generator.variables.rmin_mm.hi", "min feature radius, high (mm)", "number"],
  ["loads.0.force.2", "load force Z (N)", "number"],
];

function dig(obj, path) { return path.split(".").reduce((o, k) => (o === null || o === undefined ? o : o[k]), obj); }
function digSet(obj, path, val) {
  const ks = path.split(".");
  let o = obj;
  for (const k of ks.slice(0, -1)) { if (o[k] === null || o[k] === undefined) return false; o = o[k]; }
  o[ks[ks.length - 1]] = val;
  return true;
}

async function openSpecSheet(project, rid, priorMetrics) {
  const { el, button } = R.ctx.dom;
  const insp = R.ctx.inspector;
  insp.show(el("div.prose.dim", { text: "loading the run's spec…" }), { name: `spec sheet · ${project}`, coord: rid });
  const r = await R.ctx.api.get(`/api/golden/file/${encodeURIComponent(project)}/${rid}/inputs/spec.json`);
  if (!R || R.dead) return;
  if (!r.ok) {
    insp.show(R.ctx.dom.note("spec unavailable", r.error || `HTTP ${r.status}`, { fault: true }), { name: `spec sheet · ${project}`, coord: rid });
    return;
  }
  // the file route returns the file body; accept both envelope and raw object shapes
  const spec = (r.data && r.data.schema === "aces-golden-spec/1") ? r.data : (r.data && r.data.data) || r.data;
  if (!spec || typeof spec !== "object" || spec.schema !== "aces-golden-spec/1") {
    insp.show(R.ctx.dom.note("spec unreadable", "inputs/spec.json did not arrive as an aces-golden-spec/1 object", { fault: true }), { name: `spec sheet · ${project}`, coord: rid });
    return;
  }
  const wrap = el("div.sheet");
  // current scores (read-only): what this run achieved
  if (priorMetrics) {
    const rows = Object.entries(priorMetrics).filter(([, v]) => typeof v === "number")
      .map(([k, v]) => [METRIC_LABELS[k] || k, fmtNum(v)]);
    wrap.append(el("div.sheet-sec", [el("div.sheet-cap", { text: "this run scored" }), R.ctx.dom.table(["metric", "value"], rows)]));
  }
  const inputs = {};
  const tbl = el("table.t-score");
  for (const [path, label, kind] of SHEET_FIELDS) {
    const cur = dig(spec, path);
    const inp = el("input", { type: "text", value: cur === null || cur === undefined ? "" : String(cur), "aria-label": label });
    inp.dataset.path = path; inp.dataset.kind = kind;
    if (cur === null || cur === undefined) { inp.disabled = true; inp.title = "not present in this spec"; }
    inputs[path] = inp;
    tbl.append(el("tr", [el("td", { text: label }), el("td", [inp])]));
  }
  wrap.append(el("div.sheet-sec", [el("div.sheet-cap", { text: "edit — the spec beneath follows" }), tbl]));
  const raw = el("textarea.t-spec", { rows: "12", text: JSON.stringify(spec, null, 1), spellcheck: "false", "aria-label": "full spec JSON" });
  const sync = () => { raw.value = JSON.stringify(spec, null, 1); };
  for (const inp of Object.values(inputs)) {
    inp.addEventListener("change", () => {
      const v = inp.dataset.kind === "number" ? Number(inp.value) : inp.value;
      if (inp.dataset.kind === "number" && !Number.isFinite(v)) { inp.value = String(dig(spec, inp.dataset.path) ?? ""); return; }
      if (digSet(spec, inp.dataset.path, v)) sync();
    });
  }
  raw.addEventListener("change", () => {
    try { const parsed = JSON.parse(raw.value); Object.keys(spec).forEach((k) => delete spec[k]); Object.assign(spec, parsed); }
    catch (e) { state.textContent = `JSON error: ${e.message}`; }
  });
  const state = el("span.t-state");
  const rerun = button("Re-run with these values", {
    cls: "btn primary",
    title: "launches aces golden run with this edited spec as the same project's next run",
    onclick: async () => {
      rerun.disabled = true; state.textContent = "starting…";
      const item = `${(R.thread && R.thread.runs.find((x) => x.project === project) || {}).item || "part"}-edit`;
      const r2 = await R.ctx.api.post("/api/loop/spec/confirm", { thread: R.threadId, item, spec, confirm: project });
      if (!R || R.dead) return;
      if (r2.ok) { state.textContent = `job ${r2.data.job} started`; await loadThread(R.threadId); }
      else { state.textContent = r2.error || `HTTP ${r2.status}`; rerun.disabled = false; }
    },
  });
  wrap.append(el("div.sheet-sec", [el("div.sheet-cap", { text: "the full spec (advanced)" }), raw]));
  wrap.append(el("div.t-card-actions", [state, rerun]));
  insp.show(wrap, { name: `spec sheet · ${project}`, coord: `${rid} · edits re-run as the next candidate` });
}

// ---------- compare: siblings in one thread, side by side ----------
async function renderCompare() {
  const { el } = R.ctx.dom;
  const th = R.thread;
  if (!th || !Array.isArray(th.runs) || th.runs.length < 2) return;
  const old = R.b.log.querySelector(".t-compare");
  const results = [];
  for (const run of th.runs) {
    const rid = await latestRunId(run.project);
    if (!rid) continue;
    const r = await R.ctx.api.get(`/api/golden/run/${encodeURIComponent(run.project)}/${rid}`);
    if (r.ok && r.data && r.data.result && r.data.result.winner && r.data.result.winner.metrics) {
      results.push({ project: run.project, item: run.item, metrics: r.data.result.winner.metrics, outcome: r.data.outcome || r.data.result.outcome });
    }
  }
  if (results.length < 2) { if (old) old.remove(); return; }
  const keys = [...new Set(results.flatMap((x) => Object.keys(x.metrics)))];
  const tbl = el("table.t-compare-tbl");
  tbl.append(el("tr", [el("th", { text: "metric" }), ...results.map((x) => el("th", { text: `${x.item} (${x.project})` }))]));
  for (const k of keys) {
    tbl.append(el("tr", [el("td", { text: METRIC_LABELS[k] || k }), ...results.map((x) => el("td.mono", { text: typeof x.metrics[k] === "number" ? fmtNum(x.metrics[k]) : "—" }))]));
  }
  const card = el("div.t-card.t-compare", [
    el("div.t-card-title", { text: `Compare — ${results.length} candidates in this thread` }), tbl]);
  if (old) old.replaceWith(card); else R.b.log.append(el("div.t-msg.agent", [el("div.t-who", { text: "aces loop" }), card]));
}

// re-check running jobs on jobs stamps and every few seconds
function pollRuns() {
  if (!R || !R.thread) return;
  const chips = R.b.log.querySelectorAll(".t-run");
  for (const chip of chips) watchJob(chip, Number(chip.dataset.job));
}

// ---------- sending ----------
function renderFileChips(box) {
  const { el, clear } = R.ctx.dom;
  if (!box) return;
  clear(box);
  R.pendingFiles.forEach((f, i) => {
    box.append(el("span.t-chip", [
      el("span", { text: f.name }),
      el("button.t-chip-x", { text: "✕", title: "drop this file", onclick: () => { R.pendingFiles.splice(i, 1); renderFileChips(box); } }),
    ]));
  });
}

function readB64(f) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => reject(new Error(`cannot read ${f.name}`));
    r.readAsDataURL(f);
  });
}

async function sendMessage(ta) {
  const text = ta.value;
  const files = R.pendingFiles.splice(0);
  if (R.busy || ((!text || !text.trim()) && !files.length)) return;
  R.busy = true;
  setState("");
  try {
    // files go first, each its own upload (the loop answers each); the text rides after so the plan sees them
    for (const f of files) {
      setState(`uploading ${f.name}…`);
      let data_b64;
      try { data_b64 = await readB64(f); }
      catch (e) { setState(e.message); R.pendingFiles.unshift(...files.slice(files.indexOf(f))); renderFileChips(R.b.chips); return; }
      const ru = await R.ctx.api.post("/api/loop/upload", { thread: R.threadId, name: f.name, data_b64 });
      if (!R || R.dead) return;
      if (!ru.ok) { setState(ru.error || `HTTP ${ru.status}`); R.pendingFiles.unshift(...files.slice(files.indexOf(f))); renderFileChips(R.b.chips); return; }
      R.thread = ru.data.thread;
      R.threadId = ru.data.thread.id;
      storeTid(R.threadId);
    }
    renderFileChips(R.b.chips);
    if (text && text.trim()) {
      setState("");
      const r = await R.ctx.api.post("/api/loop/message", { thread: R.threadId, text: text.trim() });
      if (!R || R.dead) return;
      if (!r.ok) { setState(r.error || `HTTP ${r.status}`); return; }
      ta.value = "";
      R.thread = r.data.thread;
      R.threadId = r.data.thread.id;
      storeTid(R.threadId);
    }
    const u = new URL(location.href); u.searchParams.set("thread", R.threadId); history.replaceState(null, "", u);
    renderLog();
    pollRuns(false);
  } finally { R.busy = false; }
}

function setState(t) {
  if (!R) return;
  R.state = t;
  const st = R.b.composer.querySelector(".t-state");
  if (st) st.textContent = t;
}
