"""GRID CONTROL and rig-state proxies, the mode switch and the fabric manifest (build spec 3.4 and 3.5).

Reads (all GET, cached by the Hub, the envelope carries staleness and the error line):

    /api/rig/status      GC /api/status with the queue normalisation (-1 -> null + "GRID CONTROL reported -1")
    /api/rig/modes       GC /api/modes (switchable_reported) + the SIMLAB console's capacity.modes (the sim pools)
    /api/rig/telemetry   GC /api/telemetry + a 240-sample ring (>= 2.5 s apart) seeded once from the console's history
    /api/rig/snapshot    rig-state tools/call rig_snapshot + classification of every service and endpoint against the
                         current mode's stop/keep lists (by-design | reachable | transition | unreachable)
    /api/rig/decisions   GC /api/decisions (token-protected; 200 with data null and meta.error when no token)
    /api/rig/switch      GC /api/switch/status with log_tail
    /api/fabric          the fabric manifest (aces.fabric.read / age_s) and the modes table

The one consequential write, two steps, both behind the POST guards of spec 3.1 and never pressed by a tool:

    POST /api/rig/switch/prepare {mode}                 -> gates on fresh GETs, a single-use 30 s nonce
    POST /api/rig/switch/confirm {mode, nonce, confirm} -> gates again, ledger request, GC POST /api/switch, ledger result
    (POST /api/rig/switch takes the same body)

Gates, in order: token configured (503), GRID CONTROL reachable (502), mode known (400), mode != current (409),
switchable === true (409), status.switching null (409), detected_mode != "transition" (409), auto_enabled false (409;
the UI gate of spec 4.5 enforced server-side too), switch engine idle or done (409). Refusals that reached the gates
(400, 409, 502) are ledgered as console.switch.refused, one entry per (to, reason class) per 60 s with the suppressed
repeats counted. The poller writes console.switch.outcome once when the engine reports done for that target, or
ok:null when the engine is unreachable for 600 s, or on the next start when a result has no outcome (the pending
marker lived in memory only). POST /api/fabric/refresh (202) runs aces.fabric.refresh in a worker thread, once per
60 s, ledgered as console.fabric.refresh. GRID CONTROL's POST /api/auto is never called.

Every GRID CONTROL request carries x-gc-token when a token is configured; no reply, log line, ledger payload or route
ever carries the token (reply excerpts are scrubbed).
"""
from __future__ import annotations

import hashlib
import json
import secrets as _random
import threading
import time
from typing import Any

from aces import config as aces_config, fabric, ledger

from . import UI_NAME, cache as cache_mod, httpc, jsonenc, secrets as tokens, server
from .server import Request, Response, envelope, error, ok

NONCE_TTL_S = 30.0
REFUSAL_WINDOW_S = 60.0
OUTCOME_UNREACHABLE_S = 600.0
REFRESH_MIN_INTERVAL_S = 60.0
TELEMETRY_RING = 240
TELEMETRY_MIN_GAP_S = 2.5
SWITCH_WARNING = "LLM reloads have 2400 s health gates; a full refill takes many minutes"
TOKEN_MISSING = "GRID CONTROL token not configured: set ACES_GC_TOKEN or ~/.config/aces/gridcontrol.env"
NO_MANIFEST = "no manifest yet; run aces fabric refresh"
RESTART_REASON = "server restarted before the switch engine reported done"
UNREACHABLE_REASON = "switch engine not reachable for 600 s"
LEDGERED_REFUSALS = (400, 409, 502)


class RigState:
    """Per-server state of this module (memory only: nonces, refusal windows, the telemetry ring, the pending outcome)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.switch_lock = threading.Lock()
        self.nonces: dict[str, dict[str, Any]] = {}
        self.refusals: dict[tuple[str, str], dict[str, Any]] = {}
        self.telemetry: list[dict[str, Any]] = []
        self.telemetry_seeded = False
        self.telemetry_sources: list[str] = []
        self.pending: dict[str, Any] | None = None
        self.refresh_running = False
        self.last_refresh_ts: float | None = None
        self.ledger_count_cache: tuple[tuple[int, int], int] | None = None


def _state(app: server.App) -> RigState:
    st = app.state.get("rig")
    if st is None:
        st = app.state["rig"] = RigState()
    return st


def _gc_base(app: server.App) -> str | None:
    base = app.settings.server.get("grid_control")
    return base.rstrip("/") if isinstance(base, str) and base else None


def _console_base(app: server.App) -> str | None:
    base = app.settings.server.get("simlab_console")
    return base.rstrip("/") if isinstance(base, str) and base else None


def _gc_headers(app: server.App) -> dict[str, str]:
    token, _ = tokens.gc_token(warn=app.log)
    return {"x-gc-token": token} if token else {}


def _ua(req: Request) -> str:
    return (req.headers.get("User-Agent") or "")[:200] if req.headers is not None else ""


def _record(app: server.App, kind: str, payload: dict[str, Any]) -> tuple[int | None, str | None]:
    """aces.ledger.record with every failure turned into (None, message) and logged; never raises."""
    try:
        n, _ = ledger.record(kind, payload)
        return n, None
    except (ValueError, TimeoutError, OSError, SystemExit) as e:
        msg = f"ledger write of {kind} failed: {e}"[:300]
        app.log(msg)
        return None, msg


# --- normalisations ------------------------------------------------------------------------------------------------------

def normalise_status(raw: Any) -> dict[str, Any]:
    """GC /api/status as received, plus the queue normalisation of spec 3.4: counts below zero become null with
    `queue_note` ("GRID CONTROL reported -1") and `raw_queue` carries the value as received."""
    if not isinstance(raw, dict):
        raise ValueError("GRID CONTROL /api/status is not a JSON object")
    st = dict(raw)
    q = raw.get("queue")
    counts: dict[str, Any] = {"queued": None, "running": None}
    note: str | None = None
    if isinstance(q, dict):
        bad: list[Any] = []
        odd: list[str] = []
        for k in ("queued", "running"):
            v = q.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                if v is not None:
                    odd.append(f"{k}={str(v)[:40]}")
                continue
            if v < 0:
                bad.append(v)
            else:
                counts[k] = v
        parts = []
        if bad:
            parts.append(", ".join(str(int(v)) if float(v).is_integer() else str(v) for v in sorted(set(bad))))
        parts.extend(odd)
        if parts:
            note = "GRID CONTROL reported " + "; ".join(parts)
    elif q is None:
        note = "GRID CONTROL reported no queue"
    else:
        note = f"GRID CONTROL reported {str(q)[:60]}"
    st["queue"] = counts
    st["raw_queue"] = q
    st["queue_note"] = note
    return st


def normalise_modes(raw: Any) -> dict[str, dict[str, Any]]:
    """GC /api/modes rows as {label, switchable, switchable_reported, pending}; a mode without a `switchable` key is
    switchable:false, switchable_reported:false."""
    if not isinstance(raw, dict):
        raise ValueError("GRID CONTROL /api/modes is not a JSON object")
    out: dict[str, dict[str, Any]] = {}
    for mode, m in raw.items():
        row = m if isinstance(m, dict) else {}
        out[str(mode)] = {"label": row.get("label"), "switchable": row.get("switchable") is True,
                          "switchable_reported": "switchable" in row, "pending": row.get("pending")}
    return out


def normalise_switch(raw: Any) -> dict[str, Any]:
    """GC /api/switch/status fields with `log_tail` (last 3) and `log` (full); `idle` is null when not reported."""
    if not isinstance(raw, dict):
        raise ValueError("GRID CONTROL /api/switch/status is not a JSON object")
    log = raw.get("log") if isinstance(raw.get("log"), list) else []
    return {"idle": raw.get("idle"), "target": raw.get("target"), "phase": raw.get("phase"), "done": raw.get("done"),
            "ok": raw.get("ok"), "startedAt": raw.get("startedAt"), "log_tail": log[-3:], "log": log, "log_lines": len(log)}


def _switch_inline(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != "log"}


def _switching_target(switching: Any) -> str:
    if isinstance(switching, dict):
        return str(switching.get("target") or switching.get("mode") or switching)[:80]
    return str(switching)[:80]


def telemetry_sample(tel: dict[str, Any], src: str, t: float | None = None) -> dict[str, Any]:
    """One ring sample {t, gpus:[{i,u,m,p,c}], cpu, mem, src} (the console's shape, cudax app.py:277-278)."""
    gpus = [{"i": g.get("index"), "u": g.get("util"), "m": g.get("mem_used_mb"), "p": g.get("power_w"), "c": g.get("temp_c")}
            for g in (tel.get("gpus") or []) if isinstance(g, dict)]
    return {"t": time.time() if t is None else t, "gpus": gpus, "cpu": (tel.get("cpu") or {}).get("load1"),
            "mem": (tel.get("mem") or {}).get("used_mb"), "src": src}


def _match_name(name: str, names: list[str]) -> str | None:
    """`apodex-2-primary` matches the stop-list name `apodex-2`; exact names match themselves."""
    for n in names:
        if isinstance(n, str) and n and (name == n or name.startswith(n + "-")):
            return n
    return None


def classify(snapshot: dict[str, Any], mode: str | None, sim_modes: dict[str, Any] | None, switching: Any = None,
             mode_error: str | None = None) -> list[dict[str, Any]]:
    """Every rig-state service and endpoint with a verdict against the current mode's sim-pool definition
    (stop_services / keep_services, cudax simlab/modes.json through the console's capacity.modes):

      reachable    the endpoint answers / the unit is active
      by-design    down and named in the mode's stop_services ("stopped by mode <mode> (by design)")
      transition   down while status.switching is set
      unreachable  down and not explained by the mode (the only verdict drawn in the fault ink)

    `word` is the vocabulary word (vocab.json "endpoint"), `verdict` the sentence, `matched` the list name that matched
    and `rule` the rule that decided."""
    pool = (sim_modes or {}).get(mode) if mode else None
    pool = pool if isinstance(pool, dict) and "stop_services" in pool else None  # a pool without a stop list explains nothing
    stop = [s for s in (pool.get("stop_services") or []) if isinstance(s, str)] if pool else []
    keep = [s for s in (pool.get("keep_services") or []) if isinstance(s, str)] if pool else []
    rows: list[dict[str, Any]] = []
    groups = (("endpoint", snapshot.get("endpoints") or [], lambda e: e.get("reachable") is True, "reachable"),
              ("service", snapshot.get("services") or [], lambda s: s.get("activeState") == "active", "systemd active"))
    for kind, items, up, up_rule in groups:
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "")
            state = it.get("activeState") if kind == "service" else ("reachable" if it.get("reachable") else (it.get("error") or "unreachable"))
            row: dict[str, Any] = {"name": name, "kind": kind, "state": state, "matched": None, "rule": None}
            if up(it):
                row.update(word="reachable", verdict="reachable", rule=up_rule)
            else:
                m = _match_name(name, stop)
                if m is not None:
                    row.update(word="by-design", verdict=f"stopped by mode {mode} (by design)", matched=m, rule="stop_services")
                elif switching not in (None, False, ""):
                    row.update(word="transition", verdict=f"switching to {_switching_target(switching)}", rule="status.switching")
                elif mode is None:
                    row.update(word="unreachable", rule="mode unknown",
                               verdict=f"unreachable (mode not known: {mode_error or 'GRID CONTROL status unavailable'})")
                elif pool is None:
                    row.update(word="unreachable", rule="no sim-pool entry",
                               verdict=f"unreachable (mode {mode} has no stop list in the sim pools)")
                else:
                    km = _match_name(name, keep)
                    row.update(word="unreachable", verdict=f"unreachable (not explained by mode {mode})", matched=km,
                               rule="keep_services" if km else "not in stop_services")
            rows.append(row)
    return rows


# --- fetchers (each returns (ok, data, error) for the cache) -----------------------------------------------------------------

def _gc_get(app: server.App, path: str) -> tuple[int, Any, str | None]:
    base = _gc_base(app)
    if not base:
        return 0, None, "not configured: server.grid_control is unset"
    return httpc.get_json(base + path, app.settings.timeout_s, headers=_gc_headers(app))


def fetch_status(app: server.App):
    status, obj, err = _gc_get(app, "/api/status")
    if err:
        return False, None, err
    try:
        return True, normalise_status(obj), None
    except ValueError as e:
        return False, None, str(e)


def fetch_modes(app: server.App):
    status, obj, err = _gc_get(app, "/api/modes")
    if err:
        return False, None, err
    try:
        return True, normalise_modes(obj), None
    except ValueError as e:
        return False, None, str(e)


def fetch_switch(app: server.App):
    status, obj, err = _gc_get(app, "/api/switch/status")
    if err:
        return False, None, err
    try:
        return True, normalise_switch(obj), None
    except ValueError as e:
        return False, None, str(e)


GC_PROXY_NOTE = "through the SIMLAB console's GET-only /gc read proxy on the rig (no GRID CONTROL token on this desktop)"


def gc_read(app: server.App, path: str, timeout: float | None = None) -> tuple[int, Any, str | None, str]:
    """GET a token-protected GRID CONTROL path: with the token held on this desktop, directly; without it, through the
    SIMLAB console's GET-only /gc proxy, which forwards with the rig's own token (cudax simlab/console/app.py). Returns
    (status, obj, error, via) where via is "token" or "proxy"; writes always need the token on this desktop."""
    timeout = timeout or app.settings.timeout_s
    token, _ = tokens.gc_token(warn=app.log)
    base = _gc_base(app)
    if token and base:
        status, obj, err = httpc.get_json(base + path, timeout, headers={"x-gc-token": token})
        return status, obj, (tokens.scrub(err, token) if err else None), "token"
    cbase = _console_base(app)
    if not cbase:
        return 0, None, "not configured: no GRID CONTROL token on this desktop and server.simlab_console is unset", "proxy"
    status, obj, err = httpc.get_json(cbase + "/gc" + path, timeout)
    if status == 503:
        err = "the SIMLAB console's /gc read proxy is not configured on the rig and this desktop holds no GRID CONTROL token"
    return status, obj, err, "proxy"


def gc_readable(app: server.App) -> bool:
    return bool((tokens.gc_token()[0] and _gc_base(app)) or _console_base(app))


def fetch_decisions(app: server.App):
    status, obj, err, via = gc_read(app, "/api/decisions")
    if err:
        return False, None, err
    rows = obj if isinstance(obj, list) else ([] if obj is None else [obj])
    return True, {"decisions": rows, "schema": "not interpreted", "n": len(rows), "via": via,
                  "via_note": None if via == "token" else GC_PROXY_NOTE}, None


def _seed_telemetry(app: server.App, st: RigState) -> None:
    """Once: the console's /api/telemetry.history (cudax app.py:277-281), labelled "console"."""
    st.telemetry_seeded = True
    base = _console_base(app)
    if not base:
        st.telemetry_sources.append("console history: not configured (server.simlab_console is unset)")
        return
    status, obj, err = httpc.get_json(base + "/api/telemetry", 8.0)
    if err or not isinstance(obj, dict):
        st.telemetry_sources.append(f"console history unavailable: {err or 'not an object'}")
        return
    hist = [h for h in (obj.get("history") or []) if isinstance(h, dict) and isinstance(h.get("t"), (int, float))]
    for h in hist[-TELEMETRY_RING:]:
        st.telemetry.append({"t": h.get("t"), "gpus": h.get("gpus") or [], "cpu": h.get("cpu"), "mem": h.get("mem"), "src": "console"})
    st.telemetry_sources.append(f"console history ({len(hist)} samples)")


def _telemetry_data(st: RigState, tel: Any) -> dict[str, Any]:
    return {"telemetry": tel, "history": list(st.telemetry), "sources_used": list(st.telemetry_sources),
            "ring": {"samples": len(st.telemetry), "cap": TELEMETRY_RING, "min_gap_s": TELEMETRY_MIN_GAP_S}}


def fetch_telemetry(app: server.App):
    st = _state(app)
    with st.lock:
        if not st.telemetry_seeded:
            _seed_telemetry(app, st)
    status, obj, err = _gc_get(app, "/api/telemetry")
    if err:
        if status == 404:
            err = "GRID CONTROL does not serve /api/telemetry (HTTP 404)"
        return False, None, err
    if not isinstance(obj, dict):
        return False, None, "GRID CONTROL /api/telemetry is not a JSON object"
    with st.lock:
        now = time.time()
        if not st.telemetry or now - float(st.telemetry[-1].get("t") or 0) >= TELEMETRY_MIN_GAP_S:
            st.telemetry.append(telemetry_sample(obj, "gc", now))
            del st.telemetry[:-TELEMETRY_RING]
        if "gc" not in st.telemetry_sources:
            st.telemetry_sources.append("gc")
        return True, _telemetry_data(st, obj), None


def rigstate_call(app: server.App, name: str, arguments: dict | None = None) -> tuple[bool, Any, str | None]:
    """rig-state tools/call (the aces.fabric.rig_tool shape, src/aces-base/aces/fabric.py:54-69)."""
    url = app.settings.server.get("rig_state")
    if not isinstance(url, str) or not url:
        return False, None, "not configured: server.rig_state is unset"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    status, reply, err = httpc.post_json(url, body, app.settings.timeout_s)
    if err:
        reason = err[len("unreachable: "):] if err.startswith("unreachable: ") else err
        return False, None, f"rig-state unreachable: {reason}"
    if not isinstance(reply, dict):
        return False, None, "rig-state: the reply is not a JSON-RPC object"
    if reply.get("error"):
        e = reply["error"] if isinstance(reply["error"], dict) else {}
        return False, None, f"rig-state JSON-RPC error {e.get('code')}: {str(e.get('message'))[:120]}"
    result = reply.get("result") or {}
    if isinstance(result, dict) and result.get("structuredContent") is not None:
        return True, result["structuredContent"], None
    if isinstance(result, dict):
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    return True, json.loads(block["text"]), None
                except (json.JSONDecodeError, TypeError):
                    return True, block.get("text"), None
    return True, result, None


def _sim_modes_entry(app: server.App) -> cache_mod.Entry:
    base = _console_base(app)
    return app.cache.get("rig.sim_modes", 30.0, lambda: fetch_sim_modes(app), source=f"{base}/api/capacity" if base else None)


def fetch_sim_modes(app: server.App):
    base = _console_base(app)
    if not base:
        return False, None, "not configured: server.simlab_console is unset"
    status, obj, err = httpc.get_json(base + "/api/capacity", 8.0)
    if err:
        reason = err[len("unreachable: "):] if err.startswith("unreachable: ") else err
        return False, None, f"SIMLAB console unreachable at {base}: {reason}"
    modes = obj.get("modes") if isinstance(obj, dict) else None
    if not isinstance(modes, dict):
        return False, None, "SIMLAB console /api/capacity carries no modes table"
    return True, modes, None


def fetch_snapshot(app: server.App):
    ok_, sc, err = rigstate_call(app, "rig_snapshot")
    if not ok_:
        return False, None, err
    if not isinstance(sc, dict):
        return False, None, "rig-state rig_snapshot: structuredContent is not an object"
    st_e = app.hub.get("rig.status") if app.hub.source("rig.status") else None
    status = st_e.data if st_e is not None and isinstance(st_e.data, dict) else None
    sm_e = _sim_modes_entry(app)
    mode = status.get("mode") if status else None
    switching = status.get("switching") if status else None
    rows = classify(sc, mode, sm_e.data, switching, st_e.error if st_e is not None else "rig.status not polled")
    pool = (sm_e.data or {}).get(mode) if (mode and isinstance(sm_e.data, dict)) else None
    data = dict(sc)
    data["classification"] = rows
    data["classification_basis"] = {
        "mode": mode, "mode_source": (_gc_base(app) or "") + "/api/status" if status else None,
        "mode_error": None if status else (st_e.error if st_e is not None else "rig.status not polled"),
        "switching": switching, "sim_modes_source": sm_e.source, "sim_modes_error": sm_e.error,
        "stop_services": (pool or {}).get("stop_services"), "keep_services": (pool or {}).get("keep_services"),
        "rest": (pool or {}).get("rest"),
        "counts": {w: sum(1 for r in rows if r["word"] == w) for w in ("reachable", "by-design", "transition", "unreachable")}}
    return True, data, None


def fetch_manifest(app: server.App):
    cfg = app.settings.cfg
    path = fabric.manifest_path(cfg)
    max_age = int((cfg.get("fabric") or {}).get("max_age_s") or 900)
    age = fabric.age_s(cfg)
    data: dict[str, Any] = {"path": str(path), "exists": age is not None, "age_s": None if age is None else round(age, 1),
                            "max_age_s": max_age, "stale": age is None or age > max_age, "generated": None, "mode": None,
                            "reason": None if age is not None else NO_MANIFEST}
    if age is not None:
        try:
            m = fabric.read(cfg)
        except SystemExit as e:
            data["reason"] = f"manifest unreadable: {e}"
            m = None
        if isinstance(m, dict):
            data["generated"] = m.get("generated")
            data["mode"] = (m.get("mode") or {}).get("name") if isinstance(m.get("mode"), dict) else None
            data["health"] = m.get("health")
    return True, data, None


def _manifest_inline(d: dict[str, Any]) -> dict[str, Any]:
    return {"age_s": d.get("age_s"), "stale": d.get("stale"), "path": d.get("path")}


def _ledger_count(app: server.App, st: RigState) -> int | None:
    p = ledger.path()
    try:
        s = p.stat()
    except OSError:
        return 0
    key = (s.st_mtime_ns, s.st_size)
    if st.ledger_count_cache and st.ledger_count_cache[0] == key:
        return st.ledger_count_cache[1]
    try:
        with open(p, "rb") as fh:
            n = sum(1 for line in fh if line.strip())
    except OSError:
        return None
    st.ledger_count_cache = (key, n)
    return n


def fetch_console_health(app: server.App):
    st = _state(app)
    status_e = app.cache.peek("rig.status")
    man_e = app.cache.peek("fabric.manifest")
    status = status_e.data if status_e is not None and isinstance(status_e.data, dict) else None
    man = man_e.data if man_e is not None and isinstance(man_e.data, dict) else None
    data = {"mode": status.get("mode") if status else None, "detected_mode": status.get("detected_mode") if status else None,
            "switching": status.get("switching") if status else None, "auto_enabled": status.get("auto_enabled") if status else None,
            "queue": status.get("queue") if status else None, "queue_note": status.get("queue_note") if status else None,
            "rig_ok": status_e.ok if status_e is not None else None, "rig_stale": status_e.stale if status_e is not None else False,
            "rig_error": status_e.error if status_e is not None else "not polled yet",
            "rig_fetched_utc": status_e.fetched_utc if status_e is not None else None,
            "manifest_age_s": man.get("age_s") if man else None, "manifest_stale": man.get("stale") if man else None,
            "manifest_max_age_s": man.get("max_age_s") if man else None,
            "ledger_entries": _ledger_count(app, st), "clients": app.broker.count(), "poller": app.hub.state,
            "version": server.__version__, "fixture": app.fixture, "switch_outcome_pending": app.switch_outcome_pending}
    return True, data, None


# --- the switch ------------------------------------------------------------------------------------------------------------

def evaluate_gates(app: server.App, mode: str) -> tuple[Response | None, dict[str, Any]]:
    """The gates of spec 3.4 on fresh GETs (never from cache). Returns (refusal or None, context)."""
    ctx: dict[str, Any] = {"gates": {}}
    if app.fixture:
        return error(503, "fixture mode: the mode switch is disabled while fixtures are replayed", "fixture"), ctx
    token, _ = tokens.gc_token(warn=app.log)
    if not token:
        return error(503, TOKEN_MISSING, "not-configured"), ctx
    base = _gc_base(app)
    if not base:
        return error(503, "not configured: server.grid_control is unset", "not-configured"), ctx
    st_e = app.hub.refresh("rig.status")
    md_e = app.hub.refresh("rig.modes")
    sw_e = app.hub.refresh("rig.switch")
    for e, what in ((st_e, "/api/status"), (md_e, "/api/modes"), (sw_e, "/api/switch/status")):
        if not e.ok or not isinstance(e.data, dict):
            return error(502, f"GRID CONTROL {what}: {e.error or 'no data'}", "upstream", source=base + what), ctx
    status, modes, sw = st_e.data, md_e.data, sw_e.data
    ctx.update(status=status, modes=modes, switch=sw)
    target = modes.get(mode)
    if target is None:
        return error(400, f"unknown mode {mode}", "unknown-mode"), ctx
    ctx["target"] = target
    if mode == status.get("mode"):
        return error(409, f"{mode} is the current mode", "current-mode"), ctx
    if not target.get("switchable"):
        if target.get("switchable_reported"):
            return error(409, f"{mode} is not switchable ({target.get('pending') or 'no pending text'})", "not-switchable"), ctx
        return error(409, "switchable not reported for this mode", "not-switchable"), ctx
    gates = {"detected_mode": status.get("detected_mode"), "switching": status.get("switching"),
             "auto_enabled": status.get("auto_enabled"), "switch_idle": sw.get("idle"), "switch_done": sw.get("done")}
    ctx["gates"] = gates
    if status.get("switching") not in (None, False, ""):
        return error(409, f"switching in progress ({_switching_target(status.get('switching'))})", "switching"), ctx
    if status.get("detected_mode") == "transition":
        return error(409, "detected_mode is transition", "transition"), ctx
    if status.get("auto_enabled") is True:
        return error(409, "auto mode is on; switching by hand is refused", "auto-enabled"), ctx
    if not (sw.get("idle") is True or sw.get("done") is True):
        return error(409, f"switch engine busy: {sw.get('target')} {sw.get('phase')}", "engine-busy"), ctx
    return None, ctx


def _ledger_refusal(app: server.App, req: Request, to: str, resp: Response, ctx: dict[str, Any]) -> None:
    """console.switch.refused, rate-limited to one entry per (to, reason class) per 60 s."""
    st = _state(app)
    key = (to, resp.code or "refused")
    now = time.time()
    with st.lock:
        w = st.refusals.get(key)
        if w is not None and now - w["first_ts"] < REFUSAL_WINDOW_S:
            w["suppressed"] += 1
            app.counters["suppressed_refusals"] += 1
            return
        suppressed_prev = w["suppressed"] if w is not None else 0
        st.refusals[key] = {"first_ts": now, "suppressed": 0}
    payload = {"to": to, "reason": resp.error, "code": resp.code, "gates": ctx.get("gates") or {}, "peer": req.peer,
               "user_agent": _ua(req), "repeats_suppressed_in_window": suppressed_prev}
    _record(app, "console.switch.refused", payload)


def _prune_nonces(st: RigState, now: float) -> None:
    for k in [k for k, v in st.nonces.items() if v["spent"] or now > v["expires"] + 60]:
        st.nonces.pop(k, None)


def h_switch_prepare(req: Request) -> Response:
    app = req.app
    st = _state(app)
    body = req.json or {}
    mode = body.get("mode") or body.get("to")
    if not isinstance(mode, str) or not mode or len(mode) > 80:
        return error(400, "body.mode must be a mode name", "bad-request")
    with st.switch_lock:
        refusal, ctx = evaluate_gates(app, mode)
        if refusal is not None:
            if refusal.status in LEDGERED_REFUSALS:
                _ledger_refusal(app, req, mode, refusal, ctx)
            return refusal
        status, target = ctx["status"], ctx["target"]
        sm_e = _sim_modes_entry(app)
        pool = (sm_e.data or {}).get(mode) if isinstance(sm_e.data, dict) else None
        now = time.time()
        nonce = _random.token_hex(32)
        with st.lock:
            _prune_nonces(st, now)
            st.nonces[nonce] = {"to": mode, "from": status.get("mode"), "last_switch_ts": status.get("last_switch_ts"),
                                "peer": req.peer, "created": now, "expires": now + NONCE_TTL_S, "spent": False,
                                "label": target.get("label"), "pending": target.get("pending")}
        data = {"nonce": nonce, "expires_s": int(NONCE_TTL_S), "from": status.get("mode"), "to": mode,
                "label": target.get("label"), "pending": target.get("pending"),
                "sim_pool": (pool or {}).get("sim_gpus") if isinstance(pool, dict) else None,
                "sim_mode": pool if isinstance(pool, dict) else None, "sim_modes_error": sm_e.error,
                "gates": ctx["gates"], "warning": SWITCH_WARNING}
        return ok(data, source=f"{_gc_base(app)}/api/status,/api/modes,/api/switch/status")


def h_switch_confirm(req: Request) -> Response:
    app = req.app
    st = _state(app)
    body = req.json or {}
    mode = body.get("mode") or body.get("to")
    nonce = body.get("nonce")
    if not isinstance(mode, str) or not mode or not isinstance(nonce, str) or not nonce:
        return error(400, "body needs mode (or to), nonce and confirm", "bad-request")
    if body.get("confirm") != mode:
        return error(400, "confirm must equal the target mode name", "confirm-mismatch")
    with st.switch_lock:
        now = time.time()
        with st.lock:
            n = st.nonces.get(nonce)
            valid = (n is not None and not n["spent"] and now <= n["expires"] and n["to"] == mode and n["peer"] == req.peer)
            if valid:
                n["spent"] = True  # single use, spent whatever the gates say next
        if not valid:
            app.counters["nonce_failures"] += 1
            return error(410, "nonce expired or spent", "nonce")
        refusal, ctx = evaluate_gates(app, mode)
        if refusal is not None:
            if refusal.status in LEDGERED_REFUSALS:
                _ledger_refusal(app, req, mode, refusal, ctx)
            return refusal
        status, sw = ctx["status"], ctx["switch"]
        if status.get("last_switch_ts") != n["last_switch_ts"]:
            app.counters["nonce_failures"] += 1
            return error(410, "nonce expired or spent: the rig switched since prepare (last_switch_ts changed)", "nonce")
        base = _gc_base(app)
        token, _ = tokens.gc_token(warn=app.log)
        nonce_sha12 = hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:12]
        request_payload = {
            "from": status.get("mode"), "to": mode, "label": n["label"], "pending": n["pending"],
            "status_snapshot": {k: status.get(k) for k in ("mode", "detected_mode", "switching", "last_switch_ts", "auto_enabled")},
            "switch_snapshot": {k: sw.get(k) for k in ("idle", "done", "ok", "target")},
            "nonce_sha12": nonce_sha12, "peer": req.peer, "user_agent": _ua(req), "ui": UI_NAME}
        req_n, req_err = _record(app, "console.switch.request", request_payload)
        if req_n is None:
            return error(500, f"nothing was sent to GRID CONTROL: {req_err}", "ledger")
        t0 = time.monotonic()
        up_status, reply, up_err = httpc.post_json(base + "/api/switch", {"mode": mode}, app.settings.timeout_s,
                                                   headers={"x-gc-token": token} if token else {})
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        excerpt_src = jsonenc.dumps(reply) if reply is not None else (up_err or "")
        excerpt = tokens.scrub(excerpt_src, token)[:2048]
        result_payload = {"to": mode, "upstream_status": up_status or None, "ok": up_err is None, "reply_excerpt": excerpt,
                          "elapsed_ms": elapsed_ms, "error": up_err}
        res_n, res_err = _record(app, "console.switch.result", result_payload)
        ledger_info = {"request_n": req_n, "result_n": res_n, "result_error": res_err}
        if up_err is not None:
            return error(502, f"GRID CONTROL POST /api/switch failed: {up_err} (ledger request {req_n}, result {res_n})",
                         "upstream", source=base + "/api/switch", upstream_status=up_status or None)
        with st.lock:
            st.pending = {"to": mode, "since_utc": server.now_utc(), "since_ts": now, "since_ms": int(now * 1000),
                          "nonce_sha12": nonce_sha12, "saw_running": False}
            app.switch_outcome_pending = {"to": mode, "since_utc": st.pending["since_utc"]}
        app.hub.publish("switch.outcome", {"to": mode, "ok": None, "phase": "requested", "log_tail": [],
                                           "observed_utc": server.now_utc(), "reason": None, "pending": True}, True, None, None)
        return ok({"started": True, "to": mode, "upstream_status": up_status, "ledger": ledger_info,
                   "nonce_sha12": nonce_sha12, "warning": SWITCH_WARNING}, source=base + "/api/switch")


def _finish_outcome(app: server.App, st: RigState, outcome: dict[str, Any]) -> None:
    n, err = _record(app, "console.switch.outcome", outcome)
    st.pending = None
    app.switch_outcome_pending = None
    app.hub.publish("switch.outcome", dict(outcome, pending=False, ledger_n=n, ledger_error=err), True, None, None)


def _watch_switch(app: server.App, entry: cache_mod.Entry) -> None:
    """on_result of the rig.switch source: write console.switch.outcome once for a pending switch."""
    st = _state(app)
    with st.lock:
        p = st.pending
        if p is None:
            return
        if entry.ok and isinstance(entry.data, dict):
            sw = entry.data
            if sw.get("target") != p["to"]:
                return
            started = sw.get("startedAt")
            if not sw.get("done") and sw.get("idle") is not True:
                p["saw_running"] = True
            fresh = (isinstance(started, (int, float)) and not isinstance(started, bool) and started >= p["since_ms"] - 5000) \
                or (started is None and p["saw_running"])
            if sw.get("done") is True and fresh:
                outcome = {"to": p["to"], "ok": sw.get("ok") if isinstance(sw.get("ok"), bool) else None,
                           "phase": sw.get("phase"), "log_tail": sw.get("log_tail") or [],
                           "observed_utc": server.now_utc(), "reason": None}
                _finish_outcome(app, st, outcome)
        elif time.time() - p["since_ts"] > OUTCOME_UNREACHABLE_S:
            _finish_outcome(app, st, {"to": p["to"], "ok": None, "phase": None, "log_tail": [],
                                      "observed_utc": server.now_utc(), "reason": UNREACHABLE_REASON})


def outcomes_lost_on_restart(app: server.App) -> int:
    """At start: a console.switch.result with ok:true and no later console.switch.outcome for the same target gets its
    outcome written now with ok:null (the pending marker lived in memory only). Returns the number written."""
    try:
        entries = ledger.entries()
    except SystemExit as e:
        app.log(f"ledger unreadable at start: {e}")
        return 0
    open_results: dict[str, int] = {}
    for i, e in enumerate(entries):
        kind, payload = e.get("kind"), e.get("payload") or {}
        to = payload.get("to") if isinstance(payload, dict) else None
        if kind == "console.switch.result" and payload.get("ok") is True and isinstance(to, str):
            open_results[to] = i
        elif kind == "console.switch.outcome" and isinstance(to, str):
            open_results.pop(to, None)
    written = 0
    for to in open_results:
        n, _ = _record(app, "console.switch.outcome", {"to": to, "ok": None, "phase": None, "log_tail": [],
                                                       "observed_utc": server.now_utc(), "reason": RESTART_REASON})
        written += 1 if n is not None else 0
    return written


# --- GET handlers --------------------------------------------------------------------------------------------------------

def h_status(req: Request) -> Response:
    return envelope(req.app.hub.get("rig.status"))


def h_modes(req: Request) -> Response:
    app = req.app
    md = app.hub.get("rig.modes")
    st = app.hub.get("rig.status")
    sm = _sim_modes_entry(app)
    data = {"gridcontrol_modes": md.data, "sim_modes": sm.data,
            "current": st.data.get("mode") if isinstance(st.data, dict) else None,
            "errors": {"gridcontrol_modes": md.error, "sim_modes": sm.error, "current": st.error}}
    parts = {"gridcontrol_modes": md.meta(), "sim_modes": sm.meta(), "current": st.meta()}
    return envelope(md, data=data, extra_meta={"parts": parts})


def h_telemetry(req: Request) -> Response:
    app = req.app
    st = _state(app)
    e = app.hub.get("rig.telemetry")
    data = e.data
    if data is None:
        with st.lock:
            data = _telemetry_data(st, None)
    return envelope(e, data=data)


def h_snapshot(req: Request) -> Response:
    return envelope(req.app.hub.get("rig.snapshot"))


def h_decisions(req: Request) -> Response:
    if not gc_readable(req.app):
        # an honest absence in the 200 envelope, not a 503: the browser logs every 5xx as a console error and the
        # Ops plate draws meta.error as the reason either way
        resp = ok(None, source="GRID CONTROL /api/decisions")
        resp.meta.update({"ok": False, "error": "not configured: no GRID CONTROL token (ACES_GC_TOKEN, SIMLAB_GC_TOKEN or "
                                                "~/.config/aces/gridcontrol.env) and server.simlab_console is unset",
                          "code": "not-configured"})
        return resp
    return envelope(req.app.hub.get("rig.decisions"))


def h_switch_status(req: Request) -> Response:
    e = req.app.hub.get("rig.switch")
    data = dict(e.data) if isinstance(e.data, dict) else None
    if data is not None:
        data["outcome_pending"] = req.app.switch_outcome_pending
    return envelope(e, data=data)


def h_fabric(req: Request) -> Response:
    app = req.app
    st = _state(app)
    cfg = app.settings.cfg
    path = fabric.manifest_path(cfg)
    reason: str | None = None
    manifest: Any = None
    try:
        manifest = fabric.read(cfg)
    except SystemExit as e:
        reason = f"manifest unreadable: {e}"
    age = fabric.age_s(cfg)
    max_age = int((cfg.get("fabric") or {}).get("max_age_s") or 900)
    if manifest is None and reason is None:
        reason = NO_MANIFEST
    try:
        table = aces_config.modes_table()
        table_error = None
    except SystemExit as e:
        table, table_error = {}, str(e)
    data = {"manifest": manifest, "path": str(path), "age_s": None if age is None else round(age, 1), "max_age_s": max_age,
            "stale": age is None or age > max_age, "modes_table": table, "modes_table_error": table_error, "reason": reason,
            "refresh": {"running": st.refresh_running, "last_utc": cache_mod.utc(st.last_refresh_ts),
                        "min_interval_s": REFRESH_MIN_INTERVAL_S}}
    return ok(data, source=str(path))


def _run_refresh(app: server.App, st: RigState) -> None:
    cfg = app.settings.cfg
    t0 = time.time()
    health = mode = None
    lanes: list[Any] = []
    notes: list[Any] = []
    err: str | None = None
    try:
        m = fabric.refresh(cfg)
        health = m.get("health")
        mode = (m.get("mode") or {}).get("name") if isinstance(m.get("mode"), dict) else None
        lanes = [lane.get("id") for lane in m.get("lanes", []) if isinstance(lane, dict)]
        notes = list(m.get("notes") or [])
    except BaseException as e:  # noqa: BLE001 (SystemExit from aces.config included; the thread must report, not die)
        err = f"{type(e).__name__}: {e}"[:200]
    payload = {"duration_s": round(time.time() - t0, 2), "health": health, "mode": mode, "lanes": lanes, "notes": notes,
               "manifest_path": str(fabric.manifest_path(cfg)), "error": err}
    _record(app, "console.fabric.refresh", payload)
    with st.lock:
        st.refresh_running = False
    try:
        app.hub.refresh("fabric.manifest")
    except Exception as e:  # noqa: BLE001
        app.log(f"fabric.manifest republish failed: {e}")


def h_fabric_refresh(req: Request) -> Response:
    app = req.app
    st = _state(app)
    if app.fixture:
        return error(503, "fixture mode: a fabric refresh would call the rig", "fixture")
    now = time.time()
    with st.lock:
        if st.refresh_running:
            return error(409, "refresh already running", "busy")
        if st.last_refresh_ts is not None and now - st.last_refresh_ts < REFRESH_MIN_INTERVAL_S:
            ago = int(now - st.last_refresh_ts)
            wait = int(REFRESH_MIN_INTERVAL_S - ago) or 1
            return error(429, f"refreshed {ago} s ago", "rate-limited", next_try_s=wait, headers={"Retry-After": str(wait)})
        st.refresh_running = True
        st.last_refresh_ts = now
    threading.Thread(target=_run_refresh, args=(app, st), name="aces-console-fabric-refresh", daemon=True).start()
    return ok({"started": True}, source=str(fabric.manifest_path(app.settings.cfg)), status=202)


# --- install --------------------------------------------------------------------------------------------------------------

def install(register) -> None:
    """Register the rig and fabric routes and this module's poller sources on the current App."""
    app = server.current_app()
    _state(app)
    gc = _gc_base(app) or "grid_control unset"
    hub = app.hub
    hub.register("rig.status", lambda: fetch_status(app), 3.0, ("*",), inline=True, source=f"{gc}/api/status")
    hub.register("rig.switch", lambda: fetch_switch(app), 3.0, ("*",), inline=_switch_inline,
                 source=f"{gc}/api/switch/status", on_result=lambda e: _watch_switch(app, e))
    hub.register("rig.modes", lambda: fetch_modes(app), 30.0, ("fabric", "ops"), inline=True, source=f"{gc}/api/modes")
    hub.register("rig.telemetry", lambda: fetch_telemetry(app), 3.0, ("fabric",),
                 inline=lambda d: (d.get("history") or [None])[-1], source=f"{gc}/api/telemetry")
    hub.register("rig.snapshot", lambda: fetch_snapshot(app), 30.0, ("fabric", "ops"), inline=False, max_backoff_s=120.0,
                 source=str(app.settings.server.get("rig_state") or "rig_state unset"))
    hub.register("rig.decisions", lambda: fetch_decisions(app), 60.0, ("fabric", "ops"), inline=False,
                 source=f"{gc}/api/decisions", enabled=lambda: gc_readable(app))
    hub.register("fabric.manifest", lambda: fetch_manifest(app), 5.0, ("*",), inline=_manifest_inline,
                 source=str(fabric.manifest_path(app.settings.cfg)))
    hub.register("console.health", lambda: fetch_console_health(app), 3.0, ("*",), inline=True, source="console")
    register("GET", "/api/rig/status", h_status)
    register("GET", "/api/rig/modes", h_modes)
    register("GET", "/api/rig/telemetry", h_telemetry)
    register("GET", "/api/rig/snapshot", h_snapshot)
    register("GET", "/api/rig/decisions", h_decisions)
    register("GET", "/api/rig/switch", h_switch_status)
    register("POST", "/api/rig/switch/prepare", h_switch_prepare)
    register("POST", "/api/rig/switch/confirm", h_switch_confirm)
    register("POST", "/api/rig/switch", h_switch_confirm)
    register("GET", "/api/fabric", h_fabric)
    register("POST", "/api/fabric/refresh", h_fabric_refresh)
    if not app.fixture:
        written = outcomes_lost_on_restart(app)
        if written:
            app.log(f"wrote {written} console.switch.outcome entr{'y' if written == 1 else 'ies'} ({RESTART_REASON})")
