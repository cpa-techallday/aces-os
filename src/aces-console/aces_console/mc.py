"""Mission Control, the red-team VM, model staging and the auto-switch, as GRID CONTROL rev B serves them (cudax
ui/grid-control/index.html:584-800): the single console's Agents plate and the Ops additions, so GRID CONTROL's own page
is no longer needed for any of it.

Reads go to GRID CONTROL with the token held on this desktop; without one they go through the SIMLAB console's GET-only
/gc read proxy on the rig (rig.gc_read), and every reply says which. Every write (chat, missions, approvals, uploads,
the red-team VM, the auto-switch) needs the token on this desktop, is a human POST (the console's POST guards: loopback,
own Origin, X-ACES-Intent: human) and is ledgered. Mission Control's runs, chats and missions are agent-reported: shown
as Mission Control says, never as compute truth (rule 7 of the brief).

    GET  /api/mc/agents                  agents.list (system prompts and tool JSON dropped; tools counted)
    GET  /api/mc/runs?limit=             runs.list (default 80, at most 200)
    GET  /api/mc/run/{id}                runs.get (steps capped)
    GET  /api/mc/sessions                chat.listSessions
    GET  /api/mc/session/{id}            chat.getSession (messages capped)
    POST /api/mc/session                 {title}                         chat.createSession     console.mc.session
    POST /api/mc/session/{id}/message    {content, uploads[]}            chat.sendMessage       console.mc.message
    GET  /api/mc/missions                missions.list
    GET  /api/mc/mission/{id}            missions.get (+ the runs of its steps)
    POST /api/mc/mission                 {title, objective, uploads[], launch}  missions.create (+ launch)  console.mc.mission
    POST /api/mc/mission/{id}/{action}   launch | retryFailed | compile | complete                 console.mc.mission
    POST /api/mc/item/{id}/approve       missions.approveItem (a step's deliverable, Mission Control's review)  console.mc.approve
    POST /api/mc/upload                  {filename, mime, data_b64} (at most 16 MiB)  /api/mc/uploads  console.mc.upload
    GET  /api/mc/upload/{id}             the upload's bytes (sandboxed)
    GET  /api/mc/artifact/{id}           an artifact's bytes (attachment)
    GET  /api/rig/downloads              /downloads-status.json (model staging)
    GET  /api/rig/redteam                /api/redteam/status
    POST /api/rig/redteam/vm             {action: up|down, confirm}       /api/redteam/vm        console.redteam.vm
    POST /api/rig/auto                   {enabled, confirm}               /api/auto              console.auto
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
import urllib.parse
from typing import Any

from aces import ledger

from . import httpc, rig, secrets as tokens, server
from .server import Request, Response, envelope, error, ok

TRPC = "/api/mc/trpc/"
READ_TIMEOUT_S = 8.0
WRITE_TIMEOUT_S = 30.0
UPLOAD_CAP = 16 * 1024 * 1024
FILE_CAP = 64 * 1024 * 1024
MISSION_ACTIONS = ("launch", "retryFailed", "compile", "complete")
REDTEAM_CONFIRM = {"up": "start mission vm", "down": "END MISSION"}
AUTO_CONFIRM = {True: "enable auto-switch", False: "disable auto-switch"}
STEP_CAP, STEP_TEXT_CAP, MSG_CAP, MSG_TEXT_CAP = 400, 8000, 300, 20000
MISSING = object()
NO_TOKEN = ("not configured: writing to GRID CONTROL needs its token on this desktop (ACES_GC_TOKEN, SIMLAB_GC_TOKEN or "
            "~/.config/aces/gridcontrol.env, mode 0600); reads work without it through the rig's read proxy")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


# --- GRID CONTROL access ------------------------------------------------------------------------------------------------

def _token(app: server.App) -> str | None:
    return tokens.gc_token(warn=app.log)[0]


def _gc_base(app: server.App) -> str | None:
    base = app.settings.server.get("grid_control")
    return base.rstrip("/") if isinstance(base, str) and base else None


def _trpc_data(obj: Any) -> Any:
    try:
        return obj["result"]["data"]["json"]
    except (KeyError, TypeError):
        return MISSING


def _trpc_error(status: int, obj: Any, err: str | None) -> str:
    msg = None
    if isinstance(obj, dict):
        e = obj.get("error")
        e = e.get("json", e) if isinstance(e, dict) else e
        if isinstance(e, dict):
            msg = e.get("message")
    return f"HTTP {status}: {str(msg)[:300]}" if msg and status else (err or f"HTTP {status}")


def trpc_get(app: server.App, proc: str, inp: dict | None = None) -> tuple[bool, Any, str | None, str]:
    q = "" if inp is None else "?input=" + urllib.parse.quote(json.dumps({"json": inp}, separators=(",", ":")))
    status, obj, err, via = rig.gc_read(app, TRPC + proc + q, READ_TIMEOUT_S)
    if err:
        return False, None, f"Mission Control {proc}: {_trpc_error(status, obj, err)}", via
    data = _trpc_data(obj)
    if data is MISSING:
        return False, None, f"Mission Control {proc}: the reply carries no result.data.json", via
    return True, data, None, via


def trpc_post(app: server.App, proc: str, payload: dict) -> tuple[int, Any, str | None]:
    """(status, data, error); status 503 without a token on this desktop."""
    token, base = _token(app), _gc_base(app)
    if not token or not base:
        return 503, None, NO_TOKEN
    status, obj, err = httpc.post_json(base + TRPC + proc, {"json": payload}, WRITE_TIMEOUT_S,
                                       headers={"x-gc-token": token})
    if err:
        return (status or 502), None, tokens.scrub(f"Mission Control {proc}: {_trpc_error(status, obj, err)}", token)
    data = _trpc_data(obj)
    return status, (None if data is MISSING else data), None


def _gc_post(app: server.App, path: str, body: dict) -> tuple[int, Any, str | None]:
    token, base = _token(app), _gc_base(app)
    if not token or not base:
        return 503, None, NO_TOKEN
    status, obj, err = httpc.post_json(base + path, body, WRITE_TIMEOUT_S, headers={"x-gc-token": token})
    if err:
        detail = obj.get("error") if isinstance(obj, dict) else None
        return (status or 502), obj, tokens.scrub(f"GRID CONTROL {path}: {detail or err}", token)
    return status, obj, None


def _record(app: server.App, kind: str, payload: dict[str, Any]) -> int | None:
    try:
        n, _ = ledger.record(kind, payload)
        return n
    except (ValueError, TimeoutError, OSError, SystemExit) as e:
        app.log(f"ledger write of {kind} failed: {e}"[:300])
        return None


def _via_note(via: str) -> str | None:
    return None if via == "token" else rig.GC_PROXY_NOTE


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _clip(v: Any, n: int) -> Any:
    return v[:n] + f" [... {len(v) - n} more characters]" if isinstance(v, str) and len(v) > n else v


def _usage(r: dict) -> dict:
    """Mission Control's promptTokens/completionTokens as {"usage": {prompt, completion}}: the console's JSON encoder
    masks every key that looks like a credential (anything with "token" in it), and these are counts, not secrets."""
    out = {k: v for k, v in r.items() if k not in ("promptTokens", "completionTokens")}
    out["usage"] = {"prompt": r.get("promptTokens"), "completion": r.get("completionTokens")}
    return out


def _int_param(req: Request, name: str) -> int:
    v = req.params.get(name) or ""
    if not re.fullmatch(r"[0-9]{1,12}", v):
        raise ValueError(f"{name} must be a positive integer")
    return int(v)


# --- fetchers (cache/hub) -------------------------------------------------------------------------------------------------

def _agent_row(a: dict) -> dict:
    row = {k: v for k, v in a.items() if k not in ("systemPrompt", "toolsJson", "maxTokens")}
    row["max_output"] = a.get("maxTokens")
    try:
        tools = json.loads(a.get("toolsJson") or "[]")
        row["tools"] = len(tools) if isinstance(tools, list) else None
    except (TypeError, json.JSONDecodeError):
        row["tools"] = None
    row["system_prompt_chars"] = len(a.get("systemPrompt") or "")
    return row


def fetch_agents(app: server.App):
    okk, data, err, via = trpc_get(app, "agents.list")
    if not okk:
        return False, None, err
    rows = [_agent_row(a) for a in data if isinstance(a, dict)] if isinstance(data, list) else []
    return True, {"agents": rows, "n": len(rows), "enabled": sum(1 for a in rows if a.get("enabled")), "via": via,
                  "via_note": _via_note(via)}, None


def fetch_runs(app: server.App, limit: int = 80):
    okk, data, err, via = trpc_get(app, "runs.list", {"limit": int(limit)})
    if not okk:
        return False, None, err
    rows = [{**_usage(r), "input": _clip(r.get("input"), 600), "output": _clip(r.get("output"), 600),
             "error": _clip(r.get("error"), 600)} for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r.get("status"))] = counts.get(str(r.get("status")), 0) + 1
    return True, {"runs": rows, "n": len(rows), "counts": counts, "via": via, "via_note": _via_note(via),
                  "note": "agent-reported by Mission Control, not compute truth"}, None


def fetch_sessions(app: server.App):
    okk, data, err, via = trpc_get(app, "chat.listSessions")
    if not okk:
        return False, None, err
    rows = [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []
    return True, {"sessions": rows, "n": len(rows), "via": via, "via_note": _via_note(via)}, None


def fetch_missions(app: server.App):
    okk, data, err, via = trpc_get(app, "missions.list")
    if not okk:
        return False, None, err
    rows = []
    for m in data if isinstance(data, list) else []:
        if not isinstance(m, dict):
            continue
        items = [i for i in (m.get("items") or []) if isinstance(i, dict)]
        rows.append({**{k: v for k, v in m.items() if k != "items"}, "objective": _clip(m.get("objective"), 2000),
                     "steps": len(items), "steps_done": sum(1 for i in items if i.get("status") == "done"),
                     "steps_by_status": {s: sum(1 for i in items if i.get("status") == s)
                                         for s in sorted({str(i.get("status")) for i in items})}})
    counts: dict[str, int] = {}
    for m in rows:
        counts[str(m.get("status"))] = counts.get(str(m.get("status")), 0) + 1
    return True, {"missions": rows, "n": len(rows), "counts": counts, "via": via, "via_note": _via_note(via),
                  "note": "agent-reported by Mission Control, not compute truth"}, None


def fetch_downloads(app: server.App):
    base = _gc_base(app)
    if not base:
        return False, None, "not configured: server.grid_control is unset"
    status, obj, err = httpc.get_json(base + "/downloads-status.json", READ_TIMEOUT_S)
    if err:
        return False, None, f"GRID CONTROL /downloads-status.json: {err}"
    if not isinstance(obj, dict):
        return False, None, "GRID CONTROL /downloads-status.json: the reply is not a JSON object"
    return True, obj, None


def fetch_redteam(app: server.App):
    status, obj, err, via = rig.gc_read(app, "/api/redteam/status", READ_TIMEOUT_S)
    if err:
        return False, None, f"GRID CONTROL /api/redteam/status: {err}"
    if not isinstance(obj, dict):
        return False, None, "GRID CONTROL /api/redteam/status: the reply is not a JSON object"
    return True, {**obj, "via": via, "via_note": _via_note(via)}, None


# --- read routes ------------------------------------------------------------------------------------------------------------

def h_agents(req: Request) -> Response:
    return envelope(req.app.hub.get("mc.agents"))


def h_runs(req: Request) -> Response:
    try:
        limit = max(1, min(int(req.q("limit", "80") or 80), 200))
    except ValueError:
        return error(400, "limit must be an integer", "bad-request")
    if limit == 80:
        return envelope(req.app.hub.get("mc.runs"))
    return envelope(req.app.cache.get(f"mc.runs:{limit}", 10.0, lambda: fetch_runs(req.app, limit),
                                      source="GRID CONTROL Mission Control runs.list"))


def h_run(req: Request) -> Response:
    try:
        rid = _int_param(req, "id")
    except ValueError as e:
        return error(400, str(e), "bad-request")

    def fetch():
        okk, data, err, via = trpc_get(req.app, "runs.get", {"id": rid})
        if not okk:
            return False, None, err
        if not isinstance(data, dict):
            return False, None, f"Mission Control runs.get: run {rid} not found"
        steps = [s for s in (data.get("steps") or []) if isinstance(s, dict)]
        out = {**_usage(data), "input": _clip(data.get("input"), STEP_TEXT_CAP), "output": _clip(data.get("output"), STEP_TEXT_CAP),
               "error": _clip(data.get("error"), STEP_TEXT_CAP), "steps_total": len(steps),
               "steps": [{**s, "contentJson": _clip(s.get("contentJson"), STEP_TEXT_CAP)} for s in steps[-STEP_CAP:]],
               "via": via, "via_note": _via_note(via), "note": "agent-reported by Mission Control, not compute truth"}
        return True, out, None
    return envelope(req.app.cache.get(f"mc.run:{rid}", 5.0, fetch, source="GRID CONTROL Mission Control runs.get"))


def h_sessions(req: Request) -> Response:
    return envelope(req.app.hub.get("mc.sessions"))


def _session_entry(app: server.App, sid: int, force: bool = False):
    def fetch():
        okk, data, err, via = trpc_get(app, "chat.getSession", {"id": sid})
        if not okk:
            return False, None, err
        if not isinstance(data, dict):
            return False, None, f"Mission Control chat.getSession: session {sid} not found"
        msgs = [m for m in (data.get("messages") or []) if isinstance(m, dict)]
        return True, {**data, "messages_total": len(msgs),
                      "messages": [{**m, "content": _clip(m.get("content"), MSG_TEXT_CAP)} for m in msgs[-MSG_CAP:]],
                      "via": via, "via_note": _via_note(via)}, None
    return app.cache.get(f"mc.session:{sid}", 3.0, fetch, source="GRID CONTROL Mission Control chat.getSession", force=force)


def h_session(req: Request) -> Response:
    try:
        sid = _int_param(req, "id")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    return envelope(_session_entry(req.app, sid))


def h_missions(req: Request) -> Response:
    return envelope(req.app.hub.get("mc.missions"))


def h_mission(req: Request) -> Response:
    try:
        mid = _int_param(req, "id")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    app = req.app

    def fetch():
        okk, data, err, via = trpc_get(app, "missions.get", {"id": mid})
        if not okk:
            return False, None, err
        if not isinstance(data, dict):
            return False, None, f"Mission Control missions.get: mission {mid} not found"
        runs: dict[str, Any] = {}
        for item in data.get("items") or []:
            rid = item.get("runId") if isinstance(item, dict) else None
            if isinstance(rid, int) and str(rid) not in runs and len(runs) < 40:
                rok, run, rerr, _ = trpc_get(app, "runs.get", {"id": rid})
                runs[str(rid)] = ({k: _clip(run.get(k), 2500) for k in ("id", "status", "output", "error", "model")}
                                  if rok and isinstance(run, dict) else {"id": rid, "read_error": rerr})
        return True, {**data, "runs": runs, "via": via, "via_note": _via_note(via),
                      "note": "agent-reported by Mission Control, not compute truth"}, None
    return envelope(app.cache.get(f"mc.mission:{mid}", 5.0, fetch, source="GRID CONTROL Mission Control missions.get"))


def h_downloads(req: Request) -> Response:
    return envelope(req.app.hub.get("rig.downloads"))


def h_redteam(req: Request) -> Response:
    return envelope(req.app.hub.get("rig.redteam"))


def _proxy_bytes(req: Request, path: str, what: str, attachment: bool) -> Response:
    app = req.app
    token, base = _token(app), _gc_base(app)
    if token and base:
        url, headers = base + path, {"x-gc-token": token}
    else:
        cbase = rig._console_base(app)
        if not cbase:
            return error(503, "not configured: no GRID CONTROL token and server.simlab_console is unset", "not-configured")
        url, headers = cbase + "/gc" + path, {}
    try:
        status, hdrs, it = httpc.stream(url, READ_TIMEOUT_S * 4, headers=headers)
    except httpc.HttpcError as exc:
        return error(502, f"GRID CONTROL unreachable for {what}: {tokens.scrub(str(exc), token)[:200]}", "upstream")
    chunks, total = [], 0
    for chunk in it:
        total += len(chunk)
        if total > FILE_CAP:
            return error(413, f"{what} exceeds the console file cap of {FILE_CAP} bytes", "too-large")
        chunks.append(chunk)
    body = b"".join(chunks)
    if status != 200:
        return error(404 if status == 404 else 502, f"{what}: HTTP {status}", "not-found" if status == 404 else "upstream",
                     upstream_status=status)
    h = {k.lower(): v for k, v in hdrs.items()}
    ctype = (h.get("content-type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    disp = h.get("content-disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disp)
    name = SAFE_NAME.sub("_", urllib.parse.unquote(m.group(1)) if m else what)[:120] or "file"
    inline = not attachment and (ctype.startswith("image/") or ctype in ("text/plain", "application/pdf", "application/json"))
    headers_out = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff",
                   "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
                   "Cache-Control": "private, max-age=60"}
    if ctype.startswith("text/") or ctype == "application/json":
        ctype += "; charset=utf-8"
    return Response(status=200, body=body, content_type=ctype, headers=headers_out)


def h_upload_get(req: Request) -> Response:
    try:
        uid = _int_param(req, "id")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    return _proxy_bytes(req, f"/api/mc/uploads/{uid}", f"upload-{uid}", attachment=False)


def h_artifact_get(req: Request) -> Response:
    try:
        aid = _int_param(req, "id")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    return _proxy_bytes(req, f"/api/mc/artifacts/{aid}/download", f"artifact-{aid}", attachment=True)


# --- write routes (human POSTs, ledgered) --------------------------------------------------------------------------------------

def _write_reply(app: server.App, kind: str, payload: dict, status: int, data: Any, err: str | None,
                 invalidate: tuple[str, ...] = ()) -> Response:
    payload = {**payload, "ok": err is None, "error": err}
    # a write refused before it reached the rig (no token on this desktop) is not an action: nothing is ledgered
    n = None if status == 503 else _record(app, kind, payload)
    for key in invalidate:
        if key in app.hub.topics():
            try:
                app.hub.refresh(key)
            except Exception as exc:  # noqa: BLE001
                app.log(f"refresh of {key} after {kind} failed: {exc}")
        else:
            app.cache.invalidate(key)
    if err:
        code = "not-configured" if status == 503 else "upstream"
        return error(503 if status == 503 else 502, err, code, upstream_status=None if status == 503 else status)
    return ok({"result": data, "ledger_n": n})


def _uploads_arg(body: dict) -> list[int]:
    ups = body.get("uploads") or []
    if not isinstance(ups, list) or not all(isinstance(u, int) and u >= 0 for u in ups) or len(ups) > 50:
        raise ValueError("uploads must be a list of at most 50 upload ids")
    return ups


def h_session_create(req: Request) -> Response:
    body = req.json or {}
    title = body.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > 200):
        return error(400, "title must be a string of at most 200 characters", "bad-request")
    title = (title or "").strip() or "session " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    status, data, err = trpc_post(req.app, "chat.createSession", {"title": title})
    sid = data.get("id") or data.get("sessionId") if isinstance(data, dict) else None
    return _write_reply(req.app, "console.mc.session", {"title": title, "id": sid}, status, data, err, ("mc.sessions",))


def h_message(req: Request) -> Response:
    try:
        sid = _int_param(req, "id")
        body = req.json or {}
        content = body.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 48_000:
            raise ValueError("content must be a non-empty string of at most 48000 characters")
        ups = _uploads_arg(body)
    except ValueError as e:
        return error(400, str(e), "bad-request")
    status, data, err = trpc_post(req.app, "chat.sendMessage", {"sessionId": sid, "content": content, "uploads": ups})
    payload = {"session": sid, "chars": len(content), "sha256": _sha(content), "uploads": len(ups)}
    req.app.cache.invalidate(f"mc.session:{sid}")
    return _write_reply(req.app, "console.mc.message", payload, status, data, err, ("mc.runs",))


def h_mission_create(req: Request) -> Response:
    body = req.json or {}
    title, objective = body.get("title"), body.get("objective")
    try:
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("title must be a non-empty string of at most 200 characters")
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 48_000:
            raise ValueError("objective must be a non-empty string of at most 48000 characters")
        files = body.get("files") or []
        if not isinstance(files, list) or len(files) > 50 or not all(
                isinstance(f, dict) and isinstance(f.get("id"), int) and isinstance(f.get("filename"), str) for f in files):
            raise ValueError("files must be a list of at most 50 {id, filename} rows from POST /api/mc/upload")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    launch = body.get("launch") is True
    # the manifest GRID CONTROL appends for context files (cudax ui/grid-control/index.html:757), so agents find them
    manifest = ("\n\n[context files]\n" + "\n".join(f"- #{f['id']} {f['filename'][:200]} (fetch: /api/uploads/{f['id']})"
                                                     for f in files)) if files else ""
    status, data, err = trpc_post(req.app, "missions.create", {"title": title.strip(), "objective": objective + manifest})
    mid = None
    if isinstance(data, dict):
        mid = data.get("id") or (data.get("mission") or {}).get("id")
    launch_error = None
    if not err and launch and isinstance(mid, int):
        lstatus, _ldata, launch_error = trpc_post(req.app, "missions.launch", {"id": mid})
    payload = {"title": title.strip()[:200], "objective_chars": len(objective), "objective_sha256": _sha(objective),
               "files": len(files), "launch": launch, "id": mid, "launch_error": launch_error}
    resp = _write_reply(req.app, "console.mc.mission", payload, status, data, err, ("mc.missions",))
    if resp.status == 200 and isinstance(resp.data, dict):
        resp.data.update({"id": mid, "launched": launch and not launch_error, "launch_error": launch_error})
    return resp


def h_mission_action(req: Request) -> Response:
    try:
        mid = _int_param(req, "id")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    action = req.params.get("action") or ""
    if action not in MISSION_ACTIONS:
        return error(400, f"action must be one of {', '.join(MISSION_ACTIONS)}", "bad-request")
    status, data, err = trpc_post(req.app, "missions." + action, {"id": mid})
    req.app.cache.invalidate(f"mc.mission:{mid}")
    return _write_reply(req.app, "console.mc.mission", {"id": mid, "action": action}, status, data, err, ("mc.missions",))


def h_approve(req: Request) -> Response:
    try:
        iid = _int_param(req, "id")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    status, data, err = trpc_post(req.app, "missions.approveItem", {"itemId": iid})
    return _write_reply(req.app, "console.mc.approve", {"item": iid}, status, data, err, ("mc.missions",))


def h_upload(req: Request) -> Response:
    body = req.json or {}
    name, mime, b64 = body.get("filename"), body.get("mime") or "application/octet-stream", body.get("data_b64")
    try:
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or "/" in name or "\\" in name:
            raise ValueError("filename must be a plain file name of at most 200 characters")
        if not isinstance(mime, str) or len(mime) > 100 or not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", mime):
            raise ValueError("mime must be a media type such as image/png")
        if not isinstance(b64, str):
            raise ValueError("data_b64 must be the file's bytes in base64")
        data = base64.b64decode(b64, validate=True)
    except (ValueError, binascii.Error) as e:
        return error(400, str(e), "bad-request")
    if len(data) > UPLOAD_CAP:
        return error(413, f"the file exceeds the upload cap of {UPLOAD_CAP} bytes", "too-large")
    token, base = _token(req.app), _gc_base(req.app)
    payload = {"filename": name[:200], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mime": mime}
    if not token or not base:
        return _write_reply(req.app, "console.mc.upload", payload, 503, None, NO_TOKEN)
    status, obj, err = httpc.post_bytes(base + "/api/mc/uploads", data, WRITE_TIMEOUT_S,
                                        headers={"x-gc-token": token, "x-filename": urllib.parse.quote(name),
                                                 "content-type": mime})
    if not err and not (isinstance(obj, dict) and obj.get("id") is not None):
        err = f"Mission Control upload: {(obj or {}).get('error') if isinstance(obj, dict) else 'no id in the reply'}"
    if err:
        err = tokens.scrub(str(err), token)
    payload["id"] = obj.get("id") if isinstance(obj, dict) else None
    return _write_reply(req.app, "console.mc.upload", payload, status or 502, obj, err)


def h_redteam_vm(req: Request) -> Response:
    body = req.json or {}
    action = body.get("action")
    if action not in REDTEAM_CONFIRM:
        return error(400, "action must be up or down", "bad-request")
    if body.get("confirm") != REDTEAM_CONFIRM[action]:
        return error(400, f"confirm must be the words '{REDTEAM_CONFIRM[action]}'", "confirm-mismatch")
    status, obj, err = _gc_post(req.app, "/api/redteam/vm", {"action": action})
    return _write_reply(req.app, "console.redteam.vm", {"action": action}, status, obj, err, ("rig.redteam",))


def h_auto(req: Request) -> Response:
    body = req.json or {}
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return error(400, "enabled must be true or false", "bad-request")
    if body.get("confirm") != AUTO_CONFIRM[enabled]:
        return error(400, f"confirm must be the words '{AUTO_CONFIRM[enabled]}'", "confirm-mismatch")
    status, obj, err = _gc_post(req.app, "/api/auto", {"enabled": enabled})
    return _write_reply(req.app, "console.auto", {"enabled": enabled}, status, obj, err, ("rig.status",))


# --- install ----------------------------------------------------------------------------------------------------------------------

def install(register) -> None:
    app = server.current_app()
    hub = app.hub
    gc = _gc_base(app) or "grid_control unset"
    readable = lambda: rig.gc_readable(app)  # noqa: E731
    hub.register("mc.runs", lambda: fetch_runs(app), 10.0, ("agents",), source=f"{gc}{TRPC}runs.list", enabled=readable)
    hub.register("mc.missions", lambda: fetch_missions(app), 15.0, ("agents",), source=f"{gc}{TRPC}missions.list",
                 enabled=readable)
    hub.register("mc.sessions", lambda: fetch_sessions(app), 30.0, ("agents",), source=f"{gc}{TRPC}chat.listSessions",
                 enabled=readable)
    hub.register("mc.agents", lambda: fetch_agents(app), 60.0, ("agents",), source=f"{gc}{TRPC}agents.list",
                 enabled=readable)
    hub.register("rig.redteam", lambda: fetch_redteam(app), 30.0, ("ops",), source=f"{gc}/api/redteam/status",
                 enabled=readable)
    hub.register("rig.downloads", lambda: fetch_downloads(app), 60.0, ("ops",), source=f"{gc}/downloads-status.json")
    server.allow_body("/api/mc/upload", UPLOAD_CAP * 4 // 3 + 4096)
    register("GET", "/api/mc/agents", h_agents)
    register("GET", "/api/mc/runs", h_runs)
    register("GET", "/api/mc/run/{id}", h_run)
    register("GET", "/api/mc/sessions", h_sessions)
    register("GET", "/api/mc/session/{id}", h_session)
    register("POST", "/api/mc/session", h_session_create)
    register("POST", "/api/mc/session/{id}/message", h_message)
    register("GET", "/api/mc/missions", h_missions)
    register("GET", "/api/mc/mission/{id}", h_mission)
    register("POST", "/api/mc/mission", h_mission_create)
    register("POST", "/api/mc/mission/{id}/{action}", h_mission_action)
    register("POST", "/api/mc/item/{id}/approve", h_approve)
    register("POST", "/api/mc/upload", h_upload)
    register("GET", "/api/mc/upload/{id}", h_upload_get)
    register("GET", "/api/mc/artifact/{id}", h_artifact_get)
    register("GET", "/api/rig/downloads", h_downloads)
    register("GET", "/api/rig/redteam", h_redteam)
    register("POST", "/api/rig/redteam/vm", h_redteam_vm)
    register("POST", "/api/rig/auto", h_auto)
