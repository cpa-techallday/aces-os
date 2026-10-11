"""The HTTP server (build spec 3.1-3.3): ThreadingHTTPServer, loopback peer check, headers, the CSP with the importmap
hash, POST guards, the router, the envelope and the error shape, static files under the safefs rules, /vocab.json,
/api/health, /api/config, /api/events, and fixtures mode.

Router contract (frozen by the integrator). `ROUTES` is the list of registered routes; `register(method, pattern,
handler)` adds one (a second registration of the same method and pattern replaces the handler). `pattern` is a path
with `{name}` segments, e.g. "/api/golden/run/{project}/{run_id}"; a final `{name:path}` segment captures the rest of
the path for file routes ("/api/golden/file/{project}/{run_id}/{rel:path}"). `handler(req) -> Response`; `req` carries
`method, path, params (dict of {name} captures), query (dict of lists), json (parsed object body or None), peer`, plus
`headers`, `app` (this server's App) and `handler` (the raw BaseHTTPRequestHandler, for streams).
`Response(status=200, data=None, meta=None, error=None, code=None, body=None, content_type=None, headers=None)`: JSON
when `body` is None (the envelope `{ok:true, data, meta}` or the error shape `{ok:false, error, code, source, status,
next_try_s}`), raw bytes with `content_type` otherwise. `envelope(entry)` builds a Response from a cache Entry,
`ok(data)` from a computed value, `error(status, message, code)` an error.

Module install: at start `rig.install(register)` runs, then `sim`, `records`, `ledgers`, `system` when they are
importable (Builder B); a missing module is recorded as "not installed" in /api/health.modules and the server runs
without it. Modules keep per-server state in `app.state[<name>]` and read the current App through `req.app` or
`current_app()`.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import platform
import re
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import aces

from . import FIXTURE_STAMP, UI_NAME, __version__, cache as cache_mod, config as console_config, httpc, jsonenc
from . import poller as poller_mod, safefs, secrets, sse

LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
BODY_CAP = 64 * 1024
BODY_CAPS: dict[str, int] = {}  # exact paths allowed a larger JSON body (uploads), set by the modules at install


def allow_body(path: str, cap: int) -> None:
    """Let one exact route path take a JSON body of up to `cap` bytes (base64 uploads); every other route keeps 64 KiB."""
    BODY_CAPS[path] = int(cap)
INTENT_HEADER = "X-ACES-Intent"
INTENT_HUMAN = "human"
CSP_TEMPLATE = ("default-src 'self'; script-src 'self'{hash}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "font-src 'self'; connect-src 'self'; worker-src 'none'; object-src 'none'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'")
IMPORTMAP_RE = re.compile(r"<script\s+type=([\"']?)importmap\1[^>]*>(.*?)</script>", re.S | re.I)
VOCAB_PATH = Path(__file__).resolve().parent / "vocab.json"
STREAMED = object()  # Response.body sentinel: the handler already wrote the stream
MODULES = ("rig", "sim", "records", "ledgers", "system", "mc")
FIXTURE_BASES = ("grid_control", "rig_state", "simlab_mcp", "simlab_console", "mlflow")

Route = namedtuple("Route", "method pattern regex handler")
ROUTES: list[Route] = []
APP: "App | None" = None


# --- the router ---------------------------------------------------------------------------------------------------------

def _compile(pattern: str) -> re.Pattern:
    out = "^"
    for token in re.split(r"(\{[^}]+\})", pattern):
        if not token:
            continue
        if token.startswith("{") and token.endswith("}"):
            name, _, kind = token[1:-1].partition(":")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"bad route parameter {token!r} in {pattern!r}")
            out += f"(?P<{name}>.+)" if kind == "path" else f"(?P<{name}>[^/]+)"
        else:
            out += re.escape(token)
    return re.compile(out + "$")


def register(method: str, pattern: str, handler: Callable[["Request"], "Response"]) -> None:
    """Register `handler` for `method` requests whose path matches `pattern` (see the module docstring)."""
    method = method.upper()
    route = Route(method, pattern, _compile(pattern), handler)
    for i, r in enumerate(ROUTES):
        if r.method == method and r.pattern == pattern:
            ROUTES[i] = route
            return
    ROUTES.append(route)


def match(method: str, path: str) -> tuple[Route | None, dict[str, str], bool]:
    """(route, params, path_known): the first route of `method` matching `path`; path_known says another method has
    this path (405 rather than 404)."""
    known = False
    for r in ROUTES:
        m = r.regex.match(path)
        if m is None:
            continue
        if r.method == method:
            return r, dict(m.groupdict()), True
        known = True
    return None, {}, known


class Request:
    __slots__ = ("method", "path", "params", "query", "json", "peer", "headers", "app", "handler", "raw")

    def __init__(self, method: str, path: str, params: dict[str, str], query: dict[str, list[str]], body: Any,
                 peer: str, headers, app: "App", handler, raw: bytes):
        self.method, self.path, self.params, self.query, self.json = method, path, params, query, body
        self.peer, self.headers, self.app, self.handler, self.raw = peer, headers, app, handler, raw

    def q(self, name: str, default: str | None = None) -> str | None:
        vals = self.query.get(name)
        return vals[0] if vals else default


class Response:
    __slots__ = ("status", "data", "meta", "error", "code", "body", "content_type", "headers", "source",
                 "upstream_status", "next_try_s")

    def __init__(self, status: int = 200, data: Any = None, meta: dict | None = None, error: str | None = None,
                 code: str | None = None, body: bytes | object | None = None, content_type: str | None = None,
                 headers: dict[str, str] | None = None, source: str | None = None, upstream_status: int | None = None,
                 next_try_s: float | None = None):
        self.status, self.data, self.meta, self.error, self.code = status, data, meta, error, code
        self.body, self.content_type, self.headers = body, content_type, headers
        self.source, self.upstream_status, self.next_try_s = source, upstream_status, next_try_s


def now_utc() -> str:
    return cache_mod.utc(time.time()) or ""


def default_meta(source: str | None = None, ttl_s: float = 0.0) -> dict[str, Any]:
    return {"source": source, "fetched_utc": now_utc(), "age_s": 0.0, "ttl_s": ttl_s, "stale": False, "ok": True,
            "error": None, "next_try_s": None, "fixture": None}


def ok(data: Any, source: str | None = None, ttl_s: float = 0.0, status: int = 200, headers: dict | None = None) -> Response:
    """A 200 envelope for a value the server computed itself."""
    return Response(status=status, data=data, meta=default_meta(source, ttl_s), headers=headers)


def envelope(entry: cache_mod.Entry, data: Any = None, source: str | None = None, extra_meta: dict | None = None) -> Response:
    """The 200 envelope for a cache Entry (data, staleness and error text from the entry)."""
    meta = entry.meta()
    if source is not None:
        meta["source"] = source
    if extra_meta:
        meta.update(extra_meta)
    return Response(status=200, data=entry.data if data is None else data, meta=meta)


def error(status: int, message: str, code: str, source: str | None = None, upstream_status: int | None = None,
          next_try_s: float | None = None, headers: dict | None = None) -> Response:
    return Response(status=status, error=message, code=code, source=source, upstream_status=upstream_status,
                    next_try_s=next_try_s, headers=headers)


def file_response(req: "Request", root: str | Path, rel: str, max_bytes: int | None = None, kinds=None,
                  table: dict | None = None, sandbox: bool = True) -> Response:
    """Serve root/rel under the safefs rules with the headers of spec 3.13 rule 5 (304 on If-None-Match)."""
    try:
        data, ctype, digest = safefs.open_under(root, rel, max_bytes=max_bytes, kinds=kinds, table=table)
    except safefs.SafeFsError as e:
        code = {400: "bad-path", 403: "refused", 404: "not-found", 413: "too-large", 415: "unsupported-type"}.get(e.status, "read-error")
        prefix = "refusing to read " if e.status == 403 and not e.reason.startswith("refusing") else ""
        return error(e.status, f"{prefix}{rel}: {e.reason}" if prefix else e.reason, code)
    headers = safefs.headers_for(ctype, digest, rel.rsplit("/", 1)[-1], sandbox=sandbox)
    inm = req.headers.get("If-None-Match") if req is not None and req.headers is not None else None
    if inm and inm.strip() == headers["ETag"]:
        return Response(status=304, body=b"", headers={"ETag": headers["ETag"], "Cache-Control": headers["Cache-Control"]})
    return Response(status=200, body=data, content_type=ctype, headers=headers)


# --- the CSP and the static tree ------------------------------------------------------------------------------------------

def importmap_hash(html: str) -> str | None:
    """base64(sha256) of the inline importmap body of `html`, or None when there is none."""
    m = IMPORTMAP_RE.search(html)
    if m is None:
        return None
    return base64.b64encode(hashlib.sha256(m.group(2).encode("utf-8")).digest()).decode("ascii")


def csp_for(html: str | None) -> str:
    h = importmap_hash(html) if html else None
    return CSP_TEMPLATE.format(hash=f" 'sha256-{h}'" if h else "")


def static_manifest(static_dir: Path) -> str | None:
    """sha256 over every regular file of the static tree (sorted relative path + content), or None without a tree."""
    if not (static_dir / "index.html").is_file():
        return None
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(static_dir):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(static_dir).as_posix()
            h.update(rel.encode("utf-8") + b"\n")
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
    return h.hexdigest()


# --- the application ---------------------------------------------------------------------------------------------------

class App:
    """One server's state: settings, cache, SSE broker, poller hub, counters, the log, the installed modules."""

    def __init__(self, settings: console_config.Settings):
        global APP
        self.settings = settings
        self.fixture: str | None = settings.fixture
        self.started_utc = now_utc()
        self.cache = cache_mod.Cache()
        self.broker = sse.Broker(fixture=self.fixture)
        self.hub = poller_mod.Hub(self.cache, self.broker, fixture=self.fixture, log=self.log)
        self.counters: dict[str, int] = {"refused_posts": 0, "suppressed_refusals": 0, "nonce_failures": 0}
        self.switch_outcome_pending: dict[str, Any] | None = None
        self.modules: dict[str, str] = {}
        self.state: dict[str, Any] = {}
        self.httpd: ThreadingHTTPServer | None = None
        self._log_lock = threading.Lock()
        self._log_fd: int | None = None
        self._csp_cache: tuple[float, str] | None = None
        self.stopping = False
        try:
            settings.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"aces-console: cannot create {settings.state_dir}: {e}", file=sys.stderr)
        if settings.fixtures_dir is not None:
            httpc.set_fixtures(settings.fixtures_dir, {k: settings.server.get(k) for k in FIXTURE_BASES})
        else:
            httpc.set_fixtures(None)
        self.static_manifest_sha256 = static_manifest(settings.static_dir)
        APP = self
        install_core(register)
        self.install_modules()

    # --- modules -----------------------------------------------------------------------------------------------------

    def install_modules(self) -> None:
        for name in MODULES:
            full = f"aces_console.{name}"
            try:
                mod = importlib.import_module(full)
            except ModuleNotFoundError as e:
                self.modules[name] = "not installed" if e.name == full else f"import failed: {e}"[:200]
                continue
            except ImportError as e:
                self.modules[name] = f"import failed: {e}"[:200]
                continue
            try:
                mod.install(register)
                self.modules[name] = "installed"
            except Exception as e:  # noqa: BLE001
                self.modules[name] = f"install failed: {type(e).__name__}: {e}"[:200]
                self.log(f"module {name}: install failed\n{traceback.format_exc()}")

    # --- logging (server.log: method, path without query, status, duration; never headers or bodies) --------------------

    def _open_log(self) -> int | None:
        if self._log_fd is None:
            try:
                self._log_fd = os.open(str(self.settings.log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            except OSError:
                self._log_fd = -1
        return None if self._log_fd == -1 else self._log_fd

    def log(self, message: str) -> None:
        fd = self._open_log()
        if fd is None:
            return
        line = f"{now_utc()} {message.rstrip()}\n".encode("utf-8", "replace")
        with self._log_lock:
            try:
                os.write(fd, line)
            except OSError:
                pass

    def log_request(self, method: str, path: str, status: int, ms: float) -> None:
        self.log(f"{method} {path[:200]} {status} {ms:.0f}ms")

    def close_log(self) -> None:
        with self._log_lock:
            if self._log_fd is not None and self._log_fd >= 0:
                try:
                    os.close(self._log_fd)
                except OSError:
                    pass
            self._log_fd = None

    # --- the page ----------------------------------------------------------------------------------------------------

    def csp(self) -> str:
        index = self.settings.static_dir / "index.html"
        try:
            mtime = index.stat().st_mtime
        except OSError:
            return csp_for(None)
        if self._csp_cache is None or self._csp_cache[0] != mtime:
            try:
                html = index.read_text(encoding="utf-8", errors="replace")
            except OSError:
                html = None
            self._csp_cache = (mtime, csp_for(html))
        return self._csp_cache[1]

    # --- dispatch ----------------------------------------------------------------------------------------------------

    def dispatch(self, method: str, path: str, qs: str, handler: BaseHTTPRequestHandler) -> Response:
        path = urllib.parse.unquote(path)
        route, params, known = match(method, path)
        if route is None:
            if known:
                return error(405, f"method {method} is not allowed on {path}", "method-not-allowed")
            return error(404, f"no route {method} {path}", "not-found")
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        cap = BODY_CAPS.get(path, BODY_CAP)
        if length > cap:
            if length <= 8 * 1024 * 1024:
                handler.rfile.read(length)  # drain, so the client reads the 413 instead of a reset
            handler.close_connection = True
            return error(413, f"request body larger than {cap // 1024} KiB", "too-large")
        raw = handler.rfile.read(length) if length > 0 else b""
        body: Any = None
        if method == "POST":
            refused = self._post_guard(handler, raw)
            if refused is not None:
                self.counters["refused_posts"] += 1
                return refused
            body = json.loads(raw.decode("utf-8"))
        query = urllib.parse.parse_qs(qs, keep_blank_values=True)
        req = Request(method, path, params, query, body, handler.client_address[0], handler.headers, self, handler, raw)
        return route.handler(req)

    def _post_guard(self, handler: BaseHTTPRequestHandler, raw: bytes) -> Response | None:
        """The POST guards of spec 3.1 (loopback is checked before routing): Origin equal to this server's origin,
        X-ACES-Intent: human, Content-Type application/json, a JSON object body. Any failure is 403 {code: refused}."""
        origin = handler.headers.get("Origin")
        if origin != self.settings.origin:
            return error(403, "refused: Origin header missing or not this console's origin", "refused")
        if (handler.headers.get(INTENT_HEADER) or "").strip().lower() != INTENT_HUMAN:
            return error(403, f"refused: header {INTENT_HEADER}: {INTENT_HUMAN} is required", "refused")
        ctype = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return error(403, "refused: Content-Type must be application/json", "refused")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error(403, "refused: the body is not JSON", "refused")
        if not isinstance(body, dict):
            return error(403, "refused: the body must be a JSON object", "refused")
        return None

    # --- lifecycle ---------------------------------------------------------------------------------------------------

    def write_pid(self) -> None:
        try:
            self.settings.pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError as e:
            self.log(f"cannot write {self.settings.pid_file}: {e}")

    def remove_pid(self) -> None:
        try:
            if self.settings.pid_file.is_file() and self.settings.pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.settings.pid_file.unlink()
        except OSError:
            pass

    def request_stop(self, delay_s: float = 0.3) -> None:
        """Stop the server from a handler (POST /api/system/stop): bye to the SSE clients, then shut down."""
        if self.stopping:
            return
        self.stopping = True

        def _go() -> None:
            time.sleep(delay_s)
            self.hub.stop()
            self.broker.close_all(bye=True)
            if self.httpd is not None:
                self.httpd.shutdown()

        threading.Thread(target=_go, name="aces-console-stop", daemon=True).start()

    def shutdown(self) -> None:
        self.stopping = True
        self.hub.stop()
        self.broker.close_all(bye=True)
        if self.httpd is not None:
            try:
                self.httpd.server_close()
            except OSError:
                pass
        self.remove_pid()
        httpc.set_fixtures(None)
        self.close_log()


def current_app() -> "App":
    if APP is None:
        raise RuntimeError("no aces-console App is running in this process")
    return APP


# --- core routes ----------------------------------------------------------------------------------------------------------

def h_index(req: Request) -> Response:
    app = req.app
    try:
        data, ctype, digest = safefs.open_under(app.settings.static_dir, "index.html", table=safefs.STATIC_KINDS)
    except safefs.SafeFsError as e:
        status = 503 if e.status == 404 else e.status
        return error(status, f"static tree not found at {app.settings.static_dir} ({app.settings.static_source}): {e.reason}",
                     "no-static")
    headers = {"Content-Security-Policy": app.csp(), "Cache-Control": "no-store", "ETag": f'"{digest}"'}
    return Response(status=200, body=data, content_type=ctype, headers=headers)


def h_static(req: Request) -> Response:
    rel = req.params.get("rel", "")
    resp = file_response(req, req.app.settings.static_dir, rel, table=safefs.STATIC_KINDS,
                         sandbox=rel.lower().endswith((".html", ".svg")))
    if resp.status == 200 and resp.headers is not None:
        resp.headers["Cache-Control"] = "public, max-age=60"
        resp.headers.pop("Content-Disposition", None)
    return resp


def h_favicon(req: Request) -> Response:
    return Response(status=204, body=b"")


def h_vocab(req: Request) -> Response:
    return file_response(req, VOCAB_PATH.parent, VOCAB_PATH.name, kinds=("json",), sandbox=False)


def h_health(req: Request) -> Response:
    app = req.app
    s = app.settings
    data = {"version": __version__, "ui": UI_NAME, "aces_version": getattr(aces, "__version__", None), "pid": os.getpid(),
            "started_utc": app.started_utc, "bind": s.bind, "port": s.port, "origin": s.origin,
            "clients": app.broker.count(), "poller": app.hub.state, "sources": app.hub.sources_health(),
            "counters": dict(app.counters), "switch_outcome_pending": app.switch_outcome_pending,
            "static_manifest_sha256": app.static_manifest_sha256, "static_dir": str(s.static_dir),
            "static_source": s.static_source, "fixture": app.fixture, "modules": dict(app.modules)}
    return ok(data, source="console")


def h_config(req: Request) -> Response:
    s = req.app.settings
    keys = ("name", "host", "grid_control", "rig_state", "simlab_mcp", "simlab_console", "mlflow", "webodm", "timeout_s")
    data = {"server": {k: s.server.get(k) for k in keys}, "console": s.console_table(), "tokens": secrets.sources(),
            "paths": s.paths(), "platform": {"os": platform.system().lower() or os.name, "python": platform.python_version(),
                                             "machine": platform.machine()}}
    return ok(data, source="console")


def h_events(req: Request) -> Response:
    app = req.app
    plate = req.q("plate")
    if app.broker.count() >= app.broker.max_clients:
        return error(503, f"too many streams ({app.broker.max_clients}); poll the GET routes instead", "too-many-streams")
    served = app.broker.serve(req.handler, plate, req.headers.get("Last-Event-ID"))
    if not served:
        return error(503, f"too many streams ({app.broker.max_clients}); poll the GET routes instead", "too-many-streams")
    return Response(status=200, body=STREAMED)


def install_core(reg: Callable[[str, str, Callable], None]) -> None:
    reg("GET", "/", h_index)
    reg("GET", "/index.html", h_index)
    reg("GET", "/static/{rel:path}", h_static)
    reg("GET", "/favicon.ico", h_favicon)
    reg("GET", "/vocab.json", h_vocab)
    reg("GET", "/api/health", h_health)
    reg("GET", "/api/config", h_config)
    reg("GET", "/api/events", h_events)


# --- the handler and the server ---------------------------------------------------------------------------------------------

class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = f"aces-console/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: App

    def log_message(self, fmt, *args):  # the server keeps its own log (method, path, status, duration)
        return

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def _handle(self, method: str) -> None:
        t0 = time.monotonic()
        path, _, qs = self.path.partition("?")
        status = 0
        try:
            if self.client_address[0] not in LOOPBACK_PEERS:
                resp = error(403, "refused: the peer is not loopback", "refused")
            elif method not in ("GET", "POST"):
                resp = error(405, f"method {method} is not supported", "method-not-allowed")
            else:
                resp = self.app.dispatch(method, path, qs, self)
            status = resp.status
            if resp.body is not STREAMED:
                self._send(resp, path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            status = status or 0
        except Exception as exc:  # noqa: BLE001 (a console bug: one line to the client, the trace to server.log)
            self.app.log(f"{method} {path[:200]} failed\n{traceback.format_exc()}")
            status = 500
            try:
                self._send(error(500, f"console bug: {type(exc).__name__}: {str(exc)[:160]}", "bug"), path)
            except OSError:
                pass
        finally:
            self.app.log_request(method, path, status, (time.monotonic() - t0) * 1000.0)

    def _send(self, resp: Response, path: str) -> None:
        app = self.app
        if resp.body is not None:
            data = resp.body if isinstance(resp.body, (bytes, bytearray)) else b""
            ctype = resp.content_type or "application/octet-stream"
        else:
            if resp.error is not None or resp.status >= 400:
                payload: dict[str, Any] = {"ok": False, "error": resp.error or "error", "code": resp.code or "error",
                                           "source": resp.source, "status": resp.upstream_status,
                                           "next_try_s": resp.next_try_s}
                if app.fixture:
                    payload["fixture"] = app.fixture
            else:
                meta = dict(resp.meta or default_meta())
                if app.fixture:
                    meta["fixture"] = app.fixture
                payload = {"ok": True, "data": resp.data, "meta": meta}
            data = jsonenc.encode(payload)
            ctype = "application/json; charset=utf-8"
        self.send_response(resp.status)
        if resp.status not in (204, 304):
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data) if resp.status not in (204, 304) else 0))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        headers = dict(resp.headers or {})
        if "Cache-Control" not in headers and (path.startswith("/api/") or resp.body is None):
            headers["Cache-Control"] = "no-store"
        for k, v in headers.items():
            if k.lower() in ("content-type", "content-length", "x-content-type-options", "referrer-policy"):
                continue
            self.send_header(k, v)
        self.end_headers()
        if resp.status not in (204, 304) and data:
            self.wfile.write(data)


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR lets a second process bind the same port on Windows; keep it only where it means TIME_WAIT reuse
    allow_reuse_address = os.name != "nt"
    request_queue_size = 64


def make_server(settings: console_config.Settings) -> tuple[ConsoleServer, App]:
    """Bind the server (loopback only) and build its App. Raises SystemExit for a non-loopback bind."""
    if not console_config.is_loopback(settings.bind):
        raise SystemExit(console_config.LAN_REFUSAL.format(bind=settings.bind))
    app = App(settings)
    handler_cls = type("BoundConsoleHandler", (ConsoleHandler,), {"app": app})
    server_cls = type("BoundConsoleServer", (ConsoleServer,),
                      {"address_family": socket.AF_INET6 if ":" in settings.bind else socket.AF_INET})
    try:
        httpd = server_cls((settings.bind, settings.port), handler_cls)
    except OSError as e:
        app.shutdown()
        raise SystemExit(f"aces-console: cannot bind {settings.origin}: {e}") from None
    app.httpd = httpd
    if settings.port == 0:  # an ephemeral port (tests): the origin follows the bound port
        settings.port = httpd.server_address[1]
        settings.origin = console_config.origin_for(settings.bind, settings.port)
    return httpd, app


def serve(settings: console_config.Settings, announce: bool = True) -> int:
    """Run the server in the foreground until SIGTERM/SIGINT or POST /api/system/stop."""
    import signal

    httpd, app = make_server(settings)
    app.write_pid()
    if announce:
        s = settings
        print(f"{UI_NAME} serving {s.origin} (static {s.static_dir} [{s.static_source}]; state {s.state_dir}"
              f"{'; ' + s.fixture if s.fixture else ''}; modules "
              + ", ".join(f"{k}={v}" for k, v in app.modules.items()) + ")", file=sys.stderr, flush=True)

    def _stop(signum, frame):  # noqa: ARG001
        app.log(f"signal {signum}: stopping")
        app.request_stop(delay_s=0.0)

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass
    app.hub.start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0
