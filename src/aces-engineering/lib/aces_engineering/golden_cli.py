"""aces-golden (`aces golden`): one command from a design space to a computed, verified and evidenced organic part.

  aces golden run     [--demo [root-mount|bone]] [--spec FILE] [--step FILE] [--project NAME] [--new]
                      [-n N] [--seed S] [--res MM] [--gen-res MM] [--tol FRAC] [--rounds R]
                      [--check auto|off|force] [--check-budget S] [--allow-thin] [--strict]
                      [--require-grade preview-grade|verified-desktop|rig-verified]
                      [--device auto|cpu|gpu] [--jobs N|auto] [--lane LANE|none] [--rig-sweep] [--rig-budget S]
                      [--conforming auto|rig|local|off]
                      [--json] [--progress auto|bar|lines|jsonl|none] [--quiet] [--note TEXT]
  aces golden show    [RUN] [--project NAME] [--json] [--bench-out FILE]   no RUN: every run, newest first
  aces golden verify  [RUN] [--project NAME] [--json] [--no-model] [--resolve]
  aces golden compute [--demo NAME | --spec FILE] [--device auto|cpu|gpu] [--lane LANE|none] [--json]
  aces golden spec    [--demo NAME] [--schema] [--out FILE]
  aces golden init    NAME [--step FILE] [--demo NAME] [--class CLS] [--hypothesis TEXT]
  aces golden report  RUN [--project NAME]

The part is computed, not drawn: a disc, a box or a user STEP is only the design space. Each design is a SIMP
topology optimisation smoothed into an organic implicit skin (gyroid-cored in the bone variant), solved on the real
voxel FEA; a GP surrogate fitted to the sweep picks the winner, which is solved again and, where the budget allows,
at a finer resolution (on a rig lane through simlab-mcp with --lane), and given a conforming tet10 second
discretisation (gmsh + CalculiX ccx, on the rig or here); the run is anchored in the simlab evidence ledger and the
aces ledger. Grades: preview-grade (every solve on its own), verified-desktop (the finer-resolution check agreed),
rig-verified (the check and the conforming check ran on the rig on validated images and agree). None is a sign-off.

Exit codes of `run`: 0 pass, 1 verification failed (or the grade is below --strict / --require-grade), 2 usage (a
--lane that cannot be used included), 3 thin sweep, 4 under-resolved, 5 infeasible, 6 evidence, ledger or rig
integrity failure, 7 internal error, 130 interrupted.
`show`: 0 found, 1 none, 2 usage. `verify` (13 checks): 0 all checks pass, 1 any fails, 2 run not found.
`compute`: 0, 2 (usage, or a --lane that cannot be used). `report`: 0, 2.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from .golden import (DEFAULT_DEMO, DEMOS, EXIT_FAIL, EXIT_INTERNAL, EXIT_OK, EXIT_USAGE, GRADES, OUTCOMES, VERSION,
                     GoldenError, dump_json, read_json, sha256_file, write_text)
from .golden.acescli import AcesCli, AcesCliError
from .golden.rundir import RunDir, RunNotFound, list_runs, slug, work_root

PROG = "aces golden"
BENCH_HEADER = "# ACES golden path benchmarks\n"
BENCH_COLUMNS = ("Machine", "CPU", "Devices used", "Workers", "Designs ok/n", "gen/fea/check mm", "Sweep s", "Loop s",
                 "Check s", "Total s", "Outcome", "Grade", "Run")


class UsageError(Exception):
    """An argparse refusal, raised instead of argparse's own exit so --json can still print one document."""

    def __init__(self, message: str, usage: str):
        super().__init__(message)
        self.message, self.usage = message, usage


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # noqa: D401 (argparse hook)
        raise UsageError(message, self.format_usage())


def _jobs_arg(text: str) -> str:
    if text == "auto":
        return text
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive number of workers or auto, not {text!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("--jobs must be at least 1")
    return str(n)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog=PROG,
                description="The ACES golden path: a design space in, a computed organic part out, verified on the "
                            "real voxel FEA and recorded in both ledgers. Desktop results are preview-grade.",
                epilog="Exit codes of run: 0 pass, 1 verification failed (or the grade is below --strict / "
                       "--require-grade), 2 usage (a --lane that cannot be used included), 3 thin sweep, "
                       "4 under-resolved, 5 infeasible, 6 evidence, ledger or rig integrity, 7 internal error, "
                       "130 interrupted. verify runs 13 checks. Details: aces golden <command> --help and the "
                       "aces-engineering skill.")
    p.add_argument("--version", action="version", version=f"aces-golden {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="{run,show,verify,compute,spec,init,report}")

    r = sub.add_parser("run", help="run the loop end to end (exit 0 only when verification passed and both ledgers verify)")
    r.add_argument("--demo", nargs="?", const=DEFAULT_DEMO, choices=DEMOS, metavar="NAME",
                   help=f"a built-in design space: {', '.join(DEMOS)} (default {DEFAULT_DEMO}); project golden-demo")
    r.add_argument("--spec", type=Path, help="an aces-golden-spec/1 JSON file")
    r.add_argument("--step", type=Path, help="use this STEP file as the design space (overrides the spec's domain)")
    r.add_argument("--project", help="the aces project (~/Work/NAME); without --spec its golden/spec.json is used")
    r.add_argument("--new", action="store_true", help="create the project when it does not exist")
    r.add_argument("-n", type=int, dest="n", help="designs in the sweep (sweep.n)")
    r.add_argument("--seed", type=int, help="sampler seed (sweep.seed)")
    r.add_argument("--res", type=float, help="FEA resolution in mm (resolution.fea_mm)")
    r.add_argument("--gen-res", type=float, dest="gen_res", help="generator resolution in mm (resolution.gen_mm)")
    r.add_argument("--tol", type=float, help="relative agreement predicted vs solved (verify.tol, default 0.10)")
    r.add_argument("--rounds", type=int, help="verification rounds (optimise.rounds)")
    r.add_argument("--check", choices=("auto", "off", "force"), help="the finer-resolution independent check (verify.check)")
    r.add_argument("--check-budget", type=float, dest="check_budget", help="CPU seconds the check may take (verify.check_budget_s)")
    r.add_argument("--allow-thin", action="store_true", dest="allow_thin", help="fit the surrogate on as few as 3 designs")
    r.add_argument("--strict", action="store_true", help="exit 1 unless the grade is verified-desktop or better")
    r.add_argument("--require-grade", choices=GRADES, dest="require_grade", metavar="GRADE",
                   help=f"exit 1 when the grade is below GRADE ({', '.join(GRADES)}); with --strict the higher wins")
    r.add_argument("--device", choices=("auto", "cpu", "gpu"), default="auto", help="where the solves run (default auto)")
    r.add_argument("--jobs", type=_jobs_arg, default="auto", metavar="N|auto", help="CPU workers for the sweep (default auto)")
    r.add_argument("--lane", metavar="LANE|none",
                   help="send the independent check and the conforming check to this rig lane through simlab-mcp "
                        "(exit 2 before anything is computed when it cannot be used); none forces this desktop; "
                        "default: [golden] lane in aces.toml, else this desktop only")
    r.add_argument("--rig-sweep", action="store_true", dest="rig_sweep",
                   help="also run the sweep's designs on the rig (golden-case); verification rounds stay on this "
                        "desktop; needs a lane")
    r.add_argument("--rig-budget", type=float, dest="rig_budget", default=1800.0, metavar="S",
                   help="seconds from each rig experiment's submission to its last pulled file (default 1800)")
    r.add_argument("--conforming", choices=("auto", "rig", "local", "off"), default="auto",
                   help="the conforming tet10 second discretisation (gmsh + CalculiX ccx): auto (on the rig with a "
                        "lane, this desktop as the fallback; else here when it can run), rig, local or off "
                        "(default auto)")
    r.add_argument("--cfd", choices=("auto", "template", "off"), default="auto",
                   help="when the spec has a flow block: template a PyFR case on the exported part and dispatch it "
                        "to the rig (auto, needs a lane), only write the case files (template), or skip the stage "
                        "(off) (default auto)")
    r.add_argument("--crash", choices=("auto", "template", "off"), default="auto",
                   help="when the spec has a crash block: template a radioss-crash tube-crush case and dispatch "
                        "it to the rig (auto, needs a lane), only write the case files (template), or skip the "
                        "stage (off) (default auto)")
    r.add_argument("--multibody", choices=("auto", "template", "off"), default="auto",
                   help="when the spec has a multibody block: template an exudyn-multibody case (iftomm benchmark "
                        "or a user model) and dispatch it to the rig (auto, needs a lane), only write the case "
                        "files (template), or skip the stage (off) (default auto)")
    r.add_argument("--json", action="store_true", dest="json_out", help="print exactly one JSON document (result.json) on stdout")
    r.add_argument("--progress", choices=("auto", "bar", "lines", "jsonl", "none"), default="auto")
    r.add_argument("--quiet", action="store_true", help="no progress output (--progress none)")
    r.add_argument("--note", help="a note carried into the evidence bundle")

    s = sub.add_parser("show", help="list runs (newest first), or show one")
    s.add_argument("run", nargs="?", metavar="RUN", help="run id, unique prefix, latest, or a path")
    s.add_argument("--project")
    s.add_argument("--json", action="store_true")
    s.add_argument("--bench-out", dest="bench_out", type=Path, metavar="FILE",
                   help="append this run's wall-time row to a benchmarks file (e.g. docs/BENCHMARKS.md)")

    v = sub.add_parser("verify", help="audit a run (13 checks): files, bundle, both ledgers, sweep, surrogate, verdict, "
                                      "report, compute, rig, conforming, manifest, resolve; never contacts the rig")
    v.add_argument("run", nargs="?", default="latest", metavar="RUN")
    v.add_argument("--project")
    v.add_argument("--json", action="store_true")
    v.add_argument("--no-model", action="store_true", dest="no_model", help="do not load the surrogate pickles")
    v.add_argument("--resolve", action="store_true", help="re-solve the winner on the CPU and compare")

    c = sub.add_parser("compute", help="what would compute where: detection and plan; computes nothing")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--demo", nargs="?", const=DEFAULT_DEMO, choices=DEMOS, metavar="NAME")
    g.add_argument("--spec", type=Path)
    c.add_argument("--device", choices=("auto", "cpu", "gpu"), default="auto")
    c.add_argument("--lane", metavar="LANE|none",
                   help="probe this rig lane (simlab-mcp) and show what a run would send; exit 2 when it cannot be "
                        "used (default: [golden] lane in aces.toml, whose refusal is a warning)")
    c.add_argument("--json", action="store_true")

    sp = sub.add_parser("spec", help="print a demo spec or the JSON schema of aces-golden-spec/1")
    sp.add_argument("--demo", nargs="?", const=DEFAULT_DEMO, choices=DEMOS, metavar="NAME")
    sp.add_argument("--schema", action="store_true")
    sp.add_argument("--out", type=Path, metavar="FILE")

    i = sub.add_parser("init", help="create a project and its golden/spec.json (from a STEP design space or a demo)")
    i.add_argument("name", metavar="NAME")
    i.add_argument("--step", type=Path, help="copy this STEP to NAME/cad/ and write a spec template for it")
    i.add_argument("--demo", nargs="?", const=DEFAULT_DEMO, choices=DEMOS, metavar="NAME")
    i.add_argument("--class", dest="cls", help="project class when the project is created (aces policy show)")
    i.add_argument("--hypothesis", help="project hypothesis when the project is created")

    rp = sub.add_parser("report", help="rebuild report.* and manifest.sha256 of a run from its records")
    rp.add_argument("run", metavar="RUN")
    rp.add_argument("--project")
    return p


# ---- helpers ------------------------------------------------------------------------------------------------------
def _err(message: str, hint: str | None = None) -> None:
    sys.stderr.write(f"aces golden: error: {message}\n" + (f"hint: {hint}\n" if hint else ""))
    sys.stderr.flush()


def _print_json(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")
    sys.stdout.flush()


@contextlib.contextmanager
def _guarded(json_mode: bool):
    """A StdioGuard on a temporary log for commands that import the engineering stack: its noise stays off the
    terminal. The log is removed on success, and on failure too when nothing was written to it; otherwise it is
    kept and its path printed."""
    from .golden.progress import StdioGuard

    fd, tmp = tempfile.mkstemp(prefix="aces-golden-", suffix=".log")
    os.close(fd)
    guard = StdioGuard(tmp, json_mode=json_mode)
    guard.start()
    ok = False
    try:
        yield guard
        ok = True
    finally:
        guard.stop()
        log = Path(tmp)
        if ok or not log.is_file() or log.stat().st_size == 0:
            with contextlib.suppress(OSError):
                log.unlink()
        else:
            sys.stderr.write(f"aces golden: what the libraries printed is in {log}\n")


def _usage_doc(message: str) -> dict:
    from .golden.pipeline import result_document

    return result_document(None, outcome=OUTCOMES[EXIT_USAGE], exit_code=EXIT_USAGE, message=message)


def _open_run(ref: str | None, project: str | None) -> RunDir:
    return RunDir.open(ref, project=project)


# ---- run ----------------------------------------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace, argv: list[str]) -> int:
    from .golden.pipeline import Options, run

    opts = Options(demo=args.demo, spec=args.spec, step=args.step, project=args.project, new=args.new, n=args.n,
                   seed=args.seed, res=args.res, gen_res=args.gen_res, tol=args.tol, rounds=args.rounds,
                   check=args.check, check_budget=args.check_budget, allow_thin=args.allow_thin, strict=args.strict,
                   device=args.device, jobs=args.jobs, lane=args.lane, rig_sweep=args.rig_sweep,
                   rig_budget=args.rig_budget, conforming=args.conforming, cfd=args.cfd, crash=args.crash,
                   multibody=args.multibody, require_grade=args.require_grade,
                   json_out=args.json_out, progress=args.progress, quiet=args.quiet, note=args.note, argv=list(argv))
    return run(opts)


# ---- show ---------------------------------------------------------------------------------------------------------
def _summary(run_json: Path) -> dict:
    meta = _safe(run_json)
    return {"run_id": meta.get("run_id") or run_json.parent.name, "project": meta.get("project") or run_json.parent.parent.parent.name,
            "run_dir": str(run_json.parent), "created_utc": meta.get("created_utc"), "finished_utc": meta.get("finished_utc"),
            "status": meta.get("status"), "outcome": meta.get("outcome"), "verdict": meta.get("verdict"),
            "grade": meta.get("grade"), "exit_code": meta.get("exit_code"),
            "wall_s": (meta.get("timings") or {}).get("total_s")}


def _safe(p: Path) -> dict:
    try:
        data = read_json(p, {})
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def cmd_show(args: argparse.Namespace) -> int:
    if args.bench_out is not None:
        try:
            run = _open_run(args.run, args.project)
        except RunNotFound as e:
            _err(e.message, e.hint)
            return 1
        return _bench_out(run, args.bench_out)
    if not args.run:
        runs = list_runs(args.project)
        rows = [_summary(p) for p in runs]
        if args.json:
            _print_json(rows)
            return 0 if rows else 1
        if not rows:
            where = work_root() / args.project / "golden" if args.project else work_root()
            print(f"no golden runs under {where} (aces golden run --demo)")
            return 1
        print(f"{'RUN':<26} {'PROJECT':<18} {'STATUS':<11} {'OUTCOME':<20} {'VERDICT':<10} {'GRADE':<17} WALL")
        for r in rows:
            print(f"{r['run_id']:<26} {str(r['project']):<18} {str(r['status']):<11} {str(r['outcome'] or '-'):<20} "
                  f"{str(r['verdict'] or '-'):<10} {str(r['grade'] or '-'):<17} {_fmt(r['wall_s'])} s")
        return 0
    try:
        run = _open_run(args.run, args.project)
    except GoldenError as e:  # RunNotFound (1), or an ambiguous prefix (2)
        if args.json:  # --json still prints one document
            _print_json({"ok": False, "run_dir": None, "error": e.message})
        _err(e.message, e.hint)
        return 1 if isinstance(e, RunNotFound) else e.code
    meta, result = run.meta(), _safe(run.path("result.json"))
    if args.json:
        _print_json(result or meta)
        return 0
    print(f"run      {run.run_id} (project {run.project})")
    print(f"dir      {run.root}")
    print(f"status   {meta.get('status')} · outcome {meta.get('outcome')} · exit {meta.get('exit_code')}")
    line = result.get("verdict_line") or (f"{meta.get('verdict')} · {meta.get('grade')}" if meta.get("verdict") else None)
    if line:
        print(f"verdict  {line}")
    if meta.get("message"):
        print(f"message  {meta.get('message')}")
    rep = (result.get("report") or {}).get("md")
    if rep:
        print(f"report   {rep}")
    w = result.get("winner") or {}
    if w.get("stl"):
        print(f"{w.get('label') or 'winner':<8} {w['stl']}")
    stages = meta.get("stages") or []
    if stages:
        print("stages   " + " · ".join(f"{s.get('name')} {s.get('status')}"
                                       + (f" {s['wall_s']:.1f} s" if isinstance(s.get("wall_s"), (int, float)) else "")
                                       for s in stages))
    return 0


_RIG_GPU = re.compile(r"rig gpu (\d+) \(([^()]*)\)\Z")
# a row that ran on several devices joins them with ", " (`rig gpu 0 (X), rig gpu 2 (X), cpu/numpy`) or ";"
_DEVICE_SPLIT = re.compile(r";|,\s*(?=rig gpu \d|rig cpu|cpu\b|gpu\b)")


def devices_used(rows: list) -> str:
    """The devices of the compute-used rows that ran, e.g. `cpu/numpy x10, <GPU name> (cupy, warp)`: CPU entries
    without the ones another entry already names (`cpu` inside `cpu/numpy x10`), GPUs with the backends that ran
    on them. Rig tokens aggregate: `rig gpu <n> (<name>)` to `rig: <name> x<k> (cupy[, mg])` (k distinct GPUs; numpy
    when the rig solve ran on its CPU), `rig cpu ...` to `rig cpu (ccx x16)`. A device string may name several devices
    joined with ", " or ";" (a sweep row: `rig gpu 0 (X), rig gpu 2 (X), rig gpu 3 (X), cpu/numpy`); each is one token.
    A rig GPU whose name a row did not record (`rig gpu 3 (None)`) counts under the name another row gives that index,
    else as `rig gpu <n> (name not recorded)`."""
    ran = [r for r in rows if isinstance(r, dict) and (r.get("count") or 0) > 0 and r.get("grade") != "not used"]
    cpu: list[str] = []
    gpus: dict[str, list[str]] = {}
    rig_gpus: dict[str, dict] = {}
    unnamed: set[str] = set()
    rig_cpu = False
    for r in ran:
        solver = str(r.get("solver") or "").lower()
        for tok in _DEVICE_SPLIT.split(str(r.get("device") or "")):
            tok = tok.strip()
            if not tok or tok in ("-", "rig", "gpu"):
                continue
            if tok.startswith("rig cpu"):
                rig_cpu = True
                continue
            if tok.startswith("rig"):
                m = _RIG_GPU.match(tok)
                if m is None:  # an experiment row (`rig gpu 0,2,3`): the case rows name the GPUs
                    continue
                name = m.group(2).strip()
                if name in ("", "None"):
                    unnamed.add(m.group(1))
                    continue
                ent = rig_gpus.setdefault(name, {"idx": set(), "backends": []})
                ent["idx"].add(m.group(1))
                for b in ("numpy" if name.lower().startswith("cpu") else "cupy",
                          "mg" if ", mg fp64" in solver else None):
                    if b and b not in ent["backends"]:
                        ent["backends"].append(b)
                continue
            if tok.startswith("cpu"):
                if tok not in cpu:
                    cpu.append(tok)
                continue
            backend = "warp" if "warp" in solver else "cupy"
            if backend not in gpus.setdefault(tok, []):
                gpus[tok].append(backend)
    cpu = [c for c in cpu if not any(o != c and o.startswith(c) for o in cpu)]
    out = cpu + [f"{name} ({', '.join(b)})" for name, b in gpus.items()]
    out += [f"rig: {name} x{len(e['idx'])} ({', '.join(e['backends'])})" for name, e in rig_gpus.items()]
    unnamed -= {i for e in rig_gpus.values() for i in e["idx"]}
    if unnamed:
        out.append(f"rig gpu {', '.join(sorted(unnamed))} (name not recorded)")
    if rig_cpu:
        out.append("rig cpu (ccx x16)")
    return ", ".join(out) or "-"


def _bench_out(run: RunDir, out: Path) -> int:
    """Append this run's row (every value read from run.json, result.json, compute.json and sweep.json)."""
    meta, res = run.meta(), _safe(run.path("result.json"))
    if meta.get("status") != "done" or not res:
        _err(f"run {run.run_id} has status {meta.get('status')}: only runs that reached the report are benchmarked; nothing appended")
        return EXIT_USAGE
    comp, sw, eff = _safe(run.path("compute.json")), _safe(run.path("sweep.json")), _safe(run.path("inputs", "spec.effective.json"))
    host = comp.get("host") or "-"
    cpu = comp.get("cpu") or {}
    resolution = meta.get("resolution") or {}
    check = next((lv.get("h_mm") for lv in resolution.get("check_levels") or [] if lv.get("level") == 1), None)
    t = meta.get("timings") or {}

    def mm(v: Any) -> str:
        return "-" if v is None else f"{v:g}"

    cells = [host, f"{cpu.get('model') or '-'} ({cpu.get('logical') or '-'} logical)", devices_used(res.get("compute_used") or []),
             ((comp.get("plan") or {}).get("sweep") or {}).get("jobs"),
             f"{sw.get('n_ok')}/{sw.get('n')}" if sw else "-",
             f"{mm(resolution.get('gen_mm'))}/{mm(resolution.get('fea_mm'))}/{mm(check)}",
             t.get("sweep_s"), t.get("loop_s"), t.get("check_s"), t.get("total_s"),
             res.get("outcome") or meta.get("outcome"), res.get("grade") or meta.get("grade"), run.run_id]
    name = eff.get("name") or meta.get("demo") or "spec"
    block = (f"\n## {meta.get('finished_utc')}  aces golden {name} on {host}\n\n"
             f"| {' | '.join(BENCH_COLUMNS)} |\n|{'---|' * len(BENCH_COLUMNS)}\n"
             f"| {' | '.join(_fmt(c).replace('|', '/') for c in cells)} |\n")
    out = Path(out)
    text = out.read_text(encoding="utf-8") if out.is_file() else BENCH_HEADER
    if text and not text.endswith("\n"):
        text += "\n"
    write_text(out, text + block)
    print(f"appended {run.run_id} to {out}")
    return 0


# ---- verify -------------------------------------------------------------------------------------------------------
def cmd_verify(args: argparse.Namespace) -> int:
    try:
        run = _open_run(args.run, args.project)
    except GoldenError as e:
        if args.json:
            _print_json({"ok": False, "run_dir": None, "checks": [], "error": e.message})
        _err(e.message, e.hint)
        return 2
    try:
        with _guarded(args.json) as guard:
            from .golden import evidence

            res = evidence.audit(run, AcesCli(), use_model=not args.no_model, resolve=args.resolve)
            out = guard.out
            if args.json:
                out.write(json.dumps({"run_dir": str(run.root), **res}, indent=1, ensure_ascii=False) + "\n")
            else:
                for c in res.get("checks") or []:
                    flag = "PASS" if c.get("ok") else ("INFO" if c.get("ok") is None else "FAIL")
                    out.write(f"{flag}  {c.get('name'):<34} {c.get('detail') or ''}\n")
                out.write(f"aces golden verify {run.run_id}: {'all checks pass' if res.get('ok') else 'FAILED'}\n")
            out.flush()
    except Exception as e:  # noqa: BLE001 (an audit that cannot run is a failed audit, with the reason)
        _err(f"the audit of {run.run_id} could not run: {type(e).__name__}: {e}")
        return EXIT_FAIL
    return EXIT_OK if res.get("ok") else EXIT_FAIL


# ---- compute ------------------------------------------------------------------------------------------------------
def cmd_compute(args: argparse.Namespace) -> int:
    """Detection and plan; with a lane (--lane, else [golden] lane in aces.toml) the live probe of section 10.5 and
    its rig: lines. Exit 2 when a --lane cannot be used (a config lane that cannot is a warning)."""
    code, refusal = EXIT_OK, None
    try:
        with _guarded(args.json) as guard:
            from .golden import compute, demo, rig, spec as spec_mod

            if args.spec:
                raw, _, base = spec_mod.load(Path(args.spec))
            else:
                raw, base = demo.spec(args.demo or DEFAULT_DEMO), Path(demo.__file__).resolve().parent
            lane, source = rig.choose_lane(args.lane)
            rec, view, notes = None, None, []
            if lane is not None:
                check_mode = str((raw.get("verify") or {}).get("check") or "auto")
                rec, client = rig.probe(lane, source, check_mode=check_mode, conforming_mode="auto",
                                        rig_sweep=False, warn=notes.append)
                if rec.get("ok"):
                    view = {"lane": lane, "lane_source": source, "url": rec.get("url"),
                            "budget_s": 1800.0, "scope": rig.needs_for(check_mode, "auto", False)}
                elif source == "cli":
                    code, refusal = EXIT_USAGE, str(rec.get("refusal"))
                else:
                    notes.append(f"rig lane {lane} (from aces.toml) unavailable: {rig.refusal_reason(rec)}; "
                                 "computing on this desktop")
            detected = compute.detect(device=args.device, probe=rec, rig_scope=(view or {}).get("scope"))
            gpu_ok = bool((detected.get("gpu") or {}).get("usable"))
            if args.device == "gpu" and not gpu_ok:
                raise GoldenError(EXIT_USAGE, f"no usable CUDA GPU here ({(detected.get('gpu') or {}).get('reason')})")
            eff = spec_mod.resolve(raw, base_dir=base, overrides={}, gpu_usable=gpu_ok)
            plan = None
            if (eff.get("derived") or {}).get("grid") is not None:
                plan = compute.plan(detected, eff, jobs="auto", device=args.device, rig=view)
            if args.json:
                guard.out.write(json.dumps({**detected, "plan": plan, "spec": eff.get("name")}, indent=1,
                                           ensure_ascii=False, default=str) + "\n")
            else:
                lines = list(compute.summary_lines(detected, plan) or [])
                if plan is None:
                    lines.append("plan: decided at run time (the grid is derived from the STEP)")
                if rec is not None:
                    lines += rig.probe_lines(rec, (plan or {}).get("rig"))
                lines += [f"warning: {n}" for n in notes]
                guard.out.write("\n".join(lines) + "\n")
            guard.out.flush()
    except GoldenError as e:
        _err(e.message, e.hint)
        return e.code
    except ValueError as e:  # SpecError
        _err(str(e))
        return EXIT_USAGE
    if refusal:
        _err(refusal)
    return code


# ---- spec ---------------------------------------------------------------------------------------------------------
def cmd_spec(args: argparse.Namespace) -> int:
    if args.schema:
        from .golden import spec as spec_mod

        obj = spec_mod.json_schema()
    else:
        from .golden import demo

        obj = demo.spec(args.demo or DEFAULT_DEMO)
    text = json.dumps(obj, indent=1, ensure_ascii=False) + "\n"
    if args.out:
        write_text(args.out, text)
        sys.stderr.write(f"wrote {args.out}\n")
    else:
        sys.stdout.write(text)
    return EXIT_OK


# ---- init ---------------------------------------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    from .golden import demo

    name = args.name
    if slug(name) != name or name in (".", ".."):
        _err(f"project name '{name}' must be letters, digits, '.', '_' or '-'")
        return EXIT_USAGE
    spec_path = work_root() / name / "golden" / "spec.json"
    if spec_path.exists():
        _err(f"{spec_path} exists: edit it, or remove it to start again")
        return EXIT_USAGE
    step = Path(args.step).expanduser().resolve() if args.step else None
    if step is not None and not step.is_file():
        _err(f"STEP file not found: {step}")
        return EXIT_USAGE
    aces = AcesCli()
    if not aces.available():
        _err(f"aces (aces-base >= 0.1.0-3) is not runnable here: {aces.problem}", "install aces-base, or set ACES_CMD")
        return EXIT_USAGE
    try:
        if aces.project_show(name) is None:
            hyp = args.hypothesis or (None if step else demo.spec(args.demo or DEFAULT_DEMO).get("hypothesis"))
            aces.project_new(name, cls=args.cls, hypothesis=hyp)
            print(f"created project {name} ({work_root() / name / 'aces.yaml'})")
        elif args.cls or args.hypothesis:
            print(f"project {name} exists: --class and --hypothesis are left as they are (aces project classify changes the class)")
        if step is not None:
            dst = work_root() / name / "cad" / step.name
            if dst.exists() and sha256_file(dst) != sha256_file(step):
                _err(f"{dst} exists with other content: rename the STEP or remove the old copy")
                return EXIT_USAGE
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(step, dst)
            aces.add_cad_source(name, f"cad/{step.name}")
            spec = demo.template(f"../cad/{step.name}")
        else:
            spec = demo.spec(args.demo or DEFAULT_DEMO)
    except AcesCliError as e:
        _err(f"aces: {e}")
        return EXIT_USAGE
    dump_json(spec_path, spec)
    print(f"wrote {spec_path}")
    if step is not None:
        print(f"the design space is {step.name} (cad/{step.name}); fill in supports, loads, keep_in and the material "
              f"(with its source), then: aces golden run --project {name}")
    else:
        print(f"next: aces golden run --project {name}")
    return EXIT_OK


# ---- report -------------------------------------------------------------------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    try:
        run = _open_run(args.run, args.project)
    except GoldenError as e:
        _err(e.message, e.hint)
        return EXIT_USAGE
    try:
        with _guarded(False) as guard:
            from .golden import report

            paths = report.write(run, report.assemble(run)) or {}
            run.hash_tree("manifest.sha256", exclude=["log.txt"])
            for k, p in paths.items():
                guard.out.write(f"{k:<5} {p}\n")
            guard.out.flush()
    except Exception as e:  # noqa: BLE001
        _err(f"the report of {run.run_id} could not be rebuilt: {type(e).__name__}: {e}")
        return EXIT_INTERNAL
    return EXIT_OK


# ---- main ---------------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):  # verdict lines carry non-ASCII (the middle dot); UTF-8 everywhere
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    if not argv:
        parser.print_help()
        return EXIT_OK
    try:
        args = parser.parse_args(argv)
    except UsageError as e:
        sys.stderr.write(e.usage)
        _err(e.message)
        if "--json" in argv:  # --json promises one document on stdout, refusals included
            _print_json(_usage_doc(e.message))
        return EXIT_USAGE
    except SystemExit as e:  # --help and --version
        return int(e.code or 0) if not isinstance(e.code, str) else EXIT_USAGE
    if not args.cmd:
        parser.print_help()
        return EXIT_OK
    try:
        if args.cmd == "run":
            return cmd_run(args, argv)
        if args.cmd == "show":
            return cmd_show(args)
        if args.cmd == "verify":
            return cmd_verify(args)
        if args.cmd == "compute":
            return cmd_compute(args)
        if args.cmd == "spec":
            return cmd_spec(args)
        if args.cmd == "init":
            return cmd_init(args)
        if args.cmd == "report":
            return cmd_report(args)
    except KeyboardInterrupt:
        return 130
    except GoldenError as e:
        _err(e.message, e.hint)
        return e.code
    except Exception as e:  # noqa: BLE001
        if os.environ.get("ACES_GOLDEN_DEBUG") == "1":
            traceback.print_exc()
        _err(f"internal error: {type(e).__name__}: {e}", "ACES_GOLDEN_DEBUG=1 prints the traceback")
        return EXIT_INTERNAL
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
