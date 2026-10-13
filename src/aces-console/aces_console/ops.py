"""The ops agent: plain-language questions about the console's own state, answered in the thread.

It is a deterministic intent router checked by loop.h_message BEFORE the design playbook. Every answer is built from
the same sources the plates read — the jobs state of this server, $ACES_WORK golden records, and the cached SIMLAB
console / simlab-mcp reads — and every claim on the card is one of those values, never a guess. What it cannot do it
says, with the list of what it can.

Intents (first match wins):
  transfer   "pull result.stp from exp-..."      simlab-mcp sim_get_file -> <state>/loop/inbox/, ledgered
  find       "find the project I started"        /api/projects rows, substring match on name/class/path
  summarise  "summarise the p6 run"              the run's run.json row + result.json scalar fields
  result     "show the latest result"            newest golden run row + an Evidence deep link
  queue      "why is there a build queue"        the one-at-a-time golden rule + the rig's rq queue counts
  status     "what's going on"                   running/finished jobs of this server + rig summary/capacity
  help       "what can you do"                   the capability list
  fallback   any other question (wh-word or "?") "I can't do that yet" + the capability list

Design requests ("draft a bracket in CAD", "build me a duct") do not match here; they fall through to the playbook.
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from pathlib import Path
from typing import Any

from . import jobs, records, server, sim

INBOX_CAP = 16 * 1024 * 1024
INBOX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
EID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

QUESTION_START = ("what", "whats", "what's", "why", "how", "where", "which", "when", "who", "is ", "are ",
                  "can you", "could you", "do we", "did ")

CAPABILITIES = [
    "status — “what's going on?”: the jobs running on this desktop and the rig's reported state",
    "queue — “why is there a build queue?”: why runs go one at a time and what is waiting",
    "find — “find the project I started”: searches project names, classes and paths",
    "result — “show the latest result [for <project>]”: the newest golden run with its verdict and a deep link",
    "summarise — “summarise the <project> run”: verdict, grade and the recorded metrics of a run",
    "transfer — “pull <file> from <experiment-id>”: copies a rig file into the loop inbox (16 MiB cap)",
    "design — “draft a 6061 bracket for 200 N in CAD”: the plain-language design loop (plan, spec, golden run)",
]


def _card(title: str, lines: list[str], kv: list[tuple[str, Any]] | None = None,
          links: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"title": title, "lines": lines,
            "kv": [[k, v] for k, v in (kv or []) if v is not None],
            "links": links or []}


def _reply(title: str, lines: list[str], kv: list[tuple[str, Any]] | None = None,
           links: list[dict[str, str]] | None = None) -> dict[str, Any]:
    text = "\n".join(lines)
    return {"text": text, "card": _card(title, lines, kv, links)}


# -------------------------------------------------------------------------------------------------- shared reads

def _hub_data(app: server.App, key: str) -> tuple[Any, str | None]:
    """(data, problem) from a hub entry; a missing hub (unit tests) or an unreachable upstream is said plainly."""
    hub = getattr(app, "hub", None)
    if hub is None:
        return None, "the data hub is not running in this process"
    try:
        e = hub.get(key)
    except Exception as exc:  # noqa: BLE001 (the hub must never break a conversation)
        return None, f"{key} could not be read: {type(exc).__name__}"
    data = getattr(e, "data", None)
    if data is None:
        return None, (getattr(e, "error", None) or f"{key} has not answered yet")
    return data, None


def _job_rows(app: server.App) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    st = jobs._state(app)
    with st.lock:
        rows = [j.summary() for j in st.jobs.values()]
    running = [r for r in rows if r["status"] == "running"]
    done = sorted((r for r in rows if r["status"] != "running"),
                  key=lambda r: str(r.get("finished_utc") or ""), reverse=True)
    return running, done


def _projects(app: server.App) -> tuple[list[dict[str, Any]], str | None]:
    try:
        ok_, data, err = records.fetch_projects(app)
    except Exception as exc:  # noqa: BLE001
        return [], f"projects could not be read: {type(exc).__name__}: {exc}"[:200]
    if not ok_ or not isinstance(data, dict):
        return [], err or "projects could not be read"
    return [p for p in data.get("projects") or [] if isinstance(p, dict)], None


def _project_tokens(text: str, stop: set[str]) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in stop and len(t) > 1]


def _scope_project(app: server.App, text: str, stop: set[str]) -> tuple[str | None, list[dict[str, Any]]]:
    """The named project when every kept token appears in one row's name/class/path; else None."""
    projs, _ = _projects(app)
    toks = _project_tokens(text, stop)
    for p in projs:
        hay = f"{p.get('name') or ''} {p.get('class') or ''} {p.get('path') or ''}".lower()
        if toks and all(t in hay for t in toks):
            return p.get("name"), projs
    return None, projs


# -------------------------------------------------------------------------------------------------- intents

def _status(app: server.App) -> dict[str, Any]:
    running, done = _job_rows(app)
    lines: list[str] = []
    kv: list[tuple[str, Any]] = []
    if running:
        for r in running:
            lines.append(f"Running: {r['title']} — {r['duration_s']} s so far, {r['lines']} log lines.")
            kv.append((f"job {r['id']}", f"{r['kind']} · {r['project']} · {r['duration_s']} s"))
    else:
        lines.append("Nothing is running on this desktop right now.")
    if done:
        r = done[0]
        outcome = r.get("result_outcome") or r.get("error") or f"exit {r.get('exit')}"
        lines.append(f"Last finished: {r['title']} — {outcome}.")
    summary, s_err = _hub_data(app, "sim.summary")
    if summary is None:
        lines.append(f"Rig: not known — {s_err}.")
    else:
        qj = summary.get("queue_jobs_total")
        lines.append(f"Rig: {summary.get('cases', '—')} cases recorded"
                     + (f", {qj} job{'s' if qj != 1 else ''} in the rig queue." if isinstance(qj, int) else "."))
    capacity, _ = _hub_data(app, "sim.capacity")
    if isinstance(capacity, dict):
        for g in (capacity.get("gpus") or [])[:4]:
            if isinstance(g, dict) and g.get("name"):
                kv.append((f"gpu {g['name']}", g.get("activity") or g.get("mode") or "—"))
    links = [{"label": "open Fabric", "href": "?plate=fabric"},
             {"label": "open Pipeline", "href": "?plate=pipeline"}]
    return _reply("What's going on", lines, kv, links)


def _queue(app: server.App) -> dict[str, Any]:
    running, done = _job_rows(app)
    lines = ["Golden runs go one at a time on this desktop: a run owns the machine (and the GPU lease on the rig) "
             "until it finishes, so a second run waits rather than contending. That is the queue you see."]
    r = running[0] if running else None
    if r:
        lines.append(f"Right now: {r['title']} holds the slot ({r['duration_s']} s in). "
                     "Your run starts when it exits; nothing is stuck.")
    else:
        lines.append("Right now the slot is free — a confirmed run would start immediately.")
    summary, s_err = _hub_data(app, "sim.summary")
    if summary is None:
        lines.append(f"The rig's own queue is not known from here: {s_err}.")
    else:
        qc = summary.get("queue_counts") or {}
        if isinstance(qc, dict) and qc:
            lines.append("Rig queue (rq jobs, a different scale from cases): " +
                         ", ".join(f"{k} {v}" for k, v in sorted(qc.items())))
    kv = [("desktop slot", r["title"] if r else "free"),
          ("behind it", "not kept — a second start is refused (409 busy) rather than queued")]
    return _reply("Why there is a queue", lines, kv, [{"label": "open Evidence", "href": "?plate=evidence"}])


FIND_STOP = {"find", "the", "project", "me", "i", "started", "my", "where", "is", "was", "that", "for", "a", "an",
             "look", "looking", "search", "locate", "called", "named", "about", "with", "on", "of", "do", "you",
             "remember", "know"}


def _find(app: server.App, text: str) -> dict[str, Any]:
    projs, err = _projects(app)
    if err:
        return _reply("Find a project", [f"I can't read the projects right now: {err}."])
    toks = _project_tokens(text, FIND_STOP)
    hits = []
    for p in projs:
        hay = f"{p.get('name') or ''} {p.get('class') or ''} {p.get('path') or ''}".lower()
        if toks and all(t in hay for t in toks):
            hits.append(p)
    if not hits:
        lines = ["No project matches that. " + (f"The projects I can see: {', '.join(p.get('name') or '?' for p in projs[:8])}."
                                                if projs else "There are no projects under $ACES_WORK yet.")]
        return _reply("Find a project", lines)
    lines, kv, links = [], [], []
    for p in hits[:5]:
        counts = p.get("counts") or {}
        bits = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()) if isinstance(v, int))
        lines.append(f"{p.get('name')} — {p.get('class') or 'class not set'}" + (f" ({bits})" if bits else "") +
                     (f" — unreadable: {p['unreadable']}" if p.get("unreadable") else ""))
        links.append({"label": f"open {p.get('name')}", "href": f"?plate=projects&project={p.get('name')}"})
        kv.append((str(p.get("name")), str(p.get("path"))))
    if len(hits) > 5:
        lines.append(f"…and {len(hits) - 5} more; narrow the name.")
    return _reply(f"Found {len(hits)} project{'s' if len(hits) != 1 else ''}", lines, kv, links)


RESULT_STOP = FIND_STOP | {"show", "see", "view", "open", "latest", "last", "result", "results", "run", "runs",
                           "golden", "outcome", "verdict", "give", "get", "summarise", "summarize", "summary",
                           "findings", "learn", "learned", "tell", "it", "its", "did", "go", "went", "performance"}


def _latest_run(app: server.App, text: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """(row, project_scoped, problem): the newest golden run, scoped to a named project when one matches."""
    project, projs = _scope_project(app, text, RESULT_STOP)
    try:
        rows = records.golden_rows(project)
    except Exception as exc:  # noqa: BLE001
        return None, project, f"golden records could not be read: {type(exc).__name__}: {exc}"[:200]
    runs = rows.get("runs") or []
    if not runs:
        where = f"project {project}" if project else "any project"
        return None, project, f"there are no golden runs for {where} yet"
    return runs[0], project, None


def _result(app: server.App, text: str) -> dict[str, Any]:
    row, project, problem = _latest_run(app, text)
    if row is None:
        return _reply("Latest result", [f"I can't show one: {problem}."])
    lines = [f"{row['project']} · {row['run_id']}" +
             (f" — verdict: {row['verdict']}" if row.get("verdict") else "") +
             (f" — outcome: {row['outcome']}" if row.get("outcome") else "") + "."]
    if row.get("unreadable"):
        lines.append(f"Note: {row['unreadable']}.")
    kv = [("project", row.get("project")), ("run", row.get("run_id")), ("status", row.get("status")),
          ("verdict", row.get("verdict")), ("outcome", row.get("outcome")), ("grade", row.get("grade")),
          ("wall", f"{row['wall_s']} s" if isinstance(row.get("wall_s"), (int, float)) else None),
          ("stages", row.get("n_stages"))]
    links = [{"label": "open in Evidence",
              "href": f"?plate=evidence&project={row['project']}&run={row['run_id']}"}]
    return _reply("Latest golden run", lines, kv, links)


def _summarise(app: server.App, text: str) -> dict[str, Any]:
    row, project, problem = _latest_run(app, text)
    if row is None:
        return _reply("Summary", [f"There is nothing to summarise: {problem}."])
    run_dir = Path(row["run_dir"])
    lines = [f"{row['project']} · {row['run_id']}" +
             (f" finished {row['finished_utc']}" if row.get("finished_utc") else "") + "."]
    if row.get("verdict") or row.get("outcome"):
        lines.append("Verdict: " + " · ".join(str(v) for v in (row.get("verdict"), row.get("outcome")) if v) +
                     (f" (grade {row['grade']})" if row.get("grade") else "") + ".")
    kv = [("status", row.get("status")), ("grade", row.get("grade")),
          ("wall", f"{row['wall_s']} s" if isinstance(row.get("wall_s"), (int, float)) else None)]
    res, rerr, present = records.read_json(run_dir, "result.json")
    if isinstance(res, dict):
        scalars: list[tuple[str, Any]] = []

        def walk(o: Any, path: str, depth: int) -> None:
            if len(scalars) >= 12:
                return
            if isinstance(o, dict) and depth < 3:
                for k, v in o.items():
                    walk(v, f"{path}.{k}" if path else str(k), depth + 1)
            elif isinstance(o, (int, float)) and not isinstance(o, bool):
                scalars.append((path, round(o, 6) if isinstance(o, float) else o))

        walk(res, "", 0)
        kv.extend(scalars)
        notes = res.get("notes") or res.get("summary")
        if isinstance(notes, str) and notes.strip():
            lines.append(notes.strip()[:400])
    elif not present:
        lines.append("The run has no result.json yet — it may still be running or it stopped before the result "
                     "stage; the row above is all that is recorded.")
    else:
        lines.append(f"result.json could not be read ({rerr}); the row above is all I can stand on.")
    lines.append("Every number above is read from the run's own records; nothing is estimated.")
    links = [{"label": "open in Evidence",
              "href": f"?plate=evidence&project={row['project']}&run={row['run_id']}"}]
    return _reply(f"Summary — {row['run_id']}", lines, kv, links)


def _transfer(app: server.App, text: str, inbox_dir: Path) -> dict[str, Any]:
    toks = TOKEN_RE.findall(text)
    fname = next((t for t in toks if "." in t and not t.lower().startswith(("http", "www"))), None)
    eid = next((t for t in toks if t != fname and EID_RE.match(t) and
                t.lower() not in {"pull", "transfer", "fetch", "download", "get", "from", "the", "file", "rig",
                                  "me", "to", "inbox", "please", "can", "you", "copy", "over", "here"}), None)
    if not fname or not eid:
        return _reply("Transfer a file", [
            "Tell me which file and which rig experiment, e.g. “pull report.html from exp-2026-10-07-cfd-r3”.",
            "The file lands in the loop inbox (16 MiB cap) with a download link here in the thread."])
    ok_, res, err, _info = sim.mcp_call(app, "sim_get_file",
                                        {"experiment_id": eid, "path": fname, "offset": 0, "max_bytes": INBOX_CAP})
    if not ok_:
        return _reply("Transfer a file", [f"The pull failed: {err}.", "Nothing was written."])
    if not isinstance(res, dict):
        return _reply("Transfer a file", ["sim_get_file answered without a result object; nothing was written."])
    try:
        data = base64.b64decode(res.get("content_b64") or "", validate=True)
    except (binascii.Error, TypeError):
        return _reply("Transfer a file", ["The rig's reply was not valid base64; nothing was written."])
    if not res.get("eof"):
        return _reply("Transfer a file",
                      [f"{fname} is larger than the {INBOX_CAP // 1048576} MiB inbox cap; nothing was written. "
                       "Ask the rig side for a smaller artifact or pull it with aces golden collect."])
    safe = f"{eid}--{Path(fname).name}"
    if not INBOX_RE.match(safe):
        return _reply("Transfer a file", [f"the name {safe!r} is not a safe inbox name; nothing was written."])
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / safe).write_bytes(data)
    n = jobs._record(app, "console.loop.transfer",
                     {"experiment": eid, "file": fname, "bytes": len(data), "sha256": res.get("sha256")})
    line = f"Pulled {fname} from {eid} — {len(data)} bytes"
    if res.get("sha256"):
        line += f", sha256 {str(res.get('sha256'))[:12]}…"
    line += f". Ledger entry {n}." if n else "."
    lines = [line]
    return _reply("Transfer a file", lines, [("experiment", eid), ("file", fname), ("bytes", len(data))],
                  [{"label": f"download {Path(fname).name}", "href": f"/api/loop/inbox/{safe}"}])


def _help() -> dict[str, Any]:
    return _reply("What I can do", ["Ask in plain language:"] + CAPABILITIES)


def _fallback() -> dict[str, Any]:
    return _reply("I can't do that yet",
                  ["That question is outside what I can answer from the console's own records. "
                   "What I can do:"] + CAPABILITIES)


# -------------------------------------------------------------------------------------------------- the router

def reply(app: server.App, text: str, inbox_dir: Path) -> dict[str, Any] | None:
    """An ops answer {text, card} for ops intents, or None to let the design playbook take the message."""
    low = text.lower().strip()
    if re.search(r"\b(pull|transfer|download|fetch)\b", low) and re.search(r"\b(file|from|result|report|stp|step|stl|csv|html|json|log)\b", low):
        return _transfer(app, text, inbox_dir)
    if re.search(r"\b(find|locate|search for|look for|where is|where's)\b", low) and "project" in low:
        return _find(app, text)
    if re.search(r"\b(summarise|summarize|summary|findings|what did we learn|what have we learned)\b", low):
        return _summarise(app, text)
    if re.search(r"\b(show|see|view|open|get|give)\b", low) and re.search(r"\b(result|results|latest run|last run)\b", low):
        return _result(app, text)
    if re.search(r"\bqueue", low) or re.search(r"\bwhy\b.*\b(waiting|queued|not running|stuck)\b", low):
        return _queue(app)
    if re.search(r"\b(status|going on|happening|running|processing|up to|progress)\b", low):
        return _status(app)
    if re.match(r"^(help|what can you do|capabilities|commands)\b", low):
        return _help()
    if low.endswith("?") or low.startswith(QUESTION_START):
        return _fallback()
    return None
