// The aces console shell: one page, seven plates, one event stream. Every value on screen comes from the
// console's own backend (/api/*) or a file it serves; absence is drawn as absence with its reason. The shell
// owns routing (?plate=), the keyboard tabs, the stamp strip, the foot, the inspector column and the stream; each
// plate module exports mount(root, ctx) and unmount() (ui-contracts.md section 4).
import { api } from "./lib/api.js";
import { vocab } from "./lib/vocab.js";
import { format } from "./lib/format.js";
import { dom, el, clear } from "./lib/dom.js";

const PLATES = ["fabric", "projects", "pipeline", "solvers", "objects", "evidence", "agents", "ops"];
const NAMES = { fabric: "Fabric", projects: "Projects", pipeline: "Pipeline", solvers: "Solvers", objects: "Objects", evidence: "Evidence", agents: "Agents", ops: "Ops" };
const STRIP_TOPICS = new Set(["rig.status", "rig.switch", "fabric.manifest", "console.health"]);
const $ = (id) => document.getElementById(id);

const S = {
  plate: null, mod: null, mounting: null, subs: [],
  last: {},                     // topic -> the last stamp received (stream or poll)
  strip: { sse: "connecting", fixture: null, stopped: false, pollReason: null },
  health: null, healthAt: null, events: null, vocabError: null,
};

// ---------- stamp bus: plates subscribe through ctx.events.on; the shell keeps the last stamp per topic ----------
const bus = new Map();
function on(topic, fn) {
  if (!bus.has(topic)) bus.set(topic, new Set());
  bus.get(topic).add(fn);
  return () => { const s = bus.get(topic); if (s) s.delete(fn); };
}
function emit(stamp) {
  for (const t of [stamp.topic, "*"]) { const s = bus.get(t); if (s) for (const fn of [...s]) { try { fn(stamp); } catch (e) { console.error(`stamp handler for ${t} failed`, e); } } }
}
const last = (topic) => S.last[topic] || null;

// ---------- the strip: mode as GRID CONTROL reports it, manifest age, stream state, FIXTURE ----------
function switchInfo(status, sw) {
  if (sw && sw.target && sw.done !== true && sw.idle !== true && (sw.phase || sw.startedAt)) return { active: true, target: sw.target, phase: sw.phase || "" };
  if (status && status.switching) {
    const t = typeof status.switching === "string" ? status.switching : (status.switching.target || JSON.stringify(status.switching));
    return { active: true, target: t, phase: typeof status.switching === "object" ? status.switching.phase || "" : "" };
  }
  if (sw && sw.done && sw.ok === false) return { active: false, failed: true, target: sw.target, phase: sw.phase || "" };
  return { active: false, failed: false };
}

function renderStrip() {
  const st = last("rig.status"), sw = last("rig.switch"), mf = last("fabric.manifest");
  const status = st && st.ok !== false ? st.payload : (st ? st.payload : null);
  const stamp = $("stamp");
  // each part names its kind (mode, phase, detected, failed, manifest, stream, fixture) so the phone strip can keep the
  // three that matter there; a part never breaks inside itself
  const parts = [];
  let cls = "stamp";
  const sweep = switchInfo(status, sw && sw.payload);
  if (status && status.mode) {
    if (sweep.active) { parts.push([`${status.mode} → ${sweep.target}`, "p-mode"]); if (sweep.phase) parts.push([String(sweep.phase), "p-phase"]); cls += " switching"; }
    else parts.push([`mode ${status.mode}`, "p-mode"]);
    if (status.detected_mode && status.detected_mode !== status.mode) parts.push([`detected ${status.detected_mode}`, "p-detected" + (status.detected_mode === "transition" ? " stale" : "")]);
    if (!sweep.active && sweep.failed) parts.push([`switch failed: ${sweep.target || "target not reported"}`, "p-failed fault"]);
  } else if (st && (st.ok === false || st.error)) { parts.push(["mode unreachable", "p-mode"]); cls += " unknown"; }
  else { parts.push(["mode unknown", "p-mode"]); cls += " unknown"; }
  if (mf && mf.payload) {
    const p = mf.payload;
    if (p.age_s === null || p.age_s === undefined) parts.push(["no manifest", "p-manifest stale"]);
    else parts.push([`manifest ${format.span(p.age_s)}${p.stale ? " stale" : ""}`, "p-manifest" + (p.stale ? " stale" : "")]);
    if (p.stale || p.age_s === null || p.age_s === undefined) cls += " stale";
  } else if (mf && mf.ok === false) parts.push(["manifest unavailable", "p-manifest stale"]);
  const sse = S.strip.stopped ? "stopped" : S.strip.sse;
  parts.push([sse, "p-stream" + (sse === "live" ? "" : " stale")]);
  if (S.strip.fixture) { parts.push([S.strip.fixture, "p-fixture fixture"]); cls += " fixture"; }
  clear(stamp);
  parts.forEach(([t, c], i) => {
    if (i) stamp.append(el("span.sep", { text: " · " }));
    stamp.append(el("span", { class: "part " + c, text: t }));
  });
  stamp.className = cls;
  const titleBits = [status && status.mode ? `GRID CONTROL reports mode ${status.mode}` : "GRID CONTROL mode not available"];
  if (status && status.detected_mode) titleBits.push(`detected_mode ${status.detected_mode}`);
  if (mf && mf.payload && mf.payload.path) titleBits.push(`fabric manifest ${mf.payload.path}`);
  titleBits.push(`event stream ${sse}${S.strip.pollReason && sse === "polling" ? ` (${S.strip.pollReason}; polling the API at its TTLs)` : ""}`);
  if (S.strip.fixture) titleBits.push("replaying captured fixtures; nothing here is live");
  stamp.title = titleBits.join(" · ");
}

function renderFreshness() {
  const f = $("freshness");
  const st = last("rig.status");
  const bits = [];
  if (S.vocabError) bits.push(S.vocabError);
  if (st) {
    const a = format.age(st.fetched_utc);
    bits.push(`rig status ${a === null ? "age not reported" : format.ago(a)}`);
    if ((st.ok === false || st.error) && st.error) bits.push(`${st.error}${st.next_try_s !== null && st.next_try_s !== undefined ? ` · retrying in ${format.num(st.next_try_s)} s` : ""}`);
  } else if (S.strip.sse === "connecting") bits.push("waiting for the first stamp");
  f.textContent = bits.join(" · ");
  f.classList.toggle("act", !!(st && (st.ok === false || st.error)) || !!S.vocabError || (S.strip.sse !== "live" && S.strip.sse !== "connecting"));
}

function renderFoot() {
  const h = S.health;
  const items = [
    `aces-console ${h && h.version ? h.version : format.DASH}`,
    `aces-base ${h && h.aces_base_version ? h.aces_base_version : format.DASH}`,
    h && h.bind ? `bind ${h.bind}:${h.port}` : `bind ${format.DASH}`,
    `health ${S.healthAt ? format.ago(Date.now() / 1000 - S.healthAt) : "not reported"}`,
  ];
  const foot = $("footFacts");
  clear(foot); foot.append(dom.facts(items));
  foot.title = h && !h.aces_base_version ? "aces-base version: not reported by /api/health" : "";
}

// ---------- inspector ----------
const inspector = {
  show(node, head = null) {
    if (head) {
      const eye = $("eye"); clear(eye);
      eye.append(el("div.eye-name" + (head.ringed === false ? "" : ".ringed"), { text: head.name || "" }), el("div.eye-coord.mono", { text: head.coord || "" }));
    }
    const body = $("columnBody"); clear(body); dom.append(body, node);
  },
  clear() {
    const eye = $("eye"); clear(eye);
    eye.append(el("div.eye-name", { text: "nothing selected" }), el("div.eye-coord.mono", { text: "select a GPU, a project, a mode or a record" }));
    clear($("columnBody"));
  },
};

// ---------- routing ----------
function plateFromURL() {
  const p = new URLSearchParams(location.search).get("plate");
  return PLATES.includes(p) ? p : "fabric";
}

function selectTab(id) {
  for (const p of PLATES) {
    const btn = $("tab-" + p), sec = $("plate-" + p);
    const on_ = p === id;
    btn.setAttribute("aria-selected", on_ ? "true" : "false");
    btn.tabIndex = on_ ? 0 : -1;
    sec.hidden = !on_;
  }
  $("plateName").textContent = NAMES[id];
  document.title = "ACES · " + NAMES[id];
}

function stripSnapshot() {
  const st = last("rig.status");
  return {
    status: st, switch: last("rig.switch"), manifest: last("fabric.manifest"), health: S.health,
    sse: S.strip.stopped ? "stopped" : S.strip.sse, fixture: S.strip.fixture,
    statusAgeS: st ? format.age(st.fetched_utc) : null,
  };
}

function makeCtx(id, root) {
  return {
    api, vocab, format, dom, plate: id,
    params: new URLSearchParams(location.search),
    inspector,
    stamp: { get: stripSnapshot },
    events: { on(topic, fn) { const off = on(topic, fn); S.subs.push(off); return off; }, last },
    navigate: (plate, params = null) => showPlate(plate, { params }),
    coord(text) { const c = $("coord-" + id); if (c) c.textContent = text || ""; },
    fixture: () => S.strip.fixture,
    root,
  };
}

async function unmountCurrent() {
  if (S.mod && typeof S.mod.unmount === "function") { try { await S.mod.unmount(); } catch (e) { console.error("unmount failed", e); } }
  S.mod = null;
  for (const off of S.subs) off();
  S.subs = [];
  inspector.clear();
  if (S.plate) { clear($("body-" + S.plate)); const c = $("coord-" + S.plate); if (c) c.textContent = ""; }
}

async function mountPlate(id) {
  S.plate = id;
  selectTab(id);
  const root = $("body-" + id);
  clear(root);
  const ctx = makeCtx(id, root);
  let mod = null;
  try { mod = await import(`./plates/${id}.js`); }
  catch (e) { root.append(dom.note("plate module failed to load", e.message, { fault: true, detail: `./plates/${id}.js` })); return; }
  S.mod = mod;
  try {
    if (typeof mod.mount !== "function") throw new Error("the module exports no mount(root, ctx)");
    await mod.mount(root, ctx);
  } catch (e) {
    console.error(`plate ${id} failed while mounting`, e);
    root.append(dom.note("plate failed while mounting", e.message, { fault: true, detail: `./plates/${id}.js` }));
  }
  if (S.events) S.events.setPlate(id);
}

async function showPlate(id, { push = true, params = null } = {}) {
  if (!PLATES.includes(id)) id = "fabric";
  if (push) {
    const u = new URL(location.href);
    u.search = "";
    u.searchParams.set("plate", id);
    if (params) for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") u.searchParams.set(k, String(v));
    history.pushState(null, "", u);
  }
  const run = async () => { await unmountCurrent(); await mountPlate(id); };
  S.mounting = S.mounting ? S.mounting.then(run, run) : run();
  const p = S.mounting;
  await p;
  if (S.mounting === p) S.mounting = null;
  document.body.dataset.ready = id;   // the harness waits for this: the plate named here has finished mounting
}

// ---------- keyboard tabs: arrows move and select, Home/End jump (spec 4.2) ----------
function wireTabs() {
  const list = $("plateList");
  list.querySelectorAll("[role=tab]").forEach((b) => b.addEventListener("click", () => showPlate(b.dataset.plate)));
  list.addEventListener("keydown", (e) => {
    const tabs = [...list.querySelectorAll("[role=tab]")];
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    let j = null;
    if (e.key === "ArrowDown" || e.key === "ArrowRight") j = (i + 1) % tabs.length;
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") j = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = tabs.length - 1;
    if (j === null) return;
    e.preventDefault();
    tabs[j].focus();
    showPlate(tabs[j].dataset.plate);
  });
  window.addEventListener("popstate", () => showPlate(plateFromURL(), { push: false }));
}

// ---------- stamps ----------
function onStamp(st) {
  if (!st || !st.topic) return;
  if (st.topic === "fixture") { S.strip.fixture = st.fixture || S.strip.fixture; renderStrip(); return; }
  if (st.topic === "bye") { S.strip.stopped = true; renderStrip(); renderFreshness(); emit(st); return; }
  if (st.topic === "polling") { S.strip.pollReason = st.payload && st.payload.reason ? st.payload.reason : null; renderStrip(); return; }
  st.received = Date.now() / 1000;
  if (st.fixture) S.strip.fixture = st.fixture;
  S.last[st.topic] = st;
  if (st.topic === "console.health" && st.payload) { S.health = st.payload; S.healthAt = Date.now() / 1000; renderFoot(); }
  if (STRIP_TOPICS.has(st.topic)) { renderStrip(); renderFreshness(); }
  emit(st);
}

function tick() {
  $("clock").textContent = new Date().toLocaleTimeString();
  renderFreshness();
  renderFoot();
}

// ---------- start ----------
(async function start() {
  const v = await vocab.load();
  S.vocabError = v.error;
  const h = await api.get("/api/health");
  if (h.ok && h.data) { S.health = h.data; S.healthAt = Date.now() / 1000; if (h.meta && h.meta.fixture) S.strip.fixture = h.meta.fixture; }
  renderFoot(); renderStrip(); renderFreshness();
  wireTabs();
  S.events = api.events(plateFromURL(), onStamp, { onState: (s) => { S.strip.sse = s; renderStrip(); renderFreshness(); } });
  await showPlate(plateFromURL(), { push: false });
  setInterval(tick, 1000);
  tick();
})();
