"""step_export: the honest faceted STEP — cube round-trip of structure, cap refusal, ASCII and binary STL."""
from __future__ import annotations

import re
import struct
import tempfile
import unittest
from pathlib import Path

from aces_engineering.golden import step_export


def cube_stl(path: Path) -> None:
    v = [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0), (0, 0, 20), (20, 0, 20), (20, 20, 20), (0, 20, 20)]
    f = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    buf = bytearray(b"\0" * 80) + struct.pack("<I", len(f))
    for t in f:
        buf += struct.pack("<12fH", 0, 0, 0, *v[t[0]], *v[t[1]], *v[t[2]], 0)
    path.write_bytes(bytes(buf))


class TestFacetedStep(unittest.TestCase):
    def test_cube_structure(self):
        with tempfile.TemporaryDirectory() as td:
            stl = Path(td) / "cube.stl"
            cube_stl(stl)
            stats = step_export.write_faceted_step(stl, Path(td) / "cube.step", name="cube")
            self.assertEqual((stats["faces"], stats["vertices"], stats["edges"]), (12, 8, 18))
            text = (Path(td) / "cube.step").read_text(encoding="utf-8")
            ids = set(re.findall(r"#(\d+)=", text))
            refs = set(re.findall(r"#(\d+)(?=[\s,)])", text))
            self.assertFalse(refs - ids, "every reference must resolve")
            self.assertTrue(text.startswith("ISO-10303-21;"))
            self.assertTrue(text.rstrip().endswith("END-ISO-10303-21;"))
            self.assertIn("FACETED_BREP", text)
            self.assertEqual(text.count("FACE_SURFACE"), 12)
            self.assertIn("mesh-derived", text)  # the honesty label travels with the file

    def test_ascii_stl(self):
        with tempfile.TemporaryDirectory() as td:
            stl = Path(td) / "cube.stl"
            lines = ["solid cube"]
            v = [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0), (0, 0, 20), (20, 0, 20), (20, 20, 20), (0, 20, 20)]
            f = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 1, 5), (0, 5, 4),
                 (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
            for t in f:
                lines.append(" facet normal 0 0 0\n  outer loop")
                for i in t:
                    lines.append(f"   vertex {v[i][0]} {v[i][1]} {v[i][2]}")
                lines.append("  endloop\n endfacet")
            lines.append("endsolid")
            stl.write_text("\n".join(lines), encoding="utf-8")
            stats = step_export.write_faceted_step(stl, Path(td) / "cube.step", name="cube")
            self.assertEqual((stats["faces"], stats["vertices"]), (12, 8))

    def test_face_cap_refusal(self):
        with tempfile.TemporaryDirectory() as td:
            stl = Path(td) / "big.stl"
            # one quad header claiming cap+1 triangles without the bytes: not a valid binary STL, so write ASCII
            # cheap: patch the cap check by writing a small STL and shrinking the cap for the test
            cube_stl(stl)
            cap = step_export.FACE_CAP
            step_export.FACE_CAP = 4
            try:
                with self.assertRaises(ValueError) as cm:
                    step_export.write_faceted_step(stl, Path(td) / "big.step")
                self.assertIn("STL is the deliverable", str(cm.exception))
            finally:
                step_export.FACE_CAP = cap


if __name__ == "__main__":
    unittest.main()
