"""Golden runs and CAD actions started from the console: the `aces golden` and `aces-cad` command lines run as jobs on
this desktop, their output streamed to the page, every start ledgered (the tools write their own golden.run and
cad.pull entries). Human POSTs only (the console's POST guards); each start names its project in a typed confirmation.

    GET  /api/jobs                       the jobs of this server's life (newest first, at most 50 kept)
    GET  /api/job/{id}?since=N           one job: status, exit, result (the tool's --json document), lines from N
    POST /api/job/{id}/cancel            SIGTERM to the job (the golden CLI records the run as interrupted)
    POST /api/golden/init                {name, class, hypothesis?, step: {filename, data_b64}}  aces golden init
    POST /api/golden/run                 {project | demo, step?, options{}, confirm}  aces golden run --json
    POST /api/cad/action                 {project, action, run, options{}, confirm?}  aces-cad <verb> --json

CAD verbs: inspect, render, drawing, pull, rerun (aces-cad run --from RUN with parameter overrides: the run's own script
in the sandbox, no new code) and prune (deletes runs beyond the newest N; typed "prune <project>"). The script-running
verb with new code (`aces-cad run SCRIPT`) is the agents' text-to-CAD path and is not offered here.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from aces import config as aces_config, ledger

from . import secrets as tokens, server
from .server import Request, Response, envelope, error, ok

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
RUN_RE = re.compile(r"^(latest|[0-9]{8}T[0-9]{6}Z-[0-9a-z]{2,16})$")
PARAM_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
KEEP_JOBS = 50
LINES_CAP = 5000
GOLDEN_WALL_S = 3 * 3600
CAD_WALL_S = 900
STEP_CAP = 64 * 1024 * 1024
DEMOS = ("root-mount", "bone")
GRADES = ("preview-grade", "verified-desktop", "rig-verified")
GOLDEN_OPTS: dict[str, tuple] = {
    "n": ("-n", int, 3, 500), "seed": ("--seed", int, 0, 2**31 - 1), "res": ("--res", float, 0.1, 50.0),
    "gen_res": ("--gen-res", float, 0.1, 50.0), "tol": ("--tol", float, 0.001, 1.0), "rounds": ("--rounds", int, 1, 20),
    "check_budget": ("--check-budget", float, 1.0, 86400.0), "rig_budget": ("--rig-budget", float, 60.0, 86400.0),
    "check": ("--check", ("auto", "off", "force")), "device": ("--device", ("auto", "cpu", "gpu")),
    "conforming": ("--conforming", ("auto", "rig", "local", "off")), "require_grade": ("--require-grade", GRADES),
    "rig_sweep": ("--rig-sweep", bool), "strict": ("--strict", bool), "allow_thin": ("--allow-thin", bool),
}
CAD_VERBS = ("inspect", "render", "drawing", "pull", "rerun", "prune")


class Job:
    def __init__(self, kind: str, project: str, title: str, display: list[str], run: str | None = None):
        self.id = pysecrets.token_hex(6)
        self.kind, self.project, self.title, self.run = kind, project, title, run
        # the logical command (aces golden run ..., aces-cad ...), never the resolved interpreter line or a local path
        self.argv_display = [os.path.basename(a) if os.path.isabs(a) else a for a in display]
        self.status = "running"
        self.exit: int | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.lines: deque = deque(maxlen=LINES_CAP)
        self.n_lines = 0
        self.stdout: list[str] = []
        self.result: Any = None
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def add(self, stream: str, text: str) -> None:
        with self.lock:
            self.lines.append({"n": self.n_lines, "t": round(time.time() - self.started, 2), "stream": stream,
                               "text": tokens.scrub(text.rstrip("\n"))[:2000]})
            self.n_lines += 1

    def summary(self) -> dict[str, Any]:
        r = self.result if isinstance(self.result, dict) else {}
        return {"id": self.id, "kind": self.kind, "project": self.project, "title": self.title, "run": self.run,
                "status": self.status, "exit": self.exit, "started_utc": server_utc(self.started),
                "finished_utc": server_utc(self.finished) if self.finished else None,
                "duration_s": round((self.finished or time.time()) - self.started, 1), "lines": self.n_lines,
                "error": self.error, "result_run_id": r.get("run_id") or r.get("id"),
                "result_outcome": r.get("outcome") or r.get("verdict") or r.get("status"), "result_grade": r.get("grade")}

    def detail(self, since: int) -> dict[str, Any]:
        with self.lock:
            lines = [ln for ln in self.lines if ln["n"] >= since]
        return {**self.summary(), "argv": self.argv_display, "result": self.result, "lines_from": since,
                "line_items": lines}


def server_utc(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


class JobsState:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.order: deque = deque(maxlen=KEEP_JOBS)
        self.lock = threading.Lock()


def _state(app: server.App) -> JobsState:
    st = app.state.get("jobs")
    if st is None:
        st = app.state["jobs"] = JobsState()
    return st


def _record(app: server.App, kind: str, payload: dict[str, Any]) -> int | None:
    try:
        n, _ = ledger.record(kind, payload)
        return n
    except (ValueError, TimeoutError, OSError, SystemExit) as e:
        app.log(f"ledger write of {kind} failed: {e}"[:300])
        return None


def _running(st: JobsState, pred) -> Job | None:
    with st.lock:
        return next((j for j in st.jobs.values() if j.status == "running" and pred(j)), None)


def start_job(app: server.App, job: Job, argv: list[str], wall_s: float, json_stdout: bool = True) -> None:
    """Spawn argv for job; stdout is the tool's --json document (kept whole), stderr its progress lines."""
    st = _state(app)
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                cwd=str(Path.home()), text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as e:
        job.status, job.error, job.finished = "failed", f"cannot start: {e}", time.time()
        with st.lock:
            st.jobs[job.id] = job
            st.order.appendleft(job.id)
        return
    job.proc = proc
    with st.lock:
        st.jobs[job.id] = job
        st.order.appendleft(job.id)
        for old in list(st.jobs):
            if old not in st.order:
                st.jobs.pop(old, None)

    def pump(stream, name):
        for line in stream:
            if name == "stdout" and json_stdout:
                job.stdout.append(line)
                if len(job.stdout) > 20000:
                    job.stdout = job.stdout[-20000:]
            else:
                job.add(name, line)

    def watch():
        t_out = threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True)
        t_err = threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True)
        t_out.start(); t_err.start()
        try:
            proc.wait(timeout=wall_s)
        except subprocess.TimeoutExpired:
            proc.terminate()
            job.error = f"stopped by the console after {wall_s:g} s"
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        t_out.join(5); t_err.join(5)
        text = "".join(job.stdout).strip()
        if text:
            try:
                job.result = json.loads(text[text.find("{"):]) if "{" in text else None
            except json.JSONDecodeError:
                for line in text.splitlines():
                    job.add("stdout", line)
        job.exit = proc.returncode
        job.finished = time.time()
        if job.status != "cancelled":
            job.status = "done" if proc.returncode == 0 else "failed"
        job.add("console", f"exit {proc.returncode} after {job.finished - job.started:.1f} s")
        for key in ("golden.runs", "cad.runs", "projects"):
            if key in app.hub.topics():
                app.cache.invalidate(key)
        app.cache.invalidate("jobs")

    threading.Thread(target=watch, name=f"aces-console-job-{job.id}", daemon=True).start()


# --- reads ------------------------------------------------------------------------------------------------------------------

def fetch_jobs(app: server.App):
    st = _state(app)
    with st.lock:
        rows = [st.jobs[i].summary() for i in st.order if i in st.jobs]
    return True, {"jobs": rows, "n": len(rows), "running": sum(1 for r in rows if r["status"] == "running"),
                  "note": "jobs live as long as this console; the runs, records and ledger entries they make persist"}, None


def h_jobs(req: Request) -> Response:
    return envelope(req.app.hub.get("jobs"))


def h_job(req: Request) -> Response:
    st = _state(req.app)
    job = st.jobs.get(req.params.get("id") or "")
    if job is None:
        return error(404, "no such job (jobs are kept for this console's life, at most 50)", "not-found")
    try:
        since = max(0, int(req.q("since", "0") or 0))
    except ValueError:
        return error(400, "since must be an integer", "bad-request")
    return ok(job.detail(since))


def h_cancel(req: Request) -> Response:
    st = _state(req.app)
    job = st.jobs.get(req.params.get("id") or "")
    if job is None:
        return error(404, "no such job", "not-found")
    if job.status != "running" or job.proc is None:
        return error(409, f"the job is {job.status}", "not-running")
    job.status = "cancelled"
    job.proc.terminate()
    job.add("console", "cancelled by the operator (SIGTERM)")
    n = _record(req.app, "console.job.cancel", {"job": job.id, "kind": job.kind, "project": job.project})
    return ok({"cancelled": job.id, "ledger_n": n})


# --- golden -------------------------------------------------------------------------------------------------------------

def _aces(args: list[str]) -> list[str] | None:
    from .system import aces_command
    return aces_command(args)


def _golden_args(options: Any) -> list[str]:
    if options is None:
        return []
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    args: list[str] = []
    for key, value in options.items():
        if key == "lane":
            continue  # validated and appended by h_golden_run (none, or a lane name)
        spec = GOLDEN_OPTS.get(key)
        if spec is None:
            raise ValueError(f"unknown option {key}")
        flag, kind = spec[0], spec[1]
        if value is None or value == "":
            continue
        if kind is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            if value:
                args.append(flag)
        elif isinstance(kind, tuple):
            if value not in kind:
                raise ValueError(f"{key} must be one of {', '.join(kind)}")
            args += [flag, value]
        else:
            try:
                v = kind(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number") from None
            if isinstance(value, bool) or not spec[2] <= v <= spec[3]:
                raise ValueError(f"{key} must be between {spec[2]} and {spec[3]}")
            args += [flag, str(v)]
    return args


def h_golden_run(req: Request) -> Response:
    app = req.app
    body = req.json or {}
    demo, project, step = body.get("demo"), body.get("project"), body.get("step")
    try:
        opts = _golden_args(body.get("options"))
        lane = (body.get("options") or {}).get("lane") if isinstance(body.get("options"), dict) else None
        note = body.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 300):
            raise ValueError("note must be text of at most 300 characters")
        if demo is not None:
            if demo not in DEMOS:
                raise ValueError(f"demo must be one of {', '.join(DEMOS)}")
            project, args = "golden-demo", ["golden", "run", "--demo", demo]
        else:
            if not isinstance(project, str) or not NAME_RE.match(project):
                raise ValueError("project must be a project name")
            pdir = Path(aces_config.WORK) / project
            if not (pdir / "aces.yaml").is_file():
                return error(404, f"no project {project} under {aces_config.WORK}", "not-found")
            args = ["golden", "run", "--project", project]
            if step is not None:
                if not isinstance(step, str) or step.startswith("/") or ".." in step.split("/") or "\\" in step \
                        or not step.lower().endswith((".step", ".stp")) or not (pdir / step).is_file():
                    raise ValueError("step must be a .step file inside the project (a relative path)")
                args += ["--step", str(pdir / step)]
            elif not (pdir / "golden" / "spec.json").is_file():
                return error(409, f"{project} has no golden/spec.json: start it from a STEP (Start from CAD) or run the demo",
                             "no-spec")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    if lane is not None:
        if not isinstance(lane, str) or not re.fullmatch(r"none|[a-z0-9][a-z0-9-]{0,39}", lane):
            return error(400, "lane must be none or a lane name", "bad-request")
        opts += ["--lane", lane]
    if body.get("confirm") != project:
        return error(400, f"confirm must be the project name {project}", "confirm-mismatch")
    st = _state(app)
    busy = _running(st, lambda j: j.kind == "golden.run")
    if busy is not None:
        return error(409, f"a golden run is already running ({busy.project}, job {busy.id}); one at a time", "busy")
    args += opts + (["--note", note] if note else []) + ["--json", "--progress", "lines"]
    argv = _aces(args)
    if argv is None:
        return error(503, "aces CLI not found (aces-base is not installed)", "not-installed")
    job = Job("golden.run", project, f"golden run {project}" + (f" (demo {demo})" if demo else ""), ["aces", *args])
    n = _record(app, "console.golden.run", {"project": project, "demo": demo, "step": step, "args": args[2:], "job": job.id})
    start_job(app, job, argv, GOLDEN_WALL_S)
    return ok({"job": job.id, "ledger_n": n, "project": project}, status=202)


def h_golden_init(req: Request) -> Response:
    app = req.app
    body = req.json or {}
    name, cls, hyp, step = body.get("name"), body.get("class"), body.get("hypothesis"), body.get("step")
    try:
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError("name must be a project name (lower case letters, digits, - and _)")
        if not isinstance(cls, str) or not re.fullmatch(r"[a-z]{2,20}", cls):
            raise ValueError("class must be a project class (aces policy show)")
        if hyp is not None and (not isinstance(hyp, str) or len(hyp) > 500):
            raise ValueError("hypothesis must be text of at most 500 characters")
        if not isinstance(step, dict) or not isinstance(step.get("filename"), str) or not isinstance(step.get("data_b64"), str):
            raise ValueError("step must be {filename, data_b64}")
        fname = os.path.basename(step["filename"])
        if not fname.lower().endswith((".step", ".stp")):
            raise ValueError("the CAD file must be a STEP file (.step or .stp)")
        data = base64.b64decode(step["data_b64"], validate=True)
    except (ValueError, binascii.Error) as e:
        return error(400, str(e), "bad-request")
    if len(data) > STEP_CAP:
        return error(413, f"the STEP file exceeds {STEP_CAP} bytes", "too-large")
    if (Path(aces_config.WORK) / name).exists():
        return error(409, f"a project {name} already exists: pick it and run, or choose another name", "exists")
    digest = hashlib.sha256(data).hexdigest()
    up = app.settings.state_dir / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    src = up / f"{digest[:16]}-{re.sub(r'[^A-Za-z0-9._-]', '_', fname)[:80]}"
    src.write_bytes(data)
    args = ["golden", "init", name, "--step", str(src), "--class", cls] + (["--hypothesis", hyp] if hyp else [])
    argv = _aces(args)
    if argv is None:
        return error(503, "aces CLI not found (aces-base is not installed)", "not-installed")
    from .system import run_capped
    res = run_capped(argv, 120.0)
    okk = res.get("exit") == 0
    n = _record(app, "console.golden.init", {"project": name, "class": cls, "step": fname, "bytes": len(data),
                                             "sha256": digest, "ok": okk, "exit": res.get("exit")})
    for key in ("projects", "golden.runs"):
        app.cache.invalidate(key)
    if not okk:
        detail = (res.get("stderr_tail") or res.get("stdout") or res.get("error") or "")[-600:]
        if res.get("exit") == 127 or "No module named" in detail:
            return error(503, "aces golden needs aces-engineering, which is not installed on this desktop", "not-installed")
        return error(502, f"aces golden init exited {res.get('exit')}: {detail}", "failed")
    return ok({"project": name, "ledger_n": n, "output": (res.get("stdout") or "")[-2000:]})


# --- CAD ------------------------------------------------------------------------------------------------------------------

def h_cad_action(req: Request) -> Response:
    app = req.app
    body = req.json or {}
    project, action, run = body.get("project"), body.get("action"), body.get("run") or "latest"
    options = body.get("options") or {}
    try:
        if not isinstance(project, str) or not NAME_RE.match(project):
            raise ValueError("project must be a project name")
        if action not in CAD_VERBS:
            raise ValueError(f"action must be one of {', '.join(CAD_VERBS)}")
        if not isinstance(run, str) or not RUN_RE.match(run):
            raise ValueError("run must be a CAD run id or latest")
        if not isinstance(options, dict):
            raise ValueError("options must be an object")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    if not (Path(aces_config.WORK) / project / "aces.yaml").is_file():
        return error(404, f"no project {project} under {aces_config.WORK}", "not-found")
    exe = shutil.which("aces-cad")
    if not exe:
        return error(503, "aces-cad is not installed on this desktop", "not-installed")
    args = [exe, "run" if action == "rerun" else action, "--json", "--project", project]
    try:
        if action == "prune":
            keep = options.get("keep")
            if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= 1000:
                raise ValueError("keep must be a whole number from 1")
            if body.get("confirm") != f"prune {project}":
                return error(400, f"confirm must be the words 'prune {project}'", "confirm-mismatch")
            args += ["--keep", str(keep)]
        elif action == "render":
            size = options.get("size", "medium")
            if size not in ("small", "medium", "large"):
                raise ValueError("size must be small, medium or large")
            args += ["--size", size, run]
        elif action == "drawing":
            proj = options.get("projection", "third-angle")
            if proj not in ("third-angle", "first-angle"):
                raise ValueError("projection must be third-angle or first-angle")
            title = options.get("title")
            if title is not None and (not isinstance(title, str) or len(title) > 120):
                raise ValueError("title must be text of at most 120 characters")
            args += ["--projection", proj] + (["--title", title] if title else []) + [run]
        elif action == "pull":
            name = options.get("name")
            if name is not None and (not isinstance(name, str) or not NAME_RE.match(name)):
                raise ValueError("name must be a plain name")
            args += (["--name", name] if name else []) + [run]
        elif action == "inspect":
            material = options.get("material")
            if material is not None and (not isinstance(material, str) or not re.fullmatch(r"[A-Za-z0-9 _-]{1,40}", material)):
                raise ValueError("material must be a material name")
            args += (["--material", material] if material else []) + [run]
        elif action == "rerun":
            if run == "latest":
                raise ValueError("re-run needs a run id")
            params = options.get("params") or {}
            if not isinstance(params, dict) or len(params) > 60:
                raise ValueError("params must be an object of at most 60 overrides")
            for k, v in params.items():
                if not PARAM_KEY_RE.match(str(k)) or not isinstance(v, (int, float, str, bool)) or (isinstance(v, str) and len(v) > 200):
                    raise ValueError(f"parameter {k}: a name and a number, text or true/false")
            args += ["--from", run] + [a for k, v in params.items() for a in ("--param", f"{k}={json.dumps(v)}")]
            if options.get("render") is True:
                args.append("--render")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    st = _state(app)
    busy = _running(st, lambda j: j.kind.startswith("cad.") and j.project == project)
    if busy is not None:
        return error(409, f"a CAD action is already running on {project} (job {busy.id})", "busy")
    job = Job(f"cad.{action}", project, f"aces-cad {action} {project}" + ("" if action == "prune" else f" {run}"),
              ["aces-cad", *args[1:]], run=None if action == "prune" else run)
    n = _record(app, f"console.cad.{action}", {"project": project, "run": job.run, "options": options, "job": job.id})
    start_job(app, job, args, CAD_WALL_S)
    return ok({"job": job.id, "ledger_n": n}, status=202)


def install(register) -> None:
    app = server.current_app()
    _state(app)
    app.hub.register("jobs", lambda: fetch_jobs(app), 2.0, ("evidence", "objects", "projects"), inline=True,
                     source="this console's jobs")
    server.allow_body("/api/golden/init", STEP_CAP * 4 // 3 + 4096)
    register("GET", "/api/jobs", h_jobs)
    register("GET", "/api/job/{id}", h_job)
    register("POST", "/api/job/{id}/cancel", h_cancel)
    register("POST", "/api/golden/run", h_golden_run)
    register("POST", "/api/golden/init", h_golden_init)
    register("POST", "/api/cad/action", h_cad_action)
