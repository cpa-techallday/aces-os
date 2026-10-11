// Agents plate: Mission Control, as GRID CONTROL rev B served it (cudax ui/grid-control/index.html:617-905), so its
// page is no longer needed: the agent runs (live and recent) with their step timelines, the chat sessions with a
// composer and attachments, the missions (projects) with their steps, approvals and actions, and the agents.
// Everything here is agent-reported by Mission Control, never compute truth (brief rule 7). Reads work without a
// GRID CONTROL token on this desktop (through the rig's read proxy, said on the plate); every write needs the token,
// is a human POST and is ledgered by the backend. Bindings: /api/mc/{runs,run/{id},sessions,session/{id},missions,
// mission/{id},agents,upload,upload/{id},artifact/{id}}; stamps mc.runs, mc.missions, mc.sessions, mc.agents.
// Deep link ?plate=agents&run=<id>&session=<id>&mission=<id>.
let R = null;
const PATHS = { runs: "/api/mc/runs", missions: "/api/mc/missions", sessions: "/api/mc/sessions", agents: "/api/mc/agents" };
const KIND_ICON = { thinking: "step-think", tool_call: "step-tool", tool_result: "step-result", output: "step-output", error: "step-error", info: "step-info" };

export async function mount(root, ctx) {
  const { dom } = ctx; const { el, block } = dom;
  const p = ctx.params;
  R = {
    ctx, root, res: {}, runs: null, missions: null, sessions: null, agents: null, dead: false, poll: null,
    run: num(p.get("run")), runRes: null, runDetail: null,
    session: num(p.get("session")), sessionRes: null, sessionDetail: null, chatFiles: [], chatState: "",
    mission: num(p.get("mission")), missionRes: null, missionDetail: null, missionFiles: [], missionState: "", actionState: {},
    b: { banner: el("div"), live: el("div"), runs: el("div"), run: el("div"), chat: el("div"), missions: el("div"), mission: el("div"), create: el("div"), agents: el("div"), foot: el("div.plate-foot.mono") },
  };
  root.append(
    R.b.banner,
    block("runs · live and recent, as Mission Control reports them", [R.b.live, R.b.runs], { span2: true, id: "agents-runs" }),
    block("run · step timeline", [R.b.run], { span2: true, id: "agents-run" }),
    block("chat · sessions with the agent fleet", [R.b.chat], { span2: true, id: "agents-chat" }),
    block("missions · Mission Control projects", [R.b.missions], { id: "agents-missions" }),
    block("mission · steps, deliverables and actions", [R.b.mission], { id: "agents-mission" }),
    block("new mission", [R.b.create], { span2: true, id: "agents-create" }),
    block("agents · the fleet's configured agents", [R.b.agents], { span2: true, id: "agents-agents" }),
    R.b.foot,
  );
  for (const [topic, name] of [["mc.runs", "runs"], ["mc.missions", "missions"], ["mc.sessions", "sessions"], ["mc.agents", "agents"]]) {
    ctx.events.on(topic, (st) => { if (st.changed || st.ok === false) refetch(name).then(renderAll); });
  }
  await Promise.all(Object.keys(PATHS).map(refetch));
  if (!R || R.dead) return;
  if (!R.session && R.sessions && R.sessions.sessions && R.sessions.sessions.length) R.session = newest(R.sessions.sessions).id;
  renderAll();
  await Promise.all([R.run ? loadRun(R.run) : null, R.session ? loadSession(R.session) : null, R.mission ? loadMission(R.mission) : null]);
}

export function unmount() { if (!R) return; R.dead = true; if (R.poll) clearInterval(R.poll); R = null; }

function num(v) { const n = Number(v); return Number.isInteger(n) && n >= 0 && v !== null && v !== "" ? n : null; }
function newest(rows) { return rows.slice().sort((a, b) => String(b.updatedAt || b.createdAt || "").localeCompare(String(a.updatedAt || a.createdAt || "")))[0]; }

async function refetch(name) {
  const r = await R.ctx.api.get(PATHS[name]);
  if (!R || R.dead) return;
  R.res[name] = r;
  if (r.ok) { if (r.data !== null && r.data !== undefined) R[name] = r.data; else if (!r.meta || r.meta.stale !== true) R[name] = null; }
}

function setURL() {
  const u = new URL(location.href); u.searchParams.set("plate", "agents");
  for (const k of ["run", "session", "mission"]) { if (R[k] !== null && R[k] !== undefined) u.searchParams.set(k, String(R[k])); else u.searchParams.delete(k); }
  history.replaceState(null, "", u);
}

function chip(word) {
  const { vocab, dom } = R.ctx;
  if (word === null || word === undefined || word === "") return dom.el("span.dim", { text: "not reported" });
  return vocab.known("mc", word) ? vocab.state("mc", word) : dom.el("span", { text: String(word), title: `${word}: not a word of the Mission Control vocabulary; shown as received` });
}

function agentName(id) {
  const a = R.agents && Array.isArray(R.agents.agents) ? R.agents.agents.find((x) => x.id === id) : null;
  return a ? a.name : id === null || id === undefined ? "no agent" : `agent #${id}`;
}

function elapsed(r) {
  const { format } = R.ctx;
  const t0 = format.epochS(r.startedAt || r.createdAt);
  const t1 = r.finishedAt ? format.epochS(r.finishedAt) : (r.status === "running" || r.status === "queued" ? Date.now() / 1000 : null);
  return t0 !== null && t1 !== null ? format.dur(Math.max(0, t1 - t0)) : format.DASH;
}

function asText(c) {
  if (c === null || c === undefined) return "";
  if (typeof c === "string") return c;
  if (typeof c === "object") return c.text || c.content || c.output || c.message || JSON.stringify(c, null, 1);
  return String(c);
}

function viaNote() {
  const d = R.runs || R.missions || R.sessions || R.agents;
  return d && d.via === "proxy" ? d.via_note : null;
}

function writeResult(r, what) {
  if (r.ok) return { cls: "", text: `${what}: done (ledger #${r.data && r.data.ledger_n !== null && r.data.ledger_n !== undefined ? r.data.ledger_n : "not written"})` };
  return { cls: r.status === 503 || r.status === 400 ? "act" : "fault", text: `${what}: ${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})` };
}

function renderAll() { renderBanner(); renderRuns(); renderRun(); renderChat(); renderMissions(); renderMission(); renderCreate(); renderAgents(); renderFoot(); }

function renderBanner() {
  const { dom } = R.ctx; const { clear, note } = dom;
  const box = R.b.banner; clear(box);
  box.append(note("agent-reported", "Mission Control's runs, chats and missions are what the agents say they did: the compute truth is the Pipeline and Evidence plates."));
  const v = viaNote();
  if (v) box.append(note("reads through the rig's read proxy", `${v}. Sending chats, creating or launching missions, approving steps and uploading need the GRID CONTROL token on this desktop (~/.config/aces/gridcontrol.env, mode 0600).`, { act: true }));
}

// ---------- runs ----------
function renderRuns() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table, icon } = dom;
  const live = R.b.live, list = R.b.runs; clear(live); clear(list);
  const abs = absence(R.res.runs, "Mission Control runs");
  if (abs) list.append(abs);
  const d = R.runs;
  if (!d) { if (!abs) list.append(note("runs not loaded", "no reply yet")); return; }
  const rows = Array.isArray(d.runs) ? d.runs : [];
  const running = rows.filter((x) => x.status === "running" || x.status === "queued");
  const counts = d.counts || {};
  R.ctx.coord(`${rows.length} recent runs · ${Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(" · ")}`);
  live.append(el("div.line", [el("span.k", { text: "now" }), running.length ? el("b.ink-cyan", { text: `${running.filter((x) => x.status === "running").length} running · ${running.filter((x) => x.status === "queued").length} queued` }) : el("b", { text: "nothing running: the agent fleet is idle" })]));
  const pick = (x) => { R.run = x.id; setURL(); loadRun(x.id); renderRuns(); };
  list.append(table([
    { th: "run", render: (x) => el("button.linkish" + (x.id === R.run ? ".eye" : ""), { type: "button", text: `#${x.id}`, onclick: () => pick(x), title: `open run #${x.id}` }) },
    { th: "agent", render: (x) => [icon(x.kind === "chat" ? "kind-chat" : "kind-mission"), " ", agentName(x.agentId)], cls: "nm" },
    { th: "status", render: (x) => chip(x.status) },
    { th: "review", render: (x) => (x.review && x.review !== "none" ? chip(x.review) : el("span.dim", { text: "none" })) },
    { th: "elapsed", render: (x) => elapsed(x) },
    { th: "model", render: (x) => String(x.model || "").split("/").pop() || format.DASH },
    { th: "tokens", num: true, render: (x) => (x.usage && (x.usage.prompt !== null || x.usage.completion !== null) ? ((x.usage.prompt || 0) + (x.usage.completion || 0)).toLocaleString() : "not reported") },
    { th: "input", render: (x) => el("span.dim", { text: asText(x.input).slice(0, 120) }), cls: "body" },
  ], rows, { empty: "Mission Control reports no runs", caption: "Mission Control runs", cls: "wrapnames" }));
}

async function loadRun(id) {
  const r = await R.ctx.api.get(`/api/mc/run/${id}`);
  if (!R || R.dead || R.run !== id) return;
  R.runRes = r; R.runDetail = r.ok ? r.data : null;
  renderRun();
}

function renderRun() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, kv, icon } = dom;
  const box = R.b.run; clear(box);
  if (R.run === null) { box.append(note("no run selected", "pick a run in the list to read its input, output, error and step timeline")); return; }
  const abs = absence(R.runRes, `run #${R.run}`);
  if (abs) box.append(abs);
  const d = R.runDetail;
  if (!d) { if (!abs) box.append(note(`run #${R.run}`, "loading")); return; }
  box.append(el("h3.sub", [icon(d.kind === "chat" ? "kind-chat" : "kind-mission"), ` run #${d.id} · ${agentName(d.agentId)} `, chip(d.status)]));
  box.append(kv([["model", String(d.model || "").split("/").pop() || null], ["tokens", d.usage ? `${(d.usage.prompt || 0).toLocaleString()} in → ${(d.usage.completion || 0).toLocaleString()} out` : null], ["elapsed", elapsed(d)], ["trigger", d.trigger], ["kind", d.kind], ["review", d.review && d.review !== "none" ? d.review : null], ["parent", d.parentRunId ? `#${d.parentRunId}` : null], ["retry of", d.retryOf ? `#${d.retryOf}` : null], ["started", d.startedAt ? format.utc(d.startedAt) : null], ["finished", d.finishedAt ? format.utc(d.finishedAt) : null]]));
  box.append(el("div.cap", { text: "input · as written" }), el("pre.pre", { text: asText(d.input) || "empty" }));
  if (d.error) box.append(el("div.cap.ink-fault", { text: "error · how it failed" }), el("pre.pre.err", { text: asText(d.error) }));
  if (d.output) box.append(el("div.cap", { text: "output" }), el("pre.pre", { text: asText(d.output) }));
  const steps = Array.isArray(d.steps) ? d.steps : [];
  box.append(el("div.cap", { text: `step timeline · ${steps.length}${d.steps_total > steps.length ? " of " + d.steps_total + " (the last ones)" : ""}` }));
  if (!steps.length) box.append(el("div.prose.dim", { text: "no steps recorded" }));
  const line = el("div.stepline");
  for (const s of steps) {
    let body = s.contentJson;
    try { const j = JSON.parse(s.contentJson); body = typeof j === "string" ? j : JSON.stringify(j, null, 1); } catch (e) { /* shown as stored */ }
    line.append(el("div.step" + (s.kind === "error" ? ".err" : ""), [icon(KIND_ICON[s.kind] || "step-info"), el("span.nm", { text: s.name || s.kind || "step" }), el("span.ms", { text: s.elapsedMs ? format.dur(s.elapsedMs / 1000) : "" }), el("span.bd", { text: asText(body).slice(0, 4000) })]));
  }
  box.append(line);
  const arts = Array.isArray(d.artifacts) ? d.artifacts : [];
  if (arts.length) box.append(el("div.line", [el("span.k", { text: "artifacts" }), ...arts.map((a) => el("a.chip", { href: `/api/mc/artifact/${a.id}`, download: a.filename || "", text: `${a.filename || "artifact " + a.id}${a.sizeBytes ? " · " + format.bytes(a.sizeBytes) : ""}` }))]));
}

// ---------- chat ----------
async function loadSession(id) {
  const r = await R.ctx.api.get(`/api/mc/session/${id}`);
  if (!R || R.dead || R.session !== id) return;
  R.sessionRes = r; R.sessionDetail = r.ok ? r.data : null;
  renderChat();
}

function renderChat() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, button } = dom;
  const box = R.b.chat; clear(box);
  const abs = absence(R.res.sessions, "chat sessions");
  if (abs) box.append(abs);
  const list = R.sessions && Array.isArray(R.sessions.sessions) ? R.sessions.sessions.slice().sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || ""))) : [];
  const sel = el("select", { "aria-label": "chat session", onchange: (e) => { R.session = Number(e.target.value); setURL(); R.sessionDetail = null; renderChat(); loadSession(R.session); } });
  for (const s of list.slice(0, 300)) sel.append(el("option", { value: String(s.id), text: `${s.title || "session " + s.id} · ${agentName(s.agentId)} · ${format.utcShort(s.updatedAt || s.createdAt)}`, selected: s.id === R.session ? "selected" : null }));
  box.append(el("div.btnrow", [el("label.k", { text: `session (${list.length})` }), sel, button("New session", { onclick: newSession, title: "chat.createSession (needs the GRID CONTROL token; ledgered)" })]));
  const d = R.sessionDetail;
  const logBox = el("div.chatlog", { role: "log", "aria-live": "polite" });
  if (R.session === null) logBox.append(note("no session", "create one, or pick one above"));
  else if (!d) logBox.append(absence(R.sessionRes, `session #${R.session}`) || note(`session #${R.session}`, "loading"));
  else {
    const msgs = Array.isArray(d.messages) ? d.messages : [];
    if (d.messages_total > msgs.length) logBox.append(el("div.prose.dim", { text: `the last ${msgs.length} of ${d.messages_total} messages` }));
    if (!msgs.length) logBox.append(el("div.prose.dim", { text: "empty session" }));
    for (const m of msgs) {
      const mine = m.role === "user";
      logBox.append(el("div.msg" + (mine ? ".user" : ".assistant"), [el("div.who", { text: mine ? "you" : agentName(m.agentId || d.agentId) }), el("div.body", { text: asText(m.content) })]));
    }
  }
  box.append(logBox);
  const ta = el("textarea", { rows: "3", placeholder: "message the agent (Ctrl+Enter sends)", "aria-label": "chat message" });
  const file = el("input", { type: "file", multiple: true, hidden: true, onchange: (e) => attach(e.target.files, R.chatFiles, () => renderChat()) });
  const send = button("Send", { cls: "btn primary", disabled: R.session === null, onclick: () => sendMessage(ta.value) });
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send.click(); });
  box.append(ta, el("div.btnrow", [button("Attach files", { onclick: () => file.click() }), file, ...R.chatFiles.map((u) => el("span.chip", { text: u.filename })), send, el("span.result", { text: R.chatState })]));
  setTimeout(() => { logBox.scrollTop = logBox.scrollHeight; }, 0);
}

async function newSession() {
  const r = await R.ctx.api.post("/api/mc/session", {});
  if (!R || R.dead) return;
  R.chatState = writeResult(r, "new session").text;
  if (r.ok && r.data && r.data.result && (r.data.result.id || r.data.result.sessionId)) { R.session = r.data.result.id || r.data.result.sessionId; setURL(); await refetch("sessions"); await loadSession(R.session); }
  renderChat();
}

async function attach(files, into, done) {
  for (const f of files) {
    if (f.size > 16 * 1024 * 1024) { R.chatState = `${f.name}: larger than the 16 MiB upload cap`; continue; }
    const b64 = await new Promise((res, rej) => { const fr = new FileReader(); fr.onload = () => res(String(fr.result).split(",", 2)[1] || ""); fr.onerror = () => rej(fr.error); fr.readAsDataURL(f); });
    const r = await R.ctx.api.post("/api/mc/upload", { filename: f.name, mime: f.type || "application/octet-stream", data_b64: b64 });
    if (!R || R.dead) return;
    if (r.ok && r.data && r.data.result && r.data.result.id !== undefined) into.push({ id: r.data.result.id, filename: r.data.result.filename || f.name });
    else R.chatState = R.missionState = writeResult(r, `upload ${f.name}`).text;
  }
  done();
}

async function sendMessage(text) {
  if (!text || !text.trim() || R.session === null) return;
  const r = await R.ctx.api.post(`/api/mc/session/${R.session}/message`, { content: text, uploads: R.chatFiles.map((u) => u.id) });
  if (!R || R.dead) return;
  R.chatState = r.ok ? "sent: the agent is working; the reply appears here" : writeResult(r, "send").text;
  if (r.ok) {
    R.chatFiles = [];
    const sid = R.session; const t0 = Date.now();
    if (R.poll) clearInterval(R.poll);
    R.poll = setInterval(async () => {
      if (!R || R.dead || R.session !== sid) { if (R && R.poll) { clearInterval(R.poll); R.poll = null; } return; }
      await loadSession(sid);
      const msgs = R && R.sessionDetail && Array.isArray(R.sessionDetail.messages) ? R.sessionDetail.messages : [];
      const last = msgs[msgs.length - 1];
      if ((last && last.role !== "user") || Date.now() - t0 > 180000) { clearInterval(R.poll); R.poll = null; R.chatState = last && last.role !== "user" ? "" : "no reply within 3 min: Mission Control may still be working (see Runs)"; renderChat(); }
    }, 3000);
  }
  renderChat();
}

// ---------- missions ----------
function renderMissions() {
  const { dom } = R.ctx; const { el, clear, absence, note, table } = dom;
  const box = R.b.missions; clear(box);
  const abs = absence(R.res.missions, "Mission Control missions");
  if (abs) box.append(abs);
  const d = R.missions;
  if (!d) { if (!abs) box.append(note("missions not loaded", "no reply yet")); return; }
  const rows = (Array.isArray(d.missions) ? d.missions : []).slice().sort((a, b) => (b.id || 0) - (a.id || 0));
  const pick = (m) => { R.mission = m.id; setURL(); R.missionDetail = null; renderMissions(); renderMission(); loadMission(m.id); };
  box.append(table([
    { th: "mission", render: (m) => el("button.linkish" + (m.id === R.mission ? ".eye" : ""), { type: "button", text: m.title || `mission ${m.id}`, onclick: () => pick(m) }), cls: "nm" },
    { th: "status", render: (m) => chip(m.status) },
    { th: "steps", render: (m) => (m.steps ? `${m.steps_done}/${m.steps} done` : "no plan yet") },
    { th: "updated", render: (m) => R.ctx.format.utcShort(m.updatedAt || m.createdAt) },
  ], rows, { empty: "Mission Control reports no missions", caption: "Mission Control missions", cls: "wrapnames" }));
}

async function loadMission(id) {
  const r = await R.ctx.api.get(`/api/mc/mission/${id}`);
  if (!R || R.dead || R.mission !== id) return;
  R.missionRes = r; R.missionDetail = r.ok ? r.data : null;
  renderMission();
}

async function missionAction(id, action) {
  R.actionState[`m${id}`] = `${action}: sending`; renderMission();
  const r = await R.ctx.api.post(`/api/mc/mission/${id}/${action}`, {});
  if (!R || R.dead) return;
  R.actionState[`m${id}`] = writeResult(r, action).text;
  await Promise.all([refetch("missions"), loadMission(id)]);
  renderMissions(); renderMission();
}

async function approve(itemId, missionId) {
  const r = await R.ctx.api.post(`/api/mc/item/${itemId}/approve`, {});
  if (!R || R.dead) return;
  R.actionState[`i${itemId}`] = writeResult(r, "approve").text;
  await loadMission(missionId);
}

function renderMission() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, kv, button } = dom;
  const box = R.b.mission; clear(box);
  if (R.mission === null) { box.append(note("no mission selected", "pick a mission to read its objective, steps, outputs and deliverables")); return; }
  const abs = absence(R.missionRes, `mission #${R.mission}`);
  if (abs) box.append(abs);
  const m = R.missionDetail;
  if (!m) { if (!abs) box.append(note(`mission #${R.mission}`, "loading")); return; }
  const acts = [];
  if (["planning", "planned"].includes(m.status)) acts.push(["launch", "Launch"]);
  if (m.status === "running") acts.push(["retryFailed", "Retry failed steps"]);
  if (["review", "running"].includes(m.status)) acts.push(["compile", "Compile deliverables"]);
  if (m.status === "review") acts.push(["complete", "Mark done"]);
  box.append(el("h3.sub", [`${m.title || "mission " + m.id} `, chip(m.status)]));
  box.append(el("div.btnrow", [...acts.map(([a, label]) => button(label, { cls: a === "launch" ? "btn primary" : "btn", title: `missions.${a} (needs the GRID CONTROL token; ledgered)`, onclick: () => missionAction(m.id, a) })), el("span.result", { text: R.actionState[`m${m.id}`] || "" })]));
  if (m.planError) box.append(el("pre.pre.err", { text: `plan error: ${m.planError}` }));
  box.append(kv([["created", format.utc(m.createdAt)], ["updated", format.utc(m.updatedAt)], ["completed", m.completedAt ? format.utc(m.completedAt) : null]]));
  box.append(el("div.cap", { text: "objective" }), el("pre.pre", { text: String(m.objective || "").replace(/\n*\[context files\]\n(- #\d+ .+\n?)+/g, "") || "empty" }));
  const files = [...String(m.objective || "").matchAll(/^- #(\d+) (.+?)(?: \(fetch: [^)]*\))?$/gm)].map((x) => ({ id: x[1], name: x[2] }));
  if (files.length) box.append(el("div.line", [el("span.k", { text: "context files" }), ...files.map((f) => el("a.chip", { href: `/api/mc/upload/${f.id}`, target: "_blank", rel: "noopener", text: f.name }))]));
  const items = Array.isArray(m.items) ? m.items : [];
  if (!items.length) box.append(el("div.prose.dim", { text: "plan not generated yet: launch it and the planner splits it into steps and assigns agents" }));
  for (const i of items) {
    const run = m.runs && i.runId ? m.runs[String(i.runId)] : null;
    const arts = Array.isArray(i.artifacts) ? i.artifacts : [];
    box.append(el("div.mstep", [
      el("div.hd", [el("b", { text: `step ${i.seq} · ${i.title || ""}` }), chip(i.status), i.status === "review" ? button("Approve", { cls: "btn", title: "missions.approveItem: accept this step's deliverable (Mission Control's review; ledgered)", onclick: () => approve(i.id, m.id) }) : null, el("span.result", { text: R.actionState[`i${i.id}`] || "" })]),
      el("div.mono.dim", { text: `agent ${agentName(i.agentId)} · deliverable ${i.deliverable_type || "not recorded"}${i.runId ? " · run #" + i.runId : ""}` }),
      i.brief ? el("div.brief", { text: String(i.brief).slice(0, 800) }) : null,
      run && run.error ? el("pre.pre.err", { text: asText(run.error) }) : null,
      run && run.output ? el("pre.pre", { text: asText(run.output) }) : null,
      arts.length ? el("div.line", arts.map((a) => el("a.chip", { href: `/api/mc/artifact/${a.id}`, download: a.filename || "", text: `${a.filename}${a.sizeBytes ? " · " + format.bytes(a.sizeBytes) : ""}` }))) : null,
    ]));
  }
  const comp = Array.isArray(m.compilerRuns) ? m.compilerRuns : [];
  if (comp.length) {
    box.append(el("div.cap", { text: "final deliverables" }));
    for (const c of comp) box.append(el("div.line", [el("span.mono", { text: `compiler run #${c.id} ` }), chip(c.status), ...(c.artifacts || []).map((a) => el("a.chip", { href: `/api/mc/artifact/${a.id}`, download: a.filename || "", text: a.filename }))]));
  }
}

function renderCreate() {
  const { dom } = R.ctx; const { el, clear, button } = dom;
  const box = R.b.create; clear(box);
  const title = el("input", { type: "text", placeholder: "mission name", "aria-label": "mission name", maxlength: "200" });
  const obj = el("textarea", { rows: "4", placeholder: "the mission: what the agents should do and deliver", "aria-label": "mission objective" });
  const file = el("input", { type: "file", multiple: true, hidden: true, onchange: (e) => attach(e.target.files, R.missionFiles, () => renderCreate()) });
  const go = async (launch) => {
    if (!title.value.trim() || !obj.value.trim()) { R.missionState = "a mission needs both a name and the objective"; renderCreate(); return; }
    const r = await R.ctx.api.post("/api/mc/mission", { title: title.value, objective: obj.value, files: R.missionFiles.map((f) => ({ id: f.id, filename: f.filename })), launch });
    if (!R || R.dead) return;
    R.missionState = r.ok ? `mission #${r.data.id} created${launch ? (r.data.launched ? " and launched" : `; launch failed: ${r.data.launch_error}`) : " as a draft"}` : writeResult(r, "create").text;
    if (r.ok) { R.missionFiles = []; R.mission = r.data.id; setURL(); await refetch("missions"); renderMissions(); loadMission(r.data.id); }
    renderCreate();
  };
  box.append(title, obj, el("div.btnrow", [button("Attach context files", { onclick: () => file.click() }), file, ...R.missionFiles.map((f) => el("span.chip", { text: f.filename })),
    button("Save draft", { onclick: () => go(false), title: "missions.create (needs the GRID CONTROL token; ledgered)" }),
    button("Create and launch", { cls: "btn primary", onclick: () => go(true), title: "missions.create then missions.launch (needs the GRID CONTROL token; ledgered)" }),
    el("span.result", { text: R.missionState })]));
}

// ---------- agents ----------
function renderAgents() {
  const { dom } = R.ctx; const { el, clear, absence, note, table } = dom;
  const box = R.b.agents; clear(box);
  const abs = absence(R.res.agents, "Mission Control agents");
  if (abs) box.append(abs);
  const d = R.agents;
  if (!d) { if (!abs) box.append(note("agents not loaded", "no reply yet")); return; }
  const rows = Array.isArray(d.agents) ? d.agents : [];
  const runs = R.runs && Array.isArray(R.runs.runs) ? R.runs.runs : [];
  box.append(table([
    { th: "agent", key: "name", cls: "nm" },
    { th: "role", key: "role", cls: "body" },
    { th: "specialty", key: "specialty", cls: "body" },
    { th: "model", render: (a) => a.model || "auto" },
    { th: "state", render: (a) => chip(a.enabled ? "enabled" : "disabled") },
    { th: "tools", num: true, render: (a) => (a.tools === null || a.tools === undefined ? "not readable" : String(a.tools)) },
    { th: "recent runs", num: true, render: (a) => String(runs.filter((x) => x.agentId === a.id).length) },
    { th: "now", render: (a) => { const cur = runs.find((x) => x.agentId === a.id && (x.status === "running" || x.status === "queued")); return cur ? [chip(cur.status), ` #${cur.id}`] : el("span.dim", { text: "idle" }); } },
  ], rows, { empty: "Mission Control reports no agents", caption: "Mission Control agents", cls: "wrapnames" }));
}

function renderFoot() {
  const { dom } = R.ctx;
  const box = R.b.foot; dom.clear(box);
  const v = viaNote();
  box.append(dom.facts([`source GRID CONTROL Mission Control${v ? " · " + v : " · with this desktop's token"}`, "agent-reported, never compute truth", "every write ledgered (console.mc.*)"]));
}
