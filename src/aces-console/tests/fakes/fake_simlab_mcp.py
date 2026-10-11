"""A stand-in for simlab-mcp (192.168.1.65:8022/mcp, cudax simlab/mcp_server.py) for tests.

Streamable HTTP, JSON-RPC 2.0 over POST. `initialize` answers a serverInfo that names this fake and sets an
Mcp-Session-Id header; every later request must carry that header (as FastMCP's stateful transport requires), else
400. `notifications/initialized` answers 202. `tools/list` replays simlab-mcp-tools.json and `tools/call sim_whoami`
replays simlab-whoami.json (ids rewritten to the request id; `sse=True` frames the reply as text/event-stream the way
the live server answers). `tools/call sim_status` has no 2026-10-11 capture and answers a JSON-RPC -32601 error that
says so. With `token` set, a request without `Authorization: Bearer <token>` answers 401. Every request is recorded
with its lower-cased headers so a test can assert the bearer header without printing it.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import FIXTURES


class FakeSimlabMcp:
    def __init__(self, token: str | None = None, sse: bool = False, fixtures_dir: Path = FIXTURES):
        self.token = token
        self.sse = sse
        self.fixtures_dir = fixtures_dir
        self.requests: list[dict[str, Any]] = []
        self.sessions: set[str] = set()
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _reply(self, status: int, obj: Any, session: str | None = None) -> None:
                if obj is None:
                    data, ctype = b"", "application/json"
                elif fake.sse:
                    data = ("event: message\ndata: " + json.dumps(obj) + "\n\n").encode("utf-8")
                    ctype = "text/event-stream"
                else:
                    data = json.dumps(obj).encode("utf-8")
                    ctype = "application/json"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if session:
                    self.send_header("Mcp-Session-Id", session)
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = None
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                with fake.lock:
                    fake.requests.append({"path": self.path, "headers": hdrs, "body": body})
                if fake.token is not None and hdrs.get("authorization") != f"Bearer {fake.token}":
                    self._reply(401, {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "fake_simlab_mcp: unauthorized"}})
                    return
                rid = body.get("id") if isinstance(body, dict) else None
                method = body.get("method") if isinstance(body, dict) else None
                if method == "initialize":
                    sid = secrets.token_hex(16)
                    with fake.lock:
                        fake.sessions.add(sid)
                    params = body.get("params") or {}
                    self._reply(200, {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": params.get("protocolVersion"), "capabilities": {},
                                                                              "serverInfo": {"name": "fake simlab-mcp (test stand-in)", "version": "0"}}}, sid)
                    return
                sid = hdrs.get("mcp-session-id")
                with fake.lock:
                    known = sid in fake.sessions
                if not known:
                    self._reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Bad Request: Missing session ID"}})
                    return
                if method == "notifications/initialized":
                    self._reply(202, None)
                    return
                if method == "tools/list":
                    obj = _load(fake.fixtures_dir / "simlab-mcp-tools.json")
                    self._reply(200, dict(obj, id=rid))
                    return
                if method == "tools/call":
                    name = (body.get("params") or {}).get("name")
                    if name == "sim_whoami":
                        obj = _load(fake.fixtures_dir / "simlab-whoami.json")
                        self._reply(200, dict(obj, id=rid))
                        return
                    if name == "sim_list_experiments":  # captured 2026-10-11 (aces-console 0.1.0 SIMLAB actions)
                        obj = _load(fake.fixtures_dir / "simlab-list-experiments.json")
                        self._reply(200, dict(obj, id=rid))
                        return
                    if name == "sim_cancel":  # a write tool: a stand-in reply, never a capture (nothing is cancelled)
                        args = (body.get("params") or {}).get("arguments") or {}
                        self._reply(200, {"jsonrpc": "2.0", "id": rid, "result": {"structuredContent": {"result": {
                            "experiment_id": args.get("experiment_id"), "cancelled": 0, "note": "fake_simlab_mcp stand-in"}},
                            "content": [], "isError": False}})
                        return
                    self._reply(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601,
                                                                            "message": f"fake_simlab_mcp: tool {name} has no 2026-10-11 capture and is not replayed"}})
                    return
                self._reply(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"fake_simlab_mcp: method {method} is not replayed"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def calls(self, name: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if isinstance(r["body"], dict) and r["body"].get("method") == "tools/call"
                    and (r["body"].get("params") or {}).get("name") == name]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def _load(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
