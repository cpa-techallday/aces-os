"""aces golden: the ACES golden path, one command from a design space to a verified, evidenced organic part.

Design space (STEP) -> voxel -> SIMP generator -> organic implicit skin -> real voxel FEA -> sweep -> surrogate ->
verify (+ finer-resolution independent check on this desktop or on a rig lane through simlab-mcp, + GPU and Warp
parity, + a conforming tet10 second discretisation) -> evidence (simlab ledger + aces ledger) -> report. This module
holds the constants, the error type, the shared protocols and small JSON and hash helpers. Standard library only:
importing it must stay instant."""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import math
import os
import tempfile
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Protocol

VERSION = "0.2.0"
SCHEMA = {k: f"aces-golden-{k}/1" for k in (
    "spec", "run", "result", "compute", "compute-used", "inputs", "domain", "case", "skin", "sweep", "surrogate",
    "optimise", "validation", "winner", "bundle", "ledger", "ledgers", "report", "event", "rig", "rig-session",
    "rig-case", "outcome", "files", "conforming", "conforming-case")}
LEDGER_KIND = "golden.run"
DEMOS = ("root-mount", "bone")
DEFAULT_DEMO = "root-mount"
DEMO_PROJECT = "golden-demo"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_THIN = 3
EXIT_UNDER_RESOLVED = 4
EXIT_INFEASIBLE = 5
EXIT_EVIDENCE = 6
EXIT_INTERNAL = 7
EXIT_INTERRUPTED = 130
OUTCOMES = {EXIT_OK: "pass", EXIT_FAIL: "verification-failed", EXIT_USAGE: "usage", EXIT_THIN: "thin-sweep",
            EXIT_UNDER_RESOLVED: "under-resolved", EXIT_INFEASIBLE: "infeasible", EXIT_EVIDENCE: "evidence-failed",
            EXIT_INTERNAL: "error", EXIT_INTERRUPTED: "interrupted"}

GRADE_PREVIEW = "preview-grade"
GRADE_VERIFIED_DESKTOP = "verified-desktop"
# produced (golden 0.2.0) when the level-1 check solved on the rig with a validated golden-check image, the conforming
# tet10 check ran on the rig with a validated ccx-check image and agrees, the rig ran this desktop's golden code and every
# pulled file verified (golden.verify.grade, section 13 of the rig build spec)
GRADE_RIG = "rig-verified"
GRADES = (GRADE_PREVIEW, GRADE_VERIFIED_DESKTOP, GRADE_RIG)
GRADE_RANK = {GRADE_PREVIEW: 0, GRADE_VERIFIED_DESKTOP: 1, GRADE_RIG: 2}

METRICS = ("mass_kg", "mass_voxel_kg", "mesh_volume_mm3", "voxel_volume_mm3", "solid_fraction",
           "compliance_Nmm", "umax_mm", "vm_max_MPa", "vm_p99_MPa", "vm_p95_MPa")
OUTPUTS = tuple(m for m in METRICS if m != "solid_fraction")  # what a spec may name as objective or constraint
STAGES = ("compute", "inputs", "domain", "sweep", "loop", "check", "export", "cfd", "evidence", "report")
ROLES = ("sweep", "verify", "check", "parity", "conforming")
DESIGN_STATUSES = ("ok", "empty", "not-watertight", "multi-body", "detached-load", "detached-support",
                   "disconnected", "unconverged", "non-finite", "error", "rig-error", "rig-timeout", "rig-unreachable",
                   "rig-cancelled", "rig-integrity")
RIG_STATUSES = ("rig-error", "rig-timeout", "rig-unreachable", "rig-cancelled", "rig-integrity")


class GoldenError(Exception):
    """A refusal or failure with its exit code. `message` is one line for the user; `hint` is optional."""

    def __init__(self, code: int, message: str, *, hint: str | None = None, stage: str | None = None):
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.hint = hint
        self.stage = stage


def clean(o: Any) -> Any:
    """JSON-safe deep copy: numpy scalars and arrays become Python numbers and lists, Path becomes str, datetime
    becomes ISO 8601, tuples become lists, non-finite floats become None (records never hold NaN or inf)."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if o is None or isinstance(o, bool):
        return o
    if isinstance(o, int):
        return int(o)
    if isinstance(o, str):
        return str(o)
    if isinstance(o, float):
        return float(o) if math.isfinite(o) else None
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (_dt.datetime, _dt.date)):
        return o.isoformat()
    if hasattr(o, "tolist"):
        return clean(o.tolist())
    if hasattr(o, "item"):
        return clean(o.item())
    return str(o)


def canonical_json(o: Any) -> str:
    return json.dumps(clean(o), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stage_wall_sum(stages: Any, names: Any) -> float:
    """wall_s summed over the run.json stages whose name is in `names`, in run.json order, by plain float addition. The
    report's summary wall time and its provenance check (evidence.resolve_key) both call this, so they agree bit for bit
    on every Python: the built-in sum() of floats is compensated (Neumaier) from Python 3.12 on and can differ from a
    plain loop in the last bit."""
    total = 0.0
    for st in stages or []:
        if st.get("name") in names and isinstance(st.get("wall_s"), (int, float)):
            total += float(st["wall_s"])
    return total


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: "str | os.PathLike[str]") -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _replace(src: str, dst: Path) -> None:
    """os.replace, retried for up to about 2 s on Windows, where it raises PermissionError while another process
    (a concurrent `aces golden show`, a test reading run.json) holds the target open; POSIX never needs the retry."""
    for attempt in range(100):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 99:
                raise
            time.sleep(0.02)


def _atomic_write(path: Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def dump_json(path: "str | os.PathLike[str]", obj: Any, *, indent: int = 1) -> Path:
    """Atomic UTF-8 JSON, LF line endings, trailing newline, no NaN."""
    text = json.dumps(clean(obj), indent=indent, ensure_ascii=False, allow_nan=False) + "\n"
    return _atomic_write(Path(path), text.encode("utf-8"))


def write_text(path: "str | os.PathLike[str]", text: str) -> Path:
    """Atomic UTF-8 text with LF line endings (CRLF in `text` is normalised)."""
    return _atomic_write(Path(path), text.replace("\r\n", "\n").encode("utf-8"))


def read_json(path: "str | os.PathLike[str]", default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


class DesignBarLike(Protocol):
    def done(self, row: dict) -> None: ...
    def close(self) -> None: ...


class ProgressLike(Protocol):
    def stage(self, i: int, name: str, text: str) -> None: ...
    def line(self, text: str) -> None: ...
    def note(self, text: str, level: str = "info") -> None: ...
    def designs(self, total: int, desc: str) -> DesignBarLike: ...
    def watch(self, beacons: Callable[[], list[Path]], label: str) -> AbstractContextManager[None]: ...
    def event(self, kind: str, **data: Any) -> None: ...


class NullProgress:
    """No-op progress for library use and tests (also a no-op design bar)."""

    def stage(self, i: int, name: str, text: str) -> None:
        pass

    def line(self, text: str) -> None:
        pass

    def note(self, text: str, level: str = "info") -> None:
        pass

    def designs(self, total: int, desc: str) -> "NullProgress":
        return self

    def done(self, row: dict) -> None:
        pass

    def close(self) -> None:
        pass

    def watch(self, beacons: Callable[[], list[Path]], label: str) -> AbstractContextManager[None]:
        return contextlib.nullcontext()

    def event(self, kind: str, **data: Any) -> None:
        pass


class RunDirLike(Protocol):
    root: Path
    run_id: str
    project: str
    exp_id: str

    def path(self, *parts: str) -> Path: ...
    def rel(self, p: "Path | str") -> str: ...
    def case_dir(self, case_id: str) -> Path: ...
    def write_json(self, rel: str, obj: Any) -> Path: ...
    def read_json(self, rel: str, default: Any = None) -> Any: ...
    def write_text(self, rel: str, text: str) -> Path: ...
    def register_case(self, case: dict) -> Path: ...
    def cases(self) -> list[dict]: ...
