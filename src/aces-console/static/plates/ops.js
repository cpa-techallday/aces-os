// Ops plate: the one consequential control of the console (the rig mode switch: a human act, two steps, every
// gate of spec 4.5 printed with its reason, a typed confirmation, never pressed by anything but a person), the
// switch engine's timeline, GRID CONTROL's decision log, aces doctor verbatim, the policy, the configured links,
// installed packages and desktop entries with Open, and Stop console. Bindings: /api/rig/modes, /api/rig/switch,
// POST /api/rig/switch/prepare, POST /api/rig/switch, /api/rig/decisions, /api/doctor, POST /api/doctor/run,
// /api/policy, /api/system/*, /api/config; stamps rig.status, rig.switch, switch.outcome, rig.modes, fabric.manifest.
const PATHS = { modes: "/api/rig/modes", config: "/api/config", switch: "/api/rig/switch", decisions: "/api/rig/decisions", doctor: "/api/doctor", policy: "/api/policy", packages: "/api/system/packages", entries: "/api/system/entries", links: "/api/system/links", redteam: "/api/rig/redteam", downloads: "/api/rig/downloads" };
let R = null;

export async function mount(root, ctx) {
  const { dom } = ctx; const { el, block } = dom;
  R = {
    ctx, root, res: {}, stamps: {}, modes: null, config: null, switch: null, decisions: null, doctor: null, policy: null, packages: null, entries: null, links: null,
    target: ctx.params.get("mode") || "", session: [], outcomes: [], prepare: null, dialog: null, countdown: null, expired: false, busy: false,
    doctorBusy: false, doctorPoll: null, openResult: {}, stopResult: null, autoResult: null, rtResult: null, redteam: null, downloads: null, dead: false,
    b: { switch: el("div"), timeline: el("div"), decisions: el("div"), auto: el("div"), redteam: el("div"), downloads: el("div"), doctor: el("div"), policy: el("div"), links: el("div"), packages: el("div"), entries: el("div"), stop: el("div") },
  };
  root.append(
    block("mode switch · a human act, confirmed here", [R.b.switch], { id: "ops-switch" }),
    block("switch engine · GRID CONTROL /api/switch/status", [R.b.timeline], { id: "ops-timeline" }),
    block("decisions · GRID CONTROL decision log", [R.b.decisions], { id: "ops-decisions" }),
    block("auto-switch · GRID CONTROL /api/auto", [R.b.auto], { id: "ops-auto" }),
    block("red team · GRID CONTROL /api/redteam", [R.b.redteam], { id: "ops-redteam" }),
    block("model staging · GRID CONTROL downloads-status.json", [R.b.downloads], { span2: true, id: "ops-downloads" }),
    block("doctor · aces doctor, verbatim", [R.b.doctor], { id: "ops-doctor" }),
    block("policy · aces policy", [R.b.policy], { id: "ops-policy" }),
    block("packages · pacman -Q aces-* omarchy*", [R.b.packages], { id: "ops-packages" }),
    block("links · as configured and as the rig reports them", [R.b.links], { span2: true, id: "ops-links" }),
    block("desktop entries · owned by aces-* packages", [R.b.entries], { span2: true, id: "ops-entries" }),
    block("stop console", [R.b.stop], { span2: true, id: "ops-stop" }),
  );
  ctx.events.on("rig.status", () => { renderSwitch(); renderAuto(); });
  ctx.events.on("rig.redteam", (st) => { if (st.changed || st.ok === false) refetch("redteam").then(renderRedteam); });
  ctx.events.on("rig.downloads", (st) => { if (st.changed || st.ok === false) refetch("downloads").then(renderDownloads); });
  ctx.events.on("rig.switch", (st) => { R.stamps.switch = st; if (st.payload) R.switch = st.payload; renderTimeline(); renderSwitch(); });
  ctx.events.on("fabric.manifest", () => renderSwitch());
  ctx.events.on("rig.modes", (st) => { R.stamps.modes = st; if (st.ok !== false && st.payload && typeof st.payload === "object") R.modes = "gridcontrol_modes" in st.payload ? { ...(R.modes || {}), ...st.payload } : { ...(R.modes || {}), gridcontrol_modes: st.payload }; renderSwitch(); }); // the stamp inlines GRID CONTROL's mode table only
  ctx.events.on("switch.outcome", (st) => { if (st.payload) { R.outcomes.push({ ...st.payload, received_utc: new Date().toISOString() }); renderTimeline(); } });
  ctx.events.on("doctor", (st) => { if (st.changed) refetch("doctor").then(renderDoctor); });
  ctx.events.on("system.packages", (st) => { if (st.changed) refetch("packages").then(renderPackages); });
  ctx.events.on("system.entries", (st) => { if (st.changed) refetch("entries").then(renderEntries); });
  ctx.events.on("rig.decisions", (st) => { if (st.changed) refetch("decisions").then(renderDecisions); });
  await Promise.all(Object.keys(PATHS).map(refetch));
  if (R.dead) return;
  renderAll();
  renderInspector();
}

export function unmount() {
  if (!R) return;
  R.dead = true;
  if (R.countdown) clearInterval(R.countdown);
  if (R.doctorPoll) clearInterval(R.doctorPoll);
  if (R.dialog) { try { R.dialog.close(); } catch (e) { /* closed */ } R.dialog.remove(); }
  R = null;
}

async function refetch(name) {
  const r = await R.ctx.api.get(PATHS[name]);
  if (!R || R.dead) return;
  R.res[name] = r;
  if (r.ok) { if (r.data !== null && r.data !== undefined) R[name] = r.data; else if (!r.meta || r.meta.stale !== true) R[name] = null; }
}

function renderAll() { renderSwitch(); renderTimeline(); renderDecisions(); renderAuto(); renderRedteam(); renderDownloads(); renderDoctor(); renderPolicy(); renderLinks(); renderPackages(); renderEntries(); renderStop(); }

// ---------- auto-switch, red team, model staging (GRID CONTROL rev B's controls, here; every write typed, ledgered) ----------
function typedAction(placeholder, label, words, run, result) {
  const { dom } = R.ctx; const { el, button } = dom;
  const input = el("input.type.short", { type: "text", autocomplete: "off", spellcheck: "false", placeholder, "aria-label": placeholder });
  const btn = button(label, { cls: "btn", disabled: true, onclick: async () => { btn.disabled = true; await run(input.value); } });
  input.addEventListener("input", () => { btn.disabled = input.value !== words; });
  return el("div.actrow", [input, btn, result ? el("span.result" + (result.cls ? "." + result.cls : ""), { text: result.text }) : null]);
}
function actionResult(r, what) {
  return r.ok ? { cls: "", text: `${what}: accepted (ledger #${r.data && r.data.ledger_n !== null && r.data.ledger_n !== undefined ? r.data.ledger_n : "not written"})` }
    : { cls: r.status === 503 || r.status === 400 ? "act" : "fault", text: `${what}: ${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})` };
}
function renderAuto() {
  const { dom } = R.ctx; const { el, clear, note } = dom;
  const box = R.b.auto; clear(box);
  const st = R.ctx.stamp.get().status; const status = st ? st.payload : null;
  const on = status ? status.auto_enabled === true : null;
  box.append(el("div.line", [el("span.k", { text: "auto-switch" }), el("b" + (on ? ".ink-amber" : ""), { text: on === null ? "not reported" : on ? "on: GRID CONTROL switches modes by itself" : "off: modes change only when a person switches them" })]));
  if (on === null) { box.append(note("auto-switch state unknown", "no rig status received yet")); return; }
  const words = on ? "disable auto-switch" : "enable auto-switch";
  box.append(el("div.prose", { text: on ? "Turning it off returns mode switching to people only." : "Turning it on lets GRID CONTROL change the rig's mode without a person; the switch above is refused while it is on. Type the words to enable the button." }));
  box.append(typedAction(`type ${words}`, on ? "Turn auto-switch off" : "Turn auto-switch on", words, async (typed) => {
    const r = await R.ctx.api.post("/api/rig/auto", { enabled: !on, confirm: typed });
    if (!R || R.dead) return;
    R.autoResult = actionResult(r, on ? "auto-switch off" : "auto-switch on"); renderAuto();
  }, R.autoResult));
}
function renderRedteam() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, kv } = dom;
  const box = R.b.redteam; clear(box);
  const abs = absence(R.res.redteam, "red team status");
  if (abs) box.append(abs);
  const s = R.redteam;
  if (!s) { if (!abs) box.append(note("red team status not loaded", "no reply yet")); return; }
  const vms = Array.isArray(s.vms) ? s.vms : [];
  const iso = s.iso || null;
  box.append(kv([["mission VM", vms.length ? vms.join(", ") : "no VM", vms.length ? "ink-cyan" : null], ["Tor transproxy", s.tor_transproxy === true ? "live :9040" : s.tor_transproxy === false ? "down" : "not reported", s.tor_transproxy === false ? "ink-fault" : null],
    ["ik-redteam model", s.ik_redteam], ["ISO", iso ? (iso.bytes >= iso.target_bytes ? "ready" : `${format.bytes(iso.bytes)} of ${format.bytes(iso.target_bytes)} (${s.iso_unit || "unit not reported"})`) : "not reported"], ["read", s.via === "proxy" ? "through the rig's read proxy" : "with this desktop's token"]]));
  if (vms.length) {
    box.append(el("div.prose", { text: "Ending the mission destroys the VM and everything written in it. Type END MISSION to enable the button." }));
    box.append(typedAction("type END MISSION", "End mission (destroy the VM)", "END MISSION", async (typed) => {
      const r = await R.ctx.api.post("/api/rig/redteam/vm", { action: "down", confirm: typed });
      if (!R || R.dead) return;
      R.rtResult = actionResult(r, "end mission"); renderRedteam(); refetch("redteam").then(renderRedteam);
    }, R.rtResult));
  } else {
    box.append(el("div.prose", { text: "Starting boots the BlackArch mission VM with Tor-only egress; handover requires IsTor verification. Type start mission vm to enable the button." }));
    box.append(typedAction("type start mission vm", "Start the mission VM", "start mission vm", async (typed) => {
      const r = await R.ctx.api.post("/api/rig/redteam/vm", { action: "up", confirm: typed });
      if (!R || R.dead) return;
      R.rtResult = actionResult(r, "start mission VM"); renderRedteam(); refetch("redteam").then(renderRedteam);
    }, R.rtResult));
  }
}
function renderDownloads() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table } = dom;
  const box = R.b.downloads; clear(box);
  const abs = absence(R.res.downloads, "model staging");
  if (abs) box.append(abs);
  const d = R.downloads;
  if (!d) { if (!abs) box.append(note("model staging not loaded", "no reply yet")); return; }
  const items = Array.isArray(d.items) ? d.items : [];
  box.append(el("div.line", [el("span.k", { text: "progress" }), el("b", { text: `${d.completed ?? "?"} of ${d.total ?? "?"} complete` }), el("span.k", { text: "current" }), el("b", { text: d.current || "none" }), el("span.k", { text: "updated" }), el("b", { text: d.updated ? format.utc(d.updated) : "not reported" })]));
  box.append(table([
    { th: "repository", key: "repo", cls: "nm" },
    { th: "pattern", key: "pattern" },
    { th: "state", render: (i) => (i.done ? el("span.state.ink-chalk", { text: "done" }) : el("span.state.ink-amber", { text: "queued" })) },
    { th: "notes", key: "notes", cls: "body" },
  ], items, { empty: "no items in downloads-status.json", caption: "model staging", cls: "wrapnames" }));
}

// ---------- gates of spec 4.5: every one printed with its state and reason ----------
function gates() {
  const { format } = R.ctx;
  const snap = R.ctx.stamp.get();
  const st = snap.status; const status = st && st.ok !== false ? st.payload : (st ? st.payload : null);
  const sw = R.switch || (snap.switch && snap.switch.payload) || null;
  const mf = snap.manifest && snap.manifest.payload ? snap.manifest.payload : null;
  const ageS = snap.statusAgeS;
  const g = [];
  g.push({ name: "rig status younger than 30 s", ok: !!status && ageS !== null && ageS <= 30, why: !status ? "no rig status received" : ageS === null ? "status age not reported" : ageS > 30 ? `last rig status is ${format.span(ageS)} old` : null });
  g.push({ name: "detected_mode is not transition", ok: !!status && status.detected_mode !== "transition", why: !status ? "no rig status" : status.detected_mode === "transition" ? "detected_mode is transition" : null });
  g.push({ name: "no switch in progress", ok: !!status && (status.switching === null || status.switching === undefined), why: !status ? "no rig status" : status.switching ? `switching in progress (${typeof status.switching === "string" ? status.switching : status.switching.target || "target not reported"})` : null });
  g.push({ name: "switch engine idle or done", ok: !sw || sw.idle === true || sw.done === true, why: sw && !(sw.idle === true || sw.done === true) ? `switch engine busy: ${sw.target || "target not reported"} ${sw.phase || ""}`.trim() : null });
  g.push({ name: "auto mode off", ok: !!status && status.auto_enabled !== true, why: !status ? "no rig status" : status.auto_enabled === true ? "auto mode is on; switching by hand is refused" : null });
  const modes = R.modes && R.modes.gridcontrol_modes ? R.modes.gridcontrol_modes : null;
  const t = R.target; const m = modes && t ? modes[t] : null;
  g.push({ name: "a target mode is chosen", ok: !!t, why: t ? null : "choose a target mode" });
  if (t) {
    g.push({ name: "target is not the current mode", ok: !!status && t !== status.mode, why: status && t === status.mode ? `${t} is the current mode` : !status ? "no rig status" : null });
    g.push({ name: "target is switchable, and reported so", ok: !!(m && m.switchable === true && m.switchable_reported !== false), why: !modes ? "GRID CONTROL mode table not loaded" : !m ? `unknown mode ${t}` : m.switchable_reported === false ? "switchable not reported for this mode" : m.switchable !== true ? `${t} is not switchable` : null });
    g.push({ name: "no pending text on the target", ok: !(m && m.pending), why: m && m.pending ? `pending: ${m.pending}` : null });
  }
  g.push({ name: "fabric manifest younger than 900 s", ok: !!mf && mf.age_s !== null && mf.age_s !== undefined && mf.age_s <= 900, why: !mf ? "no manifest stamp received" : mf.age_s === null || mf.age_s === undefined ? "no fabric manifest: run Refresh fabric on the Fabric plate" : mf.age_s > 900 ? `manifest is ${format.span(mf.age_s)} old; refresh it first` : null });
  const tok = R.config && R.config.tokens && R.config.tokens.grid_control ? R.config.tokens.grid_control : null;
  g.push({ name: "GRID CONTROL token configured on the backend", ok: tok ? !!tok.configured : null, why: tok ? (tok.configured ? null : "GRID CONTROL token not configured: set ACES_GC_TOKEN or ~/.config/aces/gridcontrol.env") : "/api/config not loaded; the backend checks on prepare" });
  g.push({ name: "not a fixture replay", ok: !snap.fixture, why: snap.fixture ? `${snap.fixture}: the switch is never pressed against a replay` : null });
  return g;
}

function renderSwitch() {
  const { dom } = R.ctx; const { el, clear, absence, note, button } = dom;
  const box = R.b.switch; clear(box);
  const snap = R.ctx.stamp.get();
  const status = snap.status && snap.status.payload ? snap.status.payload : null;
  const abs = absence(R.res.modes, "GRID CONTROL mode table");
  if (abs) box.append(abs);
  const modes = R.modes && R.modes.gridcontrol_modes ? R.modes.gridcontrol_modes : null;
  const k = (t) => el("span.k", { text: t }), b = (t, cls = "") => el("b" + (cls ? "." + cls : ""), { text: t === null || t === undefined ? "not reported" : String(t) });
  box.append(el("div.line", [k("current mode"), b(status ? status.mode : null, "ink-amber"), k("detected"), b(status ? status.detected_mode : null, status && status.detected_mode === "transition" ? "ink-amber" : ""), k("modes reported"), b(modes ? String(Object.keys(modes).length) : null)]));
  const sel = el("select.sel#opsTarget", { "aria-label": "target mode", onchange: (e) => { R.target = e.target.value; renderSwitch(); renderInspector(); } });
  sel.append(el("option", { value: "", text: "choose a target mode…" }));
  for (const [id, m] of Object.entries(modes || {}).sort((a, b2) => a[0].localeCompare(b2[0]))) {
    sel.append(el("option", { value: id, text: `${id} — ${m.label || "label not reported"}${status && status.mode === id ? " (current)" : ""}${m.pending ? " (pending)" : ""}` }));
  }
  sel.value = R.target && modes && modes[R.target] ? R.target : "";
  box.append(el("div.actrow", [el("label.lab", { for: "opsTarget", text: "target" }), sel]));
  const m = modes && R.target ? modes[R.target] : null;
  if (m && m.pending) box.append(el("div.prose", [el("span.cap.ink-amber", { text: "pending, as GRID CONTROL reports it: " }), el("span", { text: m.pending })]));
  if (m) {
    const sim = R.modes && R.modes.sim_modes ? R.modes.sim_modes[R.target] : null;
    box.append(el("div.line", [k("switchable"), b(String(m.switchable)), k("reported"), b(m.switchable_reported === false ? "no" : "yes"), k("sim pool"), b(sim ? (Array.isArray(sim.sim_gpus) ? (sim.sim_gpus.length ? sim.sim_gpus.join(" ") : "none") : "not in the sim mode table") : "not in the sim mode table")]));
  }
  const gs = gates();
  const list = el("div.gates", { role: "list", "aria-label": "switch gates" });
  for (const g of gs) {
    list.append(el("div.gate" + (g.ok === false ? ".hold" : ""), { role: "listitem" }, [el("i.ring-ic" + (g.ok === false ? ".act" : g.ok === null ? ".pending" : "")), el("span", [g.name, g.why ? el("span.why", { text: g.why }) : null])]));
  }
  box.append(el("div.cap", { text: "gates" }), list);
  const holding = gs.filter((g) => g.ok === false);
  const btn = button("Switch mode…", { cls: "btn primary", disabled: holding.length > 0 || R.busy, title: holding.length ? holding.map((g) => g.why).join("; ") : "prepares the switch: the backend re-checks every gate and issues a 30 s nonce; nothing is switched until you type the mode name and confirm", onclick: prepare });
  box.append(el("div.actrow", [btn, el("span.why", { text: holding.length ? `disabled: ${holding[0].why}${holding.length > 1 ? ` (+${holding.length - 1} more)` : ""}` : "every gate passes; the backend re-checks them on prepare and on confirm" })]));
  const lastRefusal = R.session.filter((s) => s.cls === "act" || s.cls === "fault").slice(-1)[0];
  if (lastRefusal) box.append(el("div.result." + lastRefusal.cls, { text: `${lastRefusal.event}: ${lastRefusal.detail}` }));
  R.ctx.coord(status ? `mode ${status.mode}` : "");
}

function addSession(event, detail, cls = "") { R.session.push({ t: new Date().toISOString(), event, detail, cls }); }

async function prepare() {
  if (!R || R.busy || !R.target) return;
  R.busy = true; renderSwitch();
  const r = await R.ctx.api.post("/api/rig/switch/prepare", { mode: R.target });
  if (!R || R.dead) return;
  R.busy = false;
  if (!r.ok) {
    addSession("refused at prepare", `${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})`, r.status === 502 ? "fault" : "act");
    renderSwitch(); renderTimeline(); return;
  }
  R.prepare = r.data;
  addSession("prepared", `${r.data.from} → ${r.data.to}; the nonce expires in ${r.data.expires_s} s`, "");
  renderSwitch(); renderTimeline();
  openDialog(r.data);
}

function openDialog(p) {
  const { dom } = R.ctx; const { el, kv, button } = dom;
  const d = el("dialog.confirm", { "aria-labelledby": "confirmTitle" });
  const input = el("input.type", { type: "text", autocomplete: "off", spellcheck: "false", placeholder: `type ${p.to} to confirm`, "aria-label": "type the target mode name to confirm the switch" });
  const left = el("b", { text: String(p.expires_s) });
  const countdown = el("div.countdown", ["nonce expires in ", left, " s"]);
  const confirmBtn = button("Confirm the switch", { cls: "btn primary", disabled: true, onclick: () => confirmSwitch(p, input.value, confirmBtn) });
  const cancelBtn = button("Cancel", { onclick: () => closeDialog("cancelled by the operator") });
  const sim = R.modes && R.modes.sim_modes ? R.modes.sim_modes[p.to] : null;
  const listText = (a) => (Array.isArray(a) ? (a.length ? a.join(", ") : "none") : "not recorded");
  d.append(
    el("h3#confirmTitle", { text: `Switch the rig from ${p.from} to ${p.to}?` }),
    kv([["from", p.from], ["to", p.to, "act"], ["label", p.label], ["pending", p.pending], ["sim pool after the switch", p.sim_pool === null || p.sim_pool === undefined ? "not in the sim mode table" : listText(p.sim_pool)]]),
    el("div.consequences", [
      el("div.cap", { text: "consequences from the sim mode table" }),
      sim ? kv([["sim gpus", listText(sim.sim_gpus)], ["llm gpus", listText(sim.llm_gpus)], ["rest", listText(sim.rest)], ["keep services", listText(sim.keep_services)], ["stop services", listText(sim.stop_services)]]) : el("div.prose.dim", { text: `no sim mode entry for ${p.to}: the pools of this mode are not known to this console` }),
      p.gates ? kv(Object.entries(p.gates).map(([k2, v]) => [`gate ${k2}`, String(v)])) : null,
    ]),
    el("div.warn", { text: p.warning || "" }),
    countdown, input,
    el("div.btnrow", [cancelBtn, confirmBtn]),
  );
  input.addEventListener("input", () => { confirmBtn.disabled = input.value !== p.to || R.expired; });
  d.addEventListener("close", () => { if (R && R.countdown) { clearInterval(R.countdown); R.countdown = null; } if (R) R.dialog = null; d.remove(); });
  document.body.append(d);
  R.dialog = d; R.expired = false;
  d.showModal();
  input.focus();
  let remaining = Number(p.expires_s) || 30;
  R.countdown = setInterval(() => {
    remaining -= 1; left.textContent = String(Math.max(0, remaining));
    if (remaining <= 0) { R.expired = true; confirmBtn.disabled = true; countdown.append(" — expired; prepare again"); clearInterval(R.countdown); R.countdown = null; }
  }, 1000);
}

function closeDialog(reason) {
  if (R && R.dialog) { try { R.dialog.close(); } catch (e) { /* closed */ } }
  if (reason && R) { addSession("not confirmed", reason, ""); renderTimeline(); }
}

async function confirmSwitch(p, typed, confirmBtn) {
  confirmBtn.disabled = true;
  const r = await R.ctx.api.post("/api/rig/switch", { mode: p.to, nonce: p.nonce, confirm: typed });
  if (!R || R.dead) return;
  closeDialog(null);
  if (r.ok) addSession("started", `GRID CONTROL accepted the switch to ${p.to} (upstream ${r.data && r.data.upstream_status}; ledger request #${r.data && r.data.ledger ? r.data.ledger.request_n : "?"}, result #${r.data && r.data.ledger ? r.data.ledger.result_n : "?"}); the outcome arrives on the stream`, "live");
  else addSession(r.status === 410 ? "nonce refused" : r.status === 502 ? "upstream POST failed" : "refused at confirm", `${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})`, r.status === 502 ? "fault" : "act");
  renderTimeline(); renderSwitch();
}

function renderTimeline() {
  const { dom, format } = R.ctx; const { el, clear, absence, note } = dom;
  const box = R.b.timeline; clear(box);
  const abs = absence(R.stamps.switch && R.stamps.switch.ok === false ? { ok: true, meta: { ok: false, error: R.stamps.switch.error, next_try_s: R.stamps.switch.next_try_s, stale: !!R.switch } } : R.res.switch, "switch engine status");
  if (abs) box.append(abs);
  const s = R.switch;
  if (s) {
    const k = (t) => el("span.k", { text: t }), b = (t, cls = "") => el("b" + (cls ? "." + cls : ""), { text: t === null || t === undefined ? "not reported" : String(t) });
    const busy = s.done !== true && s.idle !== true && (s.phase || s.startedAt);
    box.append(el("div.line", [k("target"), b(s.target), k("phase"), b(s.phase, busy ? "ink-cyan" : ""), k("started"), b(s.startedAt ? format.utc(s.startedAt) : null), k("done"), b(s.done === undefined ? null : String(s.done)), k("ok"), b(s.ok === undefined || s.ok === null ? (s.done ? "not reported" : "pending") : String(s.ok), s.ok === false ? "ink-fault" : "")]));
    const log = Array.isArray(s.log) ? s.log : Array.isArray(s.log_tail) ? s.log_tail : [];
    const tail = new Set(Array.isArray(s.log_tail) ? s.log_tail : log.slice(-3));
    const pre = el("pre.log", { "aria-label": "switch engine log" });
    log.forEach((line, i) => { pre.append(el("span" + (tail.has(line) ? ".tail" : ""), { text: String(line) })); if (i < log.length - 1) pre.append("\n"); });
    if (!log.length) pre.append("no log lines reported");
    box.append(el("div.cap", { text: `engine log · ${log.length} lines${Array.isArray(s.log) ? "" : " (tail only)"}` }), pre);
  } else if (!abs) box.append(note("switch engine status not loaded", "no reply yet"));
  const rows = [
    ...R.session.map((e) => ({ t: e.t, text: `${e.event}: ${e.detail}`, cls: e.cls })),
    ...R.outcomes.map((o) => ({ t: o.observed_utc || o.received_utc, text: `outcome for ${o.to}: ok ${o.ok === null ? "null" : String(o.ok)} · phase ${o.phase || "not reported"}${o.reason ? " · " + o.reason : ""}${Array.isArray(o.log_tail) && o.log_tail.length ? " · " + o.log_tail.join(" | ") : ""}`, cls: o.ok === false ? "fault" : o.ok === true ? "" : "act" })),
  ].sort((a, b2) => String(a.t).localeCompare(String(b2.t)));
  box.append(el("div.cap", { text: `this session · ${rows.length} events` }));
  const tl = el("div.timeline");
  if (!rows.length) tl.append(el("div.prose.dim", { text: "no switch prepared, confirmed or refused in this session; outcomes recorded by the backend arrive on the stream as switch.outcome" }));
  for (const r of rows) tl.append(el("div.row", [el("span.t", { text: format.utc(r.t) }), el("span" + (r.cls ? ".ink-" + (r.cls === "live" ? "cyan" : r.cls === "act" ? "amber" : r.cls) : ""), { text: r.text })]));
  box.append(tl);
}

function renderDecisions() {
  const { dom } = R.ctx; const { el, clear, absence, note, table } = dom;
  const box = R.b.decisions; clear(box);
  const r = R.res.decisions;
  const abs = absence(r, "decision log");
  if (abs) box.append(abs);
  const d = R.decisions;
  if (!d) { if (!abs) box.append(note("decision log not loaded", "no reply yet")); return; }
  const rows = Array.isArray(d.decisions) ? d.decisions : [];
  box.append(el("div.line", [el("span.k", { text: "rows" }), el("b", { text: String(rows.length) }), el("span.k", { text: "schema" }), el("b", { text: d.schema || "not interpreted" })]));
  box.append(table([{ th: "row as received", render: (row) => el("code.mono", { text: typeof row === "string" ? row : JSON.stringify(row) }), cls: "body" }], rows, { empty: "no decisions in the log", caption: "GRID CONTROL decision log, raw rows" }));
}

function renderDoctor() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, button } = dom;
  const box = R.b.doctor; clear(box);
  const r = R.res.doctor; const d = R.doctor;
  const abs = absence(r, "doctor");
  if (abs) box.append(abs);
  if (d && d.text) {
    box.append(el("div.line", [el("span.k", { text: "exit" }), el("b" + (d.exit ? ".ink-fault" : ""), { text: String(d.exit) }), el("span.k", { text: "ran" }), el("b", { text: format.utc(d.ran_at) })]));
    box.append(el("pre.log.verbatim", { "aria-label": "aces doctor output", text: d.text }));
  } else if (d) box.append(note("doctor", d.note || "doctor has not been run in this session"));
  else if (!abs) box.append(note("doctor not loaded", "no reply yet"));
  box.append(el("div.actrow", [button("Run doctor", { disabled: R.doctorBusy, title: "runs aces doctor on this desktop (local checks and the five rig reachability probes, 90 s cap); shown verbatim", onclick: runDoctor }), R.doctorResult ? el("span.result" + (R.doctorResult.cls ? "." + R.doctorResult.cls : ""), { text: R.doctorResult.text }) : null]));
}

async function runDoctor() {
  if (!R || R.doctorBusy) return;
  R.doctorBusy = true; R.doctorResult = { cls: "live", text: "running…" }; renderDoctor();
  const before = R.doctor && R.doctor.ran_at ? R.doctor.ran_at : null;
  const r = await R.ctx.api.post("/api/doctor/run", {});
  if (!R || R.dead) return;
  if (r.ok && r.data && r.data.text) { R.doctor = r.data; R.res.doctor = r; R.doctorBusy = false; R.doctorResult = null; renderDoctor(); return; }
  if (!r.ok) { R.doctorBusy = false; R.doctorResult = { cls: r.status === 409 ? "act" : r.status === 503 ? "act" : "fault", text: `${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})` }; renderDoctor(); return; }
  let tries = 0;
  if (R.doctorPoll) clearInterval(R.doctorPoll);
  R.doctorPoll = setInterval(async () => {
    tries++;
    await refetch("doctor");
    if (!R || R.dead) return;
    const ran = R.doctor && R.doctor.ran_at ? R.doctor.ran_at : null;
    if (ran && ran !== before && R.doctor.text) { clearInterval(R.doctorPoll); R.doctorPoll = null; R.doctorBusy = false; R.doctorResult = null; }
    else if (tries >= 50) { clearInterval(R.doctorPoll); R.doctorPoll = null; R.doctorBusy = false; R.doctorResult = { cls: "act", text: "doctor did not report within 100 s" }; }
    renderDoctor();
  }, 2000);
}

function renderPolicy() {
  const { dom } = R.ctx; const { el, clear, absence, note, kv } = dom;
  const box = R.b.policy; clear(box);
  const r = R.res.policy; const d = R.policy;
  const abs = absence(r, "policy");
  if (abs) box.append(abs);
  if (!d) { if (!abs) box.append(note("policy not loaded", "no reply yet")); return; }
  const p = d.policy || {};
  box.append(kv([["mode", p.mode, "act"], ["default project class", p.default_project_class], ["classes", Array.isArray(p.classes) ? p.classes.join(", ") : "not recorded"], ["overrides", String((p.overrides || []).length)], ["override file", d.override ? (d.override.present ? `present: ${d.override.path}` : `absent (${d.override.path || "no path"})`) : "not reported"]]));
  if (p.end_use_gate) box.append(el("div.prose", [el("span.cap", { text: "end-use gate " }), el("span", { text: p.end_use_gate })]));
  if (p.distribution_policy) box.append(el("div.prose", [el("span.cap", { text: "distribution policy " }), el("span", { text: p.distribution_policy })]));
}

function openButton(id, openable, noteText) {
  const { dom } = R.ctx; const { el, button } = dom;
  const res = R.openResult[id];
  return el("span.actrow", [
    button("Open", { disabled: !openable, title: openable ? `asks the desktop to open ${id} (ledgered as console.system.open)` : noteText || "not openable from this desktop", onclick: () => openId(id) }),
    res ? el("span.result" + (res.cls ? "." + res.cls : ""), { text: res.text }) : null,
  ]);
}
async function openId(id) {
  const r = await R.ctx.api.post("/api/system/open", { id });
  if (!R || R.dead) return;
  R.openResult[id] = r.ok ? { cls: "", text: `opened (${r.data && r.data.via ? r.data.via : "HTTP " + r.status})` } : { cls: r.status === 503 ? "act" : "fault", text: `${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})` };
  renderLinks(); renderEntries();
}

function renderLinks() {
  const { dom } = R.ctx; const { el, clear, absence, note, table } = dom;
  const box = R.b.links; clear(box);
  const r = R.res.links; const d = R.links;
  const abs = absence(r, "links");
  if (abs) box.append(abs);
  if (!d) { if (!abs) box.append(note("links not loaded", "no reply yet")); return; }
  const rows = Array.isArray(d.links) ? d.links : Array.isArray(d) ? d : [];
  box.append(table([
    { th: "link", key: "label", cls: "nm" },
    { th: "url", render: (l) => [l.url || "not reported", l.note ? el("span.sub", { text: l.note }) : null] },
    { th: "reach", render: (l) => l.reach || "not classified" },
    { th: "open", render: (l) => (l.replaced_by ? el("span.dim", { text: `in this console: ${l.replaced_by}` }) : openButton(l.id, !!l.openable, l.note)) },
  ], rows, { empty: "no links configured", caption: "configured and reported links", cls: "wrapnames" }));
}

function renderPackages() {
  const { dom, format } = R.ctx; const { el, clear, absence, note, table } = dom;
  const box = R.b.packages; clear(box);
  const r = R.res.packages; const d = R.packages;
  const abs = absence(r, "packages");
  if (abs) box.append(abs);
  if (!d) { if (!abs) box.append(note("packages not loaded", "no reply yet")); return; }
  const rows = Array.isArray(d.packages) ? d.packages : [];
  if (d.note) box.append(el("div.prose.dim", { text: d.note }));
  box.append(el("div.line", [el("span.k", { text: "queried" }), el("b", { text: d.queried_utc ? format.utc(d.queried_utc) : "not reported" })]));
  if (rows.length || !d.note) box.append(table([{ th: "package", key: "name", cls: "nm" }, { th: "version", key: "version" }], rows, { empty: "no aces-* or omarchy* packages reported", caption: "installed packages" }));
}

function renderEntries() {
  const { dom } = R.ctx; const { el, clear, absence, note, table } = dom;
  const box = R.b.entries; clear(box);
  const r = R.res.entries; const d = R.entries;
  const abs = absence(r, "desktop entries");
  if (abs) box.append(abs);
  if (!d) { if (!abs) box.append(note("desktop entries not loaded", "no reply yet")); return; }
  const rows = Array.isArray(d.entries) ? d.entries : [];
  if (d.note) box.append(el("div.prose.dim", { text: d.note }));
  if (rows.length || !d.note) box.append(table([
    { th: "entry", key: "name", cls: "nm" }, { th: "id", key: "id" }, { th: "exec", key: "exec" }, { th: "owner", key: "owner_pkg" },
    { th: "open", render: (e) => openButton(e.id, true, null) },
  ], rows, { empty: "no desktop entries owned by aces-* packages", caption: "desktop entries" }));
}

function renderStop() {
  const { dom } = R.ctx; const { el, clear, button } = dom;
  const box = R.b.stop; clear(box);
  const input = el("input.type.short", { type: "text", autocomplete: "off", spellcheck: "false", placeholder: "type stop", "aria-label": "type stop to enable the Stop console button" });
  const btn = button("Stop console", { disabled: true, title: "stops the console backend (ledgered as console.server.stop); the page then shows the stream as stopped", onclick: async () => {
    btn.disabled = true;
    const r = await R.ctx.api.post("/api/system/stop", { confirm: input.value });
    if (!R || R.dead) return;
    R.stopResult = r.ok ? { cls: "", text: "stop accepted; the backend is exiting" } : { cls: r.status === 400 ? "act" : "fault", text: `${r.error} (HTTP ${r.status}${r.code ? ", " + r.code : ""})` };
    renderStop();
  } });
  input.addEventListener("input", () => { btn.disabled = input.value !== "stop"; });
  box.append(el("div.prose", { text: "Stops this console's backend on this desktop. The rig is untouched; the page keeps what it has and shows the stream as stopped. Type stop to enable the button." }));
  box.append(el("div.actrow", [input, btn, R.stopResult ? el("span.result" + (R.stopResult.cls ? "." + R.stopResult.cls : ""), { text: R.stopResult.text }) : null]));
}

function renderInspector() {
  const { dom } = R.ctx; const { block, kv, el } = dom;
  const modes = R.modes && R.modes.gridcontrol_modes ? R.modes.gridcontrol_modes : null;
  const m = modes && R.target ? modes[R.target] : null;
  if (m) {
    const sim = R.modes && R.modes.sim_modes ? R.modes.sim_modes[R.target] : null;
    const listText = (a) => (Array.isArray(a) ? (a.length ? a.join(", ") : "none") : "not recorded");
    R.ctx.inspector.show([
      block("mode as GRID CONTROL reports it", kv([["id", R.target, "act"], ["label", m.label], ["switchable", String(m.switchable)], ["switchable reported", m.switchable_reported === false ? "no" : "yes"], ["pending", m.pending || "none"]])),
      block("sim mode table", sim ? kv([["sim gpus", listText(sim.sim_gpus)], ["llm gpus", listText(sim.llm_gpus)], ["rest", listText(sim.rest)], ["keep services", listText(sim.keep_services)], ["stop services", listText(sim.stop_services)], ["label", sim.label]]) : el("div.prose.dim", { text: "no entry for this mode in the sim mode table" })),
    ], { name: R.target, coord: m.label || "" });
    return;
  }
  const c = R.config;
  if (!c) { R.ctx.inspector.show(block("configuration", el("div.prose.dim", { text: R.res.config && R.res.config.error ? R.res.config.error : "/api/config not loaded" })), { name: "ops", coord: "configuration", ringed: false }); return; }
  const srv = c.server || {}, con = c.console || {}, tok = c.tokens || {}, paths = c.paths || {}, plat = c.platform || {};
  const tokRow = (name, t) => [name, t ? `${t.configured ? "configured" : "not configured"} (${t.source || "source not reported"})` : "not reported", t && t.configured ? "" : "dim"];
  R.ctx.inspector.show([
    block("server endpoints (configured)", kv([["name", srv.name], ["host", srv.host], ["grid_control", srv.grid_control], ["rig_state", srv.rig_state], ["simlab_mcp", srv.simlab_mcp], ["simlab_console", srv.simlab_console], ["mlflow", srv.mlflow], ["webodm", srv.webodm], ["timeout_s", srv.timeout_s === undefined ? null : String(srv.timeout_s)]])),
    block("console", kv([["bind", con.bind], ["port", con.port === undefined ? null : String(con.port)], ["origin", con.origin], ["state dir", con.state_dir], ["static dir", con.static_dir]])),
    block("tokens (held by the backend; values never leave it)", kv([tokRow("GRID CONTROL", tok.grid_control), tokRow("simlab", tok.simlab)])),
    block("paths", kv([["work", paths.work], ["state", paths.state], ["user config", paths.user_config], ["data dir", paths.data_dir]])),
    block("platform", kv([["os", plat.os], ["python", plat.python]])),
  ], { name: "ops", coord: `${srv.name || "server"} ${srv.host || ""} · console ${con.bind || ""}${con.port ? ":" + con.port : ""}`, ringed: false });
}
