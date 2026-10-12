// Thread plate: the conversation is the home screen of the aces console. Describe a part, a machine or a
// question; the agent on the rig (Mission Control, proxied with every token server-side) answers in the same
// thread. Above the thread, one line of plain-words rig truth (mode, GPUs, queue); below it, the composer.
// Absence is drawn with its reason ("Mission Control unreachable: ..."), never guessed.
// Bindings: /api/mc/sessions, /api/mc/session, /api/mc/session/{id}, /api/mc/session/{id}/message,
// /api/mc/upload; stamps rig.status, sim.capacity, mc.sessions. Deep link: ?plate=thread&session=<id>.

const PATHS = { sessions: "/api/mc/sessions" };
let R = null;

export async function mount(root, ctx) {
  const { el } = ctx.dom;
  R = {
    ctx, root, dead: false, poll: null,
    session: storedSession(), sessionDetail: null, sessionRes: null,
    files: [], state: "", sending: false,
    rigStatus: null, capacity: null,
    b: {
      truth: el("div.truth", { role: "status" }),
      log: el("div.t-log", { role: "log", "aria-live": "polite", "aria-label": "conversation" }),
      composer: null,
    },
  };
  const ta = el("textarea", {
    rows: "3", "aria-label": "describe a part, a machine, or a change",
    placeholder: "Describe a part, a machine, or a change — e.g. “a bracket that holds 15 kg and survives 8 g shock, as light as possible”  (Ctrl+Enter sends)",
  });
  const send = ctx.dom.button("Send", { cls: "btn primary t-send", onclick: () => sendMessage(ta) });
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send.click(); });
  const file = el("input", { type: "file", multiple: true, hidden: true, onchange: (e) => attach(e.target.files) });
  R.b.composer = el("div.t-composer", [
    ta,
    el("div.t-composer-row", [
      ctx.dom.button("Attach", { cls: "btn", title: "attach reference files to the next message", onclick: () => file.click() }),
      file,
      el("span.t-files", {}, R.files.map((u) => el("span.chip", { text: u.filename }))),
      el("span.t-state", { text: R.state }),
      send,
    ]),
  ]);
  R.b.ta = ta; R.b.fileInput = file;

  root.append(
    el("div.t-wrap", [
      el("div.t-head", [
        el("div.t-title", { text: "What are we building?" }),
        R.b.truth,
      ]),
      R.b.log,
      R.b.composer,
    ]),
  );

  ctx.events.on("rig.status", (st) => { R.rigStatus = st; renderTruth(); });
  ctx.events.on("sim.capacity", (st) => { R.capacity = st; renderTruth(); });
  ctx.events.on("mc.sessions", (st) => { if (st.changed) refetchSessions(); });

  renderTruth();
  renderLog();
  await ensureSession();
}

export function unmount() {
  if (!R) return;
  R.dead = true;
  if (R.poll) clearInterval(R.poll);
  R = null;
}

// ---------- the plain-words rig truth line ----------
function renderTruth() {
  const { el, clear } = R.ctx.dom;
  const box = R.b.truth; clear(box);
  const bits = [];
  const st = R.rigStatus;
  if (st && st.ok !== false && st.payload && st.payload.mode) {
    bits.push([`rig is in ${String(st.payload.mode).toUpperCase()} mode`, st.payload.switching ? "warn" : "ok"]);
  } else if (st && st.ok === false) bits.push([`rig status unreachable${st.error ? ": " + st.error : ""}`, "bad"]);
  else bits.push(["rig status not reported yet", "dim"]);
  const cap = R.capacity && R.capacity.ok !== false ? R.capacity.payload : null;
  if (cap) {
    const gpus = cap.gpus || cap.cards || [];
    const free = Array.isArray(gpus) ? gpus.filter((g) => !g.lease && !g.job).length : null;
    if (free !== null) bits.push([`${free} of ${gpus.length} GPUs free`, free > 0 ? "ok" : "warn"]);
    if (cap.queued !== undefined) bits.push([`${cap.queued} job${cap.queued === 1 ? "" : "s"} queued`, cap.queued > 0 ? "warn" : "dim"]);
  }
  for (const [text, tone] of bits) box.append(el("span.t-bit." + tone, { text }));
}

// ---------- the conversation ----------
function storedSession() {
  try { const v = localStorage.getItem("aces.thread.session"); return v ? Number(v) : null; } catch { return null; }
}
function storeSession(id) {
  try { if (id === null) localStorage.removeItem("aces.thread.session"); else localStorage.setItem("aces.thread.session", String(id)); } catch { }
}

async function ensureSession() {
  if (R.session !== null) { await loadSession(R.session); return; }
  // reuse the newest existing session when there is one; create one only on the first send
  const r = await R.ctx.api.get(PATHS.sessions);
  if (!R || R.dead) return;
  if (r.ok && r.data && Array.isArray(r.data.sessions) && r.data.sessions.length) {
    const list = r.data.sessions.slice().sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
    R.session = list[0].id; storeSession(R.session);
    await loadSession(R.session);
  } else if (!r.ok) {
    R.sessionRes = r;
    renderLog();
  }
}

async function refetchSessions() { if (R && R.session !== null) await loadSession(R.session); }

async function loadSession(id) {
  const r = await R.ctx.api.get(`/api/mc/session/${id}`);
  if (!R || R.dead || R.session !== id) return;
  R.sessionRes = r;
  R.sessionDetail = r.ok ? r.data : null;
  renderLog();
}

function msgText(c) {
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.map(msgText).join("\n");
  if (c && typeof c === "object") return c.text || c.content || c.output || c.message || JSON.stringify(c, null, 1);
  return String(c ?? "");
}

function renderLog() {
  const { el, clear } = R.ctx.dom;
  const box = R.b.log; clear(box);
  const d = R.sessionDetail;
  if (R.session === null && (!R.sessionRes || R.sessionRes.ok)) {
    box.append(el("div.t-empty", [
      el("div.t-empty-title", { text: "Start with one sentence." }),
      el("div.t-empty-body", { text: "Say what the thing is and what it must do — load it carries, heat it sheds, speed it sees, space it fits. A plan appears here before anything runs; nothing burns compute until you approve it." }),
    ]));
    return;
  }
  if (R.sessionRes && !R.sessionRes.ok) {
    box.append(R.ctx.dom.note("the agent could not be reached", R.sessionRes.error || `HTTP ${R.sessionRes.status}`, { fault: true, detail: R.sessionRes.source }));
    return;
  }
  const msgs = d && Array.isArray(d.messages) ? d.messages : [];
  if (!msgs.length) {
    box.append(el("div.t-empty", [el("div.t-empty-title", { text: "Empty conversation." }), el("div.t-empty-body", { text: "Type below to begin." })]));
    return;
  }
  for (const m of msgs) {
    const mine = m.role === "user";
    box.append(el("div.t-msg" + (mine ? ".user" : ".agent"), [
      el("div.t-who", { text: mine ? "you" : "aces" }),
      el("div.t-body", { text: msgText(m.content) }),
    ]));
  }
  if (R.sending) box.append(el("div.t-msg.agent", [el("div.t-who", { text: "aces" }), el("div.t-body.t-thinking", { text: "working…" })]));
  setTimeout(() => { box.scrollTop = box.scrollHeight; }, 0);
}

async function attach(files) {
  for (const f of files) {
    if (f.size > 16 * 1024 * 1024) { setState(`${f.name}: over the 16 MiB upload cap`); continue; }
    const b64 = await new Promise((res, rej) => { const fr = new FileReader(); fr.onload = () => res(String(fr.result).split(",", 2)[1] || ""); fr.onerror = () => rej(fr.error); fr.readAsDataURL(f); });
    const r = await R.ctx.api.post("/api/mc/upload", { filename: f.name, mime: f.type || "application/octet-stream", data_b64: b64 });
    if (!R || R.dead) return;
    if (r.ok && r.data && r.data.result && r.data.result.id !== undefined) R.files.push({ id: r.data.result.id, filename: r.data.result.filename || f.name });
    else setState(`upload ${f.name} failed: ${r.error || "HTTP " + r.status}`);
  }
  refreshComposer();
}

function refreshComposer() {
  const { el, clear } = R.ctx.dom;
  const holder = R.b.composer.querySelector(".t-files");
  clear(holder);
  for (const u of R.files) holder.append(el("span.chip", { text: u.filename }));
  const st = R.b.composer.querySelector(".t-state");
  st.textContent = R.state;
}

function setState(t) { if (!R) return; R.state = t; refreshComposer(); }

async function sendMessage(ta) {
  const text = ta.value;
  if (!text || !text.trim() || R.sending) return;
  R.sending = true;
  try {
    if (R.session === null) {
      const r = await R.ctx.api.post("/api/mc/session", { title: "thread " + new Date().toISOString().slice(0, 16).replace("T", " ") });
      if (!R || R.dead) return;
      const id = r.ok && r.data && r.data.result && (r.data.result.id || r.data.result.sessionId);
      if (!id) { setState(`could not start a conversation: ${r.error || "HTTP " + r.status}`); return; }
      R.session = id; storeSession(id);
    }
    const sid = R.session;
    // draw my message immediately; the server's copy arrives on the next load
    const d = R.sessionDetail;
    if (d && Array.isArray(d.messages)) d.messages.push({ role: "user", content: text });
    else R.sessionDetail = { messages: [{ role: "user", content: text }] };
    ta.value = "";
    renderLog();
    const r = await R.ctx.api.post(`/api/mc/session/${sid}/message`, { content: text, uploads: R.files.map((u) => u.id) });
    if (!R || R.dead) return;
    if (!r.ok) { setState(`send failed: ${r.error || "HTTP " + r.status}`); return; }
    R.files = [];
    setState("");
    // poll for the agent's reply (mission control is async)
    const t0 = Date.now();
    if (R.poll) clearInterval(R.poll);
    R.poll = setInterval(async () => {
      if (!R || R.dead || R.session !== sid) { if (R && R.poll) { clearInterval(R.poll); R.poll = null; } return; }
      await loadSession(sid);
      const msgs = R && R.sessionDetail && Array.isArray(R.sessionDetail.messages) ? R.sessionDetail.messages : [];
      const last = msgs[msgs.length - 1];
      if (last && last.role !== "user") { clearInterval(R.poll); R.poll = null; R.sending = false; renderLog(); }
      else if (Date.now() - t0 > 180000) { clearInterval(R.poll); R.poll = null; R.sending = false; setState("no reply within 3 min — the agent may still be working; see the Agents panel"); renderLog(); }
    }, 3000);
  } finally {
    if (R && !R.poll) R.sending = false;
  }
}
