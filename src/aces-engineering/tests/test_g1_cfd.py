"""B1 unit tests for Tier-1 CFD templating: the spec's `flow:` block and golden.cfd's derived case, .ini and
simlab experiment. stdlib unittest; the cases here are template inputs, not solver results."""
from __future__ import annotations

import configparser
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

from aces_engineering.golden import cfd, demo, spec  # noqa: E402

BASE = Path(tempfile.gettempdir())
BBOX_MM = [0.0, -20.0, -10.0, 80.0, 20.0, 10.0]   # 80 x 40 x 20 mm part


def resolve(raw):
    return spec.resolve(raw, base_dir=BASE, overrides={}, gpu_usable=False)


def flow_spec(**flow):
    raw = demo.spec("root-mount")
    raw["flow"] = {"velocity_mps": 30.0, **flow}
    return raw


class FlowSpec(unittest.TestCase):
    def test_flow_block_validates_with_defaults(self):
        eff = resolve(flow_spec())
        f = eff["flow"]
        self.assertEqual(f["medium"], "air")
        self.assertEqual(f["precision"], "single")
        self.assertEqual(f["farfield"], [3.0, 9.0, 3.0])

    def test_no_flow_block_means_no_cfd(self):
        eff = resolve(demo.spec("root-mount"))
        self.assertIsNone(eff.get("flow"))

    def test_air_past_mach03_refused(self):
        with self.assertRaises(spec.SpecError) as cm:
            resolve(flow_spec(velocity_mps=150.0))
        self.assertIn("incompressible", str(cm.exception))

    def test_water_has_no_mach_gate(self):
        eff = resolve(flow_spec(velocity_mps=150.0, medium="water"))
        self.assertEqual(eff["flow"]["medium"], "water")

    def test_unknown_flow_key_refused(self):
        with self.assertRaises(spec.SpecError):
            resolve(flow_spec(turbulence_model="k-omega"))


class DerivedCase(unittest.TestCase):
    def setUp(self):
        self.flow = spec.GoldenSpec(**flow_spec()).flow

    def test_reynolds_and_scaling(self):
        d = cfd.derived_case(self.flow, BBOX_MM)
        # default reference length: x extent 80 mm = 0.08 m; air nu 1.48e-5
        self.assertAlmostEqual(d["reference_length_m"], 0.08)
        self.assertAlmostEqual(d["reynolds"], 30.0 * 0.08 / 1.48e-5, places=0)
        # mesh-unit scaling leaves Re identical
        self.assertAlmostEqual(d["u_mms"] * (d["reference_length_m"] * 1000.0) / d["nu_mm2s"],
                               d["reynolds"], places=0)

    def test_farfield_box_is_x_aligned(self):
        d = cfd.derived_case(self.flow, BBOX_MM)
        D_mm = 80.0
        self.assertAlmostEqual(d["box_mm"][0], 0.0 - 3.0 * D_mm)      # upstream of x0
        self.assertAlmostEqual(d["box_mm"][3], 80.0 + 9.0 * D_mm)     # downstream of x1
        self.assertAlmostEqual(d["box_mm"][1], 0.0 - 3.0 * D_mm)      # lateral about the bbox centre
        self.assertAlmostEqual(d["box_mm"][5], 0.0 + 3.0 * D_mm)

    def test_integrator_defaults_follow_the_validated_ratio(self):
        d = cfd.derived_case(self.flow, BBOX_MM)
        lc_m = 0.08 / 8.0
        self.assertAlmostEqual(d["dt_s"], lc_m / (12.0 * 30.0))
        self.assertAlmostEqual(d["pseudo_dt_s"], d["dt_s"] / 20.0)
        self.assertAlmostEqual(d["tend_s"], 20.0 * 0.08 / 30.0)

    def test_reference_area_default_is_labelled_approximation(self):
        d = cfd.derived_case(self.flow, BBOX_MM)
        self.assertAlmostEqual(d["reference_area_m2"], 40.0 * 20.0 / 1e6)
        self.assertIn("approximation", d["reference_area_note"])
        d2 = cfd.derived_case(self.flow, BBOX_MM)
        self.assertEqual(d2["reference_area_m2"], d["reference_area_m2"])

    def test_degenerate_bbox_refused(self):
        with self.assertRaises(ValueError):
            cfd.derived_case(self.flow, [0, 0, 0, 0, 1, 1])

    def test_honesty_note_present(self):
        d = cfd.derived_case(self.flow, BBOX_MM)
        self.assertIn("not a converged drag value", d["honesty"])


class TemplateIni(unittest.TestCase):
    def test_ini_parses_and_carries_the_flow(self):
        flow = spec.GoldenSpec(**flow_spec()).flow
        d = cfd.derived_case(flow, BBOX_MM)
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        cp.read_string(cfd.template_ini(d, "winner"))
        self.assertEqual(cp["backend"]["precision"], "single")
        self.assertEqual(cp["solver"]["system"], "ac-navier-stokes")
        self.assertAlmostEqual(float(cp["constants"]["nu"]), 1.48e-5 * 1e6)   # mm^2/s
        self.assertAlmostEqual(float(cp["constants"]["Uin"]), 30000.0)         # mm/s
        self.assertEqual(cp["soln-plugin-nancheck"]["nsteps"], "1")


class WriteCase(unittest.TestCase):
    def test_case_files_and_experiment(self):
        flow = spec.GoldenSpec(**flow_spec()).flow
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            step = root / "winner" / "winner.step"
            step.parent.mkdir()
            step.write_text("ISO-10303-21;\nEND-ISO-10303-21;", encoding="utf-8")
            cfd_dir = root / "cfd" / "winner"
            doc = cfd.write_case(cfd_dir, name="g-cfd-test", label="winner", flow=flow,
                                 step_path=step, bbox_mm=BBOX_MM, project="p", owner="aaron")
            ini = cfd_dir / "case" / "winner.ini"
            exp_p = cfd_dir / "experiment.yaml"
            self.assertTrue(ini.is_file() and exp_p.is_file() and (cfd_dir / "cfd_case.json").is_file())
            exp = yaml.safe_load(exp_p.read_text(encoding="utf-8"))
            self.assertEqual((exp["physics"], exp["solver"]), ("cfd", "pyfr"))
            p = exp["params"]
            self.assertEqual(p["case_dir"], "case")
            self.assertEqual(p["mesh"]["from_step"], "../../winner/winner.step")
            self.assertEqual(p["force_bc"], "wall")
            self.assertEqual(p["rho"], 1.0)   # kinematic normalisation, documented in the module docstring
            self.assertAlmostEqual(p["D"], doc["reference_length_m"] * 1000.0)
            self.assertAlmostEqual(p["ref_area"], doc["reference_area_m2"] * 1e6)
            self.assertEqual(doc["schema"], "aces-golden-cfd/1")
            self.assertEqual(json.loads((cfd_dir / "cfd_case.json").read_text())["reynolds"], doc["reynolds"])


if __name__ == "__main__":
    unittest.main()
