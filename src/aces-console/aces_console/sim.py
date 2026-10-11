"""SIMLAB console, simlab-mcp and MLflow proxies (build spec 3.6 and 3.7). Builder B.

Every route is a read. The SIMLAB console (cfg.server.simlab_console, :8032 on the rig) is the rig's read-only data
plane; this module passes its JSON through unchanged and adds only labelled derivations:

    /api/sim/summary                       passthrough; the console's own `mlflow` link is shown as reported, never used
    /api/sim/capacity                      passthrough + per-GPU `activity` label + the gridcontrol_queue -1 normalisation
    /api/sim/experiments                   passthrough + queue_by_rq_status and queue_by_meta_state (two scales, never merged)
    /api/sim/experiments/{eid}?root=       passthrough (404 passthrough: {"error": "unknown experiment"})
    /api/sim/case/{eid}/{cid}?root=        passthrough + mlflow {run, ui_url, error} when mlflow_run_id is set
    /api/sim/file?eid=&root=&cid=&name=    the console's /api/file?p= with p resolved from the cached case record only
    /api/sim/file?eid=&root=&report=       ... or from the cached experiment record's reports[]
    /api/sim/registry                      rows + citable / citable_note + lane ("not recorded" when queue is absent)
    /api/sim/evidence, /evidence/verify, /audit   passthrough
    /api/sim/status/{eid}                  simlab-mcp sim_status through the copied stdlib MCP client (bearer server-side)
    /api/sim/whoami                        simlab-mcp sim_whoami + token_source
    /api/mlflow/experiments                MLflow experiments/search passthrough
    /api/mlflow/run/{run_id}               MLflow runs/get (run.info, run.data.{params,metrics,tags}) + ui_url
    /api/mlflow/runs?experiment_id=&max=   MLflow runs/search (a POST query upstream; replayed as the capture in fixtures)

Honest absence: a console that cannot be reached answers `data:null|stale` with "SIMLAB console unreachable at <url>:
<reason>"; the console's own wordings (`queue_error`, "not found or outside the output roots", {"error": "unknown
experiment"|"unknown case"}) pass through unchanged. No token is ever logged, echoed or returned (whoami reports only
`token_source`); the file route never logs the path or the query and never forwards an arbitrary `p`.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import time
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from aces import ledger

from . import __version__, cache as cache_mod, httpc, secrets as tokens, server
from .server import Request, Response, envelope, error, ok

CONSOLE_TIMEOUT_S = 8.0
FILE_TIMEOUT_S = 120.0
MLFLOW_TIMEOUT_S = 8.0
MCP_TIMEOUT_S = 15.0
FILE_CAP = 64 * 1024 * 1024
MCP_PROTOCOL = "2025-06-18"
QUEUE_CAP = 300  # the console's own cap (cudax simlab/console/app.py:306-311)
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
CITABLE_NOTE = "pass; image id not recorded, not citable against a running image"
LANE_NOT_RECORDED = "not recorded"
MLFLOW_LINK_NOTE = "as the SIMLAB console reports it; this console never uses it as a link"
SCALES_NOTE = ("two scales: rq job status (queue_by_rq_status) and the job meta state (queue_by_meta_state); "
               "they are never merged")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SimState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_whoami: dict[str, Any] | None = None


def _state(app: server.App) -> SimState:
    st = app.state.get("sim")
    if st is None:
        st = app.state["sim"] = SimState()
    return st


# --- bases and the error vocabulary ------------------------------------------------------------------------------------

def console_base(app: server.App) -> str | None:
    base = app.settings.server.get("simlab_console")
    return base.rstrip("/") if isinstance(base, str) and base else None


def mlflow_base(app: server.App) -> str | None:
    base = app.settings.server.get("mlflow")
    return base.rstrip("/") if isinstance(base, str) and base else None


def mcp_url(app: server.App) -> str | None:
    url = app.settings.server.get("simlab_mcp")
    return url if isinstance(url, str) and url else None


def _reason(err: str) -> str:
    return err[len("unreachable: "):] if err.startswith("unreachable: ") else err


def console_get(app: server.App, path: str) -> tuple[int, Any, str | None]:
    """GET <simlab_console><path>: (status, obj, error) with the console's absence wording."""
    base = console_base(app)
    if not base:
        return 0, None, "not configured: server.simlab_console is unset"
    status, obj, err = httpc.get_json(base + path, CONSOLE_TIMEOUT_S)
    if err and status == 0:
        return 0, None, f"SIMLAB console unreachable at {base}: {_reason(err)}"
    return status, obj, err


def _passthrough(app: server.App, path: str, shape: type = dict):
    status, obj, err = console_get(app, path)
    if err:
        return False, None, err
    if not isinstance(obj, shape):
        return False, None, f"SIMLAB console {path}: the reply is not a JSON {shape.__name__}"
    return True, obj, None


# --- derivations --------------------------------------------------------------------------------------------------------

def normalise_queue(q: Any) -> tuple[dict[str, Any], str | None]:
    """GRID CONTROL queue counts as the console relays them: counts below zero become null with a note (spec 3.6)."""
    counts: dict[str, Any] = {"queued": None, "running": None}
    if not isinstance(q, dict):
        return counts, None if q is None else f"GRID CONTROL reported {str(q)[:60]}"
    bad: list[str] = []
    for k in ("queued", "running"):
        v = q.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            if v is not None:
                bad.append(f"{k}={str(v)[:40]}")
            continue
        if v < 0:
            bad.append(str(int(v)) if float(v).is_integer() else str(v))
        else:
            counts[k] = v
    note = ("GRID CONTROL reported " + ", ".join(sorted(set(bad), key=bad.index))) if bad else None
    return counts, note


def gpu_activity(g: dict[str, Any], mode: Any, modes: Any) -> str:
    """The activity label of one capacity row (spec 3.6), in this order: job, lease, outside the queue, rests by mode,
    not in the sim pool, derated, idle."""
    if g.get("job"):
        return "job"
    if g.get("lease"):
        return "lease"
    procs = g.get("processes") or []
    if g.get("in_pool") and not g.get("lease") and not g.get("job") and len(procs) > 0:
        return "outside the queue (operator hand-run)"
    pool = (modes or {}).get(mode) if isinstance(modes, dict) and isinstance(mode, str) else None
    rest = pool.get("rest") if isinstance(pool, dict) else None
    if isinstance(rest, list) and g.get("index") in rest:
        return f"rests by mode {mode}"
    if not g.get("in_pool"):
        return "not in the sim pool"
    if g.get("derated"):
        return "derated"
    return "idle"


def decorate_capacity(table: dict[str, Any]) -> dict[str, Any]:
    out = dict(table)
    mode, modes = table.get("mode"), table.get("modes")
    gpus = []
    for g in table.get("gpus") or []:
        if isinstance(g, dict):
            row = dict(g)
            row["activity"] = gpu_activity(g, mode, modes)
            gpus.append(row)
        else:
            gpus.append(g)
    out["gpus"] = gpus
    counts, note = normalise_queue(table.get("gridcontrol_queue"))
    out["gridcontrol_queue"] = counts
    out["raw_gridcontrol_queue"] = table.get("gridcontrol_queue")
    out["gridcontrol_queue_note"] = note
    out["activity_counts"] = {}
    for g in gpus:
        if isinstance(g, dict):
            out["activity_counts"][g["activity"]] = out["activity_counts"].get(g["activity"], 0) + 1
    return out


def decorate_experiments(obj: dict[str, Any]) -> dict[str, Any]:
    out = dict(obj)
    jobs = obj.get("queue") if isinstance(obj.get("queue"), list) else []
    by_rq: dict[str, int] = {}
    by_meta: dict[str, int] = {}
    for j in jobs:
        if not isinstance(j, dict):
            continue
        rq = str(j.get("status"))
        by_rq[rq] = by_rq.get(rq, 0) + 1
        meta = j.get("meta") if isinstance(j.get("meta"), dict) else {}
        ms = str(meta.get("state"))
        by_meta[ms] = by_meta.get(ms, 0) + 1
    exps = obj.get("experiments") if isinstance(obj.get("experiments"), list) else []
    out["queue_by_rq_status"] = by_rq
    out["queue_by_meta_state"] = by_meta
    out["queue_n"] = len(jobs)
    out["queue_cap"] = QUEUE_CAP
    out["queue_capped"] = len(jobs) >= QUEUE_CAP
    out["queue_scales_note"] = SCALES_NOTE
    out["experiments_n"] = len(exps)
    out["cases_n"] = sum(int(e.get("n_cases") or 0) for e in exps if isinstance(e, dict))
    return out


def decorate_registry(reg: dict[str, Any]) -> dict[str, Any]:
    rows = []
    counts = {"pass": 0, "fail": 0, "other": 0, "citable": 0, "lane_recorded": 0}
    for name, row in reg.items():
        if not isinstance(row, dict):
            continue
        v = row.get("validation") if isinstance(row.get("validation"), dict) else {}
        status = v.get("status")
        image_id = v.get("image_id")
        citable = status == "pass" and bool(image_id)
        if citable:
            note = "pass"
        elif status == "pass":
            note = CITABLE_NOTE
        else:
            note = str(status) if status is not None else LANE_NOT_RECORDED
        lane = row.get("queue")
        out = dict(row)
        out.update({"name": name, "validation_status": status if status is not None else LANE_NOT_RECORDED,
                    "validation_word": status if status in ("pass", "fail") else LANE_NOT_RECORDED,
                    "image_id": image_id, "citable": citable, "citable_note": note,
                    "lane": lane if isinstance(lane, str) and lane else LANE_NOT_RECORDED,
                    "lane_recorded": isinstance(lane, str) and bool(lane)})
        rows.append(out)
        if status == "pass":
            counts["pass"] += 1
        elif status == "fail":
            counts["fail"] += 1
        else:
            counts["other"] += 1
        counts["citable"] += 1 if citable else 0
        counts["lane_recorded"] += 1 if out["lane_recorded"] else 0
    return {"solvers": rows, "n": len(rows), "counts": counts,
            "citable_rule": "citable only when validation.status == pass and validation.image_id is recorded "
                            "(cudax docs/GOLDEN_RIG.md:106-109)",
            "lane_note": "lane is the row's `queue`; absent rows read \"not recorded\", never a default"}


# --- fetchers (each returns (ok, data, error) for the cache) ---------------------------------------------------------------

def fetch_summary(app: server.App):
    ok_, obj, err = _passthrough(app, "/api/summary")
    if not ok_:
        return False, None, err
    data = dict(obj)
    data["notes"] = {"mlflow": MLFLOW_LINK_NOTE,
                     "scales": "cases counts experiment cases; queue_counts counts rq jobs: two scales"}
    data["queue_jobs_total"] = sum(v for v in (obj.get("queue_counts") or {}).values() if isinstance(v, int))
    return True, data, None


def fetch_capacity(app: server.App):
    ok_, obj, err = _passthrough(app, "/api/capacity")
    if not ok_:
        return False, None, err
    return True, decorate_capacity(obj), None


def fetch_experiments(app: server.App):
    ok_, obj, err = _passthrough(app, "/api/experiments")
    if not ok_:
        return False, None, err
    return True, decorate_experiments(obj), None


def fetch_registry(app: server.App):
    ok_, obj, err = _passthrough(app, "/api/registry")
    if not ok_:
        return False, None, err
    return True, decorate_registry(obj), None


def fetch_evidence(app: server.App):
    ok_, obj, err = _passthrough(app, "/api/evidence")
    if not ok_:
        return False, None, err
    data = dict(obj)
    rows = obj.get("ledger") if isinstance(obj.get("ledger"), list) else []
    data["n"] = len(rows)
    data["cap"] = 200
    return True, data, None


def fetch_evidence_verify(app: server.App):
    return _passthrough(app, "/api/evidence/verify")


def fetch_audit(app: server.App):
    ok_, obj, err = _passthrough(app, "/api/audit")
    if not ok_:
        return False, None, err
    data = dict(obj)
    rows = obj.get("audit") if isinstance(obj.get("audit"), list) else []
    data["n"] = len(rows)
    data["cap"] = 400
    return True, data, None


def _query(root: str | None) -> str:
    return ("?" + urllib.parse.urlencode({"root": root})) if root else ""


def _record_key(kind: str, *parts: str) -> str:
    return f"sim.{kind}:" + "|".join(parts)


def _check_key(value: Any, what: str) -> str:
    if not isinstance(value, str) or not KEY_RE.match(value):
        raise ValueError(f"{what} {str(value)[:40]!r} is not a plain identifier")
    return value


def experiment_entry(app: server.App, eid: str, root: str | None) -> cache_mod.Entry:
    base = console_base(app)
    path = f"/api/experiments/{urllib.parse.quote(eid, safe='')}{_query(root)}"

    def fetch():
        status, obj, err = console_get(app, path)
        if status == 404:
            return False, None, (obj.get("error") if isinstance(obj, dict) and obj.get("error") else "HTTP 404")
        if err:
            return False, None, err
        if not isinstance(obj, dict):
            return False, None, "SIMLAB console: the experiment record is not a JSON object"
        return True, obj, None

    return app.cache.get(_record_key("experiment", eid, root or ""), 3.5, fetch, source=f"{base}{path}" if base else None)


def case_entry(app: server.App, eid: str, cid: str, root: str | None) -> cache_mod.Entry:
    base = console_base(app)
    path = f"/api/case/{urllib.parse.quote(eid, safe='')}/{urllib.parse.quote(cid, safe='')}{_query(root)}"

    def fetch():
        status, obj, err = console_get(app, path)
        if status == 404:
            return False, None, (obj.get("error") if isinstance(obj, dict) and obj.get("error") else "HTTP 404")
        if err:
            return False, None, err
        if not isinstance(obj, dict):
            return False, None, "SIMLAB console: the case record is not a JSON object"
        data = dict(obj)
        run_id = obj.get("mlflow_run_id")
        if isinstance(run_id, str) and run_id:
            data["mlflow"] = mlflow_run_block(app, run_id)
        else:
            data["mlflow"] = {"run": None, "ui_url": None, "error": None, "note": "no mlflow_run_id on this case"}
        return True, data, None

    return app.cache.get(_record_key("case", eid, cid, root or ""), 3.5, fetch, source=f"{base}{path}" if base else None)


# --- MLflow ----------------------------------------------------------------------------------------------------------------

def _mlflow_get(app: server.App, path: str) -> tuple[int, Any, str | None, str | None]:
    """(status, obj, error, verbatim_text): the body text of a non-JSON error reply (a 403 "Invalid Host header"
    from the compose proxy, cudax docker/compose.yaml:26-32) is fetched again as text so it can be shown verbatim."""
    base = mlflow_base(app)
    if not base:
        return 0, None, "not configured: server.mlflow is unset", None
    url = base + path
    status, obj, err = httpc.get_json(url, MLFLOW_TIMEOUT_S)
    if err and status == 0:
        return 0, None, f"MLflow unreachable at {base}: {_reason(err)}", None
    text = None
    if err and obj is None and status >= 400 and httpc.fixtures() is None:
        try:
            st, _hdrs, it = httpc.stream(url, MLFLOW_TIMEOUT_S)
            raw = b"".join(it)[:2048]
            text = raw.decode("utf-8", "replace").strip() or None
        except httpc.HttpcError:
            text = None
    return status, obj, err, text


def _mlflow_error(status: int, obj: Any, err: str | None, text: str | None) -> str:
    """One line: MLflow's own JSON error ({error_code, message}) or the proxy's verbatim text when there is one."""
    if isinstance(obj, dict) and (obj.get("error_code") or obj.get("message")):
        return f"HTTP {status}: {obj.get('error_code') or 'error'}: {str(obj.get('message') or '')[:300]}".rstrip(": ")
    if text:
        return f"HTTP {status}: {text}"
    return err or f"HTTP {status}"


def fetch_mlflow_experiments(app: server.App):
    # MLflow 3.16 answers 400 to a bare GET (max_results is required there); 1000 is MLflow's own ceiling
    status, obj, err, text = _mlflow_get(app, "/api/2.0/mlflow/experiments/search?max_results=1000")
    if err:
        return False, None, _mlflow_error(status, obj, err, text)
    if not isinstance(obj, dict):
        return False, None, "MLflow experiments/search: the reply is not a JSON object"
    rows = obj.get("experiments") if isinstance(obj.get("experiments"), list) else []
    return True, {"experiments": rows, "n": len(rows), "page_more": bool(obj.get("next_page_token"))}, None


def mlflow_ui_url(app: server.App, experiment_id: Any, run_id: str) -> str | None:
    base = mlflow_base(app)
    if not base or experiment_id is None:
        return None
    return f"{base}/#/experiments/{urllib.parse.quote(str(experiment_id), safe='')}/runs/{urllib.parse.quote(run_id, safe='')}"


def run_entry(app: server.App, run_id: str) -> cache_mod.Entry:
    base = mlflow_base(app)
    path = "/api/2.0/mlflow/runs/get?" + urllib.parse.urlencode({"run_id": run_id})

    def fetch():
        status, obj, err, text = _mlflow_get(app, path)
        if err:
            if status and status >= 400:
                detail = _mlflow_error(status, obj, err, text)
                return False, None, f"not available: {detail}"
            return False, None, err
        run = obj.get("run") if isinstance(obj, dict) else None
        if not isinstance(run, dict) or not isinstance(run.get("info"), dict):
            return False, None, "MLflow run: shape not confirmed (no run.info in the reply)"
        info = run["info"]
        data = run.get("data") if isinstance(run.get("data"), dict) else {}
        return True, {"run": {"info": info, "data": {"params": data.get("params") or [], "metrics": data.get("metrics") or [],
                                                   "tags": data.get("tags") or []},
                              "inputs": run.get("inputs"), "outputs": run.get("outputs")},
                      "ui_url": mlflow_ui_url(app, info.get("experiment_id"), run_id), "run_id": run_id}, None

    return app.cache.get(f"mlflow.run:{run_id}", 30.0, fetch, source=f"{base}{path}" if base else None)


def mlflow_run_block(app: server.App, run_id: str) -> dict[str, Any]:
    """The {run, ui_url, error} block of a case record (spec 3.6), from the 30 s run cache."""
    if not RUN_ID_RE.match(run_id):
        return {"run": None, "ui_url": None, "error": f"mlflow_run_id {run_id[:40]!r} is not a 32-hex run id"}
    e = run_entry(app, run_id)
    if e.data is None:
        return {"run": None, "ui_url": None, "error": e.error or "not available", "run_id": run_id}
    return {"run": e.data["run"], "ui_url": e.data["ui_url"], "error": None, "run_id": run_id, "stale": e.stale}


def runs_entry(app: server.App, experiment_id: str, max_results: int) -> cache_mod.Entry:
    base = mlflow_base(app)
    path = "/api/2.0/mlflow/runs/search"

    def fetch():
        if not base:
            return False, None, "not configured: server.mlflow is unset"
        url = base + path
        if httpc.fixtures() is not None:  # the capture is replayed for GET of the search path
            status, obj, err = httpc.get_json(url, MLFLOW_TIMEOUT_S)
        else:
            body = {"experiment_ids": [experiment_id], "max_results": max_results, "order_by": ["attributes.start_time DESC"]}
            status, obj, err = httpc.post_json(url, body, MLFLOW_TIMEOUT_S)
        if err and status == 0:
            return False, None, f"MLflow unreachable at {base}: {_reason(err)}" if not err.startswith(("fixture", "not captured")) else err
        if err:
            return False, None, f"not available: {err}"
        runs = obj.get("runs") if isinstance(obj, dict) and isinstance(obj.get("runs"), list) else None
        if runs is None:
            return False, None, "MLflow runs/search: the reply carries no runs list"
        rows = []
        for r in runs:
            if not isinstance(r, dict) or not isinstance(r.get("info"), dict):
                continue
            info = r["info"]
            rid = info.get("run_id") or info.get("run_uuid")
            data = r.get("data") or {}
            tags = {t.get("key"): t.get("value") for t in data.get("tags") or [] if isinstance(t, dict)}
            rows.append({"info": info, "ui_url": mlflow_ui_url(app, info.get("experiment_id"), str(rid)) if rid else None,
                         "n_metrics": len(data.get("metrics") or []), "n_params": len(data.get("params") or []),
                         "run_name": info.get("run_name") or tags.get("mlflow.runName"),
                         "metrics": [m for m in data.get("metrics") or [] if isinstance(m, dict)][:60],
                         "params": [m for m in data.get("params") or [] if isinstance(m, dict)][:60]})
        return True, {"experiment_id": experiment_id, "runs": rows, "n": len(rows), "max_results": max_results,
                      "page_more": bool(obj.get("next_page_token"))}, None

    return app.cache.get(f"mlflow.runs:{experiment_id}:{max_results}", 30.0, fetch, source=f"{base}{path}" if base else None)


# --- the copied stdlib MCP client (src/aces-engineering/lib/aces_engineering/mcp_client.py:104-182) -----------------------------

class McpError(RuntimeError):
    """kind: http (status set) | network | rpc | protocol. The message never contains the bearer token."""

    def __init__(self, message: str, *, kind: str = "protocol", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class McpClient:
    """JSON-RPC 2.0 over streamable HTTP with the bearer token as Authorization only. In fixtures mode every call goes
    through httpc.post_json so the 2026-10-11 captures are replayed (initialize is echoed, uncaptured tools are
    "not captured in fixtures")."""

    def __init__(self, url: str, token: str | None, timeout: float = MCP_TIMEOUT_S):
        self.url, self.timeout = url, timeout
        self._token = token
        self.session: str | None = None
        self.protocol: str | None = None
        self._id = 0

    def __repr__(self) -> str:  # never the token
        return f"McpClient({self.url!r}, token {'set' if self._token else 'not set'})"

    def _headers(self) -> dict[str, str]:
        hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._token:
            hdrs["Authorization"] = f"Bearer {self._token}"
        if self.session:
            hdrs["Mcp-Session-Id"] = self.session
        if self.protocol:
            hdrs["MCP-Protocol-Version"] = self.protocol
        return hdrs

    def _post(self, payload: dict) -> Any:
        if httpc.fixtures() is not None:
            status, obj, err = httpc.post_json(self.url, payload, self.timeout, headers=self._headers())
            if err:
                raise McpError(err, kind="network" if status == 0 else "http", status=status or None)
            if "id" not in payload:
                return None
            return self._result_obj(obj, payload["id"])
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"), headers=self._headers(), method="POST")
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:  # noqa: S310 (LAN endpoint from the aces config)
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 (a reset while reading the error body)
                detail = ""
            raise McpError(f"HTTP {exc.code} from {self.url}: {tokens.scrub(detail, self._token)}", kind="http",
                           status=int(exc.code)) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = str(getattr(exc, "reason", exc))[:160]
            raise McpError(f"cannot reach {self.url}: {reason}", kind="network") from None
        return None if "id" not in payload else self._result(body, payload["id"])

    @staticmethod
    def _result(body: str, rid: int) -> Any:
        text = body.strip()
        msgs: list[Any] = []
        if text.startswith(("{", "[")):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise McpError(f"the reply is not JSON ({exc.msg})", kind="protocol") from None
            msgs = data if isinstance(data, list) else [data]
        else:  # text/event-stream: one JSON-RPC message per data: line
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        msgs.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        continue
        for msg in msgs:
            if isinstance(msg, dict) and msg.get("id") == rid:
                return McpClient._result_obj(msg, rid)
        raise McpError(f"no JSON-RPC response for request {rid} in the reply ({len(text)} bytes)", kind="protocol")

    @staticmethod
    def _result_obj(msg: Any, rid: int) -> Any:
        if not isinstance(msg, dict):
            raise McpError("the reply is not a JSON-RPC object", kind="protocol")
        if "error" in msg and msg["error"] is not None:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise McpError(f"JSON-RPC error {err.get('code')}: {str(err.get('message'))[:200]}", kind="rpc")
        if msg.get("id") not in (rid, None) and "result" not in msg:
            raise McpError(f"no JSON-RPC response for request {rid}", kind="protocol")
        return msg.get("result")

    def request(self, method: str, params: dict | None = None) -> Any:
        self._id += 1
        return self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})

    def initialize(self) -> dict:
        res = self.request("initialize", {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                                          "clientInfo": {"name": "aces-console", "version": __version__}}) or {}
        self.protocol = (res.get("protocolVersion") if isinstance(res, dict) else None) or MCP_PROTOCOL
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res if isinstance(res, dict) else {}

    def call(self, name: str, arguments: dict) -> dict:
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        return res if isinstance(res, dict) else {}


def tool_result(res: dict) -> Any:
    """structuredContent.result (the simlab-mcp output schema), else structuredContent, else the first text block."""
    sc = res.get("structuredContent")
    if isinstance(sc, dict):
        return sc.get("result") if "result" in sc and len(sc) == 1 else sc
    for block in res.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                return json.loads(block.get("text") or "")
            except (json.JSONDecodeError, TypeError):
                return block.get("text")
    return None


def mcp_call(app: server.App, name: str, arguments: dict | None = None) -> tuple[bool, Any, str | None, dict[str, Any]]:
    """(ok, result, error, info): one initialised session and one tools/call; the bearer token from
    secrets.simlab_token() is sent as Authorization only and reported as its source name."""
    url = mcp_url(app)
    token, source = tokens.simlab_token(warn=app.log)
    info: dict[str, Any] = {"token_source": source, "server": None, "protocol": None, "url": url}
    if not url:
        return False, None, "not configured: server.simlab_mcp is unset", info
    client = McpClient(url, token)
    try:
        init = client.initialize()
        info["server"] = init.get("serverInfo")
        info["protocol"] = client.protocol
        res = client.call(name, arguments or {})
    except McpError as e:
        return False, None, f"simlab-mcp {e.kind}: {e}", info
    if res.get("isError"):
        texts = [str(b.get("text")) for b in res.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
        return False, None, f"simlab-mcp tool error: {tokens.scrub(' '.join(texts)[:300], token) or name}", info
    return True, tool_result(res), None, info


def fetch_whoami(app: server.App):
    ok_, result, err, info = mcp_call(app, "sim_whoami")
    if not ok_:
        return False, None, err
    if not isinstance(result, dict):
        return False, None, "simlab-mcp sim_whoami: the result is not an object"
    data = dict(result)
    data["token_source"] = info["token_source"]
    data["server"] = info["server"]
    data["protocol"] = info["protocol"]
    data["n_tools"] = len(result.get("tools") or [])
    with _state(app).lock:
        _state(app).last_whoami = {"principal": result.get("principal"), "policy": result.get("policy")}
    return True, data, None


def status_entry(app: server.App, eid: str) -> cache_mod.Entry:
    def fetch():
        ok_, result, err, info = mcp_call(app, "sim_status", {"experiment_id": eid})
        if not ok_:
            return False, None, err
        if not isinstance(result, dict):
            return False, None, "simlab-mcp sim_status: the result is not an object"
        data = dict(result)
        with _state(app).lock:
            who = _state(app).last_whoami
        data["principal"] = who.get("principal") if who else None
        data["principal_note"] = None if who else "principal not known until sim_whoami has answered in this session"
        data["token_source"] = info["token_source"]
        return True, data, None

    return app.cache.get(_record_key("status", eid), 5.0, fetch, source=f"{mcp_url(app)} tools/call sim_status")


# --- handlers ---------------------------------------------------------------------------------------------------------------

def h_summary(req: Request) -> Response:
    return envelope(req.app.hub.get("sim.summary"))


def h_capacity(req: Request) -> Response:
    return envelope(req.app.hub.get("sim.capacity"))


def h_experiments(req: Request) -> Response:
    return envelope(req.app.hub.get("sim.experiments"))


def h_registry(req: Request) -> Response:
    return envelope(req.app.hub.get("sim.registry"))


def h_evidence(req: Request) -> Response:
    return envelope(req.app.hub.get("sim.evidence"))


def h_evidence_verify(req: Request) -> Response:
    return envelope(req.app.hub.get("sim.evidence.verify"))


def h_audit(req: Request) -> Response:
    return envelope(req.app.hub.get("sim.audit"))


def h_whoami(req: Request) -> Response:
    return envelope(req.app.hub.get("sim.whoami"))


def _not_found_passthrough(e: cache_mod.Entry, source: str | None) -> Response | None:
    if e.data is None and e.error in ("unknown experiment", "unknown case"):
        return error(404, e.error, "not-found", source=source, upstream_status=404)
    return None


def h_experiment(req: Request) -> Response:
    try:
        eid = _check_key(req.params.get("eid"), "experiment id")
        root = _check_key(req.q("root"), "root") if req.q("root") else None
    except ValueError as exc:
        return error(400, str(exc), "bad-request")
    e = experiment_entry(req.app, eid, root)
    return _not_found_passthrough(e, e.source) or envelope(e)


def h_case(req: Request) -> Response:
    try:
        eid = _check_key(req.params.get("eid"), "experiment id")
        cid = _check_key(req.params.get("cid"), "case id")
        root = _check_key(req.q("root"), "root") if req.q("root") else None
    except ValueError as exc:
        return error(400, str(exc), "bad-request")
    e = case_entry(req.app, eid, cid, root)
    return _not_found_passthrough(e, e.source) or envelope(e)


def resolve_file(app: server.App, eid: str, root: str | None, cid: str | None, name: str | None,
                 report: str | None) -> tuple[str | None, Response | None]:
    """The console-side absolute path of one file named by the cached case or experiment record, or an error."""
    if report is not None:
        e = experiment_entry(app, eid, root)
        if e.data is None:
            return None, _not_found_passthrough(e, e.source) or error(502, e.error or "experiment record unavailable", "upstream", source=e.source)
        for r in e.data.get("reports") or []:
            if isinstance(r, dict) and r.get("name") == report and isinstance(r.get("path"), str):
                return r["path"], None
        return None, error(404, "not a report of this experiment", "not-found")
    if cid is None or name is None:
        return None, error(400, "query needs eid, cid and name (or eid and report)", "bad-request")
    e = case_entry(app, eid, cid, root)
    if e.data is None:
        return None, _not_found_passthrough(e, e.source) or error(502, e.error or "case record unavailable", "upstream", source=e.source)
    run_dir = e.data.get("run_dir")
    if not isinstance(run_dir, str) or not run_dir:
        return None, error(404, "the case record names no run_dir", "not-found")
    for f in e.data.get("files") or []:
        if isinstance(f, dict) and f.get("name") == name:
            return posixpath.join(run_dir, name), None
    return None, error(404, "not a file of this case", "not-found")


def h_file(req: Request) -> Response:
    app = req.app
    base = console_base(app)
    if not base:
        return error(503, "not configured: server.simlab_console is unset", "not-configured")
    try:
        eid = _check_key(req.q("eid"), "experiment id")
        root = _check_key(req.q("root"), "root") if req.q("root") else None
        cid = _check_key(req.q("cid"), "case id") if req.q("cid") is not None else None
        name = req.q("name")
        report = req.q("report")
        for v, what in ((name, "name"), (report, "report")):
            if v is not None and (not isinstance(v, str) or not v or ".." in v.split("/") or v.startswith("/") or "\\" in v or len(v) > 300):
                raise ValueError(f"{what} {str(v)[:60]!r} is not a plain file name")
    except ValueError as exc:
        return error(400, str(exc), "bad-request")
    p, resp = resolve_file(app, eid, root, cid, name, report)
    if resp is not None:
        return resp
    url = f"{base}/api/file?{urllib.parse.urlencode({'p': p})}"
    try:
        status, hdrs, it = httpc.stream(url, FILE_TIMEOUT_S)
    except httpc.HttpcError as exc:
        return error(502, f"SIMLAB console unreachable at {base}: {_reason(str(exc))}", "upstream", source=base + "/api/file")
    chunks: list[bytes] = []
    total = 0
    for chunk in it:
        total += len(chunk)
        if total > FILE_CAP:
            return error(413, f"the file exceeds the console file cap of {FILE_CAP} bytes", "too-large", source=base + "/api/file")
        chunks.append(chunk)
    body = b"".join(chunks)
    if status != 200:
        text = body[:300].decode("utf-8", "replace").strip()
        code = "not-found" if status == 404 else "upstream"
        return error(404 if status == 404 else 502, text or f"HTTP {status}", code, source=base + "/api/file", upstream_status=status)
    ctype = {k.lower(): v for k, v in hdrs.items()}.get("content-type") or "application/octet-stream"
    ctype = ctype.split(";", 1)[0].strip().lower() or "application/octet-stream"
    if ctype in ("text/html", "application/xhtml+xml", "image/svg+xml", "text/plain", "text/markdown", "text/csv"):
        ctype += "; charset=utf-8"
    base_name = posixpath.basename(p or "file")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", base_name)[:120] or "file"
    headers = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff",
               "Content-Disposition": f'inline; filename="{safe_name}"', "Cache-Control": "private, max-age=60"}
    return Response(status=200, body=body, content_type=ctype, headers=headers)


def h_status(req: Request) -> Response:
    try:
        eid = _check_key(req.params.get("eid"), "experiment id")
    except ValueError as exc:
        return error(400, str(exc), "bad-request")
    return envelope(status_entry(req.app, eid))


def h_mlflow_experiments(req: Request) -> Response:
    return envelope(req.app.hub.get("mlflow.experiments"))


def h_mlflow_run(req: Request) -> Response:
    run_id = req.params.get("run_id") or ""
    if not RUN_ID_RE.match(run_id):
        return error(400, "run_id must be 32 hex characters", "bad-request")
    e = run_entry(req.app, run_id)
    if e.data is None and e.error and e.error.startswith("not available: HTTP 404"):
        return error(404, e.error, "not-found", source=e.source, upstream_status=404)
    return envelope(e)


def h_mlflow_runs(req: Request) -> Response:
    exp = req.q("experiment_id")
    if not isinstance(exp, str) or not re.fullmatch(r"[0-9]{1,12}", exp):
        return error(400, "experiment_id must be a decimal MLflow experiment id", "bad-request")
    try:
        n = int(req.q("max", "50") or 50)
    except ValueError:
        return error(400, "max must be an integer", "bad-request")
    n = max(1, min(n, 500))
    return envelope(runs_entry(req.app, exp, n))


# --- SIMLAB actions: every simlab-mcp tool, from the console (human POSTs; the writes ledgered) ---------------------------------

WRITE_TOOLS = frozenset({"sim_submit", "sim_sweep", "sim_cancel", "sim_put_spec", "sim_put_file", "sim_report",
                         "sim_report_files", "sim_evidence"})
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def fetch_tools(app: server.App):
    url = mcp_url(app)
    if not url:
        return False, None, "not configured: server.simlab_mcp is unset"
    token, source = tokens.simlab_token(warn=app.log)
    client = McpClient(url, token)
    try:
        client.initialize()
        res = client.request("tools/list") or {}
    except McpError as e:
        return False, None, f"simlab-mcp {e.kind}: {e}"
    rows = []
    for t in (res.get("tools") if isinstance(res, dict) else None) or []:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            rows.append({"name": t["name"], "description": str(t.get("description") or "")[:4000],
                         "input_schema": t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {},
                         "write": t["name"] in WRITE_TOOLS})
    return True, {"tools": rows, "n": len(rows), "write_tools": sorted(WRITE_TOOLS), "token_source": source,
                  "note": "the write tools change the rig's queue or files: confirmed by name, ledgered as console.sim.tool"}, None


def _tools_entry(app: server.App) -> cache_mod.Entry:
    return app.cache.get("sim.tools", 300.0, lambda: fetch_tools(app), source=f"{mcp_url(app)} tools/list")


def h_tools(req: Request) -> Response:
    return envelope(_tools_entry(req.app))


def _experiment_of(result: Any) -> str | None:
    if isinstance(result, dict):
        for k in ("experiment_id", "experiment", "eid", "key"):
            v = result.get(k)
            if isinstance(v, (str, int)) and str(v):
                return str(v)[:200]
    return None


def h_tool_call(req: Request) -> Response:
    app = req.app
    name = req.params.get("name") or ""
    body = req.json or {}
    args = body.get("arguments") if body.get("arguments") is not None else {}
    if not TOOL_NAME_RE.match(name):
        return error(400, "the tool name is not a simlab-mcp tool name", "bad-request")
    if not isinstance(args, dict):
        return error(400, "arguments must be a JSON object", "bad-request")
    entry = _tools_entry(app)
    if entry.data is None:
        return error(502, f"simlab-mcp tools/list failed: {entry.error}", "upstream")
    if name not in {t["name"] for t in entry.data.get("tools") or []}:
        return error(404, f"simlab-mcp lists no tool named {name}", "not-found")
    write = name in WRITE_TOOLS
    if write and body.get("confirm") != name:
        return error(400, f"confirm must be the tool name {name} (a write tool)", "confirm-mismatch")
    t0 = time.time()
    okk, result, err, info = mcp_call(app, name, args)
    n = None
    if write:
        payload = {"tool": name, "arg_keys": sorted(args)[:40],
                   "args_sha256": hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
                   "ok": okk, "error": err, "experiment": _experiment_of(result) if okk else None}
        try:
            n, _ = ledger.record("console.sim.tool", payload)
        except (ValueError, TimeoutError, OSError, SystemExit) as e:
            app.log(f"ledger write of console.sim.tool failed: {e}"[:300])
        for key in ("sim.experiments", "sim.summary"):
            app.cache.invalidate(key)
    if not okk:
        return error(502, err or f"simlab-mcp {name} failed", "upstream")
    return ok({"tool": name, "write": write, "result": result, "seconds": round(time.time() - t0, 2),
               "server": info.get("server"), "token_source": info.get("token_source"), "ledger_n": n})


# --- MLflow browser: metric history, artifact listing and artifact bytes (read only) ------------------------------------------

def _mlflow_path_arg(v: Any, required: bool) -> str | None:
    if v is None or v == "":
        if required:
            raise ValueError("path is required")
        return None
    if not isinstance(v, str) or len(v) > 500 or v.startswith("/") or "\\" in v or ".." in v.split("/"):
        raise ValueError("path must be a relative artifact path")
    return v


def h_mlflow_history(req: Request) -> Response:
    run_id, key = req.q("run_id") or "", req.q("key")
    if not RUN_ID_RE.match(run_id):
        return error(400, "run_id must be 32 hex characters", "bad-request")
    if not isinstance(key, str) or not key or len(key) > 250:
        return error(400, "key must be a metric key", "bad-request")
    app = req.app
    path = "/api/2.0/mlflow/metrics/get-history?" + urllib.parse.urlencode(
        {"run_id": run_id, "metric_key": key, "max_results": 25000})

    def fetch():
        status, obj, err, text = _mlflow_get(app, path)
        if err:
            return False, None, _mlflow_error(status, obj, err, text)
        pts = [{"step": m.get("step"), "value": m.get("value"), "timestamp": m.get("timestamp")}
               for m in (obj.get("metrics") if isinstance(obj, dict) else None) or [] if isinstance(m, dict)]
        return True, {"run_id": run_id, "key": key, "points": pts[:25000], "n": len(pts)}, None
    return envelope(app.cache.get(f"mlflow.history:{run_id}:{key}", 30.0, fetch,
                                  source=f"{mlflow_base(app)}/api/2.0/mlflow/metrics/get-history"))


def h_mlflow_artifacts(req: Request) -> Response:
    run_id = req.q("run_id") or ""
    if not RUN_ID_RE.match(run_id):
        return error(400, "run_id must be 32 hex characters", "bad-request")
    try:
        rel = _mlflow_path_arg(req.q("path"), required=False)
    except ValueError as e:
        return error(400, str(e), "bad-request")
    app = req.app
    q = {"run_id": run_id, **({"path": rel} if rel else {})}

    def fetch():
        status, obj, err, text = _mlflow_get(app, "/api/2.0/mlflow/artifacts/list?" + urllib.parse.urlencode(q))
        if err:
            return False, None, _mlflow_error(status, obj, err, text)
        files = [f for f in (obj.get("files") if isinstance(obj, dict) else None) or [] if isinstance(f, dict)]
        return True, {"run_id": run_id, "path": rel, "root_uri": obj.get("root_uri") if isinstance(obj, dict) else None,
                      "files": files, "n": len(files)}, None
    return envelope(app.cache.get(f"mlflow.artifacts:{run_id}:{rel or ''}", 30.0, fetch,
                                  source=f"{mlflow_base(app)}/api/2.0/mlflow/artifacts/list"))


def h_mlflow_artifact(req: Request) -> Response:
    run_id = req.q("run_id") or ""
    if not RUN_ID_RE.match(run_id):
        return error(400, "run_id must be 32 hex characters", "bad-request")
    try:
        rel = _mlflow_path_arg(req.q("path"), required=True)
    except ValueError as e:
        return error(400, str(e), "bad-request")
    base = mlflow_base(req.app)
    if not base:
        return error(503, "not configured: server.mlflow is unset", "not-configured")
    url = base + "/get-artifact?" + urllib.parse.urlencode({"path": rel, "run_uuid": run_id})
    try:
        status, hdrs, it = httpc.stream(url, FILE_TIMEOUT_S)
    except httpc.HttpcError as exc:
        return error(502, f"MLflow unreachable at {base}: {_reason(str(exc))}", "upstream")
    chunks, total = [], 0
    for chunk in it:
        total += len(chunk)
        if total > FILE_CAP:
            return error(413, f"the artifact exceeds the console file cap of {FILE_CAP} bytes", "too-large")
        chunks.append(chunk)
    body = b"".join(chunks)
    if status != 200:
        text = body[:300].decode("utf-8", "replace").strip()
        return error(404 if status == 404 else 502, text or f"HTTP {status}", "not-found" if status == 404 else "upstream",
                     upstream_status=status)
    ctype = ({k.lower(): v for k, v in hdrs.items()}.get("content-type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    inline = ctype.startswith("image/") or ctype in ("text/plain", "application/json", "text/csv", "application/pdf")
    if ctype.startswith("text/") or ctype == "application/json":
        ctype += "; charset=utf-8"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(rel))[:120] or "artifact"
    headers = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff",
               "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
               "Cache-Control": "private, max-age=60"}
    return Response(status=200, body=body, content_type=ctype, headers=headers)


# --- install --------------------------------------------------------------------------------------------------------------

def install(register) -> None:
    app = server.current_app()
    _state(app)
    base = console_base(app) or "simlab_console unset"
    ml = mlflow_base(app) or "mlflow unset"
    hub = app.hub
    hub.register("sim.capacity", lambda: fetch_capacity(app), 5.0, ("fabric", "pipeline", "solvers"), inline=True,
                 source=f"{base}/api/capacity")
    hub.register("sim.summary", lambda: fetch_summary(app), 20.0, ("pipeline", "solvers"), inline=False, source=f"{base}/api/summary")
    hub.register("sim.experiments", lambda: fetch_experiments(app), 4.0, ("pipeline",), inline=False, source=f"{base}/api/experiments")
    hub.register("sim.registry", lambda: fetch_registry(app), 60.0, ("solvers",), inline=False, source=f"{base}/api/registry")
    hub.register("sim.evidence", lambda: fetch_evidence(app), 30.0, ("pipeline", "evidence"), inline=False, source=f"{base}/api/evidence")
    hub.register("sim.evidence.verify", lambda: fetch_evidence_verify(app), 30.0, ("pipeline", "evidence"), inline=False,
                 source=f"{base}/api/evidence/verify")
    hub.register("sim.audit", lambda: fetch_audit(app), 30.0, ("pipeline", "evidence"), inline=False, source=f"{base}/api/audit")
    hub.register("sim.whoami", lambda: fetch_whoami(app), 300.0, ("pipeline", "ops"), inline=False,
                 source=f"{mcp_url(app) or 'simlab_mcp unset'} tools/call sim_whoami")
    hub.register("mlflow.experiments", lambda: fetch_mlflow_experiments(app), 60.0, ("pipeline",), inline=False,
                 source=f"{ml}/api/2.0/mlflow/experiments/search")
    register("GET", "/api/sim/summary", h_summary)
    register("GET", "/api/sim/capacity", h_capacity)
    register("GET", "/api/sim/experiments", h_experiments)
    register("GET", "/api/sim/experiments/{eid}", h_experiment)
    register("GET", "/api/sim/experiment/{eid}", h_experiment)
    register("GET", "/api/sim/case/{eid}/{cid}", h_case)
    register("GET", "/api/sim/file", h_file)
    register("GET", "/api/sim/registry", h_registry)
    register("GET", "/api/sim/evidence", h_evidence)
    register("GET", "/api/sim/evidence/verify", h_evidence_verify)
    register("GET", "/api/sim/audit", h_audit)
    register("GET", "/api/sim/status/{eid}", h_status)
    register("GET", "/api/sim/whoami", h_whoami)
    register("GET", "/api/mlflow/experiments", h_mlflow_experiments)
    register("GET", "/api/mlflow/run/{run_id}", h_mlflow_run)
    register("GET", "/api/mlflow/runs", h_mlflow_runs)
    register("GET", "/api/mlflow/metric-history", h_mlflow_history)
    register("GET", "/api/mlflow/artifacts", h_mlflow_artifacts)
    register("GET", "/api/mlflow/artifact", h_mlflow_artifact)
    register("GET", "/api/sim/tools", h_tools)
    register("POST", "/api/sim/tool/{name}", h_tool_call)
    server.allow_body("/api/sim/tool/sim_put_file", 12 * 1024 * 1024)
