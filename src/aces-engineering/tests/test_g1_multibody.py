"""Tier-3 multibody wiring: the spec's `multibody:` block validation, the block-to-case mapping, the vendored
iftomm reference trajectories, the simlab experiment, and the rig dispatch (fake MCP client). The solver's own
validation lives in cudax-sim-runtime (docs/validation/exudyn-20261014); here we pin the golden side of the
contract. stdlib unittest."""
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

from aces_engineering.golden import demo, multibody, spec  # noqa: E402
from aces_engineering.golden import rig as rig_mod  # noqa: E402

BASE = Path(tempfile.gettempdir())


def resolve(raw):
    return spec.resolve(raw, base_dir=BASE, overrides={}, gpu_usable=False)


def mb_spec(**block):
    raw = demo.spec("root-mount")
    raw["multibody"] = dict(block) if block else {"mode": "iftomm_slider_crank"}
    return raw


class MultibodySpec(unittest.TestCase):
    def test_block_validates_with_defaults(self):
        eff = resolve(mb_spec())
        m = eff["multibody"]
        self.assertEqual(m["mode"], "iftomm_slider_crank")
        self.assertEqual(m["end_s"], 1.0)
        self.assertEqual(m["time_step_s"], 1e-3)
        self.assertIsNone(m["script"])

    def test_no_block_means_no_multibody_stage(self):
        eff = resolve(demo.spec("root-mount"))
        self.assertIsNone(eff.get("multibody"))

    def test_script_mode_needs_the_script(self):
        with self.assertRaises(spec.SpecError) as cm:
            resolve(mb_spec(mode="script"))
        self.assertIn("script", str(cm.exception))

    def test_step_count_guard(self):
        with self.assertRaises(spec.SpecError) as cm:
            resolve(mb_spec(end_s=1000.0, time_step_s=0.001))
        self.assertIn("2e5", str(cm.exception))

    def test_unknown_key_refused(self):
        with self.assertRaises(spec.SpecError):
            resolve(mb_spec(mode="iftomm_slider_crank", solver="mbdyn"))


class CaseMapping(unittest.TestCase):
    def test_iftomm_block_maps_to_solver_case(self):
        b = resolve(mb_spec())["multibody"]
        case = multibody.case_from_block(b)
        self.assertEqual(case, {"mode": "iftomm_slider_crank", "end_s": 1.0, "time_step_s": 1e-3,
                                "references": "refs"})

    def test_script_block_maps_to_basename(self):
        b = resolve(mb_spec(mode="script", script="models/my_mechanism.py"))["multibody"]
        case = multibody.case_from_block(b)
        self.assertEqual(case, {"mode": "script", "script": "my_mechanism.py"})


class WriteCase(unittest.TestCase):
    def test_iftomm_case_copies_the_references(self):
        block = spec.GoldenSpec(**mb_spec()).multibody.model_dump()
        with tempfile.TemporaryDirectory() as td:
            mb_dir = Path(td) / "multibody"
            doc = multibody.write_case(mb_dir, name="run1-multibody", block=block, base_dir=Path(td),
                                       project="p", owner="aaron")
            for fn in multibody.REFERENCE_FILES:
                self.assertTrue((mb_dir / "refs" / fn).is_file(), fn)
            self.assertEqual(len(doc["files"]), 3)
            exp = yaml.safe_load((mb_dir / "experiment.yaml").read_text(encoding="utf-8"))
            self.assertEqual((exp["physics"], exp["solver"]), ("multibody", "exudyn-multibody"))
            self.assertEqual(exp["params"]["references"], "refs")
            self.assertEqual(exp["tags"]["kind"], "golden-tier3-multibody")
            # the rig rewrite points the references at the spec store upload
            rig_exp = multibody.rig_experiment(doc, "run1-multibody")
            self.assertEqual(rig_exp["params"]["references"], "run1-multibody/refs")

    def test_script_case_copies_the_model(self):
        with tempfile.TemporaryDirectory() as td:
            model = Path(td) / "models" / "arm.py"
            model.parent.mkdir()
            model.write_text("# exudyn model; writes exudyn_results.json\n", encoding="utf-8")
            block = spec.GoldenSpec(**mb_spec(mode="script", script="models/arm.py")).multibody.model_dump()
            mb_dir = Path(td) / "run" / "multibody"
            doc = multibody.write_case(mb_dir, name="run1-multibody", block=block, base_dir=Path(td),
                                       project="p", owner="aaron")
            self.assertTrue((mb_dir / "arm.py").is_file())
            exp = yaml.safe_load((mb_dir / "experiment.yaml").read_text(encoding="utf-8"))
            self.assertEqual(exp["params"]["script"], "arm.py")
            self.assertIn("exudyn_results.json", exp["expected"])
            rig_exp = multibody.rig_experiment(doc, "run1-multibody")
            self.assertEqual(rig_exp["params"]["script"], "run1-multibody/arm.py")

    def test_missing_script_is_an_honest_error(self):
        block = spec.GoldenSpec(**mb_spec(mode="script", script="nope.py")).multibody.model_dump()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                multibody.write_case(Path(td) / "multibody", name="run1-multibody", block=block,
                                     base_dir=Path(td), project="p", owner="aaron")


# ------------------------------------------------------------------ rig dispatch (fake MCP client)

class FakeRig:
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


FINISHED_FILES = {
    "c000/status.json": json.dumps({"status": "FINISHED", "wall_s": 42.0, "error": None}).encode(),
    "c000/results.json": json.dumps({"comparison": {"Masoudi": {"crank_angle_rad": {"max_abs": 0.26}}},
                                     "inter_reference_spread": {}}).encode(),
    "c000/evidence.json": json.dumps({"schema": "test"}).encode(),
}


class RigDispatch(unittest.TestCase):
    def test_delivered_uploads_the_references(self):
        block = spec.GoldenSpec(**mb_spec()).multibody.model_dump()
        with tempfile.TemporaryDirectory() as td:
            mb_dir = Path(td) / "multibody"
            doc = multibody.write_case(mb_dir, name="run1-multibody", block=block, base_dir=Path(td),
                                       project="p", owner="aaron")
            rig = FakeRig(files=dict(FINISHED_FILES))
            rec = multibody.dispatch(rig, multibody_dir=mb_dir, doc=doc, exp_id="run1-multibody",
                                     principal="aaron", budget_s=60.0, poll_s=0.0)
            self.assertEqual(rec["state"], "delivered")
            self.assertEqual(rec["results"]["comparison"]["Masoudi"]["crank_angle_rad"]["max_abs"], 0.26)
            self.assertEqual(len(rig.uploads), 3)
            self.assertTrue(all(u.startswith("run1-multibody/refs/") for u in rig.uploads))
            self.assertEqual(rig.submitted[1], "run1-multibody")
            self.assertIn("run1-multibody/refs", rig.spec_text)
            self.assertTrue((mb_dir / "rig" / "c000" / "results.json").is_file())

    def test_failed_case_quotes_the_rig(self):
        block = spec.GoldenSpec(**mb_spec()).multibody.model_dump()
        with tempfile.TemporaryDirectory() as td:
            mb_dir = Path(td) / "multibody"
            doc = multibody.write_case(mb_dir, name="run1-multibody", block=block, base_dir=Path(td),
                                       project="p", owner="aaron")
            files = {"c000/status.json": json.dumps(
                {"status": "FAILED", "error": "exudyn-multibody: exudyn is not importable here"}).encode()}
            rig = FakeRig(statuses=[{"counts": {"finished": 0, "failed": 1}, "jobs": [{"state": "failed"}]}],
                          files=files)
            rec = multibody.dispatch(rig, multibody_dir=mb_dir, doc=doc, exp_id="run1-multibody",
                                     principal=None, budget_s=60.0, poll_s=0.0)
            self.assertEqual(rec["state"], "failed")
            self.assertIn("not importable", rec["reason"])

    def test_budget_spent_is_pending_and_never_cancelled(self):
        block = spec.GoldenSpec(**mb_spec()).multibody.model_dump()
        with tempfile.TemporaryDirectory() as td:
            mb_dir = Path(td) / "multibody"
            doc = multibody.write_case(mb_dir, name="run1-multibody", block=block, base_dir=Path(td),
                                       project="p", owner="aaron")
            rig = FakeRig(statuses=[{"counts": {"finished": 0, "failed": 0}, "jobs": [{"state": "running"}]}])
            rec = multibody.dispatch(rig, multibody_dir=mb_dir, doc=doc, exp_id="run1-multibody",
                                     principal=None, budget_s=0.0, poll_s=0.0)
            self.assertEqual(rec["state"], "pending")
            self.assertIn("not cancelled", rec["reason"])


if __name__ == "__main__":
    unittest.main()
