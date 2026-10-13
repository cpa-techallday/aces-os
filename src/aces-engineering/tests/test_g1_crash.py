"""Tier-3 crash wiring: the spec's `crash:` block validation, the block-to-case mapping, the simlab experiment,
and the rig dispatch state machine (fake MCP client). The solver's own validation lives in cudax-sim-runtime
(docs/validation/radioss-20261014); here we pin the golden side of the contract. stdlib unittest."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parent / "lib", _HERE.parent / "vendor"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.append(str(_p))

import yaml  # noqa: E402

from aces_engineering.golden import crash, demo, rigcase, spec  # noqa: E402
from aces_engineering.golden import rig as rig_mod  # noqa: E402

BASE = Path(tempfile.gettempdir())

TUBE_BLOCK = {"width_m": 0.08, "length_m": 0.32, "thickness_m": 0.0015,
              "material": {"E_Pa": 210e9, "nu": 0.3, "rho_kg_m3": 7850.0, "yield_Pa": 250e6},
              "impactor": {"mass_kg": 500.0, "velocity_mps": 10.0},
              "end_s": 0.0144}


def resolve(raw):
    return spec.resolve(raw, base_dir=BASE, overrides={}, gpu_usable=False)


def crash_spec(**block):
    raw = demo.spec("root-mount")
    import copy
    b = copy.deepcopy(TUBE_BLOCK)
    b.update(block)
    raw["crash"] = b
    return raw


class CrashSpec(unittest.TestCase):
    def test_block_validates_with_defaults(self):
        eff = resolve(crash_spec())
        c = eff["crash"]
        self.assertTrue(c["wierzbicki_abramowicz"])
        self.assertIsNone(c["mesh_size_m"])
        self.assertEqual(c["material"]["hardening_b_Pa"], 0.0)

    def test_no_block_means_no_crash_stage(self):
        eff = resolve(demo.spec("root-mount"))
        self.assertIsNone(eff.get("crash"))

    def test_missing_material_property_refused(self):
        raw = crash_spec()
        del raw["crash"]["material"]["yield_Pa"]
        with self.assertRaises(spec.SpecError):
            resolve(raw)

    def test_missing_impactor_refused(self):
        raw = crash_spec()
        del raw["crash"]["impactor"]["velocity_mps"]
        with self.assertRaises(spec.SpecError):
            resolve(raw)

    def test_solid_bar_refused(self):
        with self.assertRaises(spec.SpecError) as cm:
            resolve(crash_spec(thickness_m=0.05))
        self.assertIn("thin-walled", str(cm.exception))

    def test_unknown_key_refused(self):
        with self.assertRaises(spec.SpecError):
            resolve(crash_spec(crush_force_kN=25.0))


class CaseMapping(unittest.TestCase):
    def test_block_maps_to_solver_case(self):
        b = resolve(crash_spec())["crash"]
        case = crash.case_from_block(b)
        self.assertEqual(case["mode"], "tube")
        self.assertEqual(case["geometry"]["width_m"], 0.08)
        self.assertEqual(case["material"]["yield_Pa"], 250e6)
        self.assertEqual(case["impactor"], {"mass_kg": 500.0, "velocity_mps": 10.0})
        self.assertEqual(case["reference"], {"kind": "wierzbicki_abramowicz"})
        self.assertNotIn("mesh", case)   # the solver's own default (width/20) applies

    def test_mesh_size_and_reference_opt_out(self):
        b = resolve(crash_spec(mesh_size_m=0.004, wierzbicki_abramowicz=False))["crash"]
        case = crash.case_from_block(b)
        self.assertEqual(case["mesh"], {"size_m": 0.004})
        self.assertNotIn("reference", case)


class WriteCase(unittest.TestCase):
    def test_case_files_and_experiment(self):
        block = spec.GoldenSpec(**crash_spec()).crash.model_dump()
        with tempfile.TemporaryDirectory() as td:
            crash_dir = Path(td) / "crash"
            doc = crash.write_case(crash_dir, name="run1-crash", block=block, project="p", owner="aaron")
            exp_p = crash_dir / "experiment.yaml"
            self.assertTrue(exp_p.is_file() and (crash_dir / "crash_case.json").is_file())
            exp = yaml.safe_load(exp_p.read_text(encoding="utf-8"))
            self.assertEqual((exp["physics"], exp["solver"]), ("crash", "radioss-crash"))
            self.assertEqual(exp["params"]["mode"], "tube")
            self.assertEqual(exp["resources"]["vram_gb"], 0)   # explicit dynamics on CPU threads
            self.assertEqual(exp["tags"]["kind"], "golden-tier3-crash")
            self.assertEqual(doc["schema"], "aces-golden-crash/1")
            # the rig rewrite changes only the id: the deck is generated in-container, no paths to fix
            rig_exp = crash.rig_experiment(doc, "run1-crash")
            self.assertEqual(rig_exp["id"], "run1-crash")
            self.assertEqual(rig_exp["params"], exp["params"])
            self.assertIn("NOT the optimised part", doc["tier_note"])


# ------------------------------------------------------------------ rig dispatch (fake MCP client)

class FakeRig:
    """The RigClient surface golden.rigcase.dispatch uses, scripted: sim_status answers in order."""

    def __init__(self, statuses=(), files=None, fail_on=None, default=None):
        self.statuses = list(statuses)
        self.files = files or {}
        self.fail_on = fail_on
        self.default = default or {"counts": {"finished": 1, "failed": 0}, "jobs": [{"state": "finished"}]}
        self.uploads = []
        self.submitted = None

    def put_file(self, name, data):
        if self.fail_on == "put_file":
            raise rig_mod.RigError("boom")
        self.uploads.append(name)
        import hashlib
        return {"sha256": hashlib.sha256(data).hexdigest()}

    def put_spec(self, text, name, principal):
        if self.fail_on == "put_spec":
            raise rig_mod.RigError("policy refuses put_spec")
        self.spec_text = text
        return {"spec_path": f"cases/uploaded/{principal or 'aaron'}/{name}"}

    def submit(self, spec_path, exp_id, lease_wait_s):
        self.submitted = (spec_path, exp_id, lease_wait_s)
        return {"queue": "sim", "exp_dir": f"/work/out/queue/{exp_id}",
                "jobs": [f"{exp_id}--c000--run--abc123"]}

    def call(self, name, args=None, **_):
        assert name == "sim_status"
        if self.statuses:
            return self.statuses.pop(0)
        return self.default

    def get_file(self, exp_id, path, **_):
        if path not in self.files:
            raise rig_mod.RigError(f"no such file {path}")
        import hashlib
        data = self.files[path]
        return data, hashlib.sha256(data).hexdigest(), 1


def _written_case(td):
    block = spec.GoldenSpec(**crash_spec()).crash.model_dump()
    crash_dir = Path(td) / "crash"
    doc = crash.write_case(crash_dir, name="run1-crash", block=block, project="p", owner="aaron")
    return crash_dir, doc


FINISHED_FILES = {
    "c000/status.json": json.dumps({"status": "FINISHED", "wall_s": 42.0, "error": None}).encode(),
    "c000/results.json": json.dumps({"crush": {"p_mean_kN": 25.38, "crush_m": 0.101},
                                     "reference": {"p_mean_ref_kN": 20.24}}).encode(),
    "c000/evidence.json": json.dumps({"schema": "test"}).encode(),
}


class RigDispatch(unittest.TestCase):
    def test_delivered_pulls_and_verifies(self):
        with tempfile.TemporaryDirectory() as td:
            crash_dir, doc = _written_case(td)
            rig = FakeRig(files=dict(FINISHED_FILES))
            rec = crash.dispatch(rig, crash_dir=crash_dir, doc=doc, exp_id="run1-crash", principal="aaron",
                                 budget_s=60.0, poll_s=0.0)
            self.assertEqual(rec["state"], "delivered")
            self.assertEqual(rec["results"]["crush"]["p_mean_kN"], 25.38)
            self.assertEqual(sorted(rec["pulled"]), ["evidence.json", "results.json", "status.json"])
            self.assertTrue((crash_dir / "rig" / "c000" / "results.json").is_file())
            self.assertEqual(rig.uploads, [])                # the deck is generated in-container
            self.assertEqual(rig.submitted[1], "run1-crash")
            self.assertTrue((crash_dir / "rig" / "dispatch.json").is_file())
            self.assertIn("radioss-crash", rig.spec_text)

    def test_failed_case_quotes_the_rig(self):
        with tempfile.TemporaryDirectory() as td:
            crash_dir, doc = _written_case(td)
            files = {"c000/status.json": json.dumps(
                {"status": "FAILED", "error": "radioss-crash: engine failed: ** ERROR: TIME STEP LESS OR EQUAL ZERO"}).encode()}
            rig = FakeRig(statuses=[{"counts": {"finished": 0, "failed": 1}, "jobs": [{"state": "failed"}]}],
                          files=files)
            rec = crash.dispatch(rig, crash_dir=crash_dir, doc=doc, exp_id="run1-crash", principal=None,
                                 budget_s=60.0, poll_s=0.0)
            self.assertEqual(rec["state"], "failed")
            self.assertIn("TIME STEP", rec["reason"])

    def test_budget_spent_is_pending_and_never_cancelled(self):
        with tempfile.TemporaryDirectory() as td:
            crash_dir, doc = _written_case(td)
            rig = FakeRig(statuses=[{"counts": {"finished": 0, "failed": 0}, "jobs": [{"state": "running"}]}])
            rec = crash.dispatch(rig, crash_dir=crash_dir, doc=doc, exp_id="run1-crash", principal=None,
                                 budget_s=0.0, poll_s=0.0)
            self.assertEqual(rec["state"], "pending")
            self.assertIn("not cancelled", rec["reason"])
            self.assertIn("run1-crash", rec["reason"])

    def test_submission_refusal_is_not_delivered(self):
        with tempfile.TemporaryDirectory() as td:
            crash_dir, doc = _written_case(td)
            rig = FakeRig(fail_on="put_spec")
            rec = crash.dispatch(rig, crash_dir=crash_dir, doc=doc, exp_id="run1-crash", principal=None,
                                 budget_s=60.0, poll_s=0.0)
            self.assertEqual(rec["state"], "not-delivered")
            self.assertIn("policy refuses", rec["reason"])
            self.assertIsNone(rig.submitted)

    def test_templated_record_names_the_manual_queue(self):
        with tempfile.TemporaryDirectory() as td:
            rec = rigcase.write_templated(Path(td) / "crash", schema=crash.RIG_SCHEMA,
                                          experiment="run1-crash", why="no rig lane")
            self.assertEqual(rec["state"], "templated")
            self.assertIn("no rig lane", rec["reason"])
            self.assertIn("sim_submit", rec["reason"])
            self.assertTrue((Path(td) / "crash" / "rig" / "dispatch.json").is_file())

    def test_experiment_id_is_filename_safe(self):
        self.assertEqual(rigcase.rig_experiment_id("20261012T101112Z my run/1", "crash"),
                         "20261012T101112Z-my-run-1-crash")

    def test_validated_row_needs_a_pass_status(self):
        class Sess:
            def __init__(self, reg):
                self.client = self
                self.reg = reg

            def call(self, name, args=None, **_):
                assert name == "sim_list_solvers"
                return self.reg

        good = Sess({"radioss-crash": {"validation": {"status": "pass"}}})
        bad = Sess({"radioss-crash": {"validation": {"status": "fail"}}})
        none = Sess({})
        self.assertIsNotNone(rigcase.validated_row(good, "radioss-crash"))
        self.assertIsNone(rigcase.validated_row(bad, "radioss-crash"))
        self.assertIsNone(rigcase.validated_row(none, "radioss-crash"))
        self.assertIsNone(rigcase.validated_row(None, "radioss-crash"))


if __name__ == "__main__":
    unittest.main()
