"""A stand-in for GRID CONTROL rev B's Mission Control, red-team, model-staging and auto-switch endpoints, and for the
SIMLAB console's GET-only /gc read proxy in front of them, for tests of aces_console/mc.py.

Captures replayed (tests/fixtures, MANIFEST.json): GET /downloads-status.json (gc-downloads-status.json) and GET
/api/redteam/status (gc-redteam-status.json). Mission Control's agents, runs, chats and missions are NOT captured on
purpose (their prompts and chat text stay out of the repository): the tRPC replies here are test stand-ins whose field
names are the ones GRID CONTROL rev B's page reads (cudax ui/grid-control/index.html:617-905) and the live key-name
probe of 2026-10-11 confirmed; their values are invented for the test and labelled so ("fake_mc stand-in").

/api/* answers 401 without `x-gc-token` equal to the fake's token (GRID CONTROL's protected routes); /gc/api/* (the rig's
read proxy) answers without a token, GET only. Every request is recorded with its lower-cased headers and body.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import FIXTURES

STANDIN = "fake_mc stand-in, not a capture"
AGENTS = [{"id": 1, "slug": "judge", "name": "Judge", "role": "reviewer", "specialty": STANDIN, "color": "#a9bbd0",
           "systemPrompt": "a system prompt the console must not pass through", "toolsJson": "[\"a\", \"b\"]",
           "model": "gpt-oss-20b", "maxTokens": 4096, "enabled": True},
          {"id": 2, "slug": "cad", "name": "CAD Specialist", "role": "builder", "specialty": STANDIN, "color": "#22d3ee",
           "systemPrompt": "", "toolsJson": "not json", "model": "GLM-4.7-Flash-AWQ", "maxTokens": 8192, "enabled": False}]
RUNS = [{"id": 11, "agentId": 1, "status": "succeeded", "kind": "chat", "review": "none", "input": STANDIN, "output": "ok",
         "error": None, "model": "gpt-oss-20b", "promptTokens": 120, "completionTokens": 30, "startedAt": "2026-10-11T00:00:00Z",
         "finishedAt": "2026-10-11T00:00:05Z", "createdAt": "2026-10-11T00:00:00Z"},
        {"id": 12, "agentId": 2, "status": "running", "kind": "chat", "review": "none", "input": STANDIN, "output": None,
         "error": None, "model": "GLM-4.7-Flash-AWQ", "promptTokens": 0, "completionTokens": 0,
         "startedAt": "2026-10-11T00:01:00Z", "finishedAt": None, "createdAt": "2026-10-11T00:01:00Z"}]
RUN_DETAIL = {**RUNS[0], "steps": [{"kind": "output", "name": "answer", "contentJson": "\"ok\"", "elapsedMs": 400}],
              "artifacts": [{"id": 5, "filename": "out.txt", "sizeBytes": 3}]}
SESSIONS = [{"id": 3, "title": STANDIN, "agentId": 1, "createdAt": "2026-10-11T00:00:00Z", "updatedAt": "2026-10-11T00:02:00Z"}]
SESSION = {**SESSIONS[0], "messages": [{"role": "user", "content": STANDIN}, {"role": "assistant", "content": "ok", "agentId": 1}]}
MISSIONS = [{"id": 7, "title": STANDIN, "objective": STANDIN, "status": "review", "planError": None,
             "createdAt": "2026-10-11T00:00:00Z", "updatedAt": "2026-10-11T00:03:00Z", "completedAt": None,
             "items": [{"id": 70, "seq": 1, "title": "step", "status": "done", "runId": 11},
                       {"id": 71, "seq": 2, "title": "step", "status": "review", "runId": None}]}]
MISSION = {**MISSIONS[0], "compilerRuns": []}


class FakeMC:
    def __init__(self, token: str | None = None, fixtures_dir: Path = FIXTURES):
        self.token = token
        self.fixtures_dir = fixtures_dir
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _record(self, method: str, body: Any) -> None:
                with fake.lock:
                    fake.requests.append({"method": method, "path": self.path.split("?")[0], "query": self.path.partition("?")[2],
                                          "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})

            def _send(self, status: int, obj: Any, ctype: str = "application/json", raw: bytes | None = None,
                      extra: dict | None = None) -> None:
                data = raw if raw is not None else json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _trpc(self, data: Any) -> None:
                self._send(200, {"result": {"data": {"json": data}}})

            def _get(self, path: str) -> None:
                if path == "/downloads-status.json":
                    return self._send(200, None, raw=(fake.fixtures_dir / "gc-downloads-status.json").read_bytes())
                if path == "/api/redteam/status":
                    return self._send(200, None, raw=(fake.fixtures_dir / "gc-redteam-status.json").read_bytes())
                if path.startswith("/api/mc/trpc/"):
                    proc = path[len("/api/mc/trpc/"):]
                    table = {"agents.list": AGENTS, "runs.list": RUNS, "runs.get": RUN_DETAIL, "chat.listSessions": SESSIONS,
                             "chat.getSession": SESSION, "missions.list": MISSIONS, "missions.get": MISSION}
                    if proc not in table:
                        return self._send(404, {"error": {"json": {"message": f"fake_mc: no procedure {proc}"}}})
                    return self._trpc(table[proc])
                if path.startswith("/api/mc/uploads/") or path.startswith("/api/mc/artifacts/"):
                    return self._send(200, None, ctype="text/plain", raw=b"abc",
                                      extra={"Content-Disposition": 'attachment; filename="out.txt"'})
                return self._send(404, {"error": f"fake_mc: no route GET {path}"})

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                self._record("GET", None)
                if path.startswith("/gc/"):
                    return self._get(path[3:])  # the rig's read proxy: forwards with the rig's token, none needed here
                if path != "/downloads-status.json" and (fake.token is None or self.headers.get("x-gc-token") != fake.token):
                    return self._send(401, {"error": "unauthorized (fake_mc)"})
                return self._get(path)

            def do_POST(self) -> None:
                path = self.path.split("?")[0]
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if path == "/api/mc/uploads":
                    self._record("POST", {"bytes": len(raw), "data": raw.decode("latin-1")})
                else:
                    try:
                        self._record("POST", json.loads(raw.decode("utf-8")) if raw else None)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._record("POST", raw.decode("utf-8", "replace"))
                if path.startswith("/gc/"):
                    return self._send(405, {"error": "the read proxy is GET-only (fake_mc)"})
                if fake.token is None or self.headers.get("x-gc-token") != fake.token:
                    return self._send(401, {"error": "unauthorized (fake_mc)"})
                if path == "/api/mc/uploads":
                    name = urllib.parse.unquote(self.headers.get("x-filename") or "")
                    return self._send(200, {"id": 9, "filename": name, "mime": self.headers.get("content-type"), "note": STANDIN})
                if path.startswith("/api/mc/trpc/"):
                    proc = path[len("/api/mc/trpc/"):]
                    replies = {"chat.createSession": {"id": 4}, "chat.sendMessage": {"runId": 13},
                               "missions.create": {"id": 8}, "missions.launch": {"ok": True},
                               "missions.retryFailed": {"ok": True}, "missions.compile": {"ok": True},
                               "missions.complete": {"ok": True}, "missions.approveItem": {"ok": True}}
                    if proc not in replies:
                        return self._send(400, {"error": {"json": {"message": f"fake_mc: no procedure {proc}"}}})
                    return self._trpc(replies[proc])
                if path == "/api/redteam/vm":
                    return self._send(200, {"ok": True, "note": STANDIN})
                if path == "/api/auto":
                    return self._send(200, {"ok": True, "note": STANDIN})
                return self._send(404, {"error": f"fake_mc: no route POST {path}"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def requests_for(self, method: str, path: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if r["method"] == method and r["path"] == path]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
