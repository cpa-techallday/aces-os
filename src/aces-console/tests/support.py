"""Shared test helpers (not a test module): an in-process console on an ephemeral loopback port, HTTP and SSE readers,
a closed loopback port for "upstream down", and per-class state directories. Standard library only."""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

from aces import config as aces_config

from aces_console import config as console_config, server

from . import ROOT

_STATE_N = [0]


def closed_port() -> int:
    """A loopback port nothing listens on (bound once, then released): connections are refused at once."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def closed_url(path: str = "") -> str:
    return f"http://127.0.0.1:{closed_port()}{path}"


def fresh_state(tag: str) -> Path:
    """A new ACES_STATE directory (the ledger lives there), made current for aces and the console."""
    _STATE_N[0] += 1
    state = ROOT / f"state-{tag}-{_STATE_N[0]}"
    state.mkdir(parents=True, exist_ok=True)
    aces_config.STATE = state
    os.environ["ACES_STATE"] = str(state)
    return state


def ledger_entries() -> list[dict]:
    p = aces_config.STATE / "ledger.jsonl"
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def ledger_text() -> str:
    p = aces_config.STATE / "ledger.jsonl"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


class Console:
    """A running console server bound to 127.0.0.1:0 whose upstreams point where the test says (closed ports by
    default, so every proxy answers an honest "unreachable")."""

    def __init__(self, *, gc: str | None = None, rigstate: str | None = None, simlab_console: str | None = None,
                 fixtures: str | Path | None = None, static_dir: str | Path | None = None, bind: str = "127.0.0.1",
                 state: Path | None = None, tag: str = "console"):
        # the state and work roots current before this console, restored by Console.close()
        self._prev_state = (aces_config.STATE, os.environ.get("ACES_STATE"))
        self._prev_work = (aces_config.WORK, os.environ.get("ACES_WORK"))
        if state is not None:  # a given state (a restart test) is made current like a fresh one
            aces_config.STATE = state
            os.environ["ACES_STATE"] = str(state)
        self.state = state or fresh_state(tag)
        overrides = {"server": {"grid_control": gc or closed_url(), "rig_state": (rigstate or closed_url()) + "/mcp",
                                "simlab_console": simlab_console or closed_url(), "simlab_mcp": closed_url("/mcp"),
                                "mlflow": closed_url(), "timeout_s": 2},
                     "console": {"bind": bind, "port": 0, "state_dir": str(self.state / "console")}}
        self.settings = console_config.load(static=str(static_dir) if static_dir else None,
                                            fixtures=str(fixtures) if fixtures else None, overrides=overrides)
        self.httpd, self.app = server.make_server(self.settings)
        self.port = self.settings.port
        self.url = self.settings.origin
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.app.hub.start()

    # --- HTTP -----------------------------------------------------------------------------------------------------------

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                timeout: float = 15.0) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        finally:
            conn.close()

    def get(self, path: str, headers: dict | None = None) -> tuple[int, dict[str, str], Any]:
        status, hdrs, data = self.request("GET", path, headers=headers)
        return status, hdrs, _maybe_json(data, hdrs)

    def human_headers(self, origin: str | None = None) -> dict[str, str]:
        return {"Origin": origin or self.url, "X-ACES-Intent": "human", "Content-Type": "application/json"}

    def post(self, path: str, body: Any, headers: dict | None = None, human: bool = True,
             raw: bytes | None = None) -> tuple[int, dict[str, str], Any]:
        hdrs = self.human_headers() if human else {}
        hdrs.update(headers or {})
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        status, rh, out = self.request("POST", path, body=data, headers=hdrs)
        return status, rh, _maybe_json(out, rh)

    # --- SSE ------------------------------------------------------------------------------------------------------------

    def open_sse(self, plate: str | None = "fabric", last_event_id: str | None = None, timeout: float = 0.3) -> socket.socket:
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        q = f"?plate={plate}" if plate else ""
        req = f"GET /api/events{q} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAccept: text/event-stream\r\n"
        if last_event_id is not None:
            req += f"Last-Event-ID: {last_event_id}\r\n"
        s.sendall((req + "\r\n").encode("ascii"))
        s.settimeout(timeout)
        return s

    @staticmethod
    def read_sse(sock: socket.socket, seconds: float) -> tuple[int, dict[str, str], list[dict[str, Any]], str]:
        """Read for `seconds`: (status, headers, records, body text). A record is {"event", "data", "id"} or
        {"comment": ...}; `data` is parsed JSON when it parses."""
        deadline = time.monotonic() + seconds
        buf = b""
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        text = buf.decode("utf-8", "replace")
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else 0
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        records: list[dict[str, Any]] = []
        for block in body.split("\n\n"):
            block = block.strip("\n")
            if not block:
                continue
            rec: dict[str, Any] = {}
            for line in block.split("\n"):
                if line.startswith(":"):
                    rec["comment"] = line[1:].strip()
                    continue
                k, _, v = line.partition(":")
                v = v.strip()
                if k == "data":
                    try:
                        rec["data"] = json.loads(v)
                    except json.JSONDecodeError:
                        rec["data"] = v
                else:
                    rec[k] = v
            records.append(rec)
        return status, headers, records, body

    def sse(self, plate: str | None = "fabric", seconds: float = 1.0, last_event_id: str | None = None):
        s = self.open_sse(plate, last_event_id)
        try:
            return self.read_sse(s, seconds)
        finally:
            s.close()

    # --- lifecycle -----------------------------------------------------------------------------------------------------

    def close(self) -> None:
        self.app.hub.stop()
        self.app.broker.close_all(bye=True)
        self.httpd.shutdown()
        self.app.shutdown()
        self.thread.join(5)
        # put the previous ACES_STATE back, so a later test class (and the devserver it starts) never inherits this
        # console's ledger (a makepkg check() found that leak: 8 entries where an empty store was expected)
        for attr, env, prev in (("STATE", "ACES_STATE", getattr(self, "_prev_state", None)),
                                ("WORK", "ACES_WORK", getattr(self, "_prev_work", None))):
            if prev is None:
                continue
            setattr(aces_config, attr, prev[0])
            if prev[1] is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = prev[1]

    def log_text(self) -> str:
        try:
            return self.settings.log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _maybe_json(data: bytes, headers: dict[str, str]) -> Any:
    if "json" in (headers.get("content-type") or "") and data:
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return data
    return data


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
