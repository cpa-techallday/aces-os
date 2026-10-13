"""Tier-3 crash: template a radioss-crash tube-crush case from the spec's `crash:` block and dispatch it to the rig.

The solver (simlab.solvers.radioss_crash, registry row radioss-crash, image simlab-radioss:0.1, validation
docs/validation/radioss-20261014 in cudax-sim-runtime) runs OpenCourant — the community fork of OpenRadioss —
inside its image; there is no in-process lane, so this stage writes the experiment and dispatches it exactly like
the CFD stage does (golden/rigcase.py). The generated deck is parametric: the adapter builds the square-tube mesh,
the LAW2 material, the TYPE7 self-contact and the two rigid walls in-container, so the only thing uploaded is the
experiment spec itself.

What the answer means, stated once and honestly: the tube is NOT the golden run's optimised part — crushing the
winner's organic mesh wants a mapped-mesh deck (solver mode=deck), which is a future slice. The Wierzbicki-
Abramowicz comparison the solver reports is a screen-level reference: the closed form itself carries ~20% scatter
against experiments, and with hardening present it underpredicts — treat it as a bound, not a verdict.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from . import rigcase

SCHEMA = "aces-golden-crash/1"
RIG_SCHEMA = "aces-golden-crash-rig/1"
SOLVER = "radioss-crash"

TIER_NOTE = ("parametric square-tube axial crush (radioss-crash / OpenCourant, LAW2, frictionless TYPE7 "
             "self-contact, free-flight rigid-wall impactor): NOT the optimised part — crushing the winner's "
             "mesh is a future deck-mode slice; the Wierzbicki-Abramowicz comparison is a screen-level bound "
             "(~20% scatter against experiments itself), not a verdict")


def case_from_block(b: Any) -> dict:
    """The spec block (a resolved dict) as the solver's case params (SI; the adapter converts to kg-mm-ms)."""
    case = {"mode": "tube",
            "geometry": {"width_m": b["width_m"], "length_m": b["length_m"], "thickness_m": b["thickness_m"]},
            "material": dict(b["material"]),
            "impactor": dict(b["impactor"]),
            "end_s": b["end_s"]}
    if b.get("mesh_size_m"):
        case["mesh"] = {"size_m": b["mesh_size_m"]}
    if b.get("wierzbicki_abramowicz", True):
        case["reference"] = {"kind": "wierzbicki_abramowicz"}
    return case


def experiment_doc(case: dict, *, exp_id: str, project: str, owner: str, hypothesis: str | None = None) -> dict:
    """A simlab experiment (physics: crash, solver: radioss-crash) for the block's tube, as the spec's dict."""
    g, m, i = case["geometry"], case["material"], case["impactor"]
    ke_j = 0.5 * i["mass_kg"] * i["velocity_mps"] ** 2
    return {
        "id": exp_id, "project": project, "owner": owner,
        "physics": "crash", "solver": SOLVER,
        "description": (f"Golden Tier-3 crash: axial crush of a square thin-walled tube "
                        f"{g['width_m'] * 1e3:g} x {g['length_m'] * 1e3:g} x {g['thickness_m'] * 1e3:g} mm, "
                        f"LAW2 steel-like material (E {m['E_Pa']:g} Pa, yield {m['yield_Pa']:g} Pa, "
                        f"rho {m['rho_kg_m3']:g} kg/m^3), impactor {i['mass_kg']:g} kg at "
                        f"{i['velocity_mps']:g} m/s ({ke_j:g} J), end {case['end_s']:g} s. {TIER_NOTE}."),
        "hypothesis": hypothesis or "the mean crush force of the spec'd tube is computable by the validated "
                                   "radioss-crash chain",
        "expected": "a finite impactor force history; crush.p_mean_kN with the energy balance and, when "
                    "reference is set, the Wierzbicki-Abramowicz closed form beside it",
        "params": case,
        "outputs": ["crush", "energy", "reference", "wall_s"],
        "resources": {"precision": "float64", "vram_gb": 0, "cpu_threads": 4, "wall_h": 1},
        "tags": {"kind": "golden-tier3-crash", "system": "golden"},
    }


def write_case(crash_dir: Path, *, name: str, block: Any, project: str, owner: str,
               hypothesis: str | None = None) -> dict:
    """Write crash/experiment.yaml and crash/crash_case.json; return the case document."""
    import yaml  # lazy: importing golden.crash stays stdlib-only

    crash_dir = Path(crash_dir)
    crash_dir.mkdir(parents=True, exist_ok=True)
    case = case_from_block(block)
    exp = experiment_doc(case, exp_id=name, project=project, owner=owner, hypothesis=hypothesis)
    exp_path = crash_dir / "experiment.yaml"
    with open(exp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(exp, f, sort_keys=False, allow_unicode=True)
    doc = {"schema": SCHEMA, "solver": SOLVER, "experiment": exp_path.name, "case": case, "tier_note": TIER_NOTE}
    with open(crash_dir / "crash_case.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    doc["experiment_doc"] = exp              # in-memory only: dispatch rewrites its id for the rig's spec store
    return doc


def rig_experiment(doc: dict, exp_id: str) -> dict:
    """The run-dir experiment rewritten for the rig's spec store. The tube deck is generated in-container, so
    only the id changes — there are no local paths to rewrite."""
    exp = copy.deepcopy(doc["experiment_doc"])
    exp["id"] = exp_id
    return exp


def dispatch(client: Any, *, crash_dir: Path, doc: dict, exp_id: str, principal: str | None,
             budget_s: float, lease_wait_s: int = rigcase.LEASE_WAIT_S, poll_s: float = rigcase.POLL_S,
             progress: Any = None) -> dict:
    """Queue the tube-crush case on the rig (docker, the solver's registered image), wait within budget_s, and
    pull the case's records back. Ends in one of golden/rigcase.py's honest states."""
    import yaml

    exp = rig_experiment(doc, exp_id)
    text = yaml.safe_dump(exp, sort_keys=False, allow_unicode=True)
    return rigcase.dispatch(client, case_dir=crash_dir, exp_id=exp_id, principal=principal, budget_s=budget_s,
                            spec_text=text, schema=RIG_SCHEMA, uploads=(),
                            lease_wait_s=lease_wait_s, poll_s=poll_s, progress=progress)
