"""What the golden path shows on the terminal, what it keeps in events.jsonl, and the guard that keeps third-party
output off the terminal.

StdioGuard: for the whole run, file descriptors 1 and 2 point at a log file, so prints, C libraries (OCP's
fontconfig lines), child processes and banners (Warp's) land in log.txt instead of on the terminal or inside a
`--json` document. The command keeps duplicates of the real stdout and stderr for itself: `.out` (the final verdict,
or exactly one JSON document with --json) and `.err` (progress and errors).

Progress: four modes. `bar` (a TTY without --json): one tqdm bar for the designs, other lines through tqdm.write.
`lines` (no TTY, or --json): plain lines. `jsonl`: every event as one JSON line on stderr. `none`: nothing on the
terminal. Every mode appends the same events to events.jsonl once the run directory exists. A heartbeat thread
reads the in-flight cases' progress.json beacons (SIMP iteration, PCG iteration and residual) every 0.5 s for the
bar, and prints a heartbeat line when nothing else was printed for 10 s, so no minute passes in silence.

Standard library only at import time; tqdm is imported when a bar is first drawn.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Iterator

from . import clean, utc_now

MODES = ("bar", "lines", "jsonl", "none")
_O_BINARY = getattr(os, "O_BINARY", 0)
_LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_BINARY


def _text_stream(fd: int):
    """A UTF-8, line-buffered text stream on `fd` that does not own it."""
    return open(fd, "w", encoding="utf-8", errors="backslashreplace", closefd=False, buffering=1)


def _flush(*streams: Any) -> None:
    for s in streams:
        with contextlib.suppress(Exception):
            s.flush()


class StdioGuard:
    """Route fds 1 and 2 to a log file while the command writes to the real terminal through `.out` and `.err`.

    start(): flush, os.dup fds 1 and 2 (kept for `.out` and `.err`), then os.dup2 the log over both; sys.stdout and
    sys.stderr are replaced by streams on the redirected fds; warnings go to the log (logging.captureWarnings).
    move(new_path): copy the log so far into `new_path` (the run directory's log.txt) and point fds 1 and 2 there.
    stop(): restore fds 1 and 2, sys.stdout and sys.stderr; `.out` and `.err` then write to the restored streams.
    Spawned workers inherit the redirected fds on POSIX; on Windows they may not, so workers never print.
    """

    def __init__(self, log_path: "Path | str", *, json_mode: bool):
        self.log_path = Path(log_path)
        self.json_mode = bool(json_mode)
        self.out: Any = sys.stdout
        self.err: Any = sys.stderr
        self.active = False
        self._saved: tuple[int, int] | None = None
        self._log_fd: int | None = None
        self._orig: tuple[Any, Any] | None = None
        self._streams: list[Any] = []

    def __enter__(self) -> "StdioGuard":
        return self.start()

    def __exit__(self, *exc: Any) -> bool:
        self.stop()
        return False

    def start(self) -> "StdioGuard":
        if self.active:
            return self
        _flush(sys.stdout, sys.stderr)
        saved = []
        for fd in (1, 2):
            try:
                saved.append(os.dup(fd))
            except OSError:  # no such stream (a detached process): the command's own output goes nowhere
                saved.append(os.open(os.devnull, os.O_WRONLY))
        self._saved = (saved[0], saved[1])
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_fd = os.open(self.log_path, _LOG_FLAGS, 0o644)
        os.dup2(self._log_fd, 1)
        os.dup2(self._log_fd, 2)
        self._orig = (sys.stdout, sys.stderr)
        sys.stdout = _text_stream(1)
        sys.stderr = _text_stream(2)
        self.out = _text_stream(self._saved[0])
        self.err = _text_stream(self._saved[1])
        self._streams = [sys.stdout, sys.stderr, self.out, self.err]
        logging.captureWarnings(True)
        self.active = True
        return self

    def move(self, new_path: "Path | str") -> Path:
        """Copy the log written so far into `new_path` and continue there; the old file is removed."""
        new_path = Path(new_path)
        if not self.active or self._log_fd is None:
            self.log_path = new_path
            return new_path
        if new_path.resolve() == self.log_path.resolve():
            return new_path
        _flush(sys.stdout, sys.stderr)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_fd = os.open(new_path, _LOG_FLAGS, 0o644)
        with open(self.log_path, "rb") as src:
            for chunk in iter(lambda: src.read(1 << 16), b""):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(new_fd, view):]
        os.dup2(new_fd, 1)
        os.dup2(new_fd, 2)
        os.close(self._log_fd)
        old, self.log_path, self._log_fd = self.log_path, new_path, new_fd
        with contextlib.suppress(OSError):
            old.unlink()
        return new_path

    def stop(self) -> None:
        if not self.active:
            return
        _flush(*self._streams)
        assert self._saved is not None and self._orig is not None
        os.dup2(self._saved[0], 1)
        os.dup2(self._saved[1], 2)
        sys.stdout, sys.stderr = self._orig
        for s in self._streams:
            with contextlib.suppress(Exception):
                s.close()  # closefd=False: the descriptors stay open
        for fd in (*self._saved, self._log_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._saved = self._log_fd = None
        self.out, self.err = sys.stdout, sys.stderr
        logging.captureWarnings(False)
        self.active = False

    def discard(self) -> None:
        """stop() and delete the log (for commands that leave nothing behind when they succeed)."""
        self.stop()
        with contextlib.suppress(OSError):
            self.log_path.unlink()


# ---- formatting ---------------------------------------------------------------------------------------------------
def _g(v: Any, spec: str = ".4g") -> str:
    if isinstance(v, bool) or v is None:
        return "-" if v is None else str(v).lower()
    if isinstance(v, (int, float)):
        return format(v, spec)
    return str(v)


def format_design_line(row: dict) -> str:
    """`d003  volfrac 0.2 rmin_mm 8 -> mass 0.0123 kg · vm_p99 41.2 MPa · umax 0.0213 mm · ok · 4.2 s`; a design
    that did not finish shows its design_status instead of the metrics."""
    params = row.get("params") or {}
    head = f"{row.get('case_id') or '?'}  " + " ".join(f"{k} {_g(v)}" for k, v in params.items())
    parts = []
    if row.get("status") == "FINISHED":
        m = row.get("metrics") or {}
        parts = [f"mass {_g(m.get('mass_kg'))} kg", f"vm_p99 {_g(m.get('vm_p99_MPa'))} MPa", f"umax {_g(m.get('umax_mm'))} mm", "ok"]
    else:
        parts = [str(row.get("design_status") or "failed")]
    wall = row.get("wall_s")
    parts.append(f"{wall:.1f} s" if isinstance(wall, (int, float)) else "- s")
    return f"{head.rstrip()} -> " + " · ".join(parts)


def read_beacon(path: "Path | str") -> dict | None:
    """One progress.json beacon (gendesign.progress) with its case id, or None while it is missing or being
    replaced."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["case"] = Path(path).parent.name
    return data


def beacon_text(b: dict) -> str:
    """`d003 simp 12/25`, `d004 pcg 340/20000 relres 3.2e-06`, or the phase name for other phases."""
    phase, step, total = b.get("phase") or "?", b.get("step"), b.get("total")
    text = f"{b.get('case')} {phase}"
    if step is not None and total:
        text += f" {step}/{total}"
    elif step is not None:
        text += f" {step}"
    if phase == "pcg" and isinstance(b.get("relres"), (int, float)):
        text += f" relres {b['relres']:.1e}"
    return text


def beacon_summary(beacons: list[dict]) -> str:
    """The bar's postfix: one case keeps its full text (`x002 pcg 700/20000 relres 3.2e-07`); several in-flight
    cases are grouped by phase (`simp 12-16/25 x6 · pcg 100-340/20000 x4`), so ten workers fit on one line."""
    if len(beacons) <= 1:
        return " · ".join(beacon_text(b) for b in beacons)
    groups: dict[str, list[dict]] = {}
    for b in beacons:
        groups.setdefault(str(b.get("phase") or "?"), []).append(b)
    parts = []
    for phase, bs in groups.items():
        steps = [int(s) for s in (b.get("step") for b in bs)
                 if isinstance(s, (int, float)) and not isinstance(s, bool)]
        totals = {b.get("total") for b in bs if b.get("total")}
        text = phase
        if steps:
            lo, hi = min(steps), max(steps)
            text += f" {lo}" if lo == hi else f" {lo}-{hi}"
            if len(totals) == 1:
                text += f"/{next(iter(totals))}"
        parts.append(f"{text} x{len(bs)}")
    return " · ".join(parts)


def terminal_columns(stream: Any) -> int | None:
    """Columns of the terminal behind `stream` (StdioGuard.err: a stream on a duplicate of the real stderr), or None.

    POSIX: TIOCGWINSZ on the stream's own descriptor. Windows: os.get_terminal_size takes only fds 0-2, which point at
    the log while the guard is up (and tqdm asks the redirected standard handle, so it sees no console either), so the
    console screen buffer of the stream's own handle is asked instead."""
    try:
        fd = stream.fileno()
    except Exception:
        return None
    if os.name != "nt":
        try:
            return int(os.get_terminal_size(fd).columns) or None
        except (OSError, ValueError):
            return None
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _Coord(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class _Rect(ctypes.Structure):
            _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT), ("Right", wintypes.SHORT),
                        ("Bottom", wintypes.SHORT)]

        class _Info(ctypes.Structure):
            _fields_ = [("dwSize", _Coord), ("dwCursorPosition", _Coord), ("wAttributes", wintypes.WORD),
                        ("srWindow", _Rect), ("dwMaximumWindowSize", _Coord)]

        info = _Info()
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if ctypes.windll.kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return int(info.srWindow.Right - info.srWindow.Left + 1) or None
    except Exception:
        pass
    return None


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _tqdm():
    try:
        from tqdm import tqdm
    except Exception:
        return None
    return tqdm


# ---- progress -----------------------------------------------------------------------------------------------------
class _Watch:
    def __init__(self, beacons: Callable[[], list], label: str):
        self.beacons, self.label, self.t0 = beacons, label, time.monotonic()


class DesignBar:
    """Per-design progress for one batch of cases (golden.DesignBarLike): a tqdm bar in bar mode, one line per design
    otherwise, and one `case` event per design in every mode."""

    def __init__(self, prog: "Progress", total: int, desc: str):
        self.prog, self.total, self.desc, self.k = prog, int(total), desc, 0
        self.bar = None
        tqdm = _tqdm() if prog.mode == "bar" else None
        if tqdm is not None:
            cols = terminal_columns(prog.err)  # tqdm's own lookup fails while fd 2 points at the log (Windows)
            self.bar = tqdm(total=self.total, desc=desc, file=prog.err, unit="design", leave=True, mininterval=0.2,
                            ncols=cols, dynamic_ncols=cols is None)
            prog._bar = self.bar

    def done(self, row: dict) -> None:
        self.k += 1
        text = format_design_line(row)
        self.prog._emit("case", case_id=row.get("case_id"), i=self.k, n=self.total, msg=text, data=row)
        self.prog._print(text)
        if self.bar is not None:
            self.bar.update(1)

    def close(self) -> None:
        if self.bar is not None:
            with contextlib.suppress(Exception):
                self.bar.close()
            if self.prog._bar is self.bar:
                self.prog._bar = None
            self.bar = None


class Progress:
    """Terminal progress and events.jsonl (golden.ProgressLike). `err` is the real stderr (StdioGuard.err).

    The heartbeat starts with the first stage and stops on close(). `tick_s` (beacon reads) and `quiet_after_s` (the
    silence after which a heartbeat line is printed) are 0.5 s and 10 s; tests shorten them."""

    def __init__(self, mode: str, err: Any, *, events_path: "Path | str | None" = None, total_stages: int = 13,
                 tick_s: float = 0.5, quiet_after_s: float = 10.0):
        if mode not in MODES:
            raise ValueError(f"progress mode must be one of {', '.join(MODES)}, not {mode!r}")
        self.mode, self.err, self.total_stages = mode, err, int(total_stages)
        self.tick_s, self.quiet_after_s = float(tick_s), float(quiet_after_s)
        self._t0 = time.monotonic()
        self._lock = threading.RLock()
        self._events_path: Path | None = None
        self._pending: list[dict] = []
        self._stage: str | None = None
        self._stage_t0 = self._t0
        self._last_out = self._t0
        self._last_hb = 0.0
        self._watches: list[_Watch] = []
        self._bar: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if events_path is not None:
            self.attach(events_path)

    # ---- events -------------------------------------------------------------------------------------------------
    def attach(self, events_path: "Path | str") -> None:
        """Start writing events.jsonl (the run directory now exists); events recorded before are written first."""
        with self._lock:
            self._events_path = Path(events_path)
            pending, self._pending = self._pending, []
            self._append(pending)

    def _append(self, events: list[dict]) -> None:
        if not events or self._events_path is None:
            return
        with contextlib.suppress(OSError):
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._events_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in events)

    def _emit(self, kind: str, *, stage: str | None = None, case_id: str | None = None, i: int | None = None,
              n: int | None = None, msg: str | None = None, data: Any = None) -> dict:
        ev = {"t": round(time.monotonic() - self._t0, 3), "ts": utc_now(), "kind": kind,
              "stage": stage if stage is not None else self._stage, "case_id": case_id, "i": i, "n": n, "msg": msg,
              "data": clean(data) if data is not None else None}
        with self._lock:
            if self._events_path is None:
                self._pending.append(ev)
            else:
                self._append([ev])
            if self.mode == "jsonl":
                self._write(json.dumps(ev, ensure_ascii=False))
                self._last_out = time.monotonic()
        return ev

    def event(self, kind: str, **data: Any) -> None:
        """A free-form event; stage, case_id, i, n and msg are lifted to the top level, the rest goes under data."""
        top = {k: data.pop(k) for k in ("stage", "case_id", "i", "n", "msg") if k in data}
        self._emit(kind, data=data or None, **top)

    # ---- terminal -----------------------------------------------------------------------------------------------
    def _write(self, text: str) -> None:
        with self._lock:
            try:
                if self.mode == "bar" and self._bar is not None:
                    tqdm = _tqdm()
                    if tqdm is not None:
                        tqdm.write(text, file=self.err)
                        return
                self.err.write(text + "\n")
                self.err.flush()
            except Exception:  # a closed terminal must not stop the computation
                pass

    def _print(self, text: str) -> None:
        if self.mode in ("bar", "lines"):
            self._write(text)
            self._last_out = time.monotonic()

    def stage(self, i: int, name: str, text: str) -> None:
        with self._lock:
            self._stage, self._stage_t0 = name, time.monotonic()
        line = f"[{i}/{self.total_stages}] {name}  {text}".rstrip()
        self._emit("stage", stage=name, i=i, n=self.total_stages, msg=text)
        self._print(line)
        self._ensure_thread()

    def line(self, text: str) -> None:
        self._emit("note", msg=text, data={"level": "line"})
        self._print(text)
        self._ensure_thread()

    def note(self, text: str, level: str = "info") -> None:
        self._emit("note", msg=text, data={"level": level})
        self._print(text if level == "info" else f"{level}: {text}")

    def designs(self, total: int, desc: str) -> DesignBar:
        self._ensure_thread()
        return DesignBar(self, total, desc)

    def watch(self, beacons: Callable[[], list[Path]], label: str) -> AbstractContextManager[None]:
        return self._watch(beacons, label)

    @contextlib.contextmanager
    def _watch(self, beacons: Callable[[], list[Path]], label: str) -> Iterator[None]:
        w = _Watch(beacons, label)
        with self._lock:
            self._watches.append(w)
        self._ensure_thread()
        try:
            yield
        finally:
            with self._lock:
                with contextlib.suppress(ValueError):
                    self._watches.remove(w)

    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    # ---- heartbeat ----------------------------------------------------------------------------------------------
    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._beat, name="aces-golden-heartbeat", daemon=True)
                self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.tick()
            except Exception:  # the heartbeat never takes the run down
                pass

    def tick(self) -> str | None:
        """One heartbeat step (the thread calls it every tick_s): refresh the bar's postfix from the beacons, and
        return (and show) a heartbeat line when nothing was shown for quiet_after_s."""
        now = time.monotonic()
        with self._lock:
            watch = self._watches[-1] if self._watches else None
            stage, stage_t0 = self._stage, self._stage_t0
        beacons: list[dict] = []
        if watch is not None:
            try:
                paths = list(watch.beacons() or [])
            except Exception:
                paths = []
            beacons = [b for b in (read_beacon(p) for p in paths) if b]
        if self.mode == "bar" and self._bar is not None:
            with contextlib.suppress(Exception):  # the postfix shows the beacons; a refresh keeps the clock moving
                if not getattr(self._bar, "dynamic_ncols", False):
                    cols = terminal_columns(self.err)  # follow a resized terminal; the line never wraps
                    if cols:
                        self._bar.ncols = cols
                if beacons:
                    self._bar.set_postfix_str(beacon_summary(beacons), refresh=True)
                else:
                    self._bar.refresh()
            return None
        if now - self._last_out < self.quiet_after_s or now - self._last_hb < self.quiet_after_s:
            return None
        self._last_hb = now
        if beacons:
            text = "heartbeat: " + " · ".join(beacon_text(b) for b in beacons) + f" ({int(now - watch.t0)} s)"
        else:
            label = watch.label if watch is not None else (stage or "start-up")
            since = watch.t0 if watch is not None else stage_t0
            text = f"... still in {label} ({int(now - since)} s)"
        self._emit("heartbeat", msg=text, data={"beacons": beacons} if beacons else None)
        if self.mode in ("bar", "lines"):
            self._write(text)
        return text

    def close(self) -> None:
        """Stop the heartbeat and close any open bar."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        if self._bar is not None:
            with contextlib.suppress(Exception):
                self._bar.close()
            self._bar = None


def make_progress(mode: str, err: Any, *, json_mode: bool, quiet: bool) -> Progress:
    """The Progress for a command: `--quiet` is none; auto is bar on a TTY without --json, else lines; bar falls back
    to lines when tqdm is missing."""
    if quiet:
        mode = "none"
    if mode in (None, "", "auto"):
        mode = "bar" if (not json_mode and _isatty(err)) else "lines"
    if mode == "bar" and _tqdm() is None:
        mode = "lines"
    return Progress(mode, err)
