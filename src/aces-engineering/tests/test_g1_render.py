"""B1 unit tests: the winner export (byte copy of the solved STL, faceted STEP under the face cap) and every render
(iso, stress on the skin, thumbnail, gallery, cutaway, winner figures) written as non-empty PNGs. stdlib unittest;
test inputs only."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parent / "lib", _HERE.parent / "vendor", _HERE):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.append(str(_p))

from aces_engineering import golden  # noqa: E402
from aces_engineering.golden import case as gcase, export, render, step_export  # noqa: E402

from test_g1_case import case_dict, make_run  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n"


class Renders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.rd, cls.eff, cls.dom, cls.exp = make_run(Path(cls.td.name))
        cls.oc = gcase.run_cases(cls.rd, cls.eff, cls.dom, cls.exp,
                                 [case_dict("v001", "verify", cls.eff, volfrac=0.66, rmin_mm=7.0, round=1)],
                                 pool=None, progress=golden.NullProgress(), kind="design")[0]

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def assertPng(self, p):
        p = Path(p)
        self.assertTrue(p.is_file(), p)
        data = p.read_bytes()
        self.assertGreater(len(data), 2000, p)
        self.assertEqual(data[:8], PNG, p)

    def test_export_then_winner_figures(self):
        self.assertEqual(self.oc["status"], "FINISHED", self.oc["error"])
        doc = export.export_winner(self.rd, "v001", self.eff)
        stl = self.rd.path("winner", "winner.stl")
        self.assertEqual(stl.read_bytes(), self.rd.path("v001", "part.stl").read_bytes())
        self.assertEqual(doc["stl"]["path"], "winner/winner.stl")
        self.assertEqual(doc["stl"]["sha256"], golden.sha256_file(stl))
        # the faceted STEP ships when the mesh is under FACE_CAP; either way the record says what happened
        self.assertEqual(doc["step"]["written"], doc["stl"]["faces"] <= step_export.FACE_CAP)
        if doc["step"]["written"]:
            sp = self.rd.path(doc["step"]["path"])
            self.assertEqual(doc["step"]["path"], "winner/winner.step")
            self.assertEqual(doc["step"]["sha256"], golden.sha256_file(sp))
            self.assertEqual(doc["step"]["faces"], doc["stl"]["faces"])
            self.assertIn("FACETED_BREP", sp.read_text(errors="replace"))
            self.assertIn("not a B-rep", doc["step"]["note"])
        else:
            self.assertIn("reason", doc["step"])
            self.assertFalse(any(self.rd.path("winner").glob("*.step")))
        self.assertEqual(doc["density"]["path"], "v001/generator/density.npy")
        self.assertEqual(doc["mass_kg"], self.oc["results"]["mass_kg"])
        self.assertEqual((doc["stl"]["watertight"], doc["stl"]["solids"]), (True, 1))
        renders = render.winner_figures(self.rd, "winner", "v001", self.dom, self.eff)
        for name in ("iso", "stress", "density_slices", "vm_slices", "disp_slices"):
            self.assertIn(name, renders)
            self.assertPng(self.rd.path(renders[name]))
        self.assertEqual(renders["iso"], "winner/winner_iso.png")
        self.assertEqual(golden.read_json(self.rd.path("winner", "winner.json"))["renders"], renders)

    def test_thumb_gallery_and_cutaway(self):
        stl = self.rd.path("v001", "part.stl")
        out = Path(self.td.name) / "renders"
        self.assertPng(render.thumb(stl, out / "thumb.png", label="v001 · test"))
        self.assertPng(render.cutaway(stl, out / "cut.png", title="cutaway test"))
        self.assertPng(render.iso(stl, out / "iso.png", title="iso test", interfaces=self.dom["patch_keep_ins"],
                                  grade="preview-grade"))
        items = [{"case_id": "v001", "png": str(out / "thumb.png"), "label": "v001 · ok", "ok": True},
                 {"case_id": "d001", "png": None, "label": "d001 · multi-body", "ok": False}]
        self.assertPng(render.gallery(items, out / "gallery.png", cols=5, highlight="v001"))

    def test_style_tokens(self):
        self.assertEqual(render.STYLE["background"], "#061225")
        self.assertEqual(render.STYLE["interface"], "#f59e0b")
        self.assertEqual(tuple(render.STYLE["stress_ramp"]), ("#1d3b6a", "#22d3ee", "#dbe7f3", "#f59e0b", "#ff4d2e"))

    def test_export_without_a_part_says_so(self):
        golden.dump_json(self.rd.path("v009", "status.json"), {"status": "FAILED", "design_status": "multi-body"})
        doc = export.export_winner(self.rd, "v009", self.eff, label="candidate")
        self.assertIsNone(doc["stl"])
        self.assertIn("multi-body", doc["note"])
        self.assertEqual(render.winner_figures(self.rd, "candidate", "v009", self.dom, self.eff), {})


if __name__ == "__main__":
    unittest.main()
