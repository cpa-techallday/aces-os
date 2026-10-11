"""A small urllib client: timeouts, no proxies, one error vocabulary, and the fixtures replay.

    get_json(url, timeout, headers=None)        -> (status, obj | None, error | None)
    post_json(url, body, timeout, headers=None) -> (status, obj | None, error | None)
    stream(url, timeout, headers=None)          -> (status, headers, iterator)   raises HttpcError on a network failure

Errors are one line each: "unreachable: <reason>" (refused, reset, DNS, timed out; status 0), "HTTP <status>" (the
body is still returned, parsed when it is JSON) and "not JSON: <msg>". MCP servers answer with text/event-stream: the
first `data:` line is parsed, as aces.fabric.http_json does (src/aces-base/aces/fabric.py:32-37).

Headers are sent exactly as given and never logged, echoed or raised: a token lives only in the caller's header dict.
Environment proxies are ignored (the rig is on the LAN). Fixtures: `set_fixtures(dir, bases)` switches every call to
a replay of the 2026-10-11 captures (see Fixtures) for builders and tests only; a write (GRID CONTROL POST /api/switch)
is never replayed.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

__all__ = ["HttpcError", "get_json", "post_json", "stream", "decode", "set_fixtures", "fixtures", "Fixtures"]


class HttpcError(RuntimeError):
    """A network-level failure of stream(): the message is the "unreachable: <reason>" line."""


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
GET_ACCEPT = "application/json"
POST_ACCEPT = "application/json, text/event-stream"
_REASON_MAX = 120
_CHUNK = 64 * 1024


def _reason(exc: BaseException) -> str:
    r = getattr(exc, "reason", None)
    text = str(r if r is not None else exc).strip() or type(exc).__name__
    return text[:_REASON_MAX]


def decode(raw: bytes) -> tuple[Any, str | None]:
    """(object, error): JSON text, or the first `data:` line of an event stream."""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None, None
    if text.startswith(("event:", "data:", ":", "id:", "retry:")):
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip()), None
                except json.JSONDecodeError as e:
                    return None, f"not JSON: {e.msg}"
        return None, "not JSON: event stream without a data line"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"not JSON: {e.msg}"


def _do(method: str, url: str, data: bytes | None, timeout: float, headers: dict) -> tuple[int, bytes, str | None]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 (LAN endpoints from the aces config)
            return int(resp.status), resp.read(), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # noqa: BLE001 (a reset while reading the error body)
            body = b""
        return int(e.code), body, f"HTTP {e.code}"
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        reason = _reason(e)
        if isinstance(e, (socket.timeout, TimeoutError)) or isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)) \
                or "timed out" in reason:
            reason = f"timed out after {timeout:g} s"
        return 0, b"", f"unreachable: {reason}"


def get_json(url: str, timeout: float, headers: dict | None = None) -> tuple[int, Any, str | None]:
    """GET `url`; (status, parsed body or None, error or None)."""
    if _FIXTURES is not None:
        return _FIXTURES.replay("GET", url, None)
    hdrs = {"Accept": GET_ACCEPT}
    hdrs.update(headers or {})
    status, raw, err = _do("GET", url, None, timeout, hdrs)
    if status == 0:
        return 0, None, err
    obj, derr = decode(raw)
    if err:
        return status, obj, err
    if derr:
        return status, None, derr
    return status, obj, None


def post_json(url: str, body: Any, timeout: float, headers: dict | None = None) -> tuple[int, Any, str | None]:
    """POST `body` as JSON to `url`; (status, parsed reply or None, error or None)."""
    if _FIXTURES is not None:
        return _FIXTURES.replay("POST", url, body)
    hdrs = {"Accept": POST_ACCEPT, "Content-Type": "application/json"}
    hdrs.update(headers or {})
    data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    status, raw, err = _do("POST", url, data, timeout, hdrs)
    if status == 0:
        return 0, None, err
    obj, derr = decode(raw)
    if err:
        return status, obj, err
    if derr:
        return status, None, derr
    return status, obj, None


def post_bytes(url: str, data: bytes, timeout: float, headers: dict | None = None) -> tuple[int, Any, str | None]:
    """POST raw bytes (an upload) to `url`; (status, parsed JSON reply or None, error or None). Never replayed."""
    if _FIXTURES is not None:
        return 0, None, "fixtures mode: uploads are not replayed"
    hdrs = {"Accept": POST_ACCEPT}
    hdrs.update(headers or {})
    status, raw, err = _do("POST", url, data, timeout, hdrs)
    if status == 0:
        return 0, None, err
    obj, derr = decode(raw)
    if err:
        return status, obj, err
    if derr:
        return status, None, derr
    return status, obj, None


def _iter_body(resp) -> Iterator[bytes]:
    try:
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def stream(url: str, timeout: float, headers: dict | None = None) -> tuple[int, dict, Iterator[bytes]]:
    """Open `url` for streaming: (status, response headers, chunk iterator). An HTTP error returns its status and
    body; a network failure raises HttpcError("unreachable: <reason>"). Not replayed in fixtures mode."""
    if _FIXTURES is not None:
        raise HttpcError("fixtures mode: streams are not replayed")
    hdrs = {"Accept": "*/*"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, method="GET", headers=hdrs)
    try:
        resp = _OPENER.open(req, timeout=timeout)  # noqa: S310
    except urllib.error.HTTPError as e:
        return int(e.code), dict(e.headers.items()), _iter_body(e)
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        raise HttpcError(f"unreachable: {_reason(e)}") from None
    return int(resp.status), dict(resp.headers.items()), _iter_body(resp)


# --- fixtures replay (builders and tests only) ----------------------------------------------------------------------

GET_FILES: dict[tuple[str, str], str] = {
    ("grid_control", "/api/status"): "gc-status.json",
    ("grid_control", "/api/modes"): "gc-modes.json",
    ("grid_control", "/api/telemetry"): "gc-telemetry.json",
    ("grid_control", "/api/switch/status"): "gc-switch-status.json",
    ("simlab_console", "/api/capacity"): "simlab-capacity.json",
    ("simlab_console", "/api/summary"): "simlab-summary.json",
    ("simlab_console", "/api/experiments"): "simlab-experiments.json",
    ("simlab_console", "/api/registry"): "simlab-registry.json",
    ("simlab_console", "/api/evidence"): "simlab-evidence.json",
    ("simlab_console", "/api/evidence/verify"): "simlab-evidence-verify.json",
    ("simlab_console", "/api/audit"): "simlab-audit.json",
    ("simlab_console", "/api/telemetry"): "simlab-telemetry.json",
    ("simlab_console", "/api/modes"): "simlab-modes.json",
    ("mlflow", "/api/2.0/mlflow/experiments/search"): "mlflow-experiments.json",
    ("mlflow", "/api/2.0/mlflow/runs/get"): "mlflow-run-get.json",
    ("mlflow", "/api/2.0/mlflow/runs/search"): "mlflow-runs-search.json",
}
GET_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("simlab_console", re.compile(r"^/api/experiments/[^/]+$"), "simlab-experiment-detail.json"),
    ("simlab_console", re.compile(r"^/api/case/[^/]+/[^/]+$"), "simlab-case-detail.json"),
]
MCP_FILES: dict[tuple[str, str], str] = {
    ("rig_state", "tools/list"): "rigstate-tools.json",
    ("rig_state", "rig_snapshot"): "rigstate-snapshot.json",
    ("simlab_mcp", "tools/list"): "simlab-mcp-tools.json",
    ("simlab_mcp", "sim_whoami"): "simlab-whoami.json",
}
NEVER_REPLAYED: set[tuple[str, str, str]] = {("POST", "grid_control", "/api/switch"), ("POST", "grid_control", "/api/auto")}


class Fixtures:
    """Replays the captured JSON files of 2026-10-11 instead of calling the upstreams. `bases` maps the config keys
    (grid_control, rig_state, simlab_mcp, simlab_console, mlflow) to their configured URLs so one path such as
    /api/telemetry resolves to the right capture. Nothing is synthesised: an uncaptured route answers
    (0, None, "not captured in fixtures: ...") and the console shows that sentence."""

    def __init__(self, directory: str | Path, bases: dict[str, str]):
        self.dir = Path(directory)
        self.bases = {k: v.rstrip("/") for k, v in bases.items() if isinstance(v, str) and v}

    def label(self, url: str) -> str | None:
        best = None
        for key, base in self.bases.items():
            if url.startswith(base) and (best is None or len(base) > len(self.bases[best])):
                best = key
        return best

    def _load(self, name: str) -> tuple[int, Any, str | None]:
        path = self.dir / name
        try:
            with open(path, encoding="utf-8") as fh:
                return 200, json.load(fh), None
        except FileNotFoundError:
            return 0, None, f"fixture file missing: {name}"
        except (OSError, json.JSONDecodeError) as e:
            return 0, None, f"fixture file unreadable: {name}: {e}"

    def replay(self, method: str, url: str, body: Any) -> tuple[int, Any, str | None]:
        label = self.label(url)
        path = urllib.parse.urlparse(url).path.rstrip("/") or "/"
        if label is None:
            return 0, None, f"not captured in fixtures: {method} {url}"
        if (method, label, path) in NEVER_REPLAYED:
            return 0, None, f"fixtures mode: {method} {path} is never replayed"
        if method == "GET":
            name = GET_FILES.get((label, path))
            if name is None:
                for plabel, pat, pname in GET_PATTERNS:
                    if plabel == label and pat.match(path):
                        name = pname
                        break
            if name is None:
                return 0, None, f"not captured in fixtures: GET {label}{path}"
            return self._load(name)
        if method == "POST" and isinstance(body, dict) and body.get("jsonrpc") == "2.0":
            rpc_method = body.get("method")
            if rpc_method == "initialize":
                params = body.get("params") or {}
                return 200, {"jsonrpc": "2.0", "id": body.get("id"),
                             "result": {"protocolVersion": params.get("protocolVersion"), "capabilities": {},
                                        "serverInfo": {"name": "aces-console fixtures replay", "version": "0"}}}, None
            if rpc_method == "notifications/initialized":
                return 202, None, None
            tool = (body.get("params") or {}).get("name") if rpc_method == "tools/call" else rpc_method
            name = MCP_FILES.get((label, tool or ""))
            if name is None:
                return 0, None, f"not captured in fixtures: {label} {rpc_method} {tool or ''}".rstrip()
            status, obj, err = self._load(name)
            if isinstance(obj, dict) and "id" in body:
                obj = dict(obj, id=body.get("id"))
            return status, obj, err
        return 0, None, f"not captured in fixtures: {method} {label}{path}"


_FIXTURES: Fixtures | None = None


def set_fixtures(directory: str | Path | None, bases: dict[str, str] | None = None) -> None:
    """Switch every call to the replay (directory given) or back to the network (None)."""
    global _FIXTURES
    _FIXTURES = Fixtures(directory, bases or {}) if directory else None


def fixtures() -> Fixtures | None:
    return _FIXTURES
