"""A stand-in for the MLflow tracking server (192.168.1.65:5000) for tests.

Replays the 2026-10-11 captures of tests/fixtures: GET /api/2.0/mlflow/experiments/search (mlflow-experiments.json) and
GET /api/2.0/mlflow/runs/get?run_id=97cfbb614b98494b8d3aaa63e46cfd9e (mlflow-run-get.json, the one captured run). Any
other run id answers 404 with {"error_code": "RESOURCE_DOES_NOT_EXIST"} and a message saying it is a fake_mlflow
stand-in (MLflow's own 404 body was not captured); tests assert only the console's behaviour on a 404.

POST /api/2.0/mlflow/runs/search answers the raw bytes of mlflow-runs-search.json as captured: that file is not valid
JSON (the credential-line stripping removed its `next_page_token` line and left a trailing comma), so the console must
report "not JSON", which is the honest outcome for this capture. With `invalid_host=True` every route answers the
compose proxy's 403 text "Invalid Host header" (cudax docker/compose.yaml:26-32), which the console shows verbatim.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import FIXTURES

CAPTURED_RUN = "97cfbb614b98494b8d3aaa63e46cfd9e"


class FakeMlflow:
    def __init__(self, fixtures_dir: Path = FIXTURES):
        self.fixtures_dir = fixtures_dir
        self.requests: list[dict[str, Any]] = []
        self.invalid_host = False
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _send(self, status: int, data: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self, method: str, body: Any) -> None:
                path, _, qs = self.path.partition("?")
                with fake.lock:
                    fake.requests.append({"method": method, "path": path, "query": urllib.parse.parse_qs(qs), "body": body,
                                          "headers": {k.lower(): v for k, v in self.headers.items()}})

            def do_GET(self) -> None:
                self._record("GET", None)
                if fake.invalid_host:
                    self._send(403, b"Invalid Host header", "text/plain; charset=utf-8")
                    return
                path, _, qs = self.path.partition("?")
                query = urllib.parse.parse_qs(qs)
                if path == "/api/2.0/mlflow/experiments/search":
                    self._send(200, (fake.fixtures_dir / "mlflow-experiments.json").read_bytes())
                    return
                if path == "/api/2.0/mlflow/runs/get":
                    rid = (query.get("run_id") or [""])[0]
                    if rid == CAPTURED_RUN:
                        self._send(200, (fake.fixtures_dir / "mlflow-run-get.json").read_bytes())
                    else:
                        self._send(404, json.dumps({"error_code": "RESOURCE_DOES_NOT_EXIST",
                                                    "message": f"fake_mlflow stand-in: no 2026-10-11 capture for run {rid}"}).encode())
                    return
                if path == "/api/2.0/mlflow/runs/search":
                    self._send(200, (fake.fixtures_dir / "mlflow-runs-search.json").read_bytes())
                    return
                # the MLflow browser's captures (2026-10-11, the captured run only)
                rid = (query.get("run_id") or query.get("run_uuid") or [""])[0]
                if path == "/api/2.0/mlflow/metrics/get-history" and rid == CAPTURED_RUN and (query.get("metric_key") or [""])[0] == "h_mm":
                    self._send(200, (fake.fixtures_dir / "mlflow-metric-history.json").read_bytes())
                    return
                if path == "/api/2.0/mlflow/artifacts/list" and rid == CAPTURED_RUN and not query.get("path"):
                    self._send(200, (fake.fixtures_dir / "mlflow-artifacts-list.json").read_bytes())
                    return
                if path == "/get-artifact" and rid == CAPTURED_RUN and (query.get("path") or [""])[0] == "ccx.cvg":
                    self._send(200, (fake.fixtures_dir / "mlflow-artifact-ccx.cvg").read_bytes(), "application/octet-stream")
                    return
                if path in ("/api/2.0/mlflow/metrics/get-history", "/api/2.0/mlflow/artifacts/list", "/get-artifact"):
                    self._send(404, json.dumps({"error_code": "RESOURCE_DOES_NOT_EXIST",
                                                "message": f"fake_mlflow stand-in: no 2026-10-11 capture for {path} {rid}"}).encode())
                    return
                self._send(404, json.dumps({"error_code": "ENDPOINT_NOT_FOUND", "message": f"fake_mlflow: no route {path}"}).encode())

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = raw.decode("utf-8", "replace")
                self._record("POST", body)
                if fake.invalid_host:
                    self._send(403, b"Invalid Host header", "text/plain; charset=utf-8")
                    return
                path = self.path.partition("?")[0]
                if path == "/api/2.0/mlflow/runs/search":
                    self._send(200, (fake.fixtures_dir / "mlflow-runs-search.json").read_bytes())
                    return
                self._send(404, json.dumps({"error_code": "ENDPOINT_NOT_FOUND", "message": f"fake_mlflow: no route POST {path}"}).encode())

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
        self.thread.join(5)
