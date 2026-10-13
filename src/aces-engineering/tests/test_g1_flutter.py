"""Tier-3 flutter screen wiring: the spec's `aeroelasticity:` block validation, the block-to-case mapping,
and the vendored flutter-screen run in-process (the Goland benchmark — the solver's own validation lives in
cudax-sim-runtime; here we pin the golden side of the contract). stdlib unittest."""
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

from aces_engineering.golden import demo, flutter, spec  # noqa: E402

BASE = Path(tempfile.gettempdir())

GOLAND_BLOCK = {"semi_span_m": 6.096, "chord_m": 1.8288, "ea_frac": 0.33, "cg_frac": 0.43,
                "ei_Nm2": 9.77e6, "gj_Nm2": 0.987e6, "m_kg_m": 35.71, "i_ea_kg_m": 8.64,
                "speed_start_mps": 60.0, "speed_stop_mps": 180.0, "speed_step_mps": 5.0}


def resolve(raw):
    return spec.resolve(raw, base_dir=BASE, overrides={}, gpu_usable=False)


def aero_spec(**aero):
    raw = demo.spec("root-mount")
    raw["aeroelasticity"] = {**GOLAND_BLOCK, **aero}
    return raw


class AeroelasticitySpec(unittest.TestCase):
    def test_block_validates_with_defaults(self):
        eff = resolve(aero_spec())
        a = eff["aeroelasticity"]
        self.assertEqual(a["rho_kg_m3"], 1.225)
        self.assertEqual(a["n_bending"], 2)
        self.assertEqual(a["stations"], 200)

    def test_no_block_means_no_flutter_stage(self):
        eff = resolve(demo.spec("root-mount"))
        self.assertIsNone(eff.get("aeroelasticity"))

    def test_missing_section_property_refused(self):
        raw = aero_spec()
        del raw["aeroelasticity"]["gj_Nm2"]
        with self.assertRaises(spec.SpecError):
            resolve(raw)

    def test_ea_outside_chord_refused(self):
        with self.assertRaises(spec.SpecError):
            resolve(aero_spec(ea_frac=1.2))

    def test_inverted_sweep_refused(self):
        with self.assertRaises(spec.SpecError) as cm:
            resolve(aero_spec(speed_stop_mps=40.0))
        self.assertIn("speed_stop_mps", str(cm.exception))

    def test_degenerate_sweep_refused(self):
        with self.assertRaises(spec.SpecError):
            resolve(aero_spec(speed_start_mps=100.0, speed_stop_mps=105.0, speed_step_mps=5.0))

    def test_unknown_key_refused(self):
        with self.assertRaises(spec.SpecError):
            resolve(aero_spec(flutter_speed_mps=137.0))


class CaseMapping(unittest.TestCase):
    def test_block_maps_to_solver_case(self):
        b = resolve(aero_spec())["aeroelasticity"]
        case = flutter.case_from_block(b)
        self.assertEqual(case["wing"]["ei_Nm2"], 9.77e6)
        self.assertEqual(case["speeds"], {"start_mps": 60.0, "stop_mps": 180.0, "step_mps": 5.0})
        self.assertEqual(case["modes"], {"n_bending": 2, "n_torsion": 2})
        self.assertEqual(case["stations"], 200)


class RunScreen(unittest.TestCase):
    """The vendored solver in-process on the Goland block: golden's answer is the adapter's answer."""

    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp(prefix="g1-flutter-"))
        cls.block = spec.GoldenSpec(**aero_spec()).aeroelasticity.model_dump()
        cls.res = flutter.run_screen(cls.block, cls.out)

    def test_goland_flutter_speed(self):
        self.assertAlmostEqual(self.res["flutter_speed_mps"], 137.16, delta=0.05 * 137.16)

    def test_screen_note_and_mach_carried(self):
        self.assertIn("screen", self.res["screen_note"])
        self.assertIn("Mach", self.res["mach_note"])

    def test_artifact_written(self):
        p = self.out / "flutter" / "flutter.json"
        self.assertTrue(p.is_file())
        on_disk = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["flutter_speed_mps"], self.res["flutter_speed_mps"])
        self.assertIn("screen_note", on_disk)

    def test_no_crossing_block_is_honest(self):
        stiff = dict(self.block, gj_Nm2=self.block["gj_Nm2"] * 100.0,
                     speed_start_mps=20.0, speed_stop_mps=60.0, speed_step_mps=10.0)
        res = flutter.run_screen(stiff, Path(tempfile.mkdtemp(prefix="g1-flutter-stiff-")))
        self.assertIsNone(res["flutter_speed_mps"])
        self.assertIn("no damping crossing", res["flutter_note"])


if __name__ == "__main__":
    unittest.main()
