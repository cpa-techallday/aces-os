"""The golden path, end to end: stage order, outcomes, exit codes and the run's own records.

    0 preflight  BLAS pinned to one thread, the stdio guard, the spec, `aces` present, then the rig lane (--lane,
                 else [golden] lane in aces.toml): its usage rules and the live probe of simlab-mcp. A --lane that
                 cannot be used exits 2 here; a config lane that cannot is a warning (desktop only)   (no run dir)
    1 compute    detect CPUs, GPUs, Warp, the conforming mesher and rig lanes; resolve and guard the spec; the kernel
                 self-test gate (7); the project; the run directory
    2 inputs     inputs/spec.json, the design-space STEP, the grid (guarded again when derived from the STEP; the CPU
                 cell warning), spec.effective.json, the compute plan, compute.json, spec.resolved.json (the simlab
                 experiment)
    3 domain     the design space voxelised through the step-structures adapter
    4 sweep      N computed designs: SIMP -> organic skin -> real voxel FEA, on the rig with --rig-sweep (the thin
                 rule follows)
    5 loop       surrogate -> optimiser -> verify on the real solver, round by round
    6 check      the finer-resolution independent check (on the rig with a lane; x101 on this desktop when the rig
                 does not deliver level 1), GPU and Warp parity, the conforming tet10 check (m001 on the rig or here;
                 m101 here when the rig does not deliver it), grade, validation.json
    7 export     the winner (or candidate) STL and renders, the sweep gallery, charts, compute_used.json
    8 evidence   files.sha256, the simlab bundle and ledger, golden.run in the aces ledger, the project
    9 report     report.md, report.html, report.json
      finish     result.json (what --json prints), run.json, manifest.sha256, the verdict on stdout

The stage functions are the generator's and the verifier's (golden.spec, demo, domain, sweep, verify, compute,
evidence, export, render, report); this module calls them in order and maps what happened to an exit code:
0 pass, 1 verification failed (or the grade is below --strict / --require-grade), 2 usage (a rig lane that cannot be
used included), 3 thin sweep, 4 under-resolved, 5 infeasible, 6 evidence, ledger or rig integrity failure, 7 internal
error, 130 interrupted (rig jobs cancelled on a best-effort basis). When several apply: a pre-compute 2 or 4, then 7,
then 3, 5, 1, then 6. Nothing numeric is produced here: every number in result.json is read back from
the records the stages wrote.
"""
from __future__ import annotations

import contextlib
import getpass
import importlib
import json
import os
import signal
import sys
import tempfile
import time
import traceback
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (DEMO_PROJECT, EXIT_EVIDENCE, EXIT_FAIL, EXIT_INFEASIBLE, EXIT_INTERNAL,
               EXIT_INTERRUPTED, EXIT_OK, EXIT_THIN, EXIT_UNDER_RESOLVED, EXIT_USAGE, GRADE_RANK,
               GRADE_VERIFIED_DESKTOP, GRADES, OUTCOMES, SCHEMA, STAGES, VERSION, GoldenError, _atomic_write, clean,
               read_json, sha256_bytes, utc_now)
from .acescli import AcesCli, AcesCliError
from .progress import Progress, StdioGuard, make_progress
from .rundir import RunDir, slug, work_root

BLAS_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
VENDORED_DEPRECATION = r".*axes.*should not be.*None.*"  # gendesign.filters on NumPy 2.x; harmless today


@dataclass
class Options:
    """`aces golden run` options (golden_cli builds them from argv)."""
    demo: str | None = None
    spec: Path | None = None
    step: Path | None = None
    project: str | None = None
    new: bool = False
    n: int | None = None
    seed: int | None = None
    res: float | None = None
    gen_res: float | None = None
    tol: float | None = None
    rounds: int | None = None
    check: str | None = None
    check_budget: float | None = None
    allow_thin: bool = False
    strict: bool = False
    device: str = "auto"
    jobs: str = "auto"
    lane: str | None = None
    rig_sweep: bool = False
    rig_budget: float = 1800.0
    conforming: str = "auto"
    cfd: str = "auto"           # auto | template | off — the flow-block CFD stage's dispatch gate
    require_grade: str | None = None
    json_out: bool = False
    progress: str = "auto"
    quiet: bool = False
    note: str | None = None
    argv: list[str] = field(default_factory=list)


def overrides_from(opts: Options) -> dict:
    """CLI flags -> dotted spec keys (recorded in derived.overrides)."""
    o: dict[str, Any] = {}
    for flag, key in (("n", "sweep.n"), ("seed", "sweep.seed"), ("res", "resolution.fea_mm"),
                      ("gen_res", "resolution.gen_mm"), ("tol", "verify.tol"), ("rounds", "optimise.rounds"),
                      ("check", "verify.check"), ("check_budget", "verify.check_budget_s")):
        v = getattr(opts, flag)
        if v is not None:
            o[key] = v
    if opts.step is not None:
        o["domain.source"] = "step"
        o["domain.step_file"] = str(Path(opts.step).expanduser().resolve())
    return o


def _mod(name: str) -> Any:
    """A golden stage module, imported when its stage starts (inside the stdio guard, so its import noise is logged)."""
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except ImportError as e:
        raise GoldenError(EXIT_INTERNAL, f"the golden module {name} cannot be imported ({type(e).__name__}: {e})") from e


def _safe_json(p: Path) -> dict:
    try:
        data = read_json(p, {})
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _g(v: Any, spec: str = ".4g") -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return format(v, spec)
    return str(v)


def _json_text(obj: Any) -> str:
    """The text golden.dump_json writes for `obj` (indent 1, UTF-8, no NaN, trailing newline)."""
    return json.dumps(clean(obj), indent=1, ensure_ascii=False, allow_nan=False) + "\n"


def _jobs(value: str | int | None) -> "int | str":
    if value in (None, "", "auto"):
        return "auto"
    return int(value)


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no login name in some containers
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _pin_environment() -> None:
    """One BLAS thread per process (determinism, and no oversubscription under the case pool), a headless
    matplotlib, no MLflow hint; the vendored filters' NumPy deprecation is known and not repeated."""
    for k in BLAS_ENV:
        os.environ[k] = "1"
    os.environ["MPLBACKEND"] = "Agg"
    os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"
    warnings.filterwarnings("ignore", category=DeprecationWarning, message=VENDORED_DEPRECATION)


def result_document(run: "RunDir | None", *, outcome: str, exit_code: int, message: str) -> dict:
    """The compact result (result.json, and exactly what --json prints). Every key is always present; values come
    from the run's records on disk (validation.json, <label>/<label>.json, evidence/ledgers.json, report.json,
    compute_used.json, run.json) and are null or empty when a record does not exist."""
    doc: dict[str, Any] = {
        "schema": SCHEMA["result"], "outcome": outcome, "verdict": None, "exit_code": int(exit_code),
        "message": message, "verdict_line": None, "grade": None, "project": None, "run_id": None, "run_dir": None,
        "report": None, "winner": None, "validation": None, "independent_check": None, "device_parity": [],
        "conforming": None, "rig": None, "evidence": None, "compute_used": [], "started_utc": None,
        "finished_utc": None, "wall_s": None}
    if run is None:
        return doc
    meta = run.meta()
    doc.update(project=run.project, run_id=run.run_id, run_dir=str(run.root), started_utc=meta.get("created_utc"),
               finished_utc=meta.get("finished_utc"), wall_s=(meta.get("timings") or {}).get("total_s"))
    val = _safe_json(run.path("validation.json"))
    if val:
        doc["verdict"], doc["grade"] = val.get("verdict"), val.get("grade")
        rounds = [r for r in (val.get("rounds") or []) if isinstance(r, dict)]
        final = next((r for r in rounds if r.get("round") == val.get("final_round")), rounds[-1] if rounds else {})
        max_rel: dict[str, float] = {}
        for row in final.get("rows") or []:
            o, e = row.get("output"), row.get("rel_err")
            if o and isinstance(e, (int, float)) and not isinstance(e, bool):
                max_rel[o] = max(float(e), max_rel.get(o, float(e)))
        doc["validation"] = {"tol": val.get("tol"), "rounds": len(rounds), "passed": val.get("passed"),
                             "final_round": val.get("final_round"), "max_rel_err": max_rel}
        ic = val.get("independent_check")
        if isinstance(ic, dict):
            lv1 = next((lv for lv in ic.get("levels") or [] if isinstance(lv, dict) and lv.get("level") == 1), {})
            doc["independent_check"] = {"ran": ic.get("ran"), "h_mm": lv1.get("h_mm"), "device": lv1.get("device"),
                                        "host": lv1.get("host"), "passed": ic.get("passed"), "reason": ic.get("reason")}
        cf = val.get("conforming")
        if isinstance(cf, dict):
            doc["conforming"] = {k: cf.get(k) for k in ("ran", "passed", "host", "h_mm", "case_id", "status", "reason")}
        rg = val.get("rig")
        if isinstance(rg, dict):
            doc["rig"] = {"lane": rg.get("lane"), "scope": rg.get("scope"), "experiments": rg.get("experiments") or [],
                          "code_identity_ok": (rg.get("code_identity") or {}).get("ok"),
                          "integrity_error": rg.get("integrity_error")}
        doc["device_parity"] = [{k: p.get(k) for k in ("case_id", "device", "backend", "max_rel_diff", "passed")}
                                for p in val.get("device_parity") or [] if isinstance(p, dict)]
    for label in ("winner", "candidate"):
        w = _safe_json(run.path(label, f"{label}.json"))
        if w:
            stl, m = w.get("stl") or {}, w.get("metrics") or {}
            doc["winner"] = {"label": label, "case_id": w.get("case_id"), "params": w.get("params"),
                             "metrics": {k: m.get(k) for k in ("mass_kg", "umax_mm", "vm_p99_MPa", "vm_max_MPa", "compliance_Nmm")},
                             "stl": str(run.path(stl["path"])) if stl.get("path") else None, "stl_sha256": stl.get("sha256")}
            break
    led = _safe_json(run.path("evidence", "ledgers.json"))
    if led:
        s, a = led.get("simlab") or {}, led.get("aces") or {}
        doc["evidence"] = {"bundle_sha256": s.get("sha256"), "simlab_ledger_entry": s.get("entry"),
                           "aces_ledger_entry": a.get("n"),
                           "ledgers_ok": bool((s.get("verify") or {}).get("ok")) and bool((a.get("verify") or {}).get("ok"))}
    reports = {k: run.path(f"report.{k}") for k in ("md", "html", "json")}
    if any(p.is_file() for p in reports.values()):
        doc["report"] = {k: (str(p) if p.is_file() else None) for k, p in reports.items()}
    doc["verdict_line"] = _safe_json(run.path("report.json")).get("verdict_line")
    doc["compute_used"] = list(_safe_json(run.path("compute_used.json")).get("rows") or [])
    return clean(doc)


class _Pipeline:
    """One `aces golden run`: the state the stages share, and the bookkeeping around them."""

    def __init__(self, opts: Options, guard: StdioGuard, progress: Progress, t_start: float, started_utc: str):
        self.opts, self.guard, self.progress = opts, guard, progress
        self.t_start, self.started_utc = t_start, started_utc
        self.run: RunDir | None = None
        self.current: str | None = None
        self.project: str | None = None
        self.demo_name: str | None = None
        self.aces: AcesCli | None = None
        self.pool: Any = None
        self.overrides = overrides_from(opts)
        self.eff: dict = {}
        self.detected: dict = {}
        self.plan: dict = {}
        self.project_info: dict = {}
        self.operator = _operator()
        self.experiment: Any = None
        self.domain: dict = {}
        self.sweep: dict = {}
        self.loop: dict | None = None
        self.validation: dict = {}
        self.thin_message: str | None = None
        self.evidence_error: GoldenError | None = None
        self.report_paths: dict = {}
        self.label: str | None = None
        self.probe: dict | None = None  # the lane probe (section 10.5), also for a config lane that was refused
        self.session: Any = None  # golden.rig.RigSession when a lane is used

    # ---- bookkeeping --------------------------------------------------------------------------------------------
    def _begin(self, i: int, name: str, text: str, *, t0: float | None = None) -> None:
        self.current = name
        if self.run is not None:
            self.run.stage(name, "running", t0=t0)
        self.progress.stage(i, name, text)

    def _end(self, name: str, note: str = "", state: str = "done") -> None:
        if self.run is not None:
            self.run.stage(name, state, note)
        self.current = None

    def _skip(self, i: int, name: str, note: str) -> None:
        self.progress.stage(i, name, f"skipped ({note})")
        if self.run is not None:
            self.run.stage(name, "skipped", note)

    def _mark_rest(self, failed_note: str, rest_note: str) -> None:
        """After a failure or an interrupt: the current stage failed, every stage not yet recorded is skipped."""
        if self.run is None:
            return
        done = {s.get("name") for s in self.run.meta().get("stages") or []}
        if self.current:
            self.run.stage(self.current, "failed", failed_note)
            done.add(self.current)
        for name in STAGES:
            if name not in done:
                self.run.stage(name, "skipped", rest_note)

    def _say_error(self, message: str, hint: str | None = None) -> None:
        if self.progress.mode == "jsonl":
            self.progress.event("note", msg=f"aces golden: error: {message}", level="error", hint=hint)
            return
        with contextlib.suppress(Exception):
            self.guard.err.write(f"aces golden: error: {message}\n" + (f"hint: {hint}\n" if hint else ""))
            self.guard.err.flush()

    def _output(self, doc: dict, *, human: bool) -> None:
        """The command's stdout: one JSON document with --json; otherwise the verdict line, the report and the part."""
        out = self.guard.out
        with contextlib.suppress(Exception):
            if self.opts.json_out:
                out.write(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
            elif human:
                lines = []
                if doc.get("verdict_line"):
                    lines.append(doc["verdict_line"])
                elif doc.get("verdict"):
                    lines.append(f"{doc['verdict']} · {doc.get('grade') or '-'} · {doc.get('message') or ''}".rstrip(" ·"))
                if (doc.get("report") or {}).get("md"):
                    lines.append(f"report: {doc['report']['md']}")
                w = doc.get("winner") or {}
                if w.get("stl"):
                    lines.append(f"{w.get('label') or 'winner'}: {w['stl']}")
                if lines:
                    out.write("\n".join(lines) + "\n")
            out.flush()

    def _close_run(self, *, status: str, code: int, message: str) -> dict:
        """run.json, result.json, the final event and (unless interrupted) manifest.sha256; returns the result."""
        assert self.run is not None
        outcome = OUTCOMES.get(code, "error")
        paths = {"report_md": None, "report_html": None, "report_json": None, "stl": None}
        for k in ("md", "html", "json"):
            p = self.run.path(f"report.{k}")
            paths[f"report_{k}"] = self.run.rel(p) if p.is_file() else None
        for label in ("winner", "candidate"):
            stl = self.run.path(label, f"{label}.stl")
            if stl.is_file():
                paths["stl"] = self.run.rel(stl)
                break
        self.run.finish(status=status, outcome=outcome, verdict=self.validation.get("verdict"),
                        grade=self.validation.get("grade"), exit_code=code, message=message, paths=paths)
        doc = result_document(self.run, outcome=outcome, exit_code=code, message=message)
        doc["started_utc"] = self.started_utc
        self.run.write_json("result.json", doc)
        self.progress.event("result", msg=message, outcome=outcome, exit_code=code, verdict=doc.get("verdict"),
                            grade=doc.get("grade"), run_dir=str(self.run.root))
        self.progress.close()  # no heartbeat may append to events.jsonl after the manifest
        if status != "interrupted":
            self.run.hash_tree("manifest.sha256", exclude=["log.txt"])
        return doc

    # ---- outcomes -----------------------------------------------------------------------------------------------
    def fail(self, code: int, message: str, *, hint: str | None = None, tb: str | None = None) -> int:
        """A refusal or an error: stages marked, a short report from whatever records exist, run.json and
        result.json (when the run directory exists), the error on stderr, the JSON document with --json."""
        if tb:
            print(tb, file=sys.stderr)  # sys.stderr is the log while the guard is up
            if os.environ.get("ACES_GOLDEN_DEBUG") == "1":
                with contextlib.suppress(Exception):
                    self.guard.err.write(tb)
            if self.run is None:
                hint = hint or f"traceback in {self.guard.log_path}"
        if self.pool is not None:
            with contextlib.suppress(Exception):
                self.pool.cancel()
            self.pool = None
        self._stop_rig(f"the run failed: {message}")
        self._say_error(message, hint)
        if self.run is None:
            doc = result_document(None, outcome=OUTCOMES.get(code, "error"), exit_code=code, message=message)
            doc["project"], doc["started_utc"], doc["finished_utc"] = self.project, self.started_utc, utc_now()
            doc["wall_s"] = round(time.time() - self.t_start, 3)
            self.progress.close()
            self._output(doc, human=False)
            return code
        self._mark_rest(message, f"not run: {self.current or 'an earlier stage'} failed" if self.current else "not run")
        self.run.finish(status="error", outcome=OUTCOMES.get(code, "error"), verdict=self.validation.get("verdict"),
                        grade=self.validation.get("grade"), exit_code=code, message=message, paths={})
        with contextlib.suppress(Exception):  # a short report.md that says what did not run, from the records that exist
            report = _mod("report")
            self.report_paths = report.write(self.run, report.assemble(self.run)) or {}
        doc = self._close_run(status="error", code=code, message=message)
        self._output(doc, human=False)
        return code

    def interrupted(self) -> int:
        """Ctrl-C: workers terminated, run.json interrupted, result.json; a second Ctrl-C exits at once."""
        with contextlib.suppress(Exception):
            signal.signal(signal.SIGINT, lambda *_: os._exit(EXIT_INTERRUPTED))
        if self.pool is not None:
            with contextlib.suppress(Exception):
                self.pool.cancel()
            self.pool = None
        self._stop_rig("interrupted")
        message = f"interrupted during {self.current or 'start-up'}"
        self._say_error(message)
        if self.run is None:
            doc = result_document(None, outcome="interrupted", exit_code=EXIT_INTERRUPTED, message=message)
            doc["project"], doc["started_utc"], doc["finished_utc"] = self.project, self.started_utc, utc_now()
            doc["wall_s"] = round(time.time() - self.t_start, 3)
            self.progress.close()
            self._output(doc, human=False)
            return EXIT_INTERRUPTED
        self._mark_rest("interrupted", "not run: interrupted")
        doc = self._close_run(status="interrupted", code=EXIT_INTERRUPTED, message=message)
        self._output(doc, human=False)
        return EXIT_INTERRUPTED

    def _stop_rig(self, reason: str) -> None:
        """Ctrl-C or a failure: cancel the rig experiments not collected yet (best effort, within the cancel budget) and
        write rig/session.json. Never raises."""
        sess = self.session
        if sess is None or getattr(sess, "closed", False):
            return
        with contextlib.suppress(Exception):
            sess.cancel_all(reason)
        with contextlib.suppress(Exception):
            sess.close()

    def _rig_block(self) -> dict | None:
        """validation.json "rig": the session closed, its evidence cross-referenced; None without a lane."""
        sess = self.session
        if sess is None:
            return None
        if not getattr(sess, "closed", False):
            sess.record_evidence()
            sess.close()
        return sess.validation_block()

    def final_code(self) -> tuple[int, str, str | None]:
        """(exit code, message, hint) from the verdict, --strict / --require-grade, a rig integrity error and the
        evidence stage, by the precedence rule (an integrity error is folded in like the evidence error: 6 when
        nothing worse applies)."""
        v = self.validation
        verdict, grade = v.get("verdict"), v.get("grade")
        reasons = [str(r) for r in (v.get("verdict_reasons") or []) if r]
        hint = None
        if verdict == "THIN":
            code, msg = EXIT_THIN, (reasons[0] if reasons else None) or self.thin_message or "thin sweep"
        elif verdict == "INFEASIBLE":
            code, msg = EXIT_INFEASIBLE, reasons[0] if reasons else "no candidate was predicted feasible with the margins"
        elif verdict == "FAIL":
            code, msg = EXIT_FAIL, "verification failed: " + (reasons[0] if reasons else "see validation.json")
        elif verdict == "PASS":
            need = None  # (rank, label): the higher of --strict (verified-desktop) and --require-grade wins
            if self.opts.strict:
                need = (GRADE_RANK[GRADE_VERIFIED_DESKTOP], "strict")
            req = self.opts.require_grade
            if req in GRADE_RANK and (need is None or GRADE_RANK[req] > need[0]):
                need = (GRADE_RANK[req], f"required grade {req}")
            if need is not None and GRADE_RANK.get(str(grade), 0) < need[0]:
                why = next((str(r) for r in (v.get("grade_reasons") or []) if r), "no finer-resolution check agreed")
                code, msg = EXIT_FAIL, f"{need[1]}: grade {grade} ({why})"
            else:
                code, msg = EXIT_OK, f"verification passed ({grade})"
        else:
            raise GoldenError(EXIT_INTERNAL, f"validation.json has no verdict this command knows ({verdict!r})")
        integrity = (v.get("rig") or {}).get("integrity_error") if isinstance(v.get("rig"), dict) else None
        if integrity:
            hint = "the pulled rig files did not verify: rig/session.json and <case>/rig.json name the file"
            if code == EXIT_OK:
                code, msg = EXIT_EVIDENCE, f"rig integrity error: {integrity}"
            else:
                msg += f"; rig integrity error too: {integrity}"
        if self.evidence_error is not None:
            hint = self.evidence_error.hint
            if code == EXIT_OK:
                code, msg = EXIT_EVIDENCE, self.evidence_error.message
            else:
                msg += f"; evidence failed too: {self.evidence_error.message}"
        return code, msg, hint

    # ---- stage 0 ------------------------------------------------------------------------------------------------
    def preflight(self) -> None:
        o = self.opts
        self.progress.line(f"aces-golden {VERSION} · loading the engineering stack ...")
        if o.demo and o.spec:
            raise GoldenError(EXIT_USAGE, "give --demo or --spec, not both")
        import aces_engineering

        aces_engineering.prepare_env()
        if o.demo:
            demo = _mod("demo")
            self.raw = demo.spec(o.demo)
            self.raw_text = _json_text(self.raw)
            self.raw_bytes = self.raw_text.encode("utf-8")
            self.base_dir = Path(demo.__file__).resolve().parent
            self.demo_name = o.demo
            self.spec_label = f"demo {o.demo}"
        else:
            spec_mod = _mod("spec")
            path = Path(o.spec).expanduser() if o.spec else None
            if path is None:
                if not o.project:
                    raise GoldenError(EXIT_USAGE, "aces golden run needs --demo, --spec FILE or --project NAME",
                                      hint="aces golden run --demo")
                path = work_root() / o.project / "golden" / "spec.json"
                if not path.is_file():
                    raise GoldenError(EXIT_USAGE, f"no spec at {path}", hint=f"aces golden init {o.project}, or pass --spec FILE")
            try:
                self.raw, self.raw_bytes, self.base_dir = spec_mod.load(path, step=Path(o.step) if o.step else None)
            except FileNotFoundError as e:
                raise GoldenError(EXIT_USAGE, f"spec file not found: {e.filename or path}") from e
            except ValueError as e:  # SpecError and malformed JSON
                raise GoldenError(EXIT_USAGE, f"{path}: {e}") from e
            self.raw_text = None
            self.spec_label = f"file {path}"
        self.project = o.project or (DEMO_PROJECT if o.demo else None)
        if not self.project:
            raise GoldenError(EXIT_USAGE, "a --spec run needs --project NAME (an aces project; --new creates it)")
        if slug(self.project) != self.project or self.project in (".", ".."):
            raise GoldenError(EXIT_USAGE, f"project name '{self.project}' must be letters, digits, '.', '_' or '-'")
        self.aces = AcesCli()
        if not self.aces.available():
            raise GoldenError(EXIT_USAGE, f"aces (aces-base >= 0.1.0-3) is not runnable here: {self.aces.problem}",
                              hint="install aces-base, or set ACES_CMD to a JSON array that runs it")
        if not self.aces.supports_record():
            raise GoldenError(EXIT_USAGE, "this aces has no `aces ledger record` (aces-base older than 0.1.0-3), so the run could not be recorded",
                              hint="sudo pacman -S aces-base")
        self._preflight_rig()

    def _preflight_rig(self) -> None:
        """The rig lane (section 10.2), the usage refusals and the live probe (10.5), before any run directory exists:
        a --lane that cannot be used exits 2 with the probe's refusal; a config lane that cannot be used is a warning
        and the run computes on this desktop (the probe record keeps the refusal)."""
        o = self.opts
        rig_mod, compute = _mod("rig"), _mod("compute")
        if o.conforming not in compute.CONFORMING_MODES:
            raise GoldenError(EXIT_USAGE, f"--conforming must be auto, rig, local or off, not {o.conforming!r}")
        if o.cfd not in ("auto", "template", "off"):
            raise GoldenError(EXIT_USAGE, f"--cfd must be auto, template or off, not {o.cfd!r}")
        if o.require_grade is not None and o.require_grade not in GRADES:
            raise GoldenError(EXIT_USAGE, f"--require-grade must be one of {', '.join(GRADES)}, not {o.require_grade!r}")
        try:
            budget = float(o.rig_budget)
        except (TypeError, ValueError):
            budget = float("nan")
        if not budget > 0:
            raise GoldenError(EXIT_USAGE, f"--rig-budget must be a positive number of seconds, not {o.rig_budget!r}")
        budget_given = any(a == "--rig-budget" or a.startswith("--rig-budget=") for a in o.argv or [])
        lane, source = rig_mod.choose_lane(o.lane)
        if lane is None:
            why = " (--lane none was given)" if source == "cli" else ""
            if o.rig_sweep:
                raise GoldenError(EXIT_USAGE, f"--rig-sweep needs a rig lane{why}", hint="aces golden run --lane LANE "
                                                                                       "--rig-sweep ...")
            if budget_given:
                raise GoldenError(EXIT_USAGE, f"--rig-budget needs a rig lane{why}", hint="aces golden run --lane LANE "
                                                                                        "--rig-budget S ...")
            if o.conforming == "rig":
                raise GoldenError(EXIT_USAGE, f"--conforming rig needs a rig lane{why}",
                                  hint="--conforming auto runs it on this desktop when gmsh and ccx are here")
        if o.conforming == "local":
            av = compute.conforming_available()
            if not av.get("usable"):
                raise GoldenError(EXIT_USAGE, f"--conforming local cannot run on this desktop: {av.get('reason')}",
                                  hint="aces golden compute shows what was found (gmsh in this venv, CalculiX ccx)")
        if lane is None:
            return
        check_mode = o.check or (self.raw.get("verify") or {}).get("check") or "auto"
        self.progress.line(f"rig: probing lane {lane} ({'--lane' if source == 'cli' else '[golden] lane in aces.toml'})")
        rec, client = rig_mod.probe(lane, source, check_mode=str(check_mode), conforming_mode=o.conforming,
                                    rig_sweep=bool(o.rig_sweep), warn=lambda m: self.progress.note(m, "warning"))
        self.probe = rec
        if not rec.get("ok"):
            if source == "cli":
                raise GoldenError(EXIT_USAGE, str(rec.get("refusal")))
            self.progress.note(f"rig lane {lane} (from aces.toml) unavailable: {rig_mod.refusal_reason(rec)}; computing "
                               "on this desktop", "warning")
            return
        scope = rig_mod.needs_for(str(check_mode), o.conforming, bool(o.rig_sweep))
        self.session = rig_mod.RigSession(rec, client, budget_s=budget, scope=scope, progress=self.progress)
        for ln in rig_mod.probe_lines(rec):
            self.progress.line("  " + ln)

    # ---- stage 1 ------------------------------------------------------------------------------------------------
    def stage_compute(self) -> None:
        o, t0 = self.opts, time.time()
        self.current = "compute"
        compute, spec_mod = _mod("compute"), _mod("spec")
        with self.progress.watch(lambda: [], "compute: detecting CPUs, GPUs, Warp and rig lanes"):
            self.detected = compute.detect(device=o.device, probe=self.probe,
                                           rig_scope=self.session.scope if self.session is not None else None)
        gpu = self.detected.get("gpu") or {}
        if o.device == "gpu" and not gpu.get("usable"):
            raise GoldenError(EXIT_USAGE, f"no usable CUDA GPU here ({gpu.get('reason') or 'none detected'})",
                              hint="run with --device auto or --device cpu")
        try:
            self.eff = spec_mod.resolve(self.raw, base_dir=self.base_dir, overrides=self.overrides,
                                        gpu_usable=bool(gpu.get("usable")))
        except ValueError as e:  # SpecError
            raise GoldenError(EXIT_USAGE, str(e)) from e
        self._guard_spec(spec_mod, geometry=(self.eff.get("derived") or {}).get("grid") is not None)
        ks = self.detected.get("kernel_selftest") or {}
        if ks.get("passed") is not True:  # no solve on a kernel that fails its own tests; no project, no run directory
            why = ks.get("error") or ("gendesign.fea3d.self_test returned False (element symmetry, rigid-body null space "
                                      "or the uniaxial patch test)")
            raise GoldenError(EXIT_INTERNAL, f"the voxel FEA kernel failed its element/patch self-test: {why}",
                              hint="python -c \"from gendesign.fea3d import self_test; self_test()\" in the engineering "
                                   "venv prints each check")
        try:
            info = self.aces.project_show(self.project)
            if info is None:
                if not (o.new or o.demo):
                    raise GoldenError(EXIT_USAGE, f"no project '{self.project}' under {work_root()}: aces project new {self.project}, or pass --new")
                self.aces.project_new(self.project, hypothesis=self.eff.get("hypothesis"))
        except AcesCliError as e:
            raise GoldenError(EXIT_USAGE, f"aces project: {e}") from e
        self.run = run = RunDir.create(self.project, self.eff, seed=int(self.eff["sweep"]["seed"]))
        run.t0 = self.t_start
        self.guard.move(run.path("log.txt"))
        self.progress.attach(run.path("events.jsonl"))
        run.update(tool={"name": "aces-golden", "version": VERSION, "python": sys.version.split()[0],
                         "argv": list(o.argv)},
                   demo=self.demo_name, spec_sha256=sha256_bytes(self.raw_bytes), note=o.note)
        self.progress.line(f"aces-golden {VERSION} · run {run.run_id} · project {self.project} · {run.root} "
                           f"(log: {run.path('log.txt')})")
        lines = [str(x) for x in (compute.summary_lines(self.detected, None) or [])]
        self._begin(1, "compute", lines[0] if lines else "detection done", t0=t0)
        for ln in lines[1:]:
            self.progress.line("  " + ln)
        self._end("compute", lines[0] if lines else "")

    def _guard_spec(self, spec_mod: Any, *, geometry: bool) -> None:
        msgs = list(spec_mod.guard(self.eff) or [])
        if msgs:
            raise GoldenError(EXIT_UNDER_RESOLVED, "under-resolved: " + "; ".join(msgs),
                              hint="change the resolution or the bounds named above (aces golden spec --schema)")
        if geometry:
            geo = list(spec_mod.check_geometry(self.eff) or [])
            if geo:
                hint = None
                if any(" grid " in m or " cells (" in m for m in geo):
                    hint = ("supports, loads and the envelope sit on the grid that gen_mm, fea_mm and the check levels "
                            "share: move them as named, or choose resolutions (and a check_factor of 1/2 or 1/4) that "
                            "divide their coordinates" + (" (the demo's boxes sit on a 4 mm grid: --res 4, 2 or 1)"
                                                          if self.demo_name else ""))
                raise GoldenError(EXIT_USAGE, "; ".join(geo), hint=hint)

    # ---- stage 2 ------------------------------------------------------------------------------------------------
    def stage_inputs(self) -> None:
        o, run, eff = self.opts, self.run, self.eff
        dom_spec = eff.get("domain") or {}
        self._begin(2, "inputs", f"spec {eff.get('name')} ({self.spec_label}), sha256 {sha256_bytes(self.raw_bytes)[:12]}; "
                                 f"design space from {dom_spec.get('source')}")
        if self.raw_text is not None:
            run.write_text("inputs/spec.json", self.raw_text)
        else:
            _atomic_write(run.path("inputs", "spec.json"), self.raw_bytes)
        spec_mod, domain_mod, compute, evidence = _mod("spec"), _mod("domain"), _mod("compute"), _mod("evidence")
        inputs_domain = domain_mod.build(eff, run.path("inputs"), self.base_dir) or {}
        derived = eff.setdefault("derived", {})
        if derived.get("grid") is None:
            derived["grid"] = spec_mod.derive_grid(inputs_domain.get("bbox_mm"), eff)
        self._guard_spec(spec_mod, geometry=True)
        gpu_usable = bool((self.detected.get("gpu") or {}).get("usable")) and o.device != "cpu"
        for msg in spec_mod.geometry_warnings(eff, gpu_usable=gpu_usable) or []:
            self.progress.note(str(msg), "warning")
        run.write_json("inputs/spec.effective.json", eff)
        self.plan = compute.plan(self.detected, eff, jobs=_jobs(o.jobs), device=o.device, rig=self.session,
                                 conforming=o.conforming)
        run.write_json("compute.json", {**self.detected, "plan": self.plan})
        try:
            self.project_info = self.aces.project_show(self.project) or {}
        except AcesCliError as e:
            raise GoldenError(EXIT_USAGE, f"aces project show {self.project}: {e}") from e
        self.experiment = evidence.experiment(eff, run, self.project_info, self.operator, plan=self.plan,
                                              detected=self.detected)
        run.write_experiment(self.experiment)
        grid = derived.get("grid") or {}
        self.progress.line(f"  design space {inputs_domain.get('source')}: STEP {_g(inputs_domain.get('step_bytes'))} B, "
                           f"B-rep {_g(inputs_domain.get('brep_volume_mm3'))} mm3; grid origin {grid.get('origin_mm')} "
                           f"envelope {grid.get('envelope_mm')} mm ({grid.get('derived_from')})")
        self.progress.line("  plan: " + self._plan_text())
        self._end("inputs", self._plan_text())

    def _plan_text(self) -> str:
        p = self.plan or {}
        sw, ver = p.get("sweep") or {}, p.get("verify") or {}
        rg, cf = p.get("rig") or {}, p.get("conforming") or {}
        parity = ", ".join(f"{x.get('case_id')} {x.get('device')}/{x.get('backend')}" for x in p.get("parity") or []) or "none"
        sent = [k for k in ("sweep", "check", "conforming") if rg.get(k)]
        rig_t = f"rig lane {rg.get('lane')} ({', '.join(sent) or 'nothing sent'})" if rg.get("lane") else "no rig lane"
        conf_t = (f"conforming {cf.get('mode')} on {'the rig' if cf.get('host') == 'rig' else 'this desktop'}"
                  if cf.get("host") else f"conforming {cf.get('mode') or self.opts.conforming} not run")
        return (f"sweep {sw.get('device')} x{sw.get('jobs')}, verify {ver.get('device')}, parity {parity}, "
                f"check {p.get('check_mode')}, {conf_t}, {rig_t}")

    # ---- stage 3 ------------------------------------------------------------------------------------------------
    def stage_domain(self) -> None:
        d = self.eff.get("derived") or {}
        res = f"{_g(d.get('gen_mm'))} mm" + (f" and {_g(d.get('fea_mm'))} mm" if d.get("fea_mm") != d.get("gen_mm") else "")
        self._begin(3, "domain", f"voxelise the design space at {res} (step-structures stage_mesh)")
        self.domain = _mod("domain").prepare(self.eff, self.run.path("inputs", "domain.step"), self.run.path("domain")) or {}
        dm = self.domain
        text = (f"grid {dm.get('shape')} at {_g(dm.get('h_gen_mm'))} mm, design share {_g(dm.get('design_share'))}, "
                f"{_g(dm.get('n_void_boxes'))} passive void boxes, {_g(dm.get('wall_s'), '.1f')} s")
        self.progress.line("  " + text)
        if self.session is not None:
            self.session.bind(self.run, self.eff, self.domain, self.experiment, operator=self.operator,
                              host=self.detected.get("host"), progress=self.progress)
        self._end("domain", text)

    # ---- stage 4 ------------------------------------------------------------------------------------------------
    def stage_sweep(self) -> None:
        eff, plan = self.eff, self.plan
        sw_plan = plan.get("sweep") or {}
        d = eff.get("derived") or {}
        n, seed = (eff.get("sweep") or {}).get("n"), (eff.get("sweep") or {}).get("seed")
        on_rig = sw_plan.get("device") == "rig" and self.session is not None
        where = (f"the rig (lane {self.session.lane})" if on_rig
                 else f"{sw_plan.get('device')} x{sw_plan.get('jobs')}")
        self._begin(4, "sweep", f"{n} computed designs (LHS seed {seed}) on {where}: "
                                f"SIMP -> organic skin -> voxel FEA at {_g(d.get('fea_mm'))} mm")
        sweep_mod = _mod("sweep")
        if sw_plan.get("device") == "cpu":
            self.pool = sweep_mod.CasePool(int(sw_plan.get("jobs") or 1))
        self.sweep = sw = sweep_mod.run_sweep(self.run, eff, self.domain, self.experiment, self.pool, plan, self.progress,
                                              rig=self.session if on_rig else None) or {}
        n_ok, n_all = int(sw.get("n_ok") or 0), int(sw.get("n") or 0)
        min_points = int((eff.get("surrogate") or {}).get("min_points") or 8)
        need = 3 if self.opts.allow_thin else min_points
        text = f"{n_ok} of {n_all} designs finished in {_g(sw.get('wall_s'), '.1f')} s ({_g(sw.get('wall_s_sum'), '.1f')} s of case time)"
        if n_ok < need:
            rows = self._sweep_rows()
            failing = Counter(str(r.get("design_status")) for r in rows if r.get("status") != "FINISHED" and r.get("design_status"))
            why = failing.most_common(1)[0][0] if failing else f"the surrogate needs at least {need} (min_points {min_points})"
            self.thin_message = f"only {n_ok} of {n_all} designs finished: {why}"
            text += f"; thin: {self.thin_message}"
            self.progress.note(self.thin_message, "warning")
        self.progress.line("  " + text)
        self._end("sweep", text)

    def _sweep_rows(self) -> list[dict]:
        rows = (_safe_json(self.run.path("sweep.json")).get("rows") if self.run else None) or self.sweep.get("rows") or []
        return [r for r in rows if isinstance(r, dict)]

    # ---- stage 5 ------------------------------------------------------------------------------------------------
    def stage_loop(self) -> None:
        if self.thin_message:
            self._skip(5, "loop", "not run: thin sweep")
            return
        eff = self.eff
        v, op, sg = eff.get("verify") or {}, eff.get("optimise") or {}, eff.get("surrogate") or {}
        self._begin(5, "loop", f"surrogate ({sg.get('kind')}) -> optimise ({op.get('method')}) -> verify on the real "
                               f"solver; tol {_g((v.get('tol') or 0) * 100)} %, up to {op.get('rounds')} round(s)")
        self.loop = _mod("verify").run_rounds(self.run, eff, self.domain, self.experiment, self.pool, self.plan, self.progress) or {}
        text = f"{self.loop.get('outcome')} after {len(self.loop.get('rounds') or [])} round(s)"
        self.progress.line("  " + text)
        self._end("loop", text)

    # ---- stage 6 ------------------------------------------------------------------------------------------------
    def _coarse_solve_s(self) -> float | None:
        """The coarse solve time the CPU check estimate scales: the sweep's median solve, or with --rig-sweep (the
        sweep solved on the rig) the final verify case's wall_s_solve on this desktop."""
        if (self.plan.get("sweep") or {}).get("device") == "rig":
            res = _safe_json(self.run.path(str((self.loop or {}).get("final_case")), "results.json"))
            v = res.get("wall_s_solve")
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        return self.sweep.get("median_solve_s")

    def stage_check(self) -> None:
        verify = _mod("verify")
        if self.thin_message:
            self._skip(6, "check", "not run: thin sweep")
            self.validation = verify.finalise(self.run, self.eff, None, None, [], None, outcome="thin-sweep",
                                              rig=self._rig_block()) or {}
        elif (self.loop or {}).get("outcome") == "pass":
            self._begin(6, "check", "finer-resolution independent check, device parity and the conforming check on the "
                                    "final verify case")
            compute, sess = _mod("compute"), self.session
            levels = compute.plan_check(self.detected, self.eff, coarse_solve_s=self._coarse_solve_s(),
                                        device=self.opts.device, rig=sess)
            conf_plan = compute.plan_conforming(self.detected, self.eff, mode=self.opts.conforming, rig=sess)
            final = str(self.loop.get("final_case"))
            seed = int((self.eff.get("derived") or {}).get("seed") or 0)
            if sess is not None:  # both experiments are submitted before anything waits (section 10.11)
                on_rig = [lv for lv in levels if lv.get("run") and lv.get("host") == "rig"]
                if on_rig:
                    sess.submit("check", [verify.check_case(lv, final, seed, lane=sess.lane) for lv in on_rig],
                                source_case=final)
                if conf_plan.get("host") == "rig":
                    sess.submit("conf", [verify.conforming_case(conf_plan, final, seed, lane=sess.lane)],
                                source_case=final)
            parity = verify.device_parity(self.run, self.eff, self.domain, self.experiment, self.loop, self.plan, self.progress)
            local = verify.local_conforming(self.run, self.eff, self.domain, self.experiment, self.loop, conf_plan,
                                            self.progress)
            check = verify.independent_check(self.run, self.eff, self.domain, self.experiment, self.loop, levels,
                                             self.pool, self.progress, rig=sess)
            conf = verify.conforming_block(self.run, self.eff, self.domain, self.experiment, self.loop, check, conf_plan,
                                           self.progress, rig=sess, local=local)
            rig_blk = self._rig_block()
            grade_ = verify.grade(self.loop, check, self.eff, conforming=conf, rig=rig_blk)
            self.validation = verify.finalise(self.run, self.eff, self.loop, check, parity, grade_, conforming=conf,
                                              rig=rig_blk) or {}
            text = f"grade {self.validation.get('grade')}, verdict {self.validation.get('verdict')}"
            self.progress.line("  " + text)
            self._end("check", text)
        else:  # FAIL or INFEASIBLE: no independent check, no parity; grade and validation.json still written
            self._skip(6, "check", "not run: verification failed")
            rig_blk = self._rig_block()
            grade_ = verify.grade(self.loop, None, self.eff, rig=rig_blk)
            self.validation = verify.finalise(self.run, self.eff, self.loop, None, [], grade_, rig=rig_blk) or {}
        if self.pool is not None:
            self.pool.close()
            self.pool = None

    # ---- stage 7 ------------------------------------------------------------------------------------------------
    def stage_export(self) -> None:
        v = self.validation
        label = "winner" if v.get("verdict") == "PASS" else "candidate"
        case_id = v.get("winner_case") or v.get("final_case")
        self._begin(7, "export", f"{label} files and renders, sweep gallery, charts, compute-used table")
        export, render, report, compute = _mod("export"), _mod("render"), _mod("report"), _mod("compute")
        notes = []
        if case_id and self.run.path(case_id, "part.stl").is_file():
            export.export_winner(self.run, case_id, self.eff, label=label)
            render.winner_figures(self.run, label, case_id, self.domain, self.eff)
            self.label = label
            notes.append(f"{label} {case_id}")
        else:
            notes.append("no verified part to export" + (f" ({case_id} has no part.stl)" if case_id else ""))
        items = []
        for r in self._sweep_rows():
            cid = str(r.get("case_id"))
            thumb = self.run.path(cid, "thumb.png")
            ok = r.get("status") == "FINISHED"
            items.append({"case_id": cid, "png": str(thumb) if thumb.is_file() else None, "ok": ok,
                          "label": f"{cid} · {_g(r.get('mass_kg'), '.3g')} kg" if ok else f"{cid} · {r.get('design_status') or 'failed'}"})
        highlight = None
        if self.label and items:  # the verified part closes the gallery (outlined when it is the winner)
            rec = _safe_json(self.run.path(self.label, f"{self.label}.json"))
            iso = self.run.path(self.label, f"{self.label}_iso.png")
            items.append({"case_id": case_id, "png": str(iso) if iso.is_file() else None, "ok": True,
                          "label": f"{case_id} · {self.label} · {_g((rec.get('metrics') or {}).get('mass_kg'), '.3g')} kg"})
            highlight = case_id if self.label == "winner" else None
        if items:
            render.gallery(items, self.run.path("figures", "sweep_gallery.png"), highlight=highlight)
        report.charts(self.run)
        log = compute.ComputeLog()
        log.from_run(self.run, self.detected, self.plan)
        log.write(self.run)
        text = ", ".join(notes)
        self.progress.line("  " + text)
        self._end("export", text)

    # ---- stage 8 ------------------------------------------------------------------------------------------------
    def stage_cfd(self) -> None:
        """Tier-1 CFD: with a `flow:` block in the spec, template a PyFR case on the exported faceted STEP and —
        --cfd auto with a live rig lane — queue it on the rig and pull the result back. Never blocks the structural
        result: every gap is a skipped stage or a recorded dispatch state, not a run failure."""
        flow = self.eff.get("flow")
        if not flow:
            self._skip(8, "cfd", "no flow block in the spec")
            return
        if self.opts.cfd == "off":
            self._skip(8, "cfd", "--cfd off")
            return
        label = self.label or "candidate"
        rec = _safe_json(self.run.path(label, f"{label}.json"))
        step_info, stl_info = rec.get("step") or {}, rec.get("stl") or {}
        step_path = self.run.path(step_info.get("path")) if step_info.get("written") else None
        bbox = stl_info.get("bbox_mm")    # flat [x0, y0, z0, x1, y1, z1]
        if not (step_path and step_path.is_file() and isinstance(bbox, list) and len(bbox) == 6):
            self._skip(8, "cfd", "no verified faceted STEP with a bbox to mesh (see the export stage's step record)")
            return
        self._begin(8, "cfd", f"PyFR case on the {label} part, {flow['velocity_mps']:g} m/s {flow['medium']}")
        cfd_mod, spec_mod = _mod("cfd"), _mod("spec")
        flow_model = spec_mod.Flow(**flow)
        cfd_dir = self.run.path("cfd", label)
        doc = cfd_mod.write_case(cfd_dir, name=cfd_mod.rig_experiment_id(self.run.run_id), label=label,
                                 flow=flow_model, step_path=step_path,
                                 bbox_mm=[float(x) for x in bbox], project=self.project,
                                 owner=self.operator, hypothesis=self.eff.get("hypothesis"))
        note = (f"Re {doc['reynolds']:.4g}, box {[round(x, 1) for x in doc['box_mm']]} mm, "
                f"case in {self.run.rel(cfd_dir)}")
        session = self.session
        pyfr_row = None
        if session is not None and self.opts.cfd == "auto":
            try:
                reg = session.client.call("sim_list_solvers") or {}
                row = reg.get("pyfr")
                if isinstance(row, dict) and (row.get("validation") or {}).get("status") == "pass":
                    pyfr_row = row
            except Exception as e:  # noqa: BLE001 — a registry we cannot read is a template-only stage, not a failure
                self.progress.note(f"cfd: the rig registry could not be read ({e}); templating only", "warning")
        if self.opts.cfd == "template" or session is None or pyfr_row is None:
            why = ("--cfd template" if self.opts.cfd == "template" else
                   "no rig lane" if session is None else "the rig registry has no validated pyfr solver")
            (cfd_dir / "rig").mkdir(parents=True, exist_ok=True)
            with open(cfd_dir / "rig" / "dispatch.json", "w", encoding="utf-8") as f:
                json.dump({"schema": "aces-golden-cfd-rig/1", "state": "templated",
                           "reason": f"{why}: case files written, nothing submitted. Queue it with the experiment "
                                     "yaml in this directory (aces-simlab remote call sim_submit, or simlab enqueue "
                                     "--docker on the rig).", "experiment": doc["experiment_doc"]["id"]}, f, indent=2)
            self.progress.line(f"  cfd: templated, not dispatched ({why})")
            self._end("cfd", f"{note} — templated, not dispatched ({why})")
            return
        rec2 = cfd_mod.dispatch(session.client, cfd_dir=cfd_dir, doc=doc,
                                exp_id=cfd_mod.rig_experiment_id(self.run.run_id), principal=session.principal,
                                budget_s=float(session.budget_s), progress=self.progress)
        text = f"{note} — rig: {rec2['state']}" + (f" ({rec2['reason']})" if rec2.get("reason") else "")
        self.progress.line("  " + text)
        self._end("cfd", text)

    # ---- stage 9 ------------------------------------------------------------------------------------------------
    def stage_evidence(self) -> None:
        v = self.validation
        self._begin(9, "evidence", "anchor the files, chain the bundle into the simlab ledger, golden.run into the aces ledger")
        try:
            led = _mod("evidence").record(self.run, self.eff, self.project_info, self.aces, operator=self.operator,
                                          note=self.opts.note, verdict=v.get("verdict"), outcome=v.get("outcome")) or {}
        except GoldenError as e:
            if e.code != EXIT_EVIDENCE:
                raise
            self.evidence_error = e
            self.progress.note(e.message, "error")
            self._end("evidence", e.message, state="failed")
            return
        s, a = led.get("simlab") or {}, led.get("aces") or {}
        text = (f"bundle {str(s.get('sha256') or '')[:12]}, simlab ledger entry {s.get('entry')} of {s.get('entries')}, "
                f"aces ledger entry {a.get('n')}; verify: simlab {(s.get('verify') or {}).get('ok')}, aces {(a.get('verify') or {}).get('ok')}")
        self.progress.line("  " + text)
        self._end("evidence", text)

    # ---- stage 10 -----------------------------------------------------------------------------------------------
    def stage_report(self) -> None:
        self._begin(10, "report", "report.md, report.html, report.json")
        report = _mod("report")
        self.report_paths = report.write(self.run, report.assemble(self.run)) or {}
        self._end("report", ", ".join(self.run.rel(p) for p in self.report_paths.values() if p))

    # ---- the whole run ------------------------------------------------------------------------------------------
    def execute(self) -> int:
        try:
            self.preflight()
            self.stage_compute()
            self.stage_inputs()
            self.stage_domain()
            self.stage_sweep()
            self.stage_loop()
            self.stage_check()
            self.stage_export()
            self.stage_cfd()
            self.stage_evidence()
            self.stage_report()
            code, message, hint = self.final_code()
        except GoldenError as e:
            return self.fail(e.code, e.message, hint=e.hint)
        except KeyboardInterrupt:
            return self.interrupted()
        except Exception as e:  # noqa: BLE001 (any failure of a stage is an internal error with its traceback logged)
            return self.fail(EXIT_INTERNAL, f"internal error in {self.current or 'start-up'}: {type(e).__name__}: {e}",
                             hint=None if self.run is None else f"traceback in {self.run.path('log.txt')}",
                             tb=traceback.format_exc())
        if code != EXIT_OK:
            self._say_error(message, hint)
        doc = self._close_run(status="done", code=code, message=message)
        self._output(doc, human=True)
        return code


def run(opts: Options) -> int:
    """`aces golden run`: the whole loop; returns the exit code (see the module docstring)."""
    t_start, started_utc = time.time(), utc_now()
    _pin_environment()
    fd, tmp_log = tempfile.mkstemp(prefix="aces-golden-", suffix=".log")
    os.close(fd)
    guard = StdioGuard(tmp_log, json_mode=opts.json_out)
    guard.start()
    progress = make_progress(opts.progress, guard.err, json_mode=opts.json_out, quiet=opts.quiet)
    pipe = _Pipeline(opts, guard, progress, t_start, started_utc)
    code = EXIT_INTERNAL
    try:
        try:
            code = pipe.execute()
        except KeyboardInterrupt:  # a Ctrl-C while an outcome was being written
            code = pipe.interrupted()
        except Exception as e:  # noqa: BLE001 (writing the outcome itself failed: say so, keep the traceback)
            print(traceback.format_exc(), file=sys.stderr)
            with contextlib.suppress(Exception):
                guard.err.write(f"aces golden: error: could not finish the run records: {type(e).__name__}: {e}\n"
                                f"hint: traceback in {guard.log_path}\n")
            code = EXIT_INTERNAL
    finally:
        progress.close()
        guard.stop()
        if pipe.run is None and code in (EXIT_OK, EXIT_USAGE, EXIT_UNDER_RESOLVED, EXIT_INTERRUPTED):
            with contextlib.suppress(OSError):
                Path(tmp_log).unlink()  # nothing was computed: no log to keep (internal errors keep theirs)
    return code
