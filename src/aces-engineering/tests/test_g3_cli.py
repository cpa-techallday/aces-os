"""aces-golden's command line and pipeline with stubbed stage modules (the generator's and the verifier's are replaced
by minimal fakes that write records in the pinned shapes), each run in a child process so the fd-level stdio guard
works on real file descriptors:

  a --lane that cannot be used (no fabric manifest here) and the rig usage refusals exit 2 with no run directory and
  still one JSON document; --json stays one document while the stages print, write to fds 1 and 2 and import (and
  initialise) Warp; the exit-code mapping for every outcome and its precedence (--strict, --require-grade, a rig
  integrity error); the run records of an error after the run directory exists; show, --bench-out, verify, report and
  compute --lane; help; Ctrl-C (POSIX).

The `aces` used is the real one (src/aces-base from this repository, else an installed aces) in temporary state. Every
spec, metric and record the stubs write is a labelled test input; nothing here is a result of the golden path.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
LIB = TESTS.parent / "lib"
ACES_BASE_SRC = TESTS.parents[1] / "aces-base"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

STUBBED = ("compute", "spec", "demo", "domain", "sweep", "verify", "evidence", "export", "render", "report")


def aces_command() -> tuple[list[str] | None, str | None]:
    if (ACES_BASE_SRC / "aces" / "cli.py").is_file():
        return [sys.executable, "-m", "aces.cli"], str(ACES_BASE_SRC)
    found = shutil.which("aces")
    return ([found], None) if found else (None, None)


# ---- the stubs (installed in the child before golden_cli runs) -----------------------------------------------------
def install_stubs(sc: dict) -> None:
    """Replace the stage modules by fakes driven by the scenario `sc` (test inputs)."""
    import aces_engineering.golden as g
    import aces_engineering.golden.compute as real_compute  # its manifest reader serves the real lane probe
    from aces_engineering.golden import EXIT_EVIDENCE, GoldenError, sha256_file

    verdict = sc.get("verdict", "PASS")

    def mod(name: str, **attrs) -> types.ModuleType:
        m = types.ModuleType(f"aces_engineering.golden.{name}")
        m.__file__ = __file__
        m.__dict__.update(attrs)
        sys.modules[m.__name__] = m
        setattr(g, name, m)
        return m

    def noise(where: str) -> None:
        if sc.get("raise_in") == where:
            raise RuntimeError(f"stub failure in {where} (test input)")
        if sc.get("interrupt_in") == where:
            raise KeyboardInterrupt  # what Ctrl-C raises in the main thread
        if not sc.get("noise"):
            return
        print(f"stub stdout noise from {where}")
        print(f"stub stderr noise from {where}", file=sys.stderr)
        os.write(1, f"raw fd1 noise from {where}\n".encode())
        os.write(2, f"raw fd2 noise from {where}\n".encode())
        try:
            import warp  # the Warp banner prints on init

            warp.init()
        except Exception:
            pass

    # spec and demo
    class SpecError(ValueError):
        pass

    def demo_spec(name: str = "root-mount") -> dict:
        return {"schema": "aces-golden-spec/1", "name": f"stub-{name}", "hypothesis": "stub hypothesis (test input)",
                "domain": {"source": "build123d", "build123d": {"ref": "stub:domain"}},
                "sweep": {"n": 10, "seed": 7}, "surrogate": {"kind": "gp", "min_points": 8},
                "verify": {"tol": 0.1}, "optimise": {"method": "auto", "rounds": 3}}

    def load(path, *, step=None):
        data = Path(path).read_bytes()
        raw = json.loads(data.decode("utf-8"))
        return raw, data, Path(path).resolve().parent

    def resolve(raw, *, base_dir, overrides, gpu_usable):
        eff = copy.deepcopy(raw)
        for k, v in (overrides or {}).items():
            node = eff
            parts = k.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = v
        grid = None if sc.get("derived_grid") else {"origin_mm": [0.0, 0.0, 0.0], "envelope_mm": [10.0, 10.0, 10.0],
                                                      "derived_from": "spec"}
        eff["derived"] = {"gen_mm": 2.0, "fea_mm": 2.0, "field_h_mm": 1.0, "check_factor": 0.5,
                          "check_levels": [{"level": 1, "h_mm": 1.0, "gpu_only": False}], "grid": grid,
                          "domain_ref": "stub:domain", "base_dir": str(base_dir), "overrides": dict(overrides or {}),
                          "seed": eff["sweep"]["seed"], "n": eff["sweep"]["n"], "tol": 0.1}
        return eff

    mod("demo", spec=demo_spec, template=lambda ref: dict(demo_spec(), domain={"source": "step", "step_file": ref}))
    mod("spec", SpecError=SpecError, load=load, resolve=resolve, guard=lambda eff: list(sc.get("guard") or []),
        check_geometry=lambda eff: list(sc.get("geometry") or []),
        geometry_warnings=lambda eff, *, gpu_usable: list(sc.get("warnings") or []),
        derive_grid=lambda bbox, eff: {"origin_mm": [0.0, 0.0, 0.0], "envelope_mm": [10.0, 10.0, 10.0], "derived_from": "step-bbox"},
        json_schema=lambda: {"title": "stub schema (test input)"})

    # compute
    class ComputeLog:
        def from_run(self, run, detected, plan):
            return self

        def rows(self) -> list:
            return [{"step": "kernel", "computation": "element and patch tests", "solver": "self_test", "device": "cpu; gpu",
                     "count": 2, "wall_s": 0.01, "grade": "-", "note": "stub row (test input)"},
                    {"step": "solve", "computation": "real FEA x n", "solver": "stage_solve", "device": "cpu/numpy x1",
                     "count": 10, "wall_s": 0.01, "grade": "preview-grade", "note": "stub row (test input)"},
                    {"step": "parity", "computation": "Warp re-solve", "solver": "WarpApplyK in VoxelFEA.solve",
                     "device": "Test GPU", "count": 1, "wall_s": 0.01, "grade": "max rel diff 1e-12", "note": "stub row (test input)"},
                    {"step": "fno", "computation": "field surrogate", "solver": "fno", "device": "-", "count": 0,
                     "wall_s": None, "grade": "not used", "note": "not used: stub row (test input)"}]

        def markdown(self) -> str:
            return ""

        def write(self, run):
            return run.write_json("compute_used.json", {"schema": "aces-golden-compute-used/1", "rows": self.rows()})

    def detect(*, device="auto", probe=None, rig_scope=None):
        noise("compute")
        return {"schema": "aces-golden-compute/1", "host": "test-host", "cpu": {"logical": 2, "usable": 2, "model": "test cpu"},
                "kernel_selftest": sc.get("kernel_selftest") or {"passed": True, "wall_s": 0.01, "source": "stub (test input)"},
                "gpu": {"usable": False, "reason": "test: no GPU"}, "warp": {"usable": False}, "rig": {"lanes": []}}

    def plan_conforming(detected, eff, *, mode="auto", rig=None):
        return {"mode": mode, "host": None, "fallback": None, "h_mm": 1.0, "threads": None, "max_dof": None,
                "timeout_s": None, "reason": "stub: not run (test input)"}

    mod("compute", detect=detect, ComputeLog=ComputeLog, CONFORMING_MODES=("auto", "rig", "local", "off"),
        conforming_available=lambda: dict(sc.get("conforming_available") or {
            "usable": False, "reason": "stub: gmsh and ccx are not here (test input)"}),
        _read_manifest=real_compute._read_manifest,
        plan=lambda detected, eff, *, jobs="auto", device="auto", rig=None, conforming="auto": {
            "sweep": {"device": "cpu", "jobs": 1, "reason": "test"}, "verify": {"device": "cpu"}, "check_mode": "auto",
            "check_budget_s": 60.0, "parity": [], "rig": {"submit": False, "reason": "test"},
            "conforming": plan_conforming(detected, eff, mode=conforming)},
        plan_check=lambda detected, eff, *, coarse_solve_s, device="auto", rig=None: [
            {"level": 1, "h_mm": 1.0, "device": "cpu", "run": True, "gating": True, "est_s": 0.1, "budget_s": 60.0,
             "reason": "test", "host": "desktop"}],
        plan_conforming=plan_conforming,
        summary_lines=lambda detected, plan: ["cpu: test cpu (test input)", "gpu: none (test input)"])

    # domain and evidence
    def build(eff, inputs_dir, base_dir):
        noise("inputs")
        Path(inputs_dir).mkdir(parents=True, exist_ok=True)
        (Path(inputs_dir) / "domain.step").write_bytes(b"ISO-10303-21; stub design space (test input)\n")
        rec = {"schema": "aces-golden-inputs/1", "source": "build123d", "step_bytes": 46, "brep_volume_mm3": None,
               "bbox_mm": [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]}
        (Path(inputs_dir) / "domain.json").write_text(json.dumps(rec), encoding="utf-8")
        return rec

    def prepare(eff, step_path, out_dir):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        rec = {"schema": "aces-golden-domain/1", "shape": [5, 5, 5], "h_gen_mm": 2.0, "design_share": 1.0,
               "n_void_boxes": 0, "wall_s": 0.0}
        (Path(out_dir) / "domain.json").write_text(json.dumps(rec), encoding="utf-8")
        return rec

    mod("domain", build=build, prepare=prepare)

    class Exp:
        def __init__(self, run, operator):
            self.run, self.operator = run, operator

        def model_dump(self, mode="python"):
            return {"id": self.run.exp_id, "project": self.run.project, "owner": self.operator, "physics": "structures",
                    "solver": "step-structures", "description": "stub experiment (test input)"}

    def record(run, eff, project, aces, *, operator, note, verdict, outcome):
        noise("evidence")
        if sc.get("evidence_fail"):
            raise GoldenError(EXIT_EVIDENCE, "stub: the aces ledger record failed (test input)",
                              hint="replay with: aces ledger record golden.run --json <payload>")
        payload = run.write_json("evidence/aces-ledger-payload.json", {"project": run.project, "run_id": run.run_id,
                                                                       "verdict": verdict, "outcome": outcome})
        rec = aces.ledger_record("golden.run", payload)
        av = aces.ledger_verify()
        led = {"schema": "aces-golden-ledgers/1", "simlab": {"sha256": "0" * 64, "entry": 1, "entries": 1, "verify": {"ok": True}},
               "aces": {"n": rec["n"], "hash": rec["hash"], "verify": {"ok": av["ok"], "message": av["message"]}}, "project": None}
        run.write_json("evidence/ledgers.json", led)
        return led

    mod("evidence", experiment=lambda eff, run, project, operator, *, plan, detected: Exp(run, operator), record=record,
        audit=lambda run, aces, *, use_model=True, resolve=False: {
            "ok": not sc.get("audit_fail"), "checks": [{"name": "stub", "ok": not sc.get("audit_fail"), "detail": "stub audit (test input)"}]})

    # sweep
    class CasePool:
        def __init__(self, jobs):
            self.jobs = jobs

        def cancel(self):
            pass

        def close(self):
            pass

    def run_sweep(run, eff, domain, experiment, pool, plan, progress, *, rig=None):
        noise("sweep")
        n = int(eff["sweep"]["n"])
        n_ok = int(sc.get("n_ok", n))
        if sc.get("sleep_in_sweep"):
            time.sleep(float(sc["sleep_in_sweep"]))
        bar, rows = progress.designs(n, "sweep"), []
        for i in range(n):
            cid, ok = f"d{i:03d}", i < n_ok
            run.register_case({"case_id": cid, "role": "sweep", "volfrac": round(0.1 + 0.01 * i, 6), "seed": 7})
            row = {"case_id": cid, "role": "sweep", "status": "FINISHED" if ok else "FAILED",
                   "design_status": "ok" if ok else "detached-load", "params": {"volfrac": round(0.1 + 0.01 * i, 6)},
                   "metrics": {"mass_kg": 0.1 + 0.001 * i} if ok else None, "wall_s": 0.01, "device": "cpu", "error": None}
            bar.done(row)
            rows.append({"case_id": cid, "status": row["status"], "design_status": row["design_status"],
                         "volfrac": row["params"]["volfrac"], "mass_kg": (row["metrics"] or {}).get("mass_kg")})
        bar.close()
        run.write_json("sweep.json", {"schema": "aces-golden-sweep/1", "n": n, "n_ok": n_ok, "rows": rows})
        return {"rows": rows, "n": n, "n_ok": n_ok, "csv_sha256": None, "json_sha256": None, "wall_s": 0.01,
                "wall_s_sum": 0.01 * n, "median_solve_s": 0.01}

    mod("sweep", CasePool=CasePool, run_sweep=run_sweep)

    # verify
    def run_rounds(run, eff, domain, experiment, pool, plan, progress):
        noise("loop")
        passed = verdict == "PASS" or sc.get("parity_fail")
        run.register_case({"case_id": "v001", "role": "verify", "round": 1, "volfrac": 0.2, "seed": 7})
        run.path("v001", "part.stl").write_text("solid stub-test-input\nendsolid stub-test-input\n", encoding="utf-8")
        outcome = "pass" if passed else ("infeasible" if verdict == "INFEASIBLE" else "verification-failed")
        rows = [{"output": "mass_kg", "rel_err": 0.012}, {"output": "vm_p99_MPa", "rel_err": 0.031}]
        return {"rounds": [{"round": 1, "case_id": "v001", "rows": rows, "passed": passed,
                            "reasons": [] if passed else ["stub: vm_p99_MPa disagrees (test input)"]}],
                "passed": passed, "outcome": outcome, "final_round": 1, "final_case": "v001",
                "winner_case": "v001" if passed else None, "winner_params": {"volfrac": 0.2}}

    def grade(loop, check, eff, *, conforming=None, rig=None):
        if not (loop or {}).get("passed"):
            return "preview-grade", ["check skipped: verification failed"]
        g_ = sc.get("grade", "verified-desktop")
        return g_, ["stub grade reason (test input)"]

    def finalise(run, eff, loop, check, parity, grade_, *, outcome=None, conforming=None, rig=None):
        loop = loop or {"rounds": [], "passed": False, "outcome": None, "final_round": None, "final_case": None,
                        "winner_case": None}
        outcome = outcome or loop["outcome"]
        if outcome == "pass" and sc.get("parity_fail"):
            v = "FAIL"
        else:
            v = {"thin-sweep": "THIN", "infeasible": "INFEASIBLE", "pass": "PASS"}.get(outcome, "FAIL")
        g_, why = grade_ if grade_ else ("preview-grade", ["check skipped: thin sweep"])
        sw = run.read_json("sweep.json", {}) or {}
        reasons = {"THIN": [f"only {sw.get('n_ok')} of {sw.get('n')} designs finished: detached-load"],
                   "INFEASIBLE": ["no candidate predicted feasible with the margins (round 1)"],
                   "FAIL": ["device disagreement on gpu/warp: max rel diff 0.1" if sc.get("parity_fail")
                            else "stub: vm_p99_MPa disagrees (test input)"], "PASS": []}[v]
        doc = {"schema": "aces-golden-validation/1", "tol": 0.1, "rounds": loop["rounds"], "outcome": outcome,
               "passed": v == "PASS", "final_round": loop.get("final_round"), "final_case": loop.get("final_case"),
               "winner_case": loop.get("winner_case") if v == "PASS" else None,
               "independent_check": check or {"ran": False, "reason": "not run", "passed": None, "levels": []},
               "device_parity": parity or [], "grade": g_, "grade_reasons": why, "verdict": v, "verdict_reasons": reasons,
               "conforming": conforming or conforming_block(), "rig": rig}
        if sc.get("rig_integrity"):  # a rig block with an integrity error (test input; no rig was contacted)
            doc["rig"] = {"lane": "sim/test", "scope": {"sweep": False, "check": True, "conforming": False},
                          "experiments": [], "code_identity": {"ok": False}, "integrity_error": sc["rig_integrity"]}
        run.write_json("validation.json", doc)
        return doc

    def conforming_block(*args, **kwargs):
        return {"ran": False, "passed": None, "status": None, "case_id": None, "host": None, "mode": "auto",
                "reason": "not run: stub (test input)", "h_mm": 1.0, "source_case": None, "reference": None,
                "record": None, "mesh": None, "solver": None, "bands": None, "rows": [], "constraints_met": None,
                "rig": None, "rig_attempt": None}

    mod("verify", run_rounds=run_rounds, grade=grade, finalise=finalise, conforming_block=conforming_block,
        local_conforming=lambda *args, **kwargs: None,
        independent_check=lambda run, eff, domain, experiment, loop, levels, pool, progress, *, rig=None: {
            "ran": True, "reason": "", "passed": True,
            "levels": [{"level": 1, "h_mm": 1.0, "device": "cpu", "host": "desktop", "ran": True, "passed": True,
                        "rows": []}]},
        device_parity=lambda run, eff, domain, experiment, loop, plan, progress: [])

    # export, render, report
    def export_winner(run, case_id, eff, *, label="winner"):
        noise("export")
        dst = run.path(label, f"{label}.stl")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run.path(case_id, "part.stl"), dst)
        rec = {"schema": "aces-golden-winner/1", "label": label, "case_id": case_id, "params": {"volfrac": 0.2},
               "metrics": {"mass_kg": 0.177, "umax_mm": 0.0312, "vm_p99_MPa": 54.5, "vm_max_MPa": 90.0, "compliance_Nmm": 1.0},
               "stl": {"path": f"{label}/{label}.stl", "sha256": sha256_file(dst)}}
        run.write_json(f"{label}/{label}.json", rec)
        return rec

    def gallery(items, png, *, cols=5, highlight=None):
        Path(png).parent.mkdir(parents=True, exist_ok=True)
        Path(png).with_suffix(".items.json").write_text(json.dumps(items), encoding="utf-8")
        Path(png).with_suffix(".highlight.json").write_text(json.dumps(highlight), encoding="utf-8")
        return Path(png)

    mod("export", export_winner=export_winner)
    mod("render", winner_figures=lambda run, label, case_id, domain, eff: {}, gallery=gallery)

    def assemble(run):
        v = run.read_json("validation.json", {}) or {}
        return {"verdict_line": f"{v.get('verdict')} · {v.get('grade')} · stub report (test input)"}

    def write(run, data):
        run.write_text("report.md", "# stub report (test input)\n\n" + str(data.get("verdict_line")) + "\n")
        run.write_text("report.html", "<p>stub report (test input)</p>\n")
        run.write_json("report.json", {"schema": "aces-golden-report/1", "verdict_line": data.get("verdict_line")})
        return {k: str(run.path(f"report.{k}")) for k in ("md", "html", "json")}

    mod("report", charts=lambda run: {}, assemble=assemble, write=write,
        verdict_line=lambda data, *, evidence=True: data.get("verdict_line"))


def child_main() -> int:
    """Entry point of the child process: stubs (unless G3_REAL), then aces_engineering.golden_cli.main(G3_ARGV)."""
    if not os.environ.get("G3_REAL"):
        install_stubs(json.loads(os.environ.get("G3_SCENARIO") or "{}"))
    from aces_engineering import golden_cli

    return golden_cli.main(json.loads(os.environ["G3_ARGV"]))


# ---- the tests ----------------------------------------------------------------------------------------------------
class Result:
    def __init__(self, cp: subprocess.CompletedProcess):
        self.rc = cp.returncode
        self.out = cp.stdout.decode("utf-8").replace("\r\n", "\n")
        self.err = cp.stderr.decode("utf-8", "replace").replace("\r\n", "\n")

    def doc(self) -> dict:
        """stdout must be exactly one JSON document."""
        dec = json.JSONDecoder()
        text = self.out.strip()
        obj, end = dec.raw_decode(text)
        if text[end:].strip():
            raise AssertionError(f"more than one document on stdout: {text[end:][:200]!r}")
        return obj


class CliTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.aces_argv0, cls.aces_pypath = aces_command()

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="g3-cli-"))
        self.work = self.tmp / "work"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, scenario: dict | None, argv: list[str], real: bool = False, root: Path | None = None) -> dict:
        root = root or self.tmp
        env = dict(os.environ)
        for k in ("ACES_GOLDEN_DEBUG", "G3_REAL"):
            env.pop(k, None)
        paths = [str(LIB)] + ([self.aces_pypath] if self.aces_pypath else []) + \
                ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        env.update({"PYTHONPATH": os.pathsep.join(paths), "ACES_WORK": str(root / "work"),
                    "ACES_STATE": str(root / "state"), "ACES_USER_CONFIG": str(root / "cfg"),
                    "XDG_DATA_HOME": str(root / "data"), "G3_SCENARIO": json.dumps(scenario or {}),
                    "G3_ARGV": json.dumps(argv), "PYTHONIOENCODING": "utf-8"})
        if self.aces_argv0:
            env["ACES_CMD"] = json.dumps(self.aces_argv0)
        if self.aces_pypath:
            env["ACES_ETC"] = str(ACES_BASE_SRC / "etc")
        if real:
            env["G3_REAL"] = "1"
        return env

    def child_cmd(self) -> list[str]:
        code = f"import sys; sys.path.insert(0, {str(TESTS)!r}); import test_g3_cli as t; raise SystemExit(t.child_main())"
        return [sys.executable, "-c", code]

    def run_cli(self, argv: list[str], scenario: dict | None = None, *, timeout: float = 180, real: bool = False,
                root: Path | None = None) -> Result:
        cp = subprocess.run(self.child_cmd(), env=self.env(scenario, argv, real, root), capture_output=True, timeout=timeout)
        return Result(cp)

    def show_here(self, argv: list[str]) -> tuple[int, str]:
        """`aces golden show ...` in this process against this test's work directory: (exit code, stdout)."""
        from aces_engineering import golden_cli

        saved = os.environ.get("ACES_WORK")
        os.environ["ACES_WORK"] = str(self.work)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = golden_cli.main(argv)
        finally:
            if saved is None:
                os.environ.pop("ACES_WORK", None)
            else:
                os.environ["ACES_WORK"] = saved
        return rc, out.getvalue() + err.getvalue() if rc else out.getvalue()

    def need_aces(self) -> None:
        if not self.aces_argv0:
            self.skipTest("no aces here: neither src/aces-base nor an installed aces-base")

    def runs(self) -> list[Path]:
        return sorted(self.work.glob("*/golden/*/run.json"))

    def run_dir(self) -> Path:
        runs = self.runs()
        self.assertEqual(len(runs), 1, runs)
        return runs[0].parent


class RefusalTests(CliTestCase):
    def test_lane_is_refused_without_a_run_directory(self) -> None:
        """No fabric manifest in this test's state: the live probe refuses with R1 before anything is computed."""
        self.need_aces()
        r = self.run_cli(["run", "--demo", "--lane", "sim/pool", "--json"])
        self.assertEqual(r.rc, 2, r.err)
        doc = r.doc()
        self.assertEqual((doc["outcome"], doc["exit_code"], doc["run_dir"], doc["verdict"]), ("usage", 2, None, None))
        self.assertEqual(doc["message"], "lane 'sim/pool' cannot be used: there is no fabric manifest (aces fabric "
                                         "refresh writes it); nothing was submitted")
        self.assertIn("aces golden: error: lane 'sim/pool' cannot be used: there is no fabric manifest", r.err)
        self.assertFalse(self.work.exists() and any(self.work.rglob("run.json")))
        self.assertEqual(r.doc()["schema"], "aces-golden-result/1")
        self.assertIsNone(doc["rig"])
        self.assertIsNone(doc["conforming"])

    def test_argparse_errors_with_json_are_one_document(self) -> None:
        r = self.run_cli(["run", "--demo", "--device", "tpu", "--json"])
        self.assertEqual(r.rc, 2)
        self.assertEqual(r.doc()["outcome"], "usage")
        self.assertIn("aces golden: error:", r.err)
        r = self.run_cli(["run", "--demo", "--jobs", "0"])
        self.assertEqual(r.rc, 2)
        self.assertEqual(r.out, "")

    def test_usage_refusals_before_the_run_directory(self) -> None:
        self.need_aces()
        spec = self.tmp / "spec.json"
        spec.write_text(json.dumps({"schema": "aces-golden-spec/1", "name": "stub-file", "sweep": {"n": 10, "seed": 1},
                                    "surrogate": {"min_points": 8}}), encoding="utf-8")
        cases = [
            (["run", "--demo", "--spec", str(spec), "--json"], None, 2, "give --demo or --spec, not both"),
            (["run", "--json"], None, 2, "needs --demo, --spec FILE or --project NAME"),
            (["run", "--spec", str(spec), "--json"], None, 2, "needs --project NAME"),
            (["run", "--spec", str(spec), "--project", "no-such-project", "--json"], None, 2, "no project 'no-such-project'"),
            (["run", "--project", "no-such-project", "--json"], None, 2, "no spec at"),
            (["run", "--demo", "--device", "gpu", "--json"], None, 2, "no usable CUDA GPU here"),
            (["run", "--demo", "--json"], {"guard": ["rmin_mm 4 is below 1.5 x gen_mm (test input)"]}, 4, "under-resolved: rmin_mm 4"),
            (["run", "--demo", "--rig-sweep", "--json"], None, 2, "--rig-sweep needs a rig lane"),
            (["run", "--demo", "--lane", "none", "--rig-sweep", "--json"], None, 2,
             "--rig-sweep needs a rig lane (--lane none was given)"),
            (["run", "--demo", "--rig-budget", "600", "--json"], None, 2, "--rig-budget needs a rig lane"),
            (["run", "--demo", "--conforming", "rig", "--json"], None, 2, "--conforming rig needs a rig lane"),
            (["run", "--demo", "--conforming", "local", "--json"], None, 2,
             "--conforming local cannot run on this desktop: stub: gmsh and ccx are not here (test input)"),
            (["run", "--demo", "--lane", "sim/pool", "--check", "off", "--conforming", "off", "--json"], None, 2,
             "lane 'sim/pool' cannot be used: there is no fabric manifest"),
        ]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda k: self.run_cli(cases[k][0], cases[k][1], root=self.tmp / f"case{k}"), range(len(cases))))
        for k, ((argv, _, code, msg), r) in enumerate(zip(cases, results)):
            self.assertEqual(r.rc, code, (argv, r.err))
            self.assertIn(msg, r.doc()["message"], argv)
            self.assertEqual(r.doc()["outcome"], "usage" if code == 2 else "under-resolved")
            self.assertEqual(list((self.tmp / f"case{k}").glob("work/*/golden/*/run.json")), [], argv)  # no run directory
        r = self.run_cli(["run", "--spec", str(spec), "--project", "made-here", "--new", "--json"])
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(r.doc()["project"], "made-here")

    def test_no_arguments_print_help(self) -> None:
        from aces_engineering import golden_cli

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(golden_cli.main([]), 0)
        self.assertIn("usage: aces golden", out.getvalue())
        self.assertIn("run,show,verify,compute,spec,init,report", out.getvalue())


class RunTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.need_aces()

    def test_json_purity_with_noisy_stages(self) -> None:
        r = self.run_cli(["run", "--demo", "--json", "--progress", "lines"], {"noise": True})
        self.assertEqual(r.rc, 0, r.err)
        doc = r.doc()
        self.assertEqual(set(doc), {"schema", "outcome", "verdict", "exit_code", "message", "verdict_line", "grade",
                                    "project", "run_id", "run_dir", "report", "winner", "validation",
                                    "independent_check", "device_parity", "conforming", "rig", "evidence",
                                    "compute_used", "started_utc", "finished_utc", "wall_s"})
        self.assertEqual((doc["outcome"], doc["verdict"], doc["grade"], doc["exit_code"]), ("pass", "PASS", "verified-desktop", 0))
        self.assertNotIn("noise", r.out)
        self.assertNotIn("initialized", r.out)  # the Warp banner ("Warp 1.x initialized:") stays in log.txt
        self.assertNotIn("noise", r.err)
        self.assertIn("[4/10] sweep", r.err)
        run = Path(doc["run_dir"])
        self.assertEqual(run, self.run_dir())
        log = (run / "log.txt").read_text(encoding="utf-8", errors="replace")
        for where in ("compute", "inputs", "sweep", "loop", "export", "evidence"):
            self.assertIn(f"stub stdout noise from {where}", log)
            self.assertIn(f"raw fd2 noise from {where}", log)
        self.assertEqual(json.loads((run / "result.json").read_text(encoding="utf-8")), doc)
        self.assertEqual(doc["winner"]["label"], "winner")
        self.assertEqual(Path(doc["winner"]["stl"]), run / "winner" / "winner.stl")
        self.assertEqual(doc["validation"], {"tol": 0.1, "rounds": 1, "passed": True, "final_round": 1,
                                             "max_rel_err": {"mass_kg": 0.012, "vm_p99_MPa": 0.031}})
        self.assertEqual(doc["independent_check"], {"ran": True, "h_mm": 1.0, "device": "cpu", "host": "desktop",
                                                    "passed": True, "reason": ""})
        self.assertEqual(doc["conforming"], {"ran": False, "passed": None, "host": None, "h_mm": 1.0, "case_id": None,
                                             "status": None, "reason": "not run: stub (test input)"})
        self.assertIsNone(doc["rig"])
        self.assertEqual(doc["evidence"]["aces_ledger_entry"], 2)  # entry 1 is project.new for golden-demo
        self.assertTrue(doc["evidence"]["ledgers_ok"])
        meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["status"], meta["exit_code"], meta["demo"]), ("done", 0, "root-mount"))
        self.assertEqual([s["name"] for s in meta["stages"]],
                         ["compute", "inputs", "domain", "sweep", "loop", "check", "export", "cfd", "evidence", "report"])
        self.assertTrue(all(s["status"] == "done" for s in meta["stages"] if s["name"] != "cfd"))
        cfd_stage = next(s for s in meta["stages"] if s["name"] == "cfd")
        self.assertEqual(cfd_stage["status"], "skipped")   # the demo spec has no flow block
        self.assertIn("no flow block", cfd_stage.get("note") or "")
        self.assertEqual(meta["tool"]["argv"], ["run", "--demo", "--json", "--progress", "lines"])
        self.assertEqual(meta["paths"]["stl"], "winner/winner.stl")
        manifest = (run / "manifest.sha256").read_text(encoding="utf-8")
        self.assertIn(" *result.json", manifest)
        self.assertIn(" *events.jsonl", manifest)
        self.assertNotIn(" *log.txt", manifest)
        ev = [json.loads(ln) for ln in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(ev[-1]["kind"], "result")
        from aces_engineering.golden import VERSION

        self.assertEqual(ev[0]["msg"], f"aces-golden {VERSION} · loading the engineering stack ...")
        self.assertEqual((run / "inputs" / "spec.json").read_text(encoding="utf-8"),
                         json.dumps(json.loads((run / "inputs" / "spec.json").read_text(encoding="utf-8")),
                                    indent=1, ensure_ascii=False) + "\n")
        self.assertEqual(json.loads((run / "spec.resolved.json").read_text(encoding="utf-8"))["id"],
                         f"golden-golden-demo-{doc['run_id']}")
        gallery = json.loads((run / "figures" / "sweep_gallery.items.json").read_text(encoding="utf-8"))
        self.assertEqual(len(gallery), 11)  # ten sweep designs and the winner
        self.assertEqual((gallery[-1]["case_id"], gallery[-1]["label"]), ("v001", "v001 · winner · 0.177 kg"))
        self.assertEqual(json.loads((run / "figures" / "sweep_gallery.highlight.json").read_text(encoding="utf-8")), "v001")

    def test_exit_code_mapping(self) -> None:
        cases = [
            ({"verdict": "PASS"}, [], 0, "pass", "PASS"),
            ({"verdict": "PASS", "grade": "preview-grade"}, [], 0, "pass", "PASS"),
            ({"verdict": "PASS", "grade": "preview-grade"}, ["--strict"], 1, "verification-failed", "PASS"),
            ({"verdict": "PASS"}, ["--strict"], 0, "pass", "PASS"),
            ({"verdict": "FAIL"}, [], 1, "verification-failed", "FAIL"),
            ({"verdict": "PASS", "parity_fail": True}, [], 1, "verification-failed", "FAIL"),
            ({"verdict": "INFEASIBLE"}, [], 5, "infeasible", "INFEASIBLE"),
            ({"n_ok": 4}, [], 3, "thin-sweep", "THIN"),
            ({"n_ok": 4}, ["--allow-thin"], 0, "pass", "PASS"),
            ({"n_ok": 2}, ["--allow-thin"], 3, "thin-sweep", "THIN"),
            ({"evidence_fail": True}, [], 6, "evidence-failed", "PASS"),
            ({"evidence_fail": True, "verdict": "FAIL"}, [], 1, "verification-failed", "FAIL"),
            ({"evidence_fail": True, "n_ok": 4}, [], 3, "thin-sweep", "THIN"),
            ({"raise_in": "sweep"}, [], 7, "error", None),
            ({"raise_in": "export", "verdict": "FAIL"}, [], 7, "error", "FAIL"),
            ({"interrupt_in": "loop"}, [], 130, "interrupted", None),
            ({"verdict": "PASS"}, ["--require-grade", "rig-verified"], 1, "verification-failed", "PASS"),
            ({"verdict": "PASS"}, ["--require-grade", "verified-desktop"], 0, "pass", "PASS"),
            ({"verdict": "PASS", "grade": "preview-grade"}, ["--require-grade", "preview-grade"], 0, "pass", "PASS"),
            ({"verdict": "PASS"}, ["--strict", "--require-grade", "rig-verified"], 1, "verification-failed", "PASS"),
            ({"verdict": "PASS", "grade": "preview-grade"}, ["--strict", "--require-grade", "preview-grade"], 1,
             "verification-failed", "PASS"),
            ({"verdict": "PASS", "rig_integrity": "x001: c000/golden/x001/mask.npy did not verify (test input)"}, [],
             6, "evidence-failed", "PASS"),
            ({"verdict": "FAIL", "rig_integrity": "x001: c000/golden/x001/mask.npy did not verify (test input)"}, [],
             1, "verification-failed", "FAIL"),
        ]
        from concurrent.futures import ThreadPoolExecutor

        def one(k: int) -> Result:  # each case in its own work and state directories, four at a time
            scenario, extra = cases[k][0], cases[k][1]
            return self.run_cli(["run", "--demo", "--json", *extra], scenario, root=self.tmp / f"case{k:02d}")

        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(one, range(len(cases))))
        for (scenario, extra, code, outcome, verdict), r in zip(cases, results):
            label = (scenario, extra, r.err[-600:])
            if code == 130:
                self.assertEqual(r.rc, 130, label)
                doc = r.doc()
                self.assertEqual((doc["outcome"], doc["exit_code"]), ("interrupted", 130))
                meta = json.loads((Path(doc["run_dir"]) / "run.json").read_text(encoding="utf-8"))
                st = {s["name"]: s["status"] for s in meta["stages"]}
                self.assertEqual((meta["status"], st["loop"], st["check"], st["report"]), ("interrupted", "failed", "skipped", "skipped"))
                self.assertTrue((Path(doc["run_dir"]) / "result.json").is_file())
                self.assertFalse((Path(doc["run_dir"]) / "manifest.sha256").exists())
                self.assertIn("aces golden: error: interrupted during loop", r.err)
                continue
            self.assertEqual(r.rc, code, label)
            doc = r.doc()
            self.assertEqual((doc["exit_code"], doc["outcome"], doc["verdict"]), (code, outcome, verdict), label)
            meta = json.loads((Path(doc["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["exit_code"], code)
            self.assertEqual(meta["status"], "error" if code == 7 else "done", label)
            if code != 0:
                self.assertIn("aces golden: error:", r.err, label)
            if scenario.get("evidence_fail") and code == 6:
                self.assertIn("hint: replay with: aces ledger record golden.run", r.err)
            if scenario.get("evidence_fail") and code in (1, 3):
                self.assertIn("evidence failed too", doc["message"])
            if extra == ["--strict"] and code == 1:
                self.assertTrue(doc["message"].startswith("strict: grade preview-grade (stub grade reason"), doc["message"])
            if "rig-verified" in extra:  # the higher requirement wins, also over --strict
                self.assertEqual(doc["message"], "required grade rig-verified: grade verified-desktop (stub grade reason "
                                                 "(test input))")
            if extra == ["--strict", "--require-grade", "preview-grade"]:
                self.assertTrue(doc["message"].startswith("strict: grade preview-grade ("), doc["message"])
            if scenario.get("rig_integrity") and code == 6:
                self.assertEqual(doc["message"], "rig integrity error: x001: c000/golden/x001/mask.npy did not verify "
                                                 "(test input)")
                self.assertFalse(doc["rig"]["code_identity_ok"])
                self.assertEqual(doc["rig"]["integrity_error"], scenario["rig_integrity"])
            if scenario.get("rig_integrity") and code == 1:
                self.assertIn("; rig integrity error too: x001:", doc["message"])
            if verdict == "THIN":
                st = {s["name"]: s for s in meta["stages"]}
                self.assertEqual((st["loop"]["status"], st["check"]["status"]), ("skipped", "skipped"))
                self.assertEqual(st["evidence"]["status"], "failed" if scenario.get("evidence_fail") else "done")
                self.assertTrue(doc["message"].startswith("only "), doc["message"])
                self.assertIsNone(doc["winner"])
            if verdict in ("FAIL", "INFEASIBLE") and code != 7:
                self.assertEqual(doc["winner"]["label"], "candidate")
            if code == 7:
                st = {s["name"]: s for s in meta["stages"]}
                failed = "sweep" if scenario.get("raise_in") == "sweep" else "export"
                self.assertEqual(st[failed]["status"], "failed")
                self.assertTrue(all(st[n]["status"] == "skipped" for n in ("evidence", "report")))
                self.assertTrue((Path(doc["run_dir"]) / "report.md").is_file())  # the short report
                self.assertIn("Traceback", (Path(doc["run_dir"]) / "log.txt").read_text(encoding="utf-8", errors="replace"))

    def test_error_after_the_run_directory_exists(self) -> None:
        r = self.run_cli(["run", "--demo", "--json"], {"derived_grid": True,
                                                       "geometry": ["load 'cup-down': plane z=37 is not on the 4 mm grid (test input)"]})
        self.assertEqual(r.rc, 2, r.err)
        doc = r.doc()
        self.assertEqual(doc["outcome"], "usage")
        run = Path(doc["run_dir"])
        meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
        st = {s["name"]: s["status"] for s in meta["stages"]}
        self.assertEqual(meta["status"], "error")
        self.assertEqual((st["compute"], st["inputs"], st["sweep"], st["report"]), ("done", "failed", "skipped", "skipped"))
        self.assertTrue((run / "result.json").is_file())
        self.assertTrue((run / "report.md").is_file())
        self.assertTrue((run / "manifest.sha256").is_file())

    def test_kernel_selftest_gate_and_cpu_cell_warning(self) -> None:
        bad = {"kernel_selftest": {"passed": False, "wall_s": 0.01, "source": "stub (test input)",
                                   "error": "AssertionError: patch test off by 3 % (test input)"}}
        r = self.run_cli(["run", "--demo", "--json"], bad, root=self.tmp / "kernel")
        self.assertEqual(r.rc, 7, r.err)
        doc = r.doc()
        self.assertEqual((doc["outcome"], doc["exit_code"], doc["run_dir"]), ("error", 7, None))
        self.assertIn("the voxel FEA kernel failed its element/patch self-test: AssertionError: patch test off by 3 % "
                      "(test input)", doc["message"])
        self.assertEqual(list((self.tmp / "kernel").glob("work/*/golden/*/run.json")), [])  # no run directory
        self.assertFalse((self.tmp / "kernel" / "work" / "golden-demo").exists())  # nor the demo project
        warn = "250,000 cells at fea_mm 1 mm on a CPU-only host: each solve will be slow (test input)"
        r = self.run_cli(["run", "--demo", "--progress", "lines"], {"warnings": [warn]}, root=self.tmp / "warn")
        self.assertEqual(r.rc, 0, r.err)
        self.assertIn(f"warning: {warn}", r.err)

    def test_human_mode_and_show(self) -> None:
        r = self.run_cli(["run", "--demo", "--progress", "lines", "--note", "test note"])
        self.assertEqual(r.rc, 0, r.err)
        lines = r.out.strip().splitlines()
        self.assertEqual(lines[0], "PASS · verified-desktop · stub report (test input)")
        self.assertTrue(lines[1].startswith("report: ") and lines[1].endswith("report.md"))
        self.assertTrue(lines[2].startswith("winner: ") and lines[2].endswith("winner.stl"))
        run = self.run_dir()
        r2 = self.run_cli(["run", "--demo", "--quiet", "--seed", "8"])
        self.assertEqual(r2.rc, 0, r2.err)
        self.assertEqual(r2.err, "")
        self.assertEqual(len(self.runs()), 2)
        # show reads records only (no stage module, no stdio guard): in this process
        rc, out = self.show_here(["show"])
        self.assertEqual(rc, 0)
        self.assertIn(run.name, out)
        rc, out = self.show_here(["show", "--json"])
        rows = json.loads(out)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["run_id"], run.name)  # newest first: the seed-8 run
        rc, out = self.show_here(["show", run.name, "--json"])
        self.assertEqual(json.loads(out)["run_id"], run.name)
        rc, out = self.show_here(["show", run.name])
        self.assertIn("verdict  PASS · verified-desktop", out)
        bench = self.tmp / "bench.md"
        rc, out = self.show_here(["show", run.name, "--bench-out", str(bench)])
        self.assertEqual(rc, 0, out)
        text = bench.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# ACES golden path benchmarks\n"))
        self.assertIn("aces golden stub-root-mount on test-host", text)
        self.assertIn("| Machine | CPU | Devices used | Workers | Designs ok/n | gen/fea/check mm |", text)
        self.assertIn("| test-host | test cpu (2 logical) | cpu/numpy x1, Test GPU (warp) | 1 | 10/10 | 2/2/1 |", text)
        self.assertIn(f"| pass | verified-desktop | {run.name} |", text)
        self.assertEqual(self.show_here(["show", "no-such-run"])[0], 1)
        self.assertEqual(self.show_here(["show", "--project", "nothing-here"])[0], 1)
        r = self.run_cli(["verify", "latest"])
        self.assertEqual(r.rc, 0, r.err)
        self.assertIn("PASS  stub", r.out)
        r = self.run_cli(["verify", "latest", "--json"], {"audit_fail": True})
        self.assertEqual(r.rc, 1)
        self.assertFalse(r.doc()["ok"])
        r = self.run_cli(["verify", "no-such-run"])
        self.assertEqual(r.rc, 2)
        r = self.run_cli(["report", "no-such-run"])
        self.assertEqual(r.rc, 2)
        r = self.run_cli(["report", run.name])
        self.assertEqual(r.rc, 0, r.err)
        r = self.run_cli(["compute", "--json"])
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(r.doc()["host"], "test-host")
        r = self.run_cli(["compute", "--lane", "sim/pool", "--json"])  # no fabric manifest in this test's state
        self.assertEqual(r.rc, 2, r.err)
        self.assertEqual(r.doc()["host"], "test-host")
        self.assertIn("aces golden: error: lane 'sim/pool' cannot be used: there is no fabric manifest", r.err)
        r = self.run_cli(["compute", "--lane", "none"])
        self.assertEqual(r.rc, 0, r.err)
        self.assertNotIn("rig:", r.out)
        r = self.run_cli(["spec", "--schema"])
        self.assertEqual(r.doc(), {"title": "stub schema (test input)"})

    def test_init(self) -> None:
        step = self.tmp / "part.step"
        step.write_bytes(b"ISO-10303-21; user design space (test input)\n")
        r = self.run_cli(["init", "bracket-1", "--step", str(step), "--hypothesis", "test hypothesis"])
        self.assertEqual(r.rc, 0, r.err)
        spec = json.loads((self.work / "bracket-1" / "golden" / "spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["domain"], {"source": "step", "step_file": "../cad/part.step"})
        self.assertEqual((self.work / "bracket-1" / "cad" / "part.step").read_bytes(), step.read_bytes())
        self.assertIn("cad_sources:\n  - cad/part.step", (self.work / "bracket-1" / "aces.yaml").read_text(encoding="utf-8"))
        r = self.run_cli(["init", "bracket-1"])
        self.assertEqual(r.rc, 2)
        self.assertIn("exists", r.err)
        r = self.run_cli(["init", "demo-copy", "--demo", "bone"])
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(json.loads((self.work / "demo-copy" / "golden" / "spec.json").read_text(encoding="utf-8"))["name"], "stub-bone")

    @unittest.skipIf(os.name != "posix", "Ctrl-C is exercised with SIGINT on POSIX")
    def test_ctrl_c(self) -> None:
        p = subprocess.Popen(self.child_cmd(), env=self.env({"sleep_in_sweep": 60}, ["run", "--demo", "--json"]),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not any(self.work.glob("*/golden/*/sweep.json")) and \
                not any(r for r in self.work.glob("*/golden/*/run.json")
                        if '"sweep"' in r.read_text(encoding="utf-8", errors="replace")):
            time.sleep(0.1)
        time.sleep(0.5)
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 130, err.decode(errors="replace"))
        doc = json.loads(out.decode("utf-8"))
        self.assertEqual((doc["outcome"], doc["exit_code"]), ("interrupted", 130))
        meta = json.loads((Path(doc["run_dir"]) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "interrupted")
        self.assertFalse((Path(doc["run_dir"]) / "manifest.sha256").exists())


class RealModuleTests(CliTestCase):
    def test_spec_demo_matches_the_generator_module(self) -> None:
        try:
            from aces_engineering.golden import demo
            expected = demo.spec()
        except Exception as e:  # the generator's modules are not here (or not finished): nothing to compare
            self.skipTest(f"golden.demo is not importable here ({type(e).__name__})")
        r = self.run_cli(["spec", "--demo"], real=True)
        self.assertEqual(r.rc, 0, r.err)
        self.assertEqual(r.doc(), json.loads(json.dumps(expected)))


if __name__ == "__main__":
    unittest.main()
