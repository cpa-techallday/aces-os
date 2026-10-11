#!/usr/bin/env python3
"""aces-console fixture devserver (Builder C's harness; standard library only).

Serves the static front-end tree the way the real backend will (the page at /, the tree at / and /static/, the
CSP with the importmap hash, the headers of spec 3.1) and answers the API routes the shell and its plates call
with the real read-only captures of 2026-10-10/11 wrapped in the envelope of spec 3.1 and stamped
"FIXTURE 2026-10-11", plus the real desktop records of a work store when ACES_WORK (or --vm-work) names one,
read through the `aces` package in process. Nothing is sampled, seeded or invented: a route whose capture is
absent answers data:null with the reason, and every consequential action (the switch, fabric refresh, open) is
refused with HTTP 503 code "fixture" after the POST guards have run. No token file is ever read; no rig host is
ever contacted; the only writes are to stdout/stderr.

    python devserver.py [--port 8070] [--bind 127.0.0.1] [--static DIR] [--samples DIR] [--vm-work ROOT] [--no-sse] [--quiet]

With --port 0 an ephemeral port is used and the first stdout line is "listening http://127.0.0.1:<port>".
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import platform
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.abspath(os.path.join(HERE, "..", ".."))            # src/aces-console
REPO = os.path.abspath(os.path.join(PKG, "..", ".."))             # the aces-os checkout
if HERE not in sys.path:   # `python -I` (3.11+) does not put the script's directory on sys.path
    sys.path.insert(0, HERE)
try:  # Builder D: the records routes of spec 3.6-3.11 (golden, CAD, SIMLAB detail, MLflow run, ledger verify and the
    import records_routes  # rig-side chain) beside this file, read from the work store and the captures; optional
except ImportError as _e:
    records_routes = None
    sys.stderr.write(f"devserver: records_routes not loaded ({_e}); the records routes answer 'not available'\n")
FIXTURE = "FIXTURE 2026-10-11"
VERSION = "0.1.0"
MAX_BODY = 65536
MAX_STREAMS = 32
RING = 512
LOOPBACK = ("127.0.0.1", "::1")
CTYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".json": "application/json", ".woff2": "font/woff2", ".woff": "font/woff",
    ".svg": "image/svg+xml", ".png": "image/png", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".ico": "image/x-icon", ".map": "application/json",
}
MASK_RE = re.compile(r"(?i)token|secret|passw|api[_-]?key|authorization")

# topic -> (route, ttl_s, plates ("*" or list), inline payload, projector) -- spec 3.2 mapped to the contract plate ids
TOPICS = {
    "rig.status": ("/api/rig/status", 3, "*", True, None),
    "rig.switch": ("/api/rig/switch", 3, "*", True, None),
    "fabric.manifest": ("/api/fabric", 5, "*", True, lambda d: {"age_s": d.get("age_s"), "stale": bool(d.get("stale")), "path": d.get("path")} if d else None),
    "console.health": ("/api/health", 5, "*", True, None),
    "ledger.aces": ("/api/ledger?n=1", 5, ["evidence"], False, None),
    "rig.modes": ("/api/rig/modes", 30, ["fabric", "ops", "solvers"], True, None),
    "rig.telemetry": ("/api/rig/telemetry", 3, ["fabric"], True, lambda d: ((d.get("history") or [None])[-1]) if d else None),
    "rig.snapshot": ("/api/rig/snapshot", 30, ["fabric", "ops"], False, None),
    "rig.decisions": ("/api/rig/decisions", 60, ["fabric", "ops"], False, None),
    "sim.capacity": ("/api/sim/capacity", 5, ["fabric", "pipeline", "solvers"], True, None),
    "sim.summary": ("/api/sim/summary", 20, ["pipeline"], False, None),
    "sim.experiments": ("/api/sim/experiments", 4, ["pipeline"], False, None),
    "sim.registry": ("/api/sim/registry", 60, ["solvers"], False, None),
    "sim.evidence": ("/api/sim/evidence", 30, ["evidence"], False, None),
    "sim.evidence.verify": ("/api/sim/evidence/verify", 30, ["evidence"], False, None),
    "sim.audit": ("/api/sim/audit", 30, ["evidence"], False, None),
    "sim.whoami": ("/api/sim/whoami", 300, ["pipeline", "ops"], False, None),
    "mlflow.experiments": ("/api/mlflow/experiments", 60, ["pipeline"], False, None),
    "projects": ("/api/projects", 10, ["projects"], False, None),
    "golden.runs": ("/api/golden/runs", 10, ["evidence"], False, None),
    "ledger.simlab": ("/api/ledger/simlab", 30, ["evidence"], False, None),
    "doctor": ("/api/doctor", 300, ["ops"], False, None),
    "system.packages": ("/api/system/packages", 300, ["ops"], False, None),
    "system.entries": ("/api/system/entries", 300, ["ops"], False, None),
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def age_of(iso):
    t = parse_utc(iso)
    return None if t is None else round(time.time() - t, 3)


def sha12(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:12]


def mask(obj):
    """The encoder's masking of spec 3.1: values of token-like keys become "[masked]"."""
    if isinstance(obj, dict):
        return {k: ("[masked]" if MASK_RE.search(str(k)) else mask(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(v) for v in obj]
    return obj


# ---------- environment and the aces package ----------
def prepare_env(args) -> None:
    os.environ.setdefault("ACES_ETC", os.path.join(REPO, "src", "aces-base", "etc"))
    if args.vm_work:
        # an explicit --vm-work names the store to read: it beats the environment (the test package points ACES_WORK,
        # ACES_STATE, ACES_USER_CONFIG and XDG_DATA_HOME at its own scratch tree before any test starts this server)
        root = os.path.abspath(args.vm_work)
        os.environ["ACES_WORK"] = os.path.join(root, "Work")
        os.environ["ACES_USER_CONFIG"] = os.path.join(root, ".config", "aces")
        os.environ["ACES_STATE"] = os.path.join(root, ".local", "state", "aces")
        os.environ["XDG_DATA_HOME"] = os.path.join(root, ".local", "share")


class Aces:
    """The aces package (aces-base) imported in process, or the reason it could not be."""

    def __init__(self):
        self.error = None
        try:
            try:
                import aces  # noqa: F401
            except ImportError:
                sys.path.insert(0, os.path.join(REPO, "src", "aces-base"))
            import aces
            from aces import config, doctor, fabric, ledger, policy, project
            self.aces, self.config, self.doctor, self.fabric, self.ledger, self.policy, self.project = aces, config, doctor, fabric, ledger, policy, project
            self.cfg = config.load()
        except Exception as e:  # noqa: BLE001 - reported, never hidden
            self.error = f"aces package not importable: {type(e).__name__}: {e}"
            self.cfg = {"server": {}, "fabric": {"manifest": None, "max_age_s": 900}}

    def ok(self) -> bool:
        return self.error is None


# ---------- the captures ----------
class Samples:
    def __init__(self, dirs):
        self.dirs = [d for d in dirs if d and os.path.isdir(d)]
        self.manifest = {}
        for d in self.dirs:
            p = os.path.join(d, "MANIFEST.json")
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8") as fh:
                        m = json.load(fh)
                except (OSError, ValueError):
                    continue
                rows = m.get("files") if isinstance(m, dict) else m
                for row in rows or []:
                    if isinstance(row, dict) and row.get("file"):
                        self.manifest.setdefault(row["file"], row)

    def get(self, name):
        """-> (obj, info) with info {file, path, captured_utc, sha256}; (None, None) when no copy exists."""
        fn = name + ".json"
        for d in self.dirs:
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    raw = fh.read()
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except ValueError as e:
                    return None, {"file": fn, "path": p, "error": f"not JSON: {e}"}
                info = dict(self.manifest.get(fn) or {})
                info.update({"file": fn, "path": p, "sha256": hashlib.sha256(raw).hexdigest()})
                info.setdefault("captured_utc", None)
                return obj, info
        return None, None


# ---------- envelopes ----------
def envelope(data, source, fetched_utc, ttl_s, error=None, stale=False):
    return {"ok": True, "data": data, "meta": {
        "source": source, "fetched_utc": fetched_utc, "age_s": age_of(fetched_utc), "ttl_s": ttl_s, "stale": bool(stale),
        "ok": error is None, "error": error, "next_try_s": None, "fixture": FIXTURE}}


def capture_envelope(data, info, ttl_s, source_route):
    src = f"{source_route} (replayed from {info['file']}, captured {info.get('captured_utc') or 'time not recorded'})"
    return 200, envelope(data, src, info.get("captured_utc"), ttl_s)


def missing(route, ttl_s, what):
    return 200, envelope(None, route, None, ttl_s, error=f"not available to the fixture devserver: {what}")


def error(status, msg, code, source=None, upstream=None):
    return status, {"ok": False, "error": msg, "code": code, "source": source, "status": upstream, "next_try_s": None}


def local_envelope(data, source, ttl_s, error_msg=None):
    return 200, envelope(data, source, utcnow(), ttl_s, error=error_msg)


# ---------- derived views (the rules of spec 3.4 and 3.6, applied to the captures) ----------
def norm_queue(q):
    """-1 (or any value below zero) becomes null with the reporting sentence; the raw value is kept beside it."""
    note = None
    out = dict(q) if isinstance(q, dict) else {}
    for k in ("queued", "running"):
        v = out.get(k)
        if isinstance(v, (int, float)) and v < 0:
            out[k] = None
            note = f"GRID CONTROL reported {int(v)}"
        elif v is None:
            out[k] = None
    return out, note


def classify(snapshot, mode, detected, sim_modes):
    stop = list(((sim_modes or {}).get(mode or "") or {}).get("stop_services") or [])

    def stopped_by(name):
        for s in stop:
            if name == s or name.startswith(s + "-"):
                return s
        return None

    out = []
    for e in snapshot.get("endpoints") or []:
        name = e.get("name", "")
        if e.get("reachable") is True:
            out.append({"name": name, "kind": "endpoint", "verdict": "reachable", "matched": None, "rule": "rig-state reports the endpoint reachable"})
            continue
        m = stopped_by(name)
        if m:
            out.append({"name": name, "kind": "endpoint", "verdict": f"stopped by mode {mode} (by design)", "matched": m, "rule": f"{m} is in sim_modes[{mode}].stop_services"})
        elif detected == "transition":
            out.append({"name": name, "kind": "endpoint", "verdict": "transition", "matched": None, "rule": "GRID CONTROL detected_mode is transition"})
        else:
            out.append({"name": name, "kind": "endpoint", "verdict": "unreachable (not explained by mode)", "matched": None, "rule": f"unreachable and not in sim_modes[{mode}].stop_services"})
    for s in snapshot.get("services") or []:
        name = s.get("name", "")
        if s.get("activeState") == "active":
            out.append({"name": name, "kind": "service", "verdict": "reachable", "matched": None, "rule": "systemd activeState is active"})
            continue
        m = stopped_by(name)
        if m:
            out.append({"name": name, "kind": "service", "verdict": f"stopped by mode {mode} (by design)", "matched": m, "rule": f"{m} is in sim_modes[{mode}].stop_services"})
        elif detected == "transition":
            out.append({"name": name, "kind": "service", "verdict": "transition", "matched": None, "rule": "GRID CONTROL detected_mode is transition"})
        else:
            out.append({"name": name, "kind": "service", "verdict": "unreachable (not explained by mode)", "matched": None, "rule": f"inactive and not in sim_modes[{mode}].stop_services"})
    return out


def activity(g, mode, modes):
    if g.get("job"):
        return "job"
    if g.get("lease"):
        return "lease"
    if g.get("in_pool") and not g.get("lease") and not g.get("job") and (g.get("processes") or []):
        return "outside the queue (operator hand-run)"
    rest = ((modes or {}).get(mode or "") or {}).get("rest") or []
    if g.get("index") in rest:
        return f"rests by mode {mode}"
    if not g.get("in_pool"):
        return "not in the sim pool"
    if g.get("derated"):
        return "derated"
    return "idle"


def reach_of(url):
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    if host in ("127.0.0.1", "localhost", "::1", "host.docker.internal"):
        return "rig-loopback", False, "rig-local address; not reachable from this desktop"
    return "lan", True, None


# ---------- the server ----------
class DevServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, *, static_dir, samples, aces, no_sse=False, quiet=False):
        super().__init__(addr, handler)
        self.static_dir = os.path.abspath(static_dir)
        self.samples = samples
        self.aces = aces
        self.no_sse = no_sse
        self.quiet = quiet
        self.started_utc = utcnow()
        self.lock = threading.Lock()
        self.seq = 0
        self.ring = []
        self.streams = 0
        self.stopping = False
        self.refused_posts = 0
        self.doctor = {"text": None, "exit": None, "ran_at": None, "running": False}
        self.origin = f"http://{self.server_address[0]}:{self.server_address[1]}"
        with open(os.path.join(self.static_dir, "index.html"), "rb") as fh:
            self.index_bytes = fh.read()
        m = re.search(rb'<script type="importmap">(.*?)</script>', self.index_bytes, re.S)
        digest = base64.b64encode(hashlib.sha256(m.group(1) if m else b"").digest()).decode("ascii")
        self.csp = ("default-src 'self'; script-src 'self' 'sha256-%s'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                    "font-src 'self'; connect-src 'self'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'" % digest)
        self.static_sha = self._static_manifest_sha()

    def _static_manifest_sha(self) -> str:
        h = hashlib.sha256()
        for root, dirs, files in os.walk(self.static_dir):
            dirs.sort()
            for fn in sorted(files):
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, self.static_dir).replace(os.sep, "/")
                with open(p, "rb") as fh:
                    h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(fh.read()).digest())
        return h.hexdigest()

    def next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def remember(self, stamp) -> None:
        with self.lock:
            self.ring.append(stamp)
            if len(self.ring) > RING:
                del self.ring[: len(self.ring) - RING]

    def replay_after(self, last_id):
        with self.lock:
            later = [s for s in self.ring if s["seq"] > last_id]
        latest = {}
        for s in later:
            latest[s["topic"]] = s
        return sorted(latest.values(), key=lambda s: s["seq"])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "aces-console-devserver/" + VERSION
    sys_version = ""

    # ---- plumbing ----
    def log_message(self, fmt, *args):  # the devserver logs method, path, status and duration itself (never a query)
        return

    def _log(self, method, path, status, t0):
        if not self.server.quiet:
            sys.stderr.write(f"{utcnow()} {method} {path} {status} {int((time.monotonic() - t0) * 1000)}ms\n")
            sys.stderr.flush()

    def _common_headers(self, api=False):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if api:
            self.send_header("Cache-Control", "no-store")

    def _send_json(self, status, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._common_headers(api=True)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return status

    def _send_bytes(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return status

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        t0 = time.monotonic()
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        status = 500
        try:
            if self.client_address[0] not in LOOPBACK:
                status = self._send_json(*error(403, "only loopback peers are served", "refused"))
            elif method == "GET" and path == "/api/events":
                status = self._sse(query)
            elif path.startswith("/api/") or path == "/vocab.json":
                if method == "POST":
                    status = self._post(path, query)
                elif records_routes is not None and records_routes.is_file_route(path):
                    status = records_routes.send_file(self, path, query)   # bytes under CSP: sandbox, not JSON
                else:
                    st, obj = self.server_get(path, query)
                    status = self._send_json(st, obj)
            elif method == "POST":
                status = self._send_json(*error(404, "no such route in the fixture devserver", "not-found", source=path))
            elif path == "/favicon.ico":
                self.send_response(204)
                self._common_headers()
                self.send_header("Content-Length", "0")
                self.end_headers()
                status = 204
            else:
                status = self._static(path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            status = 499
        except Exception as e:  # noqa: BLE001 - a console bug is one line to the client, the trace to stderr
            sys.stderr.write(f"devserver error on {method} {path}: {type(e).__name__}: {e}\n")
            try:
                status = self._send_json(*error(500, f"devserver bug: {type(e).__name__}: {e}", "bug", source=path))
            except Exception:  # noqa: BLE001
                status = 500
        self._log(method, path, status, t0)

    # ---- static ----
    def _static(self, path):
        rel = path
        if rel.startswith("/static/"):
            rel = rel[len("/static/"):]
        rel = rel.lstrip("/")
        if rel in ("", "index.html"):
            return self._send_bytes(200, self.server.index_bytes, "text/html; charset=utf-8", {"Content-Security-Policy": self.server.csp, "Cache-Control": "no-cache"})
        parts = rel.split("/")
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            return self._send_json(*error(404, "no such file", "not-found", source=path))
        full = os.path.normpath(os.path.join(self.server.static_dir, *parts))
        if not full.startswith(self.server.static_dir + os.sep) or not os.path.isfile(full) or os.path.islink(full):
            return self._send_json(*error(404, "no such file", "not-found", source=path))
        ext = os.path.splitext(full)[1].lower()
        ctype = CTYPES.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        return self._send_bytes(200, body, ctype, {"Cache-Control": "no-cache"})

    # ---- POST guards of spec 3.1 ----
    def _post(self, path, query):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.server.refused_posts += 1
            return self._send_json(*error(413, "body too large (limit 64 KiB)", "too-large", source=path))
        raw = self.rfile.read(length) if length else b""
        origin = self.headers.get("Origin")
        intent = self.headers.get("X-ACES-Intent")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        why = None
        if origin != self.server.origin:
            why = "Origin is not this console's origin"
        elif intent != "human":
            why = "X-ACES-Intent: human is missing"
        elif ctype != "application/json":
            why = "Content-Type is not application/json"
        body = None
        if why is None:
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                why = "body is not JSON"
            if why is None and not isinstance(body, dict):
                why = "body is not a JSON object"
        if why:
            self.server.refused_posts += 1
            return self._send_json(*error(403, f"refused: {why}", "refused", source=path))
        return self._send_json(*self.server_post(path, body))

    # ---- routing ----
    def server_get(self, path, query):
        s = self.server
        S = s.samples
        A = s.aces
        if path == "/vocab.json":
            for p in (os.path.join(PKG, "aces_console", "vocab.json"), os.path.join(HERE, "vocab.json")):
                if os.path.isfile(p):
                    with open(p, encoding="utf-8") as fh:
                        return 200, json.load(fh)
            return error(404, "vocab.json not found", "not-found", source=path)
        if path == "/api/health":
            return 200, {"ok": True, "version": VERSION, "pid": os.getpid(), "started_utc": s.started_utc, "bind": s.server_address[0], "port": s.server_address[1],
                         "origin": s.origin, "clients": s.streams, "poller": "fixture replay", "fixture": FIXTURE,
                         "sources": {t: {"fetched_utc": (S.manifest.get(os.path.basename(TOPICS[t][0]) + ".json") or {}).get("captured_utc"), "ok": True, "error": None, "next_try_s": None, "backoff_s": 0} for t in TOPICS},
                         "counters": {"refused_posts": s.refused_posts, "suppressed_refusals": 0}, "switch_outcome_pending": None,
                         "static_manifest_sha256": s.static_sha, "aces_base_version": getattr(A.aces, "__version__", None) if A.ok() else None,
                         "aces_error": A.error, "devserver": True}
        if path == "/api/config":
            cfg = A.cfg
            return local_envelope({
                "server": cfg.get("server", {}),
                "console": {"bind": s.server_address[0], "port": s.server_address[1], "origin": s.origin, "state_dir": os.environ.get("ACES_STATE") or (str(A.config.STATE) if A.ok() else None), "static_dir": s.static_dir},
                "tokens": {"grid_control": {"configured": False, "source": "not set"}, "simlab": {"configured": False, "source": "not set"}},
                "paths": {"work": str(A.config.WORK) if A.ok() else os.environ.get("ACES_WORK"), "state": str(A.config.STATE) if A.ok() else os.environ.get("ACES_STATE"),
                          "user_config": str(A.config.USER_CONFIG) if A.ok() else os.environ.get("ACES_USER_CONFIG"), "data_dir": os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"), "aces")},
                "platform": {"os": f"{platform.system()} {platform.release()}", "python": sys.version.split()[0]},
                "note": "fixture devserver: no token file is read; the real backend reports the token source names, never a value",
            }, "devserver configuration", 60, None if A.ok() else A.error)
        if path == "/api/rig/status":
            st, info = S.get("gc-status")
            if st is None:
                return missing(path, 3, "gc-status.json")
            q, note = norm_queue(st.get("queue"))
            data = dict(st)
            data["queue"] = q
            data["queue_note"] = note
            data["raw_queue"] = st.get("queue")
            return capture_envelope(data, info, 3, "GRID CONTROL GET /api/status")
        if path == "/api/rig/modes":
            gm, info = S.get("gc-modes")
            sm, info2 = S.get("simlab-modes")
            st, _ = S.get("gc-status")
            if gm is None:
                return missing(path, 30, "gc-modes.json")
            gridcontrol = {k: {"label": v.get("label"), "switchable": v.get("switchable") is True, "switchable_reported": "switchable" in v, "pending": v.get("pending")} for k, v in gm.items()}
            data = {"gridcontrol_modes": gridcontrol, "sim_modes": (sm or {}).get("sim_modes") if sm else None, "current": (st or {}).get("mode") if st else None,
                    "sim_modes_note": None if sm else "simlab-modes.json not available to the fixture devserver"}
            return capture_envelope(data, info, 30, "GRID CONTROL GET /api/modes + SIMLAB console capacity.modes")
        if path == "/api/rig/telemetry":
            tel, info = S.get("gc-telemetry")
            con, _ = S.get("simlab-telemetry")
            if tel is None:
                return missing(path, 3, "gc-telemetry.json")
            history = []
            for h in (con or {}).get("history") or []:
                h = dict(h)
                h["src"] = "console"
                history.append(h)
            return capture_envelope({"telemetry": tel, "history": history, "sources_used": ["gc", "console"] if con else ["gc"]}, info, 3, "GRID CONTROL GET /api/telemetry")
        if path == "/api/rig/snapshot":
            snap, info = S.get("rigstate-snapshot")
            st, _ = S.get("gc-status")
            sm, _ = S.get("simlab-modes")
            if snap is None:
                return missing(path, 30, "rigstate-snapshot.json")
            sc = (snap.get("result") or {}).get("structuredContent") or {}
            data = dict(sc)
            data["classification"] = classify(sc, (st or {}).get("mode"), (st or {}).get("detected_mode"), (sm or {}).get("sim_modes"))
            return capture_envelope(data, info, 30, "rig-state tools/call rig_snapshot")
        if path == "/api/rig/switch":
            sw, info = S.get("gc-switch-status")
            if sw is None:
                return missing(path, 3, "gc-switch-status.json")
            data = dict(sw)
            data["log_tail"] = list(sw.get("log") or [])[-3:]
            return capture_envelope(data, info, 3, "GRID CONTROL GET /api/switch/status")
        if path == "/api/rig/decisions":
            # a GET answers 200 with the envelope and meta.error (spec 3.1); a 503 on a GET would be logged by the browser as a
            # console error, which the harness forbids, so the "not configured" absence rides in meta.error here
            return 200, envelope(None, "GRID CONTROL GET /api/decisions", None, 60, error="not configured: GRID CONTROL token (the fixture devserver reads no token file)")
        if path == "/api/fabric":
            if not A.ok():
                return local_envelope(None, "aces.fabric.read", 5, A.error)
            cfg = A.cfg
            mp = A.fabric.manifest_path(cfg)
            try:
                man = A.fabric.read(cfg)
            except SystemExit as e:
                return local_envelope(None, str(mp), 5, f"manifest unreadable: {e}")
            ag = A.fabric.age_s(cfg)
            max_age = cfg["fabric"].get("max_age_s", 900)
            data = {"manifest": man, "path": str(mp), "age_s": None if ag is None else round(ag, 1), "max_age_s": max_age, "stale": bool(ag is not None and ag > max_age), "modes_table": A.config.modes_table()}
            if man is None:
                data["reason"] = "no manifest yet; run aces fabric refresh"
            return local_envelope(data, str(mp), 5)
        if path == "/api/sim/capacity":
            cap, info = S.get("simlab-capacity")
            if cap is None:
                return missing(path, 5, "simlab-capacity.json")
            data = dict(cap)
            q, note = norm_queue(cap.get("gridcontrol_queue"))
            data["gridcontrol_queue"] = q
            data["queue_note"] = note
            data["gpus"] = [dict(g, activity=activity(g, cap.get("mode"), cap.get("modes"))) for g in cap.get("gpus") or []]
            return capture_envelope(data, info, 5, "SIMLAB console GET /api/capacity")
        if path == "/api/sim/summary":
            d, info = S.get("simlab-summary")
            return capture_envelope(d, info, 20, "SIMLAB console GET /api/summary") if d else missing(path, 20, "simlab-summary.json")
        if path == "/api/sim/whoami":
            d, info = S.get("simlab-whoami")
            if d is None:
                return missing(path, 300, "simlab-whoami.json")
            res = ((d.get("result") or {}).get("structuredContent") or {}).get("result") or {}
            return capture_envelope(dict(res, token_source="not set"), info, 300, "simlab MCP sim_whoami")
        if path == "/api/sim/registry":
            d, info = S.get("simlab-registry")
            if d is None:
                return missing(path, 60, "simlab-registry.json")
            rows = {}
            for name, row in d.items():
                if name.startswith("_") or not isinstance(row, dict):
                    continue
                v = row.get("validation") or {}
                status = v.get("status")
                if status == "pass" and v.get("image_id"):
                    note = "pass"
                elif status == "pass":
                    note = "pass; image id not recorded, not citable against a running image"
                else:
                    note = str(status) if status else "not recorded"
                rows[name] = dict(row, citable=bool(status == "pass" and v.get("image_id")), citable_note=note, lane=row.get("queue") if row.get("queue") is not None else "not recorded")
            return capture_envelope({"solvers": rows}, info, 60, "SIMLAB console GET /api/registry")
        if path == "/api/sim/experiments":
            d, info = S.get("simlab-experiments")
            if d is None:
                return missing(path, 4, "simlab-experiments.json (464 KB; not copied into the harness fixtures)")
            by_rq, by_meta = {}, {}
            for j in d.get("queue") or []:
                by_rq[j.get("status") or "unknown"] = by_rq.get(j.get("status") or "unknown", 0) + 1
                m = (j.get("meta") or {}).get("state") or "not recorded"
                by_meta[m] = by_meta.get(m, 0) + 1
            return capture_envelope(dict(d, queue_by_rq_status=by_rq, queue_by_meta_state=by_meta), info, 4, "SIMLAB console GET /api/experiments")
        if path == "/api/sim/evidence":
            d, info = S.get("simlab-evidence")
            return capture_envelope(d, info, 30, "SIMLAB console GET /api/evidence") if d else missing(path, 30, "simlab-evidence.json")
        if path == "/api/sim/evidence/verify":
            d, info = S.get("simlab-evidence-verify")
            return capture_envelope(d, info, 30, "SIMLAB console GET /api/evidence/verify") if d else missing(path, 30, "simlab-evidence-verify.json")
        if path == "/api/sim/audit":
            d, info = S.get("simlab-audit")
            return capture_envelope(d, info, 30, "SIMLAB console GET /api/audit") if d else missing(path, 30, "simlab-audit.json (194 KB; not copied into the harness fixtures)")
        if path == "/api/mlflow/experiments":
            d, info = S.get("mlflow-experiments")
            return capture_envelope(d, info, 60, "MLflow GET /api/2.0/mlflow/experiments/search") if d else missing(path, 60, "mlflow-experiments.json")
        if path == "/api/projects":
            return self._projects()
        m = re.fullmatch(r"/api/projects/([A-Za-z0-9][A-Za-z0-9._-]{0,63})", path)
        if m:
            return self._project(m.group(1))
        if path == "/api/ledger":
            return self._ledger(query)
        if path == "/api/policy":
            if not A.ok():
                return local_envelope(None, "aces.policy.load", 30, A.error)
            try:
                pol = A.policy.load()
            except SystemExit as e:
                return local_envelope(None, "aces.policy.load", 30, f"policy unreadable: {e}")
            up = A.config.USER_CONFIG / "policy.json"
            return local_envelope({"policy": pol, "override": {"present": up.exists(), "path": str(up)}}, str(A.config.ETC / "policy.json"), 30)
        if path == "/api/doctor":
            d = s.doctor
            if d["text"] is None:
                return local_envelope({"text": None, "note": "doctor has not been run in this session", "running": d["running"]}, "aces.doctor.checks", 300)
            return local_envelope({"text": d["text"], "exit": d["exit"], "ran_at": d["ran_at"], "running": d["running"]}, "aces.doctor.checks (offline: the five rig probes skipped)", 300)
        if path == "/api/system/packages":
            return local_envelope({"packages": [], "queried_utc": utcnow(), "note": f"pacman not found on this host ({platform.system()}); the fixture devserver does not run package managers"}, "pacman -Q", 300)
        if path == "/api/system/entries":
            return local_envelope({"entries": [], "note": "no /usr/share/applications owned by aces-* packages on this host; the fixture devserver does not query pacman -Qo"}, "/usr/share/applications", 300)
        if path == "/api/system/links":
            st, _ = S.get("gc-status")
            su, _ = S.get("simlab-summary")
            srv = A.cfg.get("server", {})
            links = []

            def add(id_, label, url):
                if not url:
                    links.append({"id": id_, "label": label, "url": None, "reach": None, "openable": False, "note": "not reported"})
                    return
                reach, openable, note = reach_of(url)
                links.append({"id": id_, "label": label, "url": url, "reach": reach, "openable": openable, "note": note})

            add("mc_console", "Mission Control console (as GRID CONTROL reports it)", (st or {}).get("mc_console"))
            add("gridwatch", "GRID WATCH (as GRID CONTROL reports it)", (st or {}).get("gridwatch"))
            add("mlflow", "MLflow UI (as the SIMLAB console reports it)", (su or {}).get("mlflow"))
            add("grid_control", "GRID CONTROL (configured)", srv.get("grid_control"))
            add("simlab_console", "SIMLAB console (configured)", srv.get("simlab_console"))
            add("rig_state", "rig-state MCP (configured)", srv.get("rig_state"))
            if srv.get("webodm"):
                links.append({"id": "webodm", "label": "WebODM (configured, desktop-local)", "url": srv["webodm"], "reach": "desktop-loopback", "openable": True, "note": None})
            return local_envelope({"links": links}, "GRID CONTROL status + SIMLAB summary + aces config", 60)
        if records_routes is not None:
            hit = records_routes.handle_get(self, path, query)
            if hit is not None:
                return hit
        if path in ("/api/golden/runs", "/api/ledger/simlab") or path.startswith(("/api/golden/", "/api/cad/", "/api/sim/", "/api/mlflow/", "/api/mc/", "/api/rig/redteam", "/api/rig/downloads")):
            return missing(path, 10, "this route belongs to Builder B's backend; the fixture devserver serves only what Builder C's plates call")
        return error(404, "no such route in the fixture devserver", "not-found", source=path)

    def server_post(self, path, body):
        s = self.server
        if path == "/api/system/stop":
            if body.get("confirm") != "stop":
                return error(400, 'confirm must be the exact string "stop"', "bad-confirm", source=path)
            s.stopping = True
            threading.Thread(target=self._shutdown_later, daemon=True).start()
            return 200, {"ok": True, "stopping": True, "ledger": "not written by the fixture devserver"}
        if path == "/api/doctor/run":
            if not s.aces.ok():
                return error(503, s.aces.error, "not-installed", source=path)
            if s.doctor["running"]:
                return error(409, "doctor is already running", "busy", source=path)
            s.doctor["running"] = True
            threading.Thread(target=self._run_doctor, daemon=True).start()
            return 202, {"ok": True, "started": True, "note": "aces doctor --offline (the five rig probes are skipped by this devserver)"}
        if records_routes is not None:
            hit = records_routes.handle_post(self, path, body)   # golden verify and the bundle check: reads, run for real
            if hit is not None:
                return hit
        if path in ("/api/fabric/refresh", "/api/rig/switch/prepare", "/api/rig/switch", "/api/system/open"):
            what = {"/api/fabric/refresh": "fabric refresh", "/api/rig/switch/prepare": "mode switch (prepare)", "/api/rig/switch": "mode switch (confirm)", "/api/system/open": "open"}[path]
            return error(503, f"not performed by the fixture devserver: {what} (no rig is contacted and nothing is ledgered here)", "fixture", source=path)
        if path.startswith("/api/"):
            return error(503, "not performed by the fixture devserver", "fixture", source=path)
        return error(404, "no such route in the fixture devserver", "not-found", source=path)

    def _shutdown_later(self):
        time.sleep(0.6)
        self.server.shutdown()

    def _run_doctor(self):
        A = self.server.aces
        d = self.server.doctor
        try:
            rows = A.doctor.checks(A.cfg, offline=True)
            d["text"] = A.doctor.render(rows)
            d["exit"] = 1 if any(r[1] == "FAIL" for r in rows) else 0
        except Exception as e:  # noqa: BLE001
            d["text"] = f"doctor failed: {type(e).__name__}: {e}"
            d["exit"] = 1
        d["ran_at"] = utcnow()
        d["running"] = False

    # ---- desktop records through the aces package ----
    def _ledger_entries(self):
        A = self.server.aces
        try:
            es = A.ledger.entries()
        except SystemExit as e:
            return None, f"ledger unreadable: {e}"
        return es, None

    def _projects(self):
        A = self.server.aces
        if not A.ok():
            return local_envelope(None, "aces.project.list_projects", 10, A.error)
        work = A.config.WORK
        if not work.exists():
            return local_envelope({"projects": [], "reason": f"{work} does not exist", "root": str(work)}, str(work), 10)
        es, lerr = self._ledger_entries()
        rows = []
        for p in A.project.list_projects():
            try:
                d = A.project._load(p)
            except (OSError, UnicodeDecodeError) as e:
                rows.append({"name": p.parent.name, "path": str(p), "class": None, "class_source": "aces.yaml", "unreadable": f"{type(e).__name__}: {e}", "counts": {}})
                continue
            name = d.get("name") or p.parent.name
            rows.append(self._project_row(name, p, d, es))
        return local_envelope({"projects": rows, "reason": None, "root": str(work), "ledger_note": lerr}, str(work), 10)

    def _project_row(self, name, p, d, es):
        pdir = p.parent
        golden = sorted(pdir.glob("golden/*/run.json"))
        cad_runs_dir = pdir / "cad" / "local" / "runs"
        cad = [x for x in cad_runs_dir.iterdir() if x.is_dir()] if cad_runs_dir.is_dir() else []
        n_ledger = sum(1 for e in es or [] if isinstance(e.get("payload"), dict) and (e["payload"].get("project") == name or e["payload"].get("name") == name))
        cls = d.get("class")
        return {
            "name": name, "path": str(p), "class": cls, "class_source": "aces.yaml", "created": d.get("created"), "reclassified": d.get("reclassified"),
            "company": d.get("company") or "", "rd_activity": d.get("rd_activity") or "", "hypothesis": d.get("hypothesis") or "",
            "components": list(d.get("components") or []), "evidence": list(d.get("evidence") or []), "cad_sources": list(d.get("cad_sources") or []),
            "counts": {"golden_runs": len(golden), "cad_runs": len(cad), "ledger_entries": n_ledger},
            "onshape_gate": "allowed" if cls in ("personal", "open", "commercial") else "refused for this class",
        }

    def _project(self, name):
        A = self.server.aces
        if not A.ok():
            return local_envelope(None, "aces.project.show", 10, A.error)
        p = A.project.path(name)
        if not p.exists():
            return error(404, f"no project at {p}", "not-found", source=str(p))
        es, lerr = self._ledger_entries()
        d = A.project._load(p)
        row = self._project_row(d.get("name") or name, p, d, es)
        pdir = p.parent
        golden = []
        for rj in sorted(pdir.glob("golden/*/run.json")):
            try:
                if rj.stat().st_size > 1048576:
                    raise OSError("run.json exceeds 1 MiB")
                with open(rj, encoding="utf-8") as fh:
                    r = json.load(fh)
            except (OSError, ValueError) as e:
                golden.append({"run_id": rj.parent.name, "project": name, "run_dir": str(rj.parent), "status": "error", "unreadable": f"run.json unreadable: {e}"})
                continue
            golden.append({"run_id": r.get("run_id") or rj.parent.name, "project": r.get("project") or name, "run_dir": str(rj.parent), "created_utc": r.get("created_utc"), "finished_utc": r.get("finished_utc"),
                           "status": r.get("status"), "outcome": r.get("outcome"), "verdict": r.get("verdict"), "grade": r.get("grade"), "exit_code": r.get("exit_code"),
                           "wall_s": (r.get("timings") or {}).get("total_s"), "has_result": (rj.parent / "result.json").exists()})
        golden.sort(key=lambda r: (r.get("created_utc") or "", r["run_id"]), reverse=True)
        cad = []
        runs_dir = pdir / "cad" / "local" / "runs"
        if runs_dir.is_dir():
            for rd in sorted((x for x in runs_dir.iterdir() if x.is_dir()), key=lambda x: x.name, reverse=True):
                rj = rd / "run.json"
                if not rj.is_file():
                    cad.append({"run_id": rd.name, "status": "unreadable", "name": None, "started": None, "finished": None, "sandbox": None, "unreadable": "run.json missing"})
                    continue
                try:
                    if rj.stat().st_size > 8 * 1048576:
                        raise OSError("run.json exceeds 8 MiB (store limit)")
                    with open(rj, encoding="utf-8") as fh:
                        r = json.load(fh)
                except (OSError, ValueError) as e:
                    cad.append({"run_id": rd.name, "status": "unreadable", "name": None, "started": None, "finished": None, "sandbox": None, "unreadable": f"run.json unreadable: {e}"})
                    continue
                sb = r.get("sandbox") or {}
                cad.append({"run_id": r.get("run_id") or rd.name, "status": r.get("status"), "name": r.get("name"), "started": r.get("created_at"), "finished": r.get("finished_at"),
                            "sandbox": {"mode": sb.get("mode"), "confined": sb.get("confined"), "platform": sb.get("platform")}, "unreadable": None})
        ledger = []
        for n, e in enumerate(es or [], 1):
            pl = e.get("payload")
            if isinstance(pl, dict) and (pl.get("project") == name or pl.get("name") == name):
                ledger.append({"n": n, "ts": e.get("ts"), "kind": e.get("kind"), "payload": mask(pl), "hash": e.get("hash"), "prev": e.get("prev")})
        ledger.reverse()
        row.update({"ledger": ledger, "golden_runs": golden, "cad_runs": cad, "ledger_note": lerr})
        return local_envelope(row, str(p), 10)

    def _ledger(self, query):
        A = self.server.aces
        if not A.ok():
            return local_envelope(None, "aces.ledger.entries", 5, A.error)
        es, lerr = self._ledger_entries()
        path = str(A.ledger.path())
        if es is None:
            return local_envelope(None, path, 5, lerr)
        kind = (query.get("kind") or [""])[0]
        try:
            n = max(1, min(2000, int((query.get("n") or ["200"])[0])))
        except ValueError:
            return error(400, "n must be an integer", "bad-parameter", source=path)
        rows = [{"n": i, "ts": e.get("ts"), "kind": e.get("kind"), "payload": mask(e.get("payload")), "hash": e.get("hash"), "prev": e.get("prev")} for i, e in enumerate(es, 1)]
        if kind:
            rows = [r for r in rows if str(r.get("kind") or "").startswith(kind)]
        rows.reverse()
        return local_envelope({"path": path, "n_total": len(es), "entries": rows[:n]}, path, 5)

    # ---- SSE (spec 3.2), replaying the captures at their TTLs ----
    def _write(self, text):
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    def make_stamp(self, topic, prev_etag):
        route, ttl, _plates, inline, proj = TOPICS[topic]
        parsed = urllib.parse.urlsplit(route)
        st, body = self.server_get(parsed.path, urllib.parse.parse_qs(parsed.query))
        if st == 200 and isinstance(body, dict) and "data" in body:
            data = body.get("data")
            meta = body.get("meta") or {}
            ok = bool(meta.get("ok", True))
            err = meta.get("error")
            fetched = meta.get("fetched_utc")
        elif st == 200:
            data, ok, err, fetched = body, True, None, utcnow()
        else:
            data, ok, err, fetched = None, False, (body.get("error") if isinstance(body, dict) else f"HTTP {st}"), None
        etag = sha12(data)
        payload = (proj(data) if proj else data) if inline else None
        stamp = {"topic": topic, "seq": self.server.next_seq(), "fetched_utc": fetched, "etag": etag, "ok": ok, "error": err, "next_try_s": None,
                 "changed": etag != prev_etag, "fixture": FIXTURE, "payload": payload, "replayed_utc": utcnow()}
        self.server.remember(stamp)
        return stamp

    def _sse(self, query):
        s = self.server
        if s.no_sse or s.streams >= MAX_STREAMS:
            return self._send_json(*error(503, "too many event streams; poll the GET routes at their TTLs" if not s.no_sse else "event stream disabled (--no-sse); poll the GET routes at their TTLs", "too-many-streams", source="/api/events"))
        plate = (query.get("plate") or ["fabric"])[0]
        topics = [t for t, spec in TOPICS.items() if spec[2] == "*" or plate in spec[2]]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._common_headers()
        self.end_headers()
        with s.lock:
            s.streams += 1
        try:
            self._write("retry: 3000\n\n")
            self._write(f"event: fixture\ndata: {json.dumps({'fixture': FIXTURE})}\n\n")
            last_id = self.headers.get("Last-Event-ID")
            etags = {}
            if last_id and last_id.isdigit():
                for st in s.replay_after(int(last_id)):
                    if st["topic"] in topics:
                        etags[st["topic"]] = st["etag"]
                        self._write(f"id: {st['seq']}\nevent: stamp\ndata: {json.dumps(st, default=str)}\n\n")
            due = {t: 0.0 for t in topics}
            last_ping = time.monotonic()
            while not s.stopping:
                now = time.monotonic()
                for t in topics:
                    if now - due[t] >= TOPICS[t][1]:
                        st = self.make_stamp(t, etags.get(t))
                        etags[t] = st["etag"]
                        self._write(f"id: {st['seq']}\nevent: stamp\ndata: {json.dumps(st, default=str)}\n\n")
                        due[t] = now
                if now - last_ping >= 15:
                    self._write(": ping\n\n")
                    last_ping = now
                time.sleep(0.25)
            self._write("event: bye\ndata: {}\n\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with s.lock:
                s.streams -= 1
        return 200


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8070)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--static", default=os.path.join(PKG, "static"))
    ap.add_argument("--samples", default=None, help="a directory of capture JSON files (default: tests/fixtures, then tests/harness/fixtures)")
    ap.add_argument("--vm-work", default=None, help="a copied work store root holding Work/, .config/aces, .local/state/aces, .local/share")
    ap.add_argument("--no-sse", action="store_true", help="answer /api/events with 503 so the page exercises its polling fallback")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    if args.bind not in LOOPBACK:
        sys.stderr.write("devserver: only 127.0.0.1 or ::1 may be bound (LAN/VPN access needs the authenticated shared API)\n")
        return 2
    prepare_env(args)
    aces = Aces()
    samples = Samples([args.samples, os.environ.get("ACES_CONSOLE_SAMPLES"), os.path.join(PKG, "tests", "fixtures"), os.path.join(HERE, "fixtures")])
    server = DevServer((args.bind, args.port), Handler, static_dir=args.static, samples=samples, aces=aces, no_sse=args.no_sse, quiet=args.quiet)
    sys.stdout.write(f"listening http://{server.server_address[0]}:{server.server_address[1]}\n")
    sys.stdout.flush()
    if not args.quiet:
        sys.stderr.write(f"devserver: static {server.static_dir}\n")
        sys.stderr.write(f"devserver: samples {samples.dirs or 'none found'}\n")
        sys.stderr.write(f"devserver: aces {'ok (' + str(aces.config.WORK) + ')' if aces.ok() else aces.error}\n")
        sys.stderr.write(f"devserver: {FIXTURE}; loopback only; no token file read; no rig contacted\n")
        sys.stderr.flush()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping = True
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
