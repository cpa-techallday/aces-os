// Objects plate: the per-project CAD store written by aces-cad, read from disk by the backend and never through the
// CAD runtime. A project selector; the CAD runs table (status with the cad vocabulary, the sandbox mode, confinement
// and platform as recorded, an unreadable run with its reason); the run detail (the record and its files through the
// console's own file route); the drawings (aces.cad.drawing/1: views, dimensions, the hole table or "none", the SVG
// drawn inline, PNG and DXF links); the bundles with Check bundle (check_bundle.py's own lines, all-match only when
// the checker printed it) and the STL viewer (viewer.js showSTL on the bundle's part.stl); the notes timeline
// (NOTES.md entries quoted as written); the Onshape card (connected: false, always). Bindings: /api/projects,
// /api/cad/runs?project=, /api/cad/run/{p}/{id}, /api/cad/file/..., /api/cad/bundles?project=, /api/cad/bundle/...,
// POST /api/cad/bundle-check/{p}/{name}, /api/cad/notes/{p}, /api/cad/drawings/{p}, /api/cad/onshape/{p} (spec 3.10,
// 4.3 Objects). Deep link ?plate=objects&project=<name>&run=<id>.
import { jobPanel } from "../lib/jobs.js";
import { Viewer } from "../viewer.js";
import { fetchBuffer, parseBinarySTL } from "../loaders.js";

let R = null;

export async function mount(root, ctx) {
  const { dom } = ctx; const { el, block } = dom;
  R = {
    ctx, root, projectsRes: null, projects: null, project: ctx.params.get("project") || null, run: ctx.params.get("run") || null,
    res: {}, runs: null, detail: null, detailRes: null, bundles: null, notes: null, drawings: null, onshape: null,
    checks: {}, checking: {}, viewer: null, viewerFor: null, viewerNote: null, dead: false,
    b: { actions: el("div"), project: el("div"), runs: el("div"), detail: el("div"), drawings: el("div"), bundles: el("div"), viewer: el("div"), notes: el("div"), onshape: el("div"), foot: el("div.plate-foot.mono") },
  };
  root.append(
    block("project", [R.b.project], { span2: true, id: "obj-project" }),
    block("CAD runs · <project>/cad/local/runs/<id>/run.json", [R.b.runs], { span2: true, id: "obj-runs" }),
    block("run", [R.b.detail], { span2: true, id: "obj-run" }),
    block("CAD actions · aces-cad on this desktop, ledgered as console.cad.*", [R.b.actions], { span2: true, id: "obj-actions" }),
    block("drawings · aces.cad.drawing/1", [R.b.drawings], { span2: true, id: "obj-drawings" }),
    block("bundles · <project>/cad/local/<stamp>-<name>", [R.b.bundles], { span2: true, id: "obj-bundles" }),
    block("part viewer · part.stl of a bundle, through the file route", [R.b.viewer], { span2: true, id: "obj-viewer" }),
    block("notes · cad/NOTES.md, quoted as written", [R.b.notes], { id: "obj-notes" }),
    block("Onshape", [R.b.onshape], { id: "obj-onshape" }),
    R.b.foot,
  );
  await loadProjects();
}

export function unmount() {
  if (!R) return;
  R.dead = true;
  if (R.viewer) { try { R.viewer.dispose(); } catch (e) { /* disposed */ } }
  R = null;
}

// ---------- data ----------
async function loadProjects() {
  const r = await R.ctx.api.get("/api/projects");
  if (!R || R.dead) return;
  R.projectsRes = r; R.projects = r.ok ? r.data : null;
  const names = (R.projects && Array.isArray(R.projects.projects) ? R.projects.projects : []).map((p) => p.name);
  if (!R.project && names.length) R.project = names.includes("cad-vm") ? "cad-vm" : names[0];
  renderProject();
  if (R.project) await loadProject(R.project); else { renderAll(); renderInspector(); }
}

async function loadProject(name) {
  R.project = name; R.runs = null; R.bundles = null; R.notes = null; R.drawings = null; R.onshape = null; R.detail = null; R.detailRes = null; R.checks = {};
  renderAll();
  const p = encodeURIComponent(name);
  const [runs, bundles, notes, drawings, onshape] = await Promise.all([
    R.ctx.api.get(`/api/cad/runs?project=${p}`), R.ctx.api.get(`/api/cad/bundles?project=${p}`), R.ctx.api.get(`/api/cad/notes/${p}`),
    R.ctx.api.get(`/api/cad/drawings/${p}`), R.ctx.api.get(`/api/cad/onshape/${p}`),
  ]);
  if (!R || R.dead || R.project !== name) return;
  R.res = { runs, bundles, notes, drawings, onshape };
  R.runs = runs.ok ? runs.data : null; R.bundles = bundles.ok ? bundles.data : null; R.notes = notes.ok ? notes.data : null;
  R.drawings = drawings.ok ? drawings.data : null; R.onshape = onshape.ok ? onshape.data : null;
  renderAll();
  if (R.run) await loadRun(R.run); else renderInspector();
}

async function loadRun(runId) {
  R.run = runId; R.detail = null; R.detailRes = null;
  renderRuns(); renderDetail(); renderActions();
  const r = await R.ctx.api.get(`/api/cad/run/${encodeURIComponent(R.project)}/${encodeURIComponent(runId)}`);
  if (!R || R.dead || R.run !== runId) return;
  R.detailRes = r; R.detail = r.ok ? r.data : null;
  renderRuns(); renderDetail(); renderActions(); renderInspector();
}

function setURL() {
  const u = new URL(location.href); u.searchParams.set("plate", "objects");
  if (R.project) u.searchParams.set("project", R.project); else u.searchParams.delete("project");
  if (R.run) u.searchParams.set("run", R.run); else u.searchParams.delete("run");
  history.replaceState(null, "", u);
}

function selectProject(name) { if (name === R.project) return; R.run = null; R.project = name; setURL(); loadProject(name); }
function selectRun(id) { R.run = R.run === id ? null : id; setURL(); if (R.run) loadRun(R.run); else { R.detail = null; R.detailRes = null; renderRuns(); renderDetail(); renderActions(); renderInspector(); } }

function chip(vocabulary, word, title = null) {
  const { vocab, dom } = R.ctx;
  if (word === null || word === undefined || word === "") return dom.el("span.dim", { text: "status not recorded" });
  return vocab.known(vocabulary, word) ? vocab.state(vocabulary, word, { title }) : dom.el("span", { text: String(word), title: `${word}: not a word of the ${vocabulary} vocabulary; shown as received` });
}

const fileURL = (runId, rel) => `/api/cad/file/${encodeURIComponent(R.project)}/${encodeURIComponent(runId)}/${rel.split("/").map(encodeURIComponent).join("/")}`;
const bundleURL = (name, rel) => `/api/cad/bundle/${encodeURIComponent(R.project)}/${encodeURIComponent(name)}/${rel.split("/").map(encodeURIComponent).join("/")}`;

// ---------- rendering ----------
function renderAll() { renderRuns(); renderDetail(); renderActions(); renderDrawings(); renderBundles(); renderViewer(); renderNotes(); renderOnshape(); renderFoot(); }

// ---------- CAD actions: inspect, render, drawing, pull, re-run with parameters (the run's own script), prune ----------
async function cadAction(action, options, confirm = null) {
  const r = await R.ctx.api.post("/api/cad/action", { project: R.project, action, run: action === "prune" ? null : R.run, options, ...(confirm ? { confirm } : {}) });
  if (!R || R.dead) return;
  R.actionJob = r.ok ? { id: r.data.job, action } : null;
  R.actionResult = r.ok ? null : { text: `${action}: ${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})`, cls: r.status === 503 || r.status === 400 || r.status === 409 ? "act" : "fault" };
  renderActions();
}
function renderActions() {
  const { dom } = R.ctx; const { el, clear, note, button } = dom;
  const box = R.b.actions; if (!box) return; clear(box);
  if (!R.project) { box.append(note("no project", "pick a project")); return; }
  const rec = R.detail && R.detail.record ? R.detail.record : null;
  if (R.run && rec) {
    const size = el("select", { "aria-label": "render size" }, ["small", "medium", "large"].map((v) => el("option", { value: v, text: v, selected: v === "medium" ? "selected" : null })));
    const proj = el("select", { "aria-label": "drawing projection" }, ["third-angle", "first-angle"].map((v) => el("option", { value: v, text: v })));
    const pullName = el("input", { type: "text", placeholder: "pull name (optional)", "aria-label": "pull name", style: { maxWidth: "220px" } });
    box.append(el("div.cap", { text: `run ${R.run}` }));
    box.append(el("div.btnrow", [
      button("Inspect", { title: "aces-cad inspect: measure the geometry in a worker that runs no agent code", onclick: () => cadAction("inspect", {}) }),
      el("span", [button("Render", { title: "aces-cad render: PNG views", onclick: () => cadAction("render", { size: size.value }) }), " ", size]),
      el("span", [button("Drawing", { title: "aces-cad drawing: a dimensioned 1:1 mm drawing (DXF, SVG, PNG)", onclick: () => cadAction("drawing", { projection: proj.value }) }), " ", proj]),
      el("span", [button("Pull into the project", { cls: "btn primary", title: "aces-cad pull: STEP (+ STL, 3MF), parameters, sha256 and a cad.pull ledger entry", onclick: () => cadAction("pull", pullName.value.trim() ? { name: pullName.value.trim() } : {}) }), " ", pullName]),
    ]));
    const params = rec.params && typeof rec.params === "object" ? Object.entries(rec.params) : [];
    if (params.length) {
      const inputs = {};
      const form = el("div.toolform");
      for (const [key, v] of params) {
        const unit = rec.params_declared && rec.params_declared[key] && rec.params_declared[key].unit ? ` (${rec.params_declared[key].unit})` : "";
        inputs[key] = { input: el("input", { type: typeof v === "number" ? "number" : "text", step: "any", value: String(v), id: `obj-p-${key}` }), orig: v };
        form.append(el("label", { for: `obj-p-${key}`, text: key + unit }), inputs[key].input);
      }
      const withRender = el("input", { type: "checkbox", id: "obj-rerun-render", checked: true });
      box.append(el("div.cap", { text: "re-run with parameters · the run's own script in the sandbox (no new code)" }), form,
        el("div.btnrow", [el("label", { for: "obj-rerun-render" }, [withRender, " also render the iso view"]), button("Re-run", { cls: "btn primary", onclick: () => {
          const over = {};
          for (const [key, f] of Object.entries(inputs)) {
            const raw = f.input.value; if (raw === "" || String(f.orig) === raw) continue;
            over[key] = typeof f.orig === "number" ? Number(raw) : typeof f.orig === "boolean" ? raw === "true" : raw;
          }
          cadAction("rerun", { params: over, render: withRender.checked });
        } })]));
    } else box.append(el("div.prose.dim", { text: "this run recorded no parameters: re-run is not offered" }));
  } else box.append(note("no run selected", "select a CAD run above for inspect, render, drawing, pull and re-run"));
  const keep = el("input", { type: "number", min: "1", step: "1", value: "10", "aria-label": "runs to keep", style: { maxWidth: "90px" } });
  const words = `prune ${R.project}`;
  const typed = el("input.type.short", { type: "text", autocomplete: "off", spellcheck: "false", placeholder: `type ${words}`, "aria-label": `type ${words} to enable pruning` });
  const pruneBtn = button("Prune old runs", { disabled: true, title: "aces-cad prune: deletes runs beyond the newest N (runs named by a pull are kept)", onclick: () => cadAction("prune", { keep: parseInt(keep.value, 10) }, typed.value) });
  typed.addEventListener("input", () => { pruneBtn.disabled = typed.value !== words; });
  box.append(el("div.cap", { text: "project · prune deletes runs beyond the newest N; pulled runs are kept" }), el("div.actrow", [el("span.k", { text: "keep" }), keep, typed, pruneBtn]));
  if (R.actionResult) box.append(el("div.result." + R.actionResult.cls, { text: R.actionResult.text }));
  if (R.actionJob) {
    box.append(jobPanel(R.ctx, R.actionJob.id, { onDone: (j) => {
      if (!R || R.dead) return;
      const runId = j.result && (j.result.run_id || (j.result.run && j.result.run.run_id));
      loadProject(R.project).then(() => { if (j.kind === "cad.rerun" && runId) selectRun(runId); });
    } }));
  }
}

function renderProject() {
  const { dom } = R.ctx; const { el, clear, absence, note } = dom;
  const box = R.b.project; clear(box);
  const abs = absence(R.projectsRes, "projects");
  if (abs) box.append(abs);
  const d = R.projects;
  if (!d) { if (!abs) box.append(note("projects not loaded", "no reply yet")); return; }
  const rows = Array.isArray(d.projects) ? d.projects : [];
  if (d.reason) { box.append(note("no work store on this machine", d.reason, { act: true, detail: d.root || null })); return; }
  if (!rows.length) { box.append(note("no projects", `no aces.yaml under ${d.root || "$ACES_WORK"}`)); return; }
  const sel = el("select.sel#objProject", { "aria-label": "project", onchange: (e) => selectProject(e.target.value) });
  for (const p of rows) sel.append(el("option", { value: p.name, text: `${p.name} — ${p.class || "class not recorded"} · ${(p.counts || {}).cad_runs ?? "?"} CAD runs` }));
  if (R.project && rows.some((p) => p.name === R.project)) sel.value = R.project;
  const cur = rows.find((p) => p.name === R.project);
  box.append(el("div.actrow", [el("label.lab", { for: "objProject", text: "project" }), sel]));
  if (cur) box.append(el("div.line", [el("span.k", { text: "class" }), el("b.ink-amber", { text: cur.class || "not recorded" }), el("span.k", { text: "CAD sources in aces.yaml" }), el("b", { text: String((cur.cad_sources || []).length) }), el("span.k", { text: "onshape gate" }), el("b", { text: cur.onshape_gate || "not evaluated" }), el("span.k", { text: "path" }), el("b", { text: cur.path || "" })]));
}

function renderRuns() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table, button } = dom;
  const box = R.b.runs; clear(box);
  if (!R.project) { box.append(note("no project", "choose a project to list its CAD runs")); R.ctx.coord(""); return; }
  const abs = absence(R.res.runs, `CAD runs of ${R.project}`);
  if (abs) box.append(abs);
  const d = R.runs;
  if (!d) { if (!abs) box.append(note("CAD runs", "loading")); return; }
  const rows = Array.isArray(d.runs) ? d.runs : [];
  if (d.note) box.append(note(d.note, "the project has no cad/local/runs directory", { detail: R.res.runs && R.res.runs.meta ? R.res.runs.meta.source : null }));
  const counts = d.counts || {};
  box.append(el("div.line", [el("span.k", { text: "runs" }), el("b", { text: String(counts.total ?? rows.length) }), el("span.k", { text: "unreadable" }), el("b" + (counts.unreadable ? ".ink-fault" : ""), { text: String(counts.unreadable ?? rows.filter((r) => r.unreadable).length) })]));
  box.append(table([
    { th: "run", render: (r) => button(r.run_id, { title: `open run ${r.run_id}`, onclick: () => selectRun(r.run_id) }), cls: "nm" },
    { th: "name", render: (r) => r.name || (r.unreadable ? format.DASH : "unnamed") },
    { th: "status", render: (r) => (r.unreadable ? chip("cad", "unreadable", r.unreadable) : chip("cad", r.status)) },
    { th: "started", render: (r) => format.utcShort(r.started) }, { th: "finished", render: (r) => (r.finished ? format.utcShort(r.finished) : (r.unreadable ? format.DASH : "not finished")) },
    { th: "sandbox", render: (r) => (r.sandbox ? `${r.sandbox.mode || "mode not recorded"} · confined ${String(r.sandbox.confined)} · ${r.sandbox.platform || "platform not recorded"}` : "not recorded"), cls: "body" },
    { th: "unreadable", render: (r) => (r.unreadable ? el("span.ink-fault", { text: r.unreadable }) : format.DASH), cls: "body" },
  ], rows.map((r) => ({ ...r, __cls: R.run === r.run_id ? "eye" : null })), { empty: `no CAD runs in ${R.project}`, caption: `CAD runs of ${R.project}` }));
  R.ctx.coord(`${R.project} · ${rows.length} CAD runs${R.bundles && Array.isArray(R.bundles.bundles) ? ` · ${R.bundles.bundles.length} bundles` : ""}`);
}

function renderDetail() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table, kv } = dom;
  const box = R.b.detail; clear(box);
  if (!R.run) { box.append(note("no run selected", "select a run to read its record and files")); return; }
  const abs = absence(R.detailRes, `run ${R.run}`);
  if (abs) box.append(abs);
  const d = R.detail;
  if (!d) { if (!abs) box.append(note(`run ${R.run}`, "loading")); return; }
  const rec = d.record || {};
  const k = (t) => el("span.k", { text: t }), b = (t, cls = "") => el("b" + (cls ? "." + cls : ""), { text: t === null || t === undefined || t === "" ? "not recorded" : String(t) });
  box.append(el("div.line", [k("run"), b(rec.run_id || d.run_id), k("name"), b(rec.name), k("status"), chip("cad", rec.status), k("class"), b(rec.class, "ink-amber"), k("created"), b(rec.created_at ? format.utc(rec.created_at) : null), k("finished"), b(rec.finished_at ? format.utc(rec.finished_at) : null), k("entry"), b(rec.entry), k("from run"), b(rec.from_run || "none")]));
  if (rec.error) box.append(note("error, as recorded", String(rec.error), { fault: true }));
  const sb = rec.sandbox || {};
  const tot = (rec.result || {}).totals || {};
  const parts = Array.isArray((rec.result || {}).parts) ? rec.result.parts : [];
  box.append(el("div.plate-body", { style: { padding: "0", gap: "12px 24px" } }, [
    el("div", [el("div.cap", { text: "params (as run)" }), rec.params && Object.keys(rec.params).length ? kv(Object.entries(rec.params).map(([key, v]) => [key, `${typeof v === "number" ? format.num(v, 6) : String(v)}${rec.params_declared && rec.params_declared[key] && rec.params_declared[key].unit ? " " + rec.params_declared[key].unit : ""}`])) : el("div.prose.dim", { text: "no params recorded" }),
      rec.params_overridden && rec.params_overridden.length ? el("div.prose.dim", { text: `overridden: ${rec.params_overridden.join(", ")}` }) : null]),
    el("div", [el("div.cap", { text: "result" }), kv([["kind", (rec.result || {}).kind], ["parts", tot.parts !== undefined ? String(tot.parts) : String(parts.length)], ["volume", tot.volume_mm3 !== undefined ? `${format.num(tot.volume_mm3, 8)} mm³` : null], ["bbox", Array.isArray(tot.bbox_mm) ? tot.bbox_mm.map((v) => format.num(v, 4)).join(", ") + " mm" : null], ["problems", Array.isArray(rec.problems) ? (rec.problems.length ? rec.problems.join("; ") : "none") : null], ["warnings", Array.isArray(rec.warnings) ? (rec.warnings.length ? rec.warnings.join("; ") : "none") : null]])]),
    el("div", [el("div.cap", { text: "sandbox (as recorded)" }), kv([["mode", sb.mode, "act"], ["confined", sb.confined === undefined ? null : String(sb.confined)], ["platform", sb.platform], ["kernel", sb.kernel], ["seccomp", sb.seccomp ? `${sb.seccomp.active ? "active" : "inactive"}${sb.seccomp.detail ? " · " + sb.seccomp.detail : ""}` : null], ["landlock", sb.landlock ? `${sb.landlock.active ? "active" : "inactive"}${sb.landlock.detail ? " · " + sb.landlock.detail : ""}` : null], ["limits", sb.limits ? Object.entries(sb.limits).map(([key, v]) => `${key} ${v}`).join(" · ") : null], ["enforced", sb.limits_enforced ? Object.entries(sb.limits_enforced).map(([key, v]) => `${key} ${v}`).join(" · ") : null], ["missing", Array.isArray(sb.missing) ? (sb.missing.length ? sb.missing.join(", ") : "none") : null], ["note", sb.note]])]),
    el("div", [el("div.cap", { text: "timings and versions" }), kv([["wall", rec.timings_s ? format.dur(rec.timings_s.wall) : null], ["imports", rec.timings_s ? format.dur(rec.timings_s.imports) : null], ["peak rss", rec.peak_rss_mb !== undefined ? `${format.num(rec.peak_rss_mb)} MB` : null], ["script", rec.script ? `${rec.script.source || ""} · ${rec.script.bytes ?? "?"} bytes · ${format.shortHash(rec.script.sha256)}` : null], ["versions", rec.versions ? Object.entries(rec.versions).map(([key, v]) => `${key} ${v}`).join(" · ") : null], ["tool", rec.tool ? `${rec.tool.name} ${rec.tool.version}` : null]])]),
  ]));
  if (parts.length) {
    box.append(el("div.cap", { text: `parts · ${parts.length}` }));
    box.append(table([
      { th: "part", key: "name", cls: "nm" }, { th: "kind", key: "kind" }, { th: "valid", render: (p) => String(p.valid) }, { th: "volume mm³", render: (p) => format.num(p.volume_mm3, 8), num: true },
      { th: "area mm²", render: (p) => format.num(p.area_mm2, 6), num: true }, { th: "size mm", render: (p) => (Array.isArray(p.size_mm) ? p.size_mm.map((v) => format.num(v, 4)).join(" × ") : format.DASH) },
      { th: "counts", render: (p) => (p.counts ? Object.entries(p.counts).map(([key, v]) => `${key} ${v}`).join(" · ") : format.DASH), cls: "body" },
      { th: "reasons", render: (p) => (Array.isArray(p.reasons) && p.reasons.length ? p.reasons.join("; ") : "none"), cls: "body" },
    ], parts, { empty: "no parts", caption: "parts of the run" }));
  }
  const files = Array.isArray(d.files) ? d.files : [];
  box.append(el("div.cap", { text: `files · ${files.length} (served through this console's file route, sandboxed)` }));
  box.append(table([
    { th: "file", render: (f) => (f.kind && f.kind !== "other" && f.kind !== "bin" ? el("a", { href: fileURL(R.run, f.rel), target: "_blank", rel: "noopener", text: f.rel }) : el("span", { text: f.rel, title: "not served: file type outside the content-type allow-list" })), cls: "nm" },
    { th: "kind", render: (f) => f.kind || "other" }, { th: "size", render: (f) => format.bytes(f.size), num: true }, { th: "sha256", render: (f) => format.shortHash(f.sha256) },
  ], files, { empty: "no files listed", caption: `files of run ${R.run}`, cls: "wrapnames" }));
  if (rec.stdout || rec.stderr) {
    box.append(el("div.cap", { text: `worker output${rec.stdout_truncated || rec.stderr_truncated ? " (truncated by the worker)" : ""}` }));
    box.append(el("pre.log", { text: `${rec.stdout || ""}${rec.stderr ? "\n[stderr]\n" + rec.stderr : ""}` }));
  }
}

function renderDrawings() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table, kv } = dom;
  const box = R.b.drawings; clear(box);
  if (!R.project) return;
  const abs = absence(R.res.drawings, "drawings");
  if (abs) box.append(abs);
  const d = R.drawings;
  if (!d) { if (!abs) box.append(note("drawings", "loading")); return; }
  const rows = Array.isArray(d.drawings) ? d.drawings : [];
  if (!rows.length) { box.append(note("no drawings", d.note || `no aces.cad.drawing/1 record in ${R.project}`)); return; }
  const shown = R.run ? rows.filter((x) => x.run_id === R.run) : rows;
  box.append(el("div.line", [el("span.k", { text: "drawings" }), el("b", { text: String(rows.length) }), R.run ? el("span.k", { text: `of run ${R.run}` }) : null, R.run ? el("b", { text: String(shown.length) }) : null]));
  if (!shown.length) box.append(el("div.prose.dim", { text: `no drawing belongs to run ${R.run}; clear the run selection to see all ${rows.length}` }));
  for (const dr of shown) {
    // the backend lists a drawing's files relative to its run directory; served through the CAD file route
    const files = Object.fromEntries(Object.entries(dr.files || {}).filter(([, v]) => typeof v === "string" && v)
      .map(([kind, v]) => [kind, v.startsWith("/") ? v : fileURL(dr.run_id || R.run, v)]));
    const views = Array.isArray(dr.views) ? dr.views : [];
    const dims = Array.isArray(dr.dimensions) ? dr.dimensions : [];
    const card = el("div", { style: { display: "grid", gap: "8px" } });
    card.append(el("div.line", [el("span.k", { text: "drawing" }), el("b", { text: dr.id }), el("span.k", { text: "run" }), el("b", { text: dr.run_id || "not recorded" }), el("span.k", { text: "projection" }), el("b", { text: dr.projection || "not recorded" }), el("span.k", { text: "units" }), el("b", { text: dr.units || "not recorded" }), el("span.k", { text: "scale" }), el("b", { text: dr.scale || "not recorded" }), el("span.k", { text: "audit errors" }), el("b" + (dr.audit_errors ? ".ink-fault" : ""), { text: dr.audit_errors === null || dr.audit_errors === undefined ? "not recorded" : String(dr.audit_errors) })]));
    if (files.svg) card.append(el("div", { style: { background: "var(--chalk)", borderRadius: "2px", padding: "4px", maxWidth: "720px" } }, [el("img", { src: files.svg, alt: `drawing ${dr.id}: ${views.map((v) => v.name).join(", ")} views, ${dims.length} dimensions`, style: { width: "100%", height: "auto", display: "block" } })]));
    else card.append(el("div.prose.dim", { text: "no SVG file beside the drawing record" }));
    card.append(el("div.line", [el("span.k", { text: "files" }),
      files.png ? el("a", { href: files.png, target: "_blank", rel: "noopener", text: "png" }) : el("span.dim", { text: "png absent" }),
      files.dxf ? el("a", { href: files.dxf, download: `${dr.id}.dxf`, text: "dxf (download)" }) : el("span.dim", { text: "dxf absent" }),
      files.json ? el("a", { href: files.json, target: "_blank", rel: "noopener", text: "record json" }) : null]));
    card.append(el("div.cap", { text: `views · ${views.length}` }));
    card.append(table([{ th: "view", key: "name", cls: "nm" }, { th: "direction", render: (v) => (Array.isArray(v.direction) ? v.direction.join(", ") : format.DASH) }, { th: "extent mm", render: (v) => (Array.isArray(v.extent_mm) ? v.extent_mm.map((x) => format.num(x, 4)).join(" × ") : format.DASH) }, { th: "visible edges", render: (v) => String(v.visible_edges ?? format.DASH), num: true }, { th: "hidden edges", render: (v) => String(v.hidden_edges ?? format.DASH), num: true }], views, { empty: "no views recorded", caption: `views of ${dr.id}` }));
    card.append(el("div.cap", { text: `dimensions · ${dims.length}` }));
    card.append(table([{ th: "id", key: "id", cls: "nm" }, { th: "view", key: "view" }, { th: "type", key: "type" }, { th: "value mm", render: (x) => format.num(x.value_mm, 6), num: true }, { th: "text", key: "text" }, { th: "feature", key: "feature" }], dims, { empty: "no dimensions recorded", caption: `dimensions of ${dr.id}` }));
    card.append(el("div.line", [el("span.k", { text: "hole table" }), dr.hole_table ? el("b", { text: typeof dr.hole_table === "string" ? dr.hole_table : JSON.stringify(dr.hole_table) }) : el("b", { text: "none" })]));
    if (dr.method) card.append(el("div.prose.dim", { text: `method: ${dr.method}` }));
    if (Array.isArray(dr.notes) && dr.notes.length) card.append(el("div.prose.dim", { text: `notes: ${dr.notes.join("; ")}` }));
    if (Array.isArray(dr.warnings) && dr.warnings.length) card.append(el("div.prose.ink-amber", { text: `warnings: ${dr.warnings.join("; ")}` }));
    box.append(card);
  }
}

function renderBundles() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table, button } = dom;
  const box = R.b.bundles; clear(box);
  if (!R.project) return;
  const abs = absence(R.res.bundles, "bundles");
  if (abs) box.append(abs);
  const d = R.bundles;
  if (!d) { if (!abs) box.append(note("bundles", "loading")); return; }
  const rows = Array.isArray(d.bundles) ? d.bundles : [];
  if (!rows.length) { box.append(note("no bundles", d.note || `no bundle in ${R.project}`)); return; }
  box.append(table([
    { th: "bundle", key: "name", cls: "nm" },
    { th: "run", render: (b) => (b.source_summary && b.source_summary.run_id) || "not recorded" },
    { th: "part", render: (b) => (b.source_summary ? `${b.source_summary.name || "unnamed"} · ${b.source_summary.kind || "kind not recorded"}` : "source.json absent") },
    { th: "geometry sha256", render: (b) => format.shortHash(b.source_summary && b.source_summary.geometry_sha256) },
    { th: "exported", render: (b) => format.utcShort(b.source_summary && b.source_summary.exported_at) },
    { th: "files present", render: (b) => Object.entries(b.has || {}).filter(([, v]) => v).map(([k2]) => k2.replace("_", ".")).join(", ") || "none", cls: "body" },
    { th: "ledger", render: (b) => (b.outcome_summary ? (b.outcome_summary.ledger_written ? `written: entry ${b.outcome_summary.ledger_n ?? "?"} ${b.outcome_summary.ledger_kind || ""}` : "not written") : "outcome.json absent") },
    { th: "cad source", render: (b) => (b.outcome_summary ? (b.outcome_summary.cad_source_recorded ? `recorded: ${b.outcome_summary.cad_source_ref || ""}` : "not recorded") : format.DASH), cls: "body" },
    { th: "check", render: (b) => el("span.actrow", [button("Check bundle", { disabled: !!R.checking[b.name], title: "runs check_bundle.py on this bundle (a read; its own lines are shown)", onclick: () => checkBundle(b.name) }), R.checks[b.name] ? el("span.result" + (R.checks[b.name].cls ? "." + R.checks[b.name].cls : ""), { text: R.checks[b.name].text }) : null]) },
    { th: "view", render: (b) => (b.has && b.has.part_stl ? button("View part.stl", { title: "loads part.stl into the viewer below", onclick: () => viewSTL(b.name) }) : el("span.dim", { text: "no part.stl" })) },
  ], rows.map((b) => ({ ...b, __cls: R.viewerFor === b.name ? "eye" : null })), { empty: "no bundles", caption: `bundles of ${R.project}`, cls: "wrapnames" }));
  for (const b of rows) {
    const c = R.checks[b.name];
    if (c && c.lines) {
      box.append(el("div.cap", { text: `check_bundle.py · ${b.name} · exit ${c.exit}${c.all_match ? " · all files match" : ""}` }));
      const pre = el("pre.log", { "aria-label": `check_bundle.py output for ${b.name}` });
      c.lines.forEach((ln, i) => { pre.append(el("span" + (/all files match$/.test(ln) ? ".tail" : ""), { text: ln })); if (i < c.lines.length - 1) pre.append("\n"); });
      box.append(pre);
    }
  }
}

async function checkBundle(name) {
  if (!R || R.checking[name]) return;
  R.checking[name] = true; R.checks[name] = { cls: "live", text: "checking…" }; renderBundles();
  const r = await R.ctx.api.post(`/api/cad/bundle-check/${encodeURIComponent(R.project)}/${encodeURIComponent(name)}`, {});
  if (!R || R.dead) return;
  R.checking[name] = false;
  if (r.ok && r.data) R.checks[name] = { cls: r.data.all_match ? "" : "act", text: r.data.all_match ? `all files match (exit ${r.data.exit})` : `${r.data.lines ? r.data.lines.length : 0} lines, exit ${r.data.exit}; the checker did not print its all-match line`, lines: r.data.lines || [], exit: r.data.exit, all_match: !!r.data.all_match };
  else R.checks[name] = { cls: r.status === 503 || r.status === 409 ? "act" : "fault", text: `${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})` };
  renderBundles();
}

async function viewSTL(name) {
  R.viewerFor = name; R.viewerNote = { cls: "live", text: `loading ${name}/part.stl…` }; renderBundles(); renderViewer();
  try {
    const buf = await fetchBuffer(bundleURL(name, "part.stl"));
    if (!R || R.dead || R.viewerFor !== name) return;
    const parsed = parseBinarySTL(buf);
    if (!R.viewer) {
      const canvas = R.b.viewer.querySelector("canvas.viewport");
      R.viewer = new Viewer(canvas);
    }
    R.viewer.clear();
    R.viewer.showSTL(parsed);
    R.viewer.fit();
    R.viewer.requestRender(true);
    const n = parsed.positions.length / 9;
    R.viewerNote = { cls: "", text: `${name}/part.stl · ${n} triangles · bbox ${parsed.bbox ? parsed.bbox.min.map((v) => v.toFixed(1)).join(",") + " to " + parsed.bbox.max.map((v) => v.toFixed(1)).join(",") + " mm" : "not computed"} · ${buf.byteLength} bytes` };
  } catch (e) {
    if (!R || R.dead) return;
    R.viewerNote = { cls: "fault", text: `part.stl not shown: ${e.message}` };
  }
  renderViewer();
}

function renderViewer() {
  const { dom } = R.ctx; const { el, clear } = dom;
  const box = R.b.viewer;
  if (!box.querySelector("canvas.viewport")) {
    clear(box);
    box.append(el("div.viewport-wrap", { style: { minHeight: "320px" } }, [el("canvas.viewport", { "aria-label": "3D view of the selected bundle's part.stl (orbit with the mouse); the facts beside it carry the same numbers" })]));
    box.append(el("div.line#objViewerNote"));
  }
  const noteEl = box.querySelector("#objViewerNote"); clear(noteEl);
  if (R.viewerNote) noteEl.append(el("span.result" + (R.viewerNote.cls ? "." + R.viewerNote.cls : ""), { text: R.viewerNote.text }));
  else noteEl.append(el("span.dim", { text: "no part loaded: press View part.stl on a bundle" }));
}

function renderNotes() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, button } = dom;
  const box = R.b.notes; clear(box);
  if (!R.project) return;
  const abs = absence(R.res.notes, "notes");
  if (abs) box.append(abs);
  const d = R.notes;
  if (!d) { if (!abs) box.append(note("notes", "loading")); return; }
  const entries = Array.isArray(d.entries) ? d.entries : [];
  if (!entries.length) { box.append(note("no notes", d.note || `no entries in ${d.path || "NOTES.md"}`, { detail: d.path || null })); return; }
  box.append(el("div.line", [el("span.k", { text: "entries" }), el("b", { text: String(entries.length) }), el("span.k", { text: "file" }), el("b", { text: d.path || "NOTES.md" }), d.truncated ? el("span.ink-amber", { text: "truncated at 1 MiB" }) : null]));
  const tl = el("div.timeline");
  for (const e of entries.slice().reverse()) {
    tl.append(el("div.row", [el("span.t", { text: e.ts ? format.utc(e.ts) : "time not recorded" }), el("span", [
      el("div.line", [el("span.k", { text: "tags" }), el("b", { text: Array.isArray(e.tags) && e.tags.length ? e.tags.join(", ") : "none" }), el("span.k", { text: "run" }), e.run_id ? button(e.run_id, { title: "open this run", onclick: () => selectRun(e.run_id) }) : el("b", { text: "none" })]),
      el("div.prose", { text: e.body ? `“${e.body}”` : "(empty entry)" }),
    ])]));
  }
  box.append(tl);
}

function renderOnshape() {
  const { dom } = R.ctx; const { el, clear, absence, note, kv } = dom;
  const box = R.b.onshape; clear(box);
  if (!R.project) return;
  const abs = absence(R.res.onshape, "Onshape");
  if (abs) box.append(abs);
  const d = R.onshape;
  if (!d) { if (!abs) box.append(note("Onshape", "loading")); return; }
  box.append(kv([["connected", String(d.connected), d.connected ? "" : "dim"], ["project class", d.project_class || "not recorded", "act"], ["note", d.note], ["gate", d.gate_note]]));
  box.append(el("div.prose.dim", { text: "onshape.env is never opened by this console; the Onshape adapter (aces-cad-onshape) is a separate command with its own class gate" }));
}

function renderFoot() {
  const { dom, format } = R.ctx; const { clear } = dom;
  const items = [];
  for (const [label, name] of [["runs", "runs"], ["bundles", "bundles"], ["drawings", "drawings"], ["notes", "notes"]]) {
    const r = R.res[name]; const a = r && r.meta && r.meta.fetched_utc ? format.age(r.meta.fetched_utc) : null;
    items.push(`${label} ${a === null ? (r && r.ok === false ? "unavailable" : "not loaded") : format.ago(a)}`);
  }
  items.push("CAD worker actions (run, inspect, render, drawing, pull, prune) are not offered here: deferred in v1");
  clear(R.b.foot); R.b.foot.append(dom.facts(items));
}

function renderInspector() {
  const { dom, format } = R.ctx; const { block, kv, el } = dom;
  const d = R.detail;
  if (d && d.record) {
    const rec = d.record; const tot = (rec.result || {}).totals || {};
    R.ctx.inspector.show([
      block("run", kv([["status", chip("cad", rec.status)], ["name", rec.name], ["class", rec.class, "act"], ["created", rec.created_at ? format.utc(rec.created_at) : null], ["wall", rec.timings_s ? format.dur(rec.timings_s.wall) : null], ["files", String((d.files || []).length)]])),
      block("result", kv([["parts", tot.parts !== undefined ? String(tot.parts) : null], ["volume", tot.volume_mm3 !== undefined ? `${format.num(tot.volume_mm3, 8)} mm³` : null], ["bbox", Array.isArray(tot.bbox_mm) ? tot.bbox_mm.map((v) => format.num(v, 4)).join(", ") : null]])),
      block("sandbox", kv([["mode", (rec.sandbox || {}).mode, "act"], ["confined", rec.sandbox ? String(rec.sandbox.confined) : null], ["platform", (rec.sandbox || {}).platform]])),
    ], { name: rec.name || d.run_id, coord: `${d.run_id} · ${rec.status || "status not recorded"}` });
    return;
  }
  const runs = R.runs && Array.isArray(R.runs.runs) ? R.runs.runs : [];
  R.ctx.inspector.show(block("project", kv([["project", R.project || "none"], ["CAD runs", String(runs.length)], ["bundles", R.bundles && Array.isArray(R.bundles.bundles) ? String(R.bundles.bundles.length) : "not loaded"], ["drawings", R.drawings && Array.isArray(R.drawings.drawings) ? String(R.drawings.drawings.length) : "not loaded"], ["notes", R.notes && Array.isArray(R.notes.entries) ? String(R.notes.entries.length) : "not loaded"]])), { name: R.project || "objects", coord: "select a run to read its record", ringed: false });
}
