"""Tier-3 aeroelasticity: run the vendored flutter-screen against the spec's `aeroelasticity:` block.

The solver (simlab.solvers.flutter_screen, registry row flutter-screen, validation docs/validation/flutter-20261013
in cudax-sim-runtime) is pure Python + numpy — the screen runs in-process in about a second, so there is no rig
dispatch and no docker lane here; the stage writes flutter/flutter.json into the run directory and its headline
numbers into the run record.

What the answer means, stated once and honestly: this is a SCREEN of a wing's section properties (EI, GJ, mass,
inertia, EA/CG positions) named in the spec — it is not coupled to the optimised part's geometry (extracting
section stiffness from the winner's mesh is a future slice, and the report says so). The aerodynamics are
incompressible: the result carries its own Mach number and the adapter's mach_note.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCREEN_NOTE = ("flutter-screen (p-k, strip theory + Theodorsen/Jones, uniform-cantilever modal basis): a section-"
               "property screen, not a coupled analysis of the optimised part; incompressible — the answer carries "
               "its own Mach number")


def case_from_block(b: Any) -> dict:
    """The spec block (a resolved dict) as the solver's case params."""
    return {"case_id": "flutter",
            "wing": {"semi_span_m": b["semi_span_m"], "chord_m": b["chord_m"], "ea_frac": b["ea_frac"],
                     "cg_frac": b["cg_frac"], "ei_Nm2": b["ei_Nm2"], "gj_Nm2": b["gj_Nm2"],
                     "m_kg_m": b["m_kg_m"], "i_ea_kg_m": b["i_ea_kg_m"], "rho_kg_m3": b["rho_kg_m3"]},
            "modes": {"n_bending": b["n_bending"], "n_torsion": b["n_torsion"]},
            "speeds": {"start_mps": b["speed_start_mps"], "stop_mps": b["speed_stop_mps"],
                       "step_mps": b["speed_step_mps"]},
            "stations": b["stations"]}


def run_screen(block: dict, out_dir: Path) -> dict:
    """Run the screen; write <out_dir>/flutter.json; return the solver's results plus the screen note."""
    from simlab.solvers import flutter_screen as fs   # vendored cudax-sim-runtime; numpy only
    case = case_from_block(block)
    work = out_dir / "flutter"
    results = fs.Adapter().run(case, None, str(out_dir))   # writes work/flutter.json itself
    results["screen_note"] = SCREEN_NOTE
    with open(work / "flutter.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    return results
