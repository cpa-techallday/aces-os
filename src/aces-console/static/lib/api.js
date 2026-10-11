// The only fetch layer of the aces console (contract 4): same-origin GET/POST against the console's own
// backend, and the one event stream with Last-Event-ID replay and a polling fallback at the TTLs of spec 3.2.
// No token ever passes through here: the backend holds them; the page never sees one.

const GET_TIMEOUT_MS = 15000;
const POST_TIMEOUT_MS = 320000;   // a verify may run 300 s

async function request(method, path, body) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), method === "GET" ? GET_TIMEOUT_MS : POST_TIMEOUT_MS);
  const headers = { Accept: "application/json" };
  const init = { method, headers, signal: ctl.signal, credentials: "same-origin", mode: "same-origin", cache: "no-store" };
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["X-ACES-Intent"] = "human";
    init.body = JSON.stringify(body === undefined ? {} : body);
  }
  let r;
  try {
    r = await fetch(path, init);
  } catch (e) {
    clearTimeout(t);
    return { ok: false, status: 0, code: "unreachable", error: e.name === "AbortError" ? "console did not answer in time" : `console unreachable: ${e.message}`, source: path, data: null, meta: null };
  }
  clearTimeout(t);
  let j = null;
  const text = await r.text();
  try { j = text ? JSON.parse(text) : null; } catch (e) { j = null; }
  if (r.ok) {
    if (j && typeof j === "object" && "ok" in j) return { status: r.status, ...j };
    return { ok: true, status: r.status, data: j, meta: { source: path, fetched_utc: null, age_s: null, ttl_s: null, stale: false, error: null, next_try_s: null, fixture: null, note: "reply carried no envelope" } };
  }
  const e = j && typeof j === "object" ? j : {};
  return { ok: false, status: r.status, code: e.code || `http-${r.status}`, error: e.error || (text ? text.slice(0, 200) : r.statusText || `HTTP ${r.status}`), source: e.source || path, upstream_status: e.status ?? null, next_try_s: e.next_try_s ?? null, data: e.data ?? null, meta: e.meta ?? null, body: e };
}

// api.get(path) -> {ok, data, meta} (an error reply or a network failure returns ok:false with error, code, status)
export function get(path) { return request("GET", path); }
// api.post(path, body) -> the same shape; adds X-ACES-Intent: human and Content-Type: application/json
export function post(path, body) { return request("POST", path, body); }

// ---------- the event stream ----------
// Topics of spec 3.2: the TTL is the polling period in the fallback; `plates` names the plates that need the topic
// ("*" = any client). Inline topics carry their payload in the stamp; the others are notices (the page refetches).
export const SOURCES = {
  "rig.status": { path: "/api/rig/status", ttl: 3, plates: "*", inline: true },
  "rig.switch": { path: "/api/rig/switch", ttl: 3, plates: "*", inline: true },
  "fabric.manifest": { path: "/api/fabric", ttl: 5, plates: "*", inline: true, project: (d) => (d ? { age_s: d.age_s ?? null, stale: !!d.stale, path: d.path ?? null } : null) },
  "console.health": { path: "/api/health", ttl: 5, plates: "*", inline: true },
  "ledger.aces": { path: "/api/ledger?n=1", ttl: 5, plates: ["evidence"], inline: false },
  "rig.modes": { path: "/api/rig/modes", ttl: 30, plates: ["fabric", "ops", "solvers"], inline: true },
  "rig.telemetry": { path: "/api/rig/telemetry", ttl: 3, plates: ["fabric"], inline: true, project: (d) => (d && d.history && d.history.length ? d.history[d.history.length - 1] : null) },
  "rig.snapshot": { path: "/api/rig/snapshot", ttl: 30, plates: ["fabric", "ops"], inline: false },
  "rig.decisions": { path: "/api/rig/decisions", ttl: 60, plates: ["fabric", "ops"], inline: false },
  "sim.capacity": { path: "/api/sim/capacity", ttl: 5, plates: ["fabric", "pipeline", "solvers"], inline: true },
  "sim.summary": { path: "/api/sim/summary", ttl: 20, plates: ["pipeline"], inline: false },
  "sim.experiments": { path: "/api/sim/experiments", ttl: 4, plates: ["pipeline"], inline: false },
  "sim.registry": { path: "/api/sim/registry", ttl: 60, plates: ["solvers"], inline: false },
  "sim.evidence": { path: "/api/sim/evidence", ttl: 30, plates: ["evidence"], inline: false },
  "sim.evidence.verify": { path: "/api/sim/evidence/verify", ttl: 30, plates: ["evidence"], inline: false },
  "sim.audit": { path: "/api/sim/audit", ttl: 30, plates: ["evidence"], inline: false },
  "sim.whoami": { path: "/api/sim/whoami", ttl: 300, plates: ["pipeline", "ops"], inline: false },
  "mlflow.experiments": { path: "/api/mlflow/experiments", ttl: 60, plates: ["pipeline"], inline: false },
  "projects": { path: "/api/projects", ttl: 10, plates: ["projects"], inline: false },
  "golden.runs": { path: "/api/golden/runs", ttl: 10, plates: ["evidence"], inline: false },
  "ledger.simlab": { path: "/api/ledger/simlab", ttl: 30, plates: ["evidence"], inline: false },
  "doctor": { path: "/api/doctor", ttl: 300, plates: ["ops"], inline: false },
  "system.packages": { path: "/api/system/packages", ttl: 300, plates: ["ops"], inline: false },
  "system.entries": { path: "/api/system/entries", ttl: 300, plates: ["ops"], inline: false },
  "rig.redteam": { path: "/api/rig/redteam", ttl: 30, plates: ["ops"], inline: false },
  "rig.downloads": { path: "/api/rig/downloads", ttl: 60, plates: ["ops"], inline: false },
  "mc.runs": { path: "/api/mc/runs", ttl: 10, plates: ["agents"], inline: false },
  "mc.missions": { path: "/api/mc/missions", ttl: 15, plates: ["agents"], inline: false },
  "mc.sessions": { path: "/api/mc/sessions", ttl: 30, plates: ["agents"], inline: false },
  "mc.agents": { path: "/api/mc/agents", ttl: 60, plates: ["agents"], inline: false },
};

export function topicsFor(plate) {
  return Object.entries(SOURCES).filter(([, s]) => s.plates === "*" || s.plates.includes(plate)).map(([k]) => k);
}

// a short, stable fingerprint of a JSON payload for `changed` in the polling fallback (not a security hash)
function etagOf(data) {
  const s = JSON.stringify(data === undefined ? null : data);
  let h1 = 0x811c9dc5, h2 = 0x1000193;
  for (let i = 0; i < s.length; i++) { h1 = Math.imul(h1 ^ s.charCodeAt(i), 16777619); h2 = Math.imul(h2 ^ s.charCodeAt(i), 2246822519); }
  return (h1 >>> 0).toString(16).padStart(8, "0") + (h2 >>> 0).toString(16).padStart(4, "0").slice(0, 4);
}

// api.events(plate, onStamp, {onState}) -> {close(), setPlate(id), state(), lastSeq(), fixture()}
// state: "connecting" | "live" | "reconnecting" | "polling" | "stopped" (the server said bye)
export function events(plate, onStamp, { onState = null } = {}) {
  let es = null, state = "connecting", currentPlate = plate, lastSeq = 0, fixture = null, failures = 0;
  let pollTimers = [], pollEtags = {}, pollSeq = 0, retrySse = null, closed = false, pollRetryDelay = 60000;
  const setState = (s) => { if (state !== s) { state = s; onState && onState(s); } };

  function openStream() {
    if (closed) return;
    stopPolling();
    const url = `/api/events?plate=${encodeURIComponent(currentPlate)}`;
    try { es = new EventSource(url); } catch (e) { startPolling("EventSource unavailable: " + e.message); return; }
    setState(failures ? "reconnecting" : "connecting");
    es.addEventListener("open", () => { failures = 0; setState("live"); });
    es.addEventListener("fixture", (ev) => { try { fixture = JSON.parse(ev.data).fixture || null; } catch (e) { fixture = null; } onStamp({ topic: "fixture", fixture, seq: lastSeq, payload: null, changed: true, ok: true }); });
    es.addEventListener("stamp", (ev) => {
      let st = null;
      try { st = JSON.parse(ev.data); } catch (e) { return; }
      if (ev.lastEventId) { const n = Number(ev.lastEventId); if (Number.isFinite(n)) lastSeq = n; }
      if (st && st.fixture) fixture = st.fixture;
      onStamp(st);
    });
    es.addEventListener("bye", () => { closed = true; stopPolling(); try { es.close(); } catch (e) { /* closed */ } setState("stopped"); onStamp({ topic: "bye", payload: null, ok: true, changed: true }); });
    es.addEventListener("error", () => {
      if (closed) return;
      if (es.readyState === EventSource.CLOSED) {
        // a non-200 reply (the 33rd stream gets 503, spec 3.2) closes the source for good: poll instead
        failures++; startPolling("stream refused by the server");
      } else {
        failures++; setState("reconnecting");
        if (failures >= 3) { try { es.close(); } catch (e) { /* closed */ } startPolling("stream failed three times"); }
      }
    });
  }

  function stopPolling() { for (const t of pollTimers) clearInterval(t); pollTimers = []; if (retrySse) { clearTimeout(retrySse); retrySse = null; } }

  function startPolling(reason) {
    if (closed) return;
    if (es) { try { es.close(); } catch (e) { /* closed */ } es = null; }
    stopPolling();
    setState("polling");
    onStamp({ topic: "polling", payload: { reason }, ok: true, changed: true });
    const topics = topicsFor(currentPlate);
    const poll = async (topic) => {
      const src = SOURCES[topic];
      const r = await get(src.path);
      if (closed || state !== "polling") return;
      const data = r.ok ? r.data : null;
      const etag = etagOf(r.ok ? data : (r.error || null));
      const changed = pollEtags[topic] !== etag; pollEtags[topic] = etag;
      const meta = r.meta || {};
      if (meta.fixture) fixture = meta.fixture;
      onStamp({
        topic, seq: ++pollSeq, fetched_utc: meta.fetched_utc ?? null, etag, polled: true,
        ok: r.ok && meta.ok !== false, error: r.ok ? (meta.error ?? null) : (r.error || null), next_try_s: r.ok ? (meta.next_try_s ?? null) : (r.next_try_s ?? null),
        changed, fixture: meta.fixture ?? null, payload: src.inline ? (src.project ? src.project(data) : data) : null,
      });
    };
    for (const topic of topics) { poll(topic); pollTimers.push(setInterval(() => poll(topic), SOURCES[topic].ttl * 1000)); }
    // try the stream again later; the polling keeps running until it opens
    retrySse = setTimeout(() => { if (!closed && state === "polling") { failures = 0; openStream(); } }, pollRetryDelay);
    pollRetryDelay = Math.min(pollRetryDelay * 2, 300000);
  }

  openStream();
  return {
    close() { closed = true; stopPolling(); if (es) { try { es.close(); } catch (e) { /* closed */ } es = null; } setState("stopped"); },
    setPlate(id) {
      if (id === currentPlate) return;
      currentPlate = id;
      if (state === "polling") { pollEtags = {}; startPolling("plate changed"); }
      else { if (es) { try { es.close(); } catch (e) { /* closed */ } es = null; } openStream(); }
    },
    state: () => state,
    lastSeq: () => lastSeq,
    fixture: () => fixture,
  };
}

export const api = { get, post, events, SOURCES, topicsFor };
export default api;
