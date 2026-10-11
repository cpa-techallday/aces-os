// A job's live panel: the command it runs, its status, its output as it arrives (GET /api/job/{id}?since=N every
// 1.5 s), a Cancel button while it runs, and the tool's own JSON result when it ends. Used by the Evidence plate
// (golden runs) and the Objects plate (CAD actions). The panel stops polling by itself once it is off the page.
export function jobPanel(ctx, jobId, { onDone = null, title = null } = {}) {
  const { dom, format, api } = ctx; const { el, button } = dom;
  const head = el("div.line");
  const out = el("pre.pre", { "aria-live": "polite", text: "" });
  const tail = el("div");
  const wrap = el("div.jobpanel", [head, out, tail]);
  let since = 0, done = false, timer = null, ticked = false;
  const cancel = button("Cancel", { title: "SIGTERM to the job (ledgered as console.job.cancel)", onclick: async () => {
    cancel.disabled = true;
    const r = await api.post(`/api/job/${jobId}/cancel`, {});
    if (!r.ok) tail.append(el("div.prose.dim", { text: `cancel: ${r.error} (HTTP ${r.status})` }));
  } });
  const chipFor = (s) => el("span.state" + (s === "running" ? ".ink-cyan" : s === "failed" ? ".ink-fault" : s === "cancelled" ? ".ink-amber" : ".ink-chalk"), { text: s });
  async function tick() {
    if (!wrap.isConnected && ticked) { clearInterval(timer); return; }
    const r = await api.get(`/api/job/${jobId}?since=${since}`);
    if (!r.ok) { dom.clear(head); head.append(el("span.k", { text: "job" }), el("b", { text: `${jobId}: ${r.error}` })); clearInterval(timer); return; }
    const j = r.data;
    dom.clear(head);
    dom.append(head, [el("span.k", { text: "job" }), el("b", { text: title || j.title }), chipFor(j.status), el("span.k", { text: "elapsed" }), el("b", { text: format.dur(j.duration_s) }), j.status === "running" ? cancel : null]);
    for (const ln of j.line_items || []) { out.textContent += (out.textContent ? "\n" : "") + ln.text; since = Math.max(since, ln.n + 1); }
    ticked = true;
    out.scrollTop = out.scrollHeight;
    if (j.status !== "running" && !done) {
      done = true; clearInterval(timer);
      dom.clear(tail);
      if (j.error) tail.append(el("div.prose", { text: j.error }));
      if (j.result) tail.append(el("div.cap", { text: "result (the tool's JSON)" }), el("pre.pre", { text: JSON.stringify(j.result, null, 2) }));
      else if (j.status !== "done") tail.append(el("div.prose.dim", { text: `exit ${j.exit}: see the output above` }));
      if (onDone) onDone(j);
    }
  }
  timer = setInterval(() => { tick().catch(() => {}); }, 1500);
  tick().catch(() => {});
  return wrap;
}

// base64 of a File (for the JSON upload routes)
export function fileB64(file) {
  return new Promise((res, rej) => { const fr = new FileReader(); fr.onload = () => res(String(fr.result).split(",", 2)[1] || ""); fr.onerror = () => rej(fr.error); fr.readAsDataURL(file); });
}
