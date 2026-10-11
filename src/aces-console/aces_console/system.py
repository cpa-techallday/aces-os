"""Policy, doctor, packages, desktop entries, links, open and stop (build spec 3.12). Builder B.

    GET  /api/policy            aces.policy.load() -> {policy, override:{present, path, mode}}
    GET  /api/doctor            the last `aces doctor` run of this session, verbatim, or the note that none has run
    POST /api/doctor/run        202 {started}: `aces doctor` in a worker thread (90 s cap); 409 while running
    GET  /api/system/packages   pacman -Q filtered to aces-* and omarchy*; no pacman -> packages [] + note
    GET  /api/system/entries    /usr/share/applications/*.desktop owned by aces-* packages (pacman -Qo)
    GET  /api/system/links      the server-side allow-list: GC's mc_console/gridwatch, the console's mlflow, the
                                configured bases; each {id, label, url, reach, openable, note}
    POST /api/system/open {id}  xdg-open / gio launch of an openable link or entry; ledgered console.system.open
    POST /api/system/stop {confirm:"stop"}  ledger console.server.stop, bye to the SSE clients, exit

Subprocesses are capped (doctor 90 s, pacman 10 s, launchers detached) and their output is scrubbed of any configured
token before it is kept. The aces CLI is `aces` on PATH, else the importable `aces` package run through this
interpreter (a source tree); records.py reuses `aces_command` and `run_capped` for the golden verify.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from aces import config as aces_config, ledger, policy

from . import cache as cache_mod, secrets as tokens, server
from .server import Request, Response, envelope, error, ok

DOCTOR_TIMEOUT_S = 90.0
DOCTOR_ARGS: list[str] = ["doctor"]  # tests replace this with ["doctor", "--offline"] so no probe leaves the loopback
PACMAN_TIMEOUT_S = 10.0
APPLICATIONS_DIR = Path("/usr/share/applications")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal", "[::1]"})
RIG_LOCAL_NOTE = "rig-local address; not reachable from this desktop"
DOCTOR_NOT_RUN = "doctor has not been run in this session"
STDERR_TAIL = 2048


class SystemState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.doctor: dict[str, Any] = {"text": None, "exit": None, "ran_at": None, "running": False, "duration_s": None,
                                       "command": None, "error": None}
        self.launcher: Callable[[str, str], tuple[bool, str]] = launch
        self.started_ts = time.time()


def _state(app: server.App) -> SystemState:
    st = app.state.get("system")
    if st is None:
        st = app.state["system"] = SystemState()
    return st


def _record(app: server.App, kind: str, payload: dict[str, Any]) -> tuple[int | None, str | None]:
    try:
        n, _ = ledger.record(kind, payload)
        return n, None
    except (ValueError, TimeoutError, OSError, SystemExit) as e:
        msg = f"ledger write of {kind} failed: {e}"[:300]
        app.log(msg)
        return None, msg


# --- subprocess helpers (shared with records.py) --------------------------------------------------------------------------

def aces_command(args: list[str]) -> list[str] | None:
    """The `aces` CLI with `args`: /usr/bin/aces (or any `aces` on PATH), else the importable `aces` package run by
    this interpreter with its location on sys.path (a source tree; `-I` ignores PYTHONPATH), else None."""
    found = shutil.which("aces")
    if found:
        return [found, *args]
    try:
        import aces  # noqa: F401  (aces-base, already imported by this server)
    except ImportError:
        return None
    paths = [str(Path(aces.__file__).resolve().parent.parent)]
    prog = f"import sys; sys.path[:0] = {json.dumps(paths)}; from aces.cli import main; raise SystemExit(main({json.dumps(args)}))"
    return [sys.executable, "-I", "-c", prog]


def run_capped(cmd: list[str], timeout_s: float, cwd: str | None = None) -> dict[str, Any]:
    """Run `cmd` to completion with a cap: {exit, stdout, stderr_tail, duration_s, timed_out, error}; output is scrubbed
    of any configured token value. Never raises."""
    t0 = time.monotonic()
    out: dict[str, Any] = {"exit": None, "stdout": "", "stderr_tail": "", "duration_s": None, "timed_out": False, "error": None}
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False, cwd=cwd, stdin=subprocess.DEVNULL)
        out["exit"] = p.returncode
        out["stdout"] = tokens.scrub(p.stdout.decode("utf-8", "replace"))
        out["stderr_tail"] = tokens.scrub(p.stderr.decode("utf-8", "replace")[-STDERR_TAIL:])
    except subprocess.TimeoutExpired as e:
        out["timed_out"] = True
        out["error"] = f"timed out after {timeout_s:g} s"
        out["stdout"] = tokens.scrub((e.stdout or b"").decode("utf-8", "replace")) if isinstance(e.stdout, bytes) else ""
        out["stderr_tail"] = tokens.scrub((e.stderr or b"").decode("utf-8", "replace")[-STDERR_TAIL:]) if isinstance(e.stderr, bytes) else ""
    except FileNotFoundError:
        out["error"] = f"not found: {cmd[0]}"
        out["exit"] = 127
    except OSError as e:
        out["error"] = f"cannot run {cmd[0]}: {e.strerror or type(e).__name__}"
        out["exit"] = 126
    out["duration_s"] = round(time.monotonic() - t0, 3)
    return out


# --- policy ------------------------------------------------------------------------------------------------------------------

def fetch_policy(app: server.App):
    try:
        pol = policy.load()
    except SystemExit as e:
        return False, None, f"policy unreadable: {e}"
    user_path = aces_config.USER_CONFIG / "policy.json"
    present = user_path.is_file()
    mode = None
    if present:
        try:
            user = aces_config.read_json(user_path, {}) or {}
            mode = user.get("mode") if isinstance(user, dict) else None
        except SystemExit:
            mode = None
    return True, {"policy": pol, "override": {"present": present, "path": str(user_path), "mode": mode},
                  "etc_path": str(aces_config.ETC / "policy.json"),
                  "note": "the user override file is reported by presence and mode only"}, None


def h_policy(req: Request) -> Response:
    e = req.app.cache.get("system.policy", 30.0, lambda: fetch_policy(req.app), source=str(aces_config.ETC / "policy.json"))
    return envelope(e)


# --- doctor --------------------------------------------------------------------------------------------------------------------

def doctor_snapshot(st: SystemState) -> dict[str, Any]:
    with st.lock:
        d = dict(st.doctor)
    if d["text"] is None and not d["running"]:
        return {"text": None, "exit": None, "ran_at": None, "running": False, "note": DOCTOR_NOT_RUN, "command": d["command"],
                "error": d["error"]}
    if d["text"] is None and d["running"]:
        return {"text": None, "exit": None, "ran_at": None, "running": True, "note": "doctor is running", "command": d["command"]}
    return {"text": d["text"], "exit": d["exit"], "ran_at": d["ran_at"], "running": d["running"], "duration_s": d["duration_s"],
            "command": d["command"], "error": d["error"], "note": None}


def fetch_doctor(app: server.App):
    return True, doctor_snapshot(_state(app)), None


def _run_doctor(app: server.App, st: SystemState, cmd: list[str]) -> None:
    res = run_capped(cmd, DOCTOR_TIMEOUT_S)
    text = res["stdout"]
    if res["stderr_tail"]:
        text = (text + ("\n" if text and not text.endswith("\n") else "") + res["stderr_tail"]) if text else res["stderr_tail"]
    with st.lock:
        st.doctor.update({"text": text if text else ("" if res["error"] is None else None), "exit": res["exit"],
                          "ran_at": server.now_utc(), "running": False, "duration_s": res["duration_s"],
                          "error": res["error"]})
    try:
        if app.hub.source("doctor") is not None:
            app.hub.refresh("doctor")
    except Exception as e:  # noqa: BLE001
        app.log(f"doctor republish failed: {e}")


def h_doctor(req: Request) -> Response:
    return envelope(req.app.hub.get("doctor"))


def h_doctor_run(req: Request) -> Response:
    app = req.app
    st = _state(app)
    if app.fixture:
        return error(503, "fixture mode: a doctor run would probe the rig", "fixture")
    cmd = aces_command(list(DOCTOR_ARGS))
    if cmd is None:
        return error(503, "aces CLI not found (aces-base is not installed and the aces package is not importable)", "not-installed")
    with st.lock:
        if st.doctor["running"]:
            return error(409, "doctor is already running", "busy")
        st.doctor["running"] = True
        st.doctor["command"] = ["aces", *DOCTOR_ARGS]
        before = st.doctor["ran_at"]
    threading.Thread(target=_run_doctor, args=(app, st, cmd), name="aces-console-doctor", daemon=True).start()
    return ok({"started": True, "command": ["aces", *DOCTOR_ARGS], "cap_s": DOCTOR_TIMEOUT_S, "ran_at_before": before},
              source="aces doctor", status=202)


# --- packages and desktop entries --------------------------------------------------------------------------------------------

def _pacman() -> str | None:
    return shutil.which("pacman")


def fetch_packages(app: server.App):
    pac = _pacman()
    queried = server.now_utc()
    if not pac:
        return True, {"packages": [], "queried_utc": queried,
                      "note": f"pacman not found on this host ({platform.system().lower() or os.name}); package versions are not known here"}, None
    res = run_capped([pac, "-Q"], PACMAN_TIMEOUT_S)
    if res["error"] or (res["exit"] not in (0, None)):
        return False, None, f"pacman -Q failed: {res['error'] or ('exit ' + str(res['exit']))} {res['stderr_tail'][:200]}".strip()
    rows = []
    for line in res["stdout"].splitlines():
        name, _, version = line.strip().partition(" ")
        if name.startswith("aces-") or name.startswith("omarchy") or name == "aces":
            rows.append({"name": name, "version": version or None})
    return True, {"packages": rows, "queried_utc": queried, "note": None if rows else "pacman lists no aces-* or omarchy* packages"}, None


def _parse_desktop(path: Path) -> dict[str, Any]:
    name = exec_ = comment = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"name": None, "exec": None, "comment": None, "unreadable": str(e)}
    in_entry = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k == "Name" and name is None:
            name = v.strip()
        elif k == "Exec" and exec_ is None:
            exec_ = v.strip()
        elif k == "Comment" and comment is None:
            comment = v.strip()
    return {"name": name, "exec": exec_, "comment": comment}


def fetch_entries(app: server.App):
    apps_dir = APPLICATIONS_DIR
    pac = _pacman()
    if not apps_dir.is_dir():
        return True, {"entries": [], "note": f"{apps_dir} does not exist on this host"}, None
    files = sorted(p for p in apps_dir.glob("*.desktop") if p.is_file())
    if not pac:
        return True, {"entries": [], "note": f"pacman not found on this host; ownership of {len(files)} .desktop files cannot be checked"}, None
    if not files:
        return True, {"entries": [], "note": f"no .desktop files in {apps_dir}"}, None
    res = run_capped([pac, "-Qo", *[str(p) for p in files]], PACMAN_TIMEOUT_S)
    owners: dict[str, str] = {}
    for line in (res["stdout"] + "\n" + res["stderr_tail"]).splitlines():
        # "<path> is owned by <pkg> <version>"
        if " is owned by " in line:
            path_s, _, rest = line.partition(" is owned by ")
            pkg = rest.strip().split(" ")[0]
            owners[path_s.strip()] = pkg
    rows = []
    for p in files:
        pkg = owners.get(str(p))
        if not pkg or not (pkg.startswith("aces-") or pkg == "aces"):
            continue
        d = _parse_desktop(p)
        rows.append({"id": p.stem, "name": d.get("name"), "exec": d.get("exec"), "comment": d.get("comment"),
                     "owner_pkg": pkg, "path": str(p)})
    note = None if rows else "no .desktop file owned by an aces-* package"
    if res["error"]:
        note = f"pacman -Qo: {res['error']}"
    return True, {"entries": rows, "note": note, "checked": len(files)}, None


def h_packages(req: Request) -> Response:
    return envelope(req.app.hub.get("system.packages"))


def h_entries(req: Request) -> Response:
    return envelope(req.app.hub.get("system.entries"))


# --- links -------------------------------------------------------------------------------------------------------------------

def reach_of(url: Any, reported_by_rig: bool) -> tuple[str | None, bool, str | None]:
    """("lan"|"rig-loopback"|"desktop-loopback"|None, openable, note). A loopback or host.docker.internal address
    reported by the rig is rig-local and not openable; a configured loopback address is this desktop's own."""
    if not isinstance(url, str) or not url:
        return None, False, "not reported"
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        host = None
    if not host or urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return None, False, "not an http(s) URL"
    if host.lower() in LOOPBACK_HOSTS:
        if reported_by_rig:
            return "rig-loopback", False, RIG_LOCAL_NOTE
        return "desktop-loopback", True, None
    return "lan", True, None


def _peek_data(app: server.App, topic: str) -> Any:
    if app.hub.source(topic) is None:
        return None
    try:
        e = app.hub.get(topic)
    except Exception:  # noqa: BLE001
        return None
    return e.data if isinstance(e.data, dict) else None


# the separate consoles the ACES console replaces (Aaron, 2026-10-11): their content lives on these plates now, so
# they are listed as endpoints (health, provenance) but never opened from here
REPLACED_BY = {"grid_control": "Fabric, Ops and Agents plates", "mc_console": "Agents plate",
               "simlab_console": "Pipeline, Solvers and Evidence plates", "mlflow": "Pipeline plate (MLflow browser)",
               "mlflow_console": "Pipeline plate (MLflow browser)"}


def fetch_links(app: server.App):
    status = _peek_data(app, "rig.status")
    summary = _peek_data(app, "sim.summary")
    srv = app.settings.server
    rows: list[dict[str, Any]] = []

    def add(id_: str, label: str, url: Any, reported_by_rig: bool, source: str) -> None:
        reach, openable, note = reach_of(url, reported_by_rig)
        replaced = REPLACED_BY.get(id_)
        rows.append({"id": id_, "label": label, "url": url if isinstance(url, str) else None, "reach": reach,
                     "openable": openable and not replaced, "note": note, "source": source, "replaced_by": replaced})

    add("mc_console", "Mission Control console (as GRID CONTROL reports it)", (status or {}).get("mc_console"), True,
        "GRID CONTROL /api/status" if status else "GRID CONTROL /api/status (not available)")
    add("gridwatch", "GRID WATCH (as GRID CONTROL reports it)", (status or {}).get("gridwatch"), True,
        "GRID CONTROL /api/status" if status else "GRID CONTROL /api/status (not available)")
    add("mlflow_console", "MLflow UI (as the SIMLAB console reports it)", (summary or {}).get("mlflow"), True,
        "SIMLAB console /api/summary" if summary else "SIMLAB console /api/summary (not available)")
    add("grid_control", "GRID CONTROL (configured)", srv.get("grid_control"), False, "aces config server.grid_control")
    add("simlab_console", "SIMLAB console (configured)", srv.get("simlab_console"), False, "aces config server.simlab_console")
    add("mlflow", "MLflow (configured)", srv.get("mlflow"), False, "aces config server.mlflow")
    add("rig_state", "rig-state MCP (configured)", srv.get("rig_state"), False, "aces config server.rig_state")
    add("simlab_mcp", "simlab-mcp (configured)", srv.get("simlab_mcp"), False, "aces config server.simlab_mcp")
    if srv.get("webodm"):
        add("webodm", "WebODM (configured)", srv.get("webodm"), False, "aces config server.webodm")
    return True, {"links": rows, "n": len(rows), "note": "server-side allow-list; only these ids can be opened"}, None


def links_entry(app: server.App) -> cache_mod.Entry:
    return app.cache.get("system.links", 60.0, lambda: fetch_links(app), source="GRID CONTROL status + SIMLAB summary + aces config")


def h_links(req: Request) -> Response:
    return envelope(links_entry(req.app))


# --- open ------------------------------------------------------------------------------------------------------------------------

def launch(kind: str, target: str) -> tuple[bool, str]:
    """Open a URL or a .desktop entry on this desktop: xdg-open / gio launch (xdg-utils is a hard dependency on the
    target); on Windows (development) the default browser. Returns (ok, via)."""
    if kind == "entry":
        gio = shutil.which("gio")
        if gio:
            cmd, via = [gio, "launch", target], "gio launch"
        else:
            xdg = shutil.which("xdg-open")
            if not xdg:
                return False, "neither gio nor xdg-open is on PATH"
            cmd, via = [xdg, target], "xdg-open"
    else:
        xdg = shutil.which("xdg-open")
        if xdg:
            cmd, via = [xdg, target], "xdg-open"
        elif os.name == "nt":
            import webbrowser
            return bool(webbrowser.open(target)), "webbrowser (Windows development)"
        else:
            return False, "xdg-open is not on PATH"
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=(os.name != "nt"))
    except OSError as e:
        return False, f"{via}: {e.strerror or type(e).__name__}"
    return True, via


def h_open(req: Request) -> Response:
    app = req.app
    st = _state(app)
    if app.fixture:
        return error(503, "fixture mode: nothing is opened on this desktop", "fixture")
    body = req.json or {}
    id_ = body.get("id")
    if not isinstance(id_, str) or not id_ or len(id_) > 120:
        return error(400, "body.id must name a link or a desktop entry", "bad-request")
    target_kind = target = None
    link = next((l for l in (links_entry(app).data or {}).get("links", []) if l["id"] == id_), None)
    if link is not None:
        if not link["openable"]:
            return error(403, link.get("note") or f"{id_} is not openable from this desktop", "not-openable")
        target_kind, target = "url", link["url"]
    else:
        ent = app.hub.get("system.entries") if app.hub.source("system.entries") else None
        row = next((e for e in ((ent.data or {}).get("entries") or []) if e.get("id") == id_), None) if ent else None
        if row is None:
            return error(404, f"unknown id {id_}: not an openable link and not an aces desktop entry", "not-found")
        target_kind, target = "entry", row["path"]
    okd, via = st.launcher(target_kind, target)
    n, lerr = _record(app, "console.system.open", {"id": id_, "target_kind": target_kind, "ok": okd, "via": via,
                                                   "peer": req.peer})
    if not okd:
        return error(503, f"could not open {id_}: {via}", "launch-failed")
    return ok({"opened": True, "id": id_, "target_kind": target_kind, "via": via, "ledger_n": n, "ledger_error": lerr},
              source="desktop launcher")


# --- stop -----------------------------------------------------------------------------------------------------------------------

def h_stop(req: Request) -> Response:
    app = req.app
    st = _state(app)
    body = req.json or {}
    if body.get("confirm") != "stop":
        return error(400, 'confirm must be the exact string "stop"', "bad-confirm")
    if app.stopping:
        return error(409, "the console is already stopping", "busy")
    payload = {"peer": req.peer, "clients": app.broker.count(), "uptime_s": round(time.time() - st.started_ts, 1),
               "user_agent": (req.headers.get("User-Agent") or "")[:200] if req.headers is not None else "",
               "ui": server.UI_NAME, "pid": os.getpid()}
    n, lerr = _record(app, "console.server.stop", payload)
    app.request_stop()
    return ok({"stopping": True, "ledger_n": n, "ledger_error": lerr}, source="console")


# --- install -------------------------------------------------------------------------------------------------------------------

def install(register) -> None:
    app = server.current_app()
    _state(app)
    hub = app.hub
    hub.register("doctor", lambda: fetch_doctor(app), 300.0, ("ops",), inline=False, source="aces doctor (this session)")
    hub.register("system.packages", lambda: fetch_packages(app), 300.0, ("ops",), inline=False, source="pacman -Q")
    hub.register("system.entries", lambda: fetch_entries(app), 300.0, ("ops",), inline=False, source=str(APPLICATIONS_DIR))
    register("GET", "/api/policy", h_policy)
    register("GET", "/api/doctor", h_doctor)
    register("POST", "/api/doctor/run", h_doctor_run)
    register("GET", "/api/system/packages", h_packages)
    register("GET", "/api/system/entries", h_entries)
    register("GET", "/api/system/links", h_links)
    register("POST", "/api/system/open", h_open)
    register("POST", "/api/system/stop", h_stop)
