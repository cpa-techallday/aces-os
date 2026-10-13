"""Tier-3 multibody: template an exudyn-multibody case from the spec's `multibody:` block and dispatch it to the rig.

The solver (simlab.solvers.exudyn_multibody, registry row exudyn-multibody, image simlab-mbdyn:0.1, validation
docs/validation/exudyn-20261014 in cudax-sim-runtime) runs Exudyn 1.13.6 inside its image; there is no in-process
lane, so this stage writes the experiment and dispatches it exactly like the CFD stage does (golden/rigcase.py).

Two case modes:

  iftomm_slider_crank   the iftomm spatial rigid slider-crank benchmark; mechanism parameters are
                        benchmark-fixed. The three published reference trajectories (Masarati/MBDyn, Masoudi,
                        Chaojie) are carried in golden/data/mbdyn_iftomm (see its README for provenance and
                        licence), copied into the run directory next to the experiment, and uploaded with the
                        dispatch — the case is meaningless without them.

  script                a user Exudyn model .py (named in the spec, resolved against the spec file's directory),
                        copied into the run directory and run verbatim in the container. It must write
                        exudyn_results.json; running user python is the contract — say so in the hypothesis.

What the answer means, stated once and honestly: the benchmark comparison is a cross-code agreement check — the
published references disagree with each other (the solver reports the inter-reference spread beside its own
errors), so sitting inside that spread is the engineering statement, not a zero error.
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from . import rigcase

SCHEMA = "aces-golden-multibody/1"
RIG_SCHEMA = "aces-golden-multibody-rig/1"
SOLVER = "exudyn-multibody"

REFERENCE_FILES = ("Spatial_rigid_slider-crank_mechanism_Masarati.txt",
                   "Spatial_rigid_slider-crank_mechanism_Masoudi.txt",
                   "Spatial_rigid_slider-crank_mechanism_Chaojie.txt")
DATA_REFS = Path(__file__).resolve().parent / "data" / "mbdyn_iftomm"

TIER_NOTE = ("iftomm spatial rigid slider-crank benchmark (Exudyn, generalized-alpha index-2 Newmark): a "
             "cross-code agreement check against three published references that disagree with each other — "
             "inside the inter-reference spread is the statement, not a zero error")
SCRIPT_NOTE = ("user Exudyn model run verbatim in the simlab-mbdyn container (running user python is the "
               "contract); the model must write exudyn_results.json")


def case_from_block(b: Any) -> dict:
    """The spec block (a resolved dict) as the solver's case params. Paths are case-dir-relative: write_case
    copies the referenced files in, so the run-directory experiment is self-contained."""
    if b["mode"] == "script":
        return {"mode": "script", "script": Path(b["script"]).name}
    return {"mode": "iftomm_slider_crank", "end_s": b["end_s"], "time_step_s": b["time_step_s"],
            "references": "refs"}


def experiment_doc(case: dict, *, exp_id: str, project: str, owner: str, hypothesis: str | None = None) -> dict:
    """A simlab experiment (physics: multibody, solver: exudyn-multibody), as the spec's dict."""
    if case["mode"] == "script":
        desc = f"Golden Tier-3 multibody: user Exudyn model {case['script']}. {SCRIPT_NOTE}."
        outputs = ["model_results", "wall_s"]
        expected = "exudyn_results.json written by the model, with provenance"
        hypothesis = hypothesis or "the user Exudyn model runs to completion in the validated exudyn-multibody chain"
    else:
        desc = (f"Golden Tier-3 multibody: iftomm spatial rigid slider-crank, end {case['end_s']:g} s, "
                f"step {case['time_step_s']:g} s. {TIER_NOTE}.")
        outputs = ["comparison", "inter_reference_spread", "wall_s"]
        expected = "crank-angle and slider-position trajectories within the inter-reference spread"
        hypothesis = hypothesis or ("Exudyn's slider-crank trajectories sit inside the spread of the three "
                                    "published iftomm references")
    return {
        "id": exp_id, "project": project, "owner": owner,
        "physics": "multibody", "solver": SOLVER,
        "description": desc, "hypothesis": hypothesis, "expected": expected,
        "params": case,
        "outputs": outputs,
        "resources": {"precision": "float64", "vram_gb": 0, "cpu_threads": 1, "wall_h": 0.5},
        "tags": {"kind": "golden-tier3-multibody", "system": "golden"},
    }


def write_case(multibody_dir: Path, *, name: str, block: Any, base_dir: Path, project: str, owner: str,
               hypothesis: str | None = None) -> dict:
    """Write multibody/experiment.yaml, the referenced files (refs/ or the model script) and
    multibody/multibody_case.json; return the case document. `block` is the spec block as a model or dict;
    `base_dir` is the spec file's directory (the script path resolves against it)."""
    import yaml  # lazy: importing golden.multibody stays stdlib-only

    b = block if isinstance(block, dict) else block.model_dump()
    multibody_dir = Path(multibody_dir)
    multibody_dir.mkdir(parents=True, exist_ok=True)
    case = case_from_block(b)
    copied: list[str] = []
    if case["mode"] == "script":
        src = Path(b["script"])
        if not src.is_absolute():
            src = Path(base_dir) / src
        if not src.is_file():
            raise FileNotFoundError(f"multibody.script: the model {src} is not here")
        shutil.copy2(src, multibody_dir / case["script"])
        copied.append(case["script"])
    else:
        refs_dir = multibody_dir / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)
        for fn in REFERENCE_FILES:
            src = DATA_REFS / fn
            if not src.is_file():
                raise FileNotFoundError(f"multibody: the vendored iftomm reference {src} is missing — the "
                                        "benchmark is meaningless without it")
            shutil.copy2(src, refs_dir / fn)
        copied.extend(f"refs/{fn}" for fn in REFERENCE_FILES)
    exp = experiment_doc(case, exp_id=name, project=project, owner=owner, hypothesis=hypothesis)
    exp_path = multibody_dir / "experiment.yaml"
    with open(exp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(exp, f, sort_keys=False, allow_unicode=True)
    doc = {"schema": SCHEMA, "solver": SOLVER, "experiment": exp_path.name, "case": case,
           "files": copied,
           "tier_note": SCRIPT_NOTE if case["mode"] == "script" else TIER_NOTE}
    with open(multibody_dir / "multibody_case.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    doc["experiment_doc"] = exp              # in-memory only: dispatch rewrites its paths for the spec store
    return doc


def rig_experiment(doc: dict, exp_id: str) -> dict:
    """The run-dir experiment rewritten for the rig's spec store: the refs/ script uploads sit next to the spec
    (<store>/<principal>/<exp_id>/...), so params point there."""
    exp = copy.deepcopy(doc["experiment_doc"])
    exp["id"] = exp_id
    if exp["params"]["mode"] == "script":
        exp["params"]["script"] = f"{exp_id}/{exp['params']['script']}"
    else:
        exp["params"]["references"] = f"{exp_id}/refs"
    return exp


def _uploads(doc: dict, multibody_dir: Path, exp_id: str) -> list[tuple[str, bytes]]:
    multibody_dir = Path(multibody_dir)
    if doc["case"]["mode"] == "script":
        return [(f"{exp_id}/{doc['case']['script']}", (multibody_dir / doc["case"]["script"]).read_bytes())]
    return [(f"{exp_id}/refs/{fn}", (multibody_dir / "refs" / fn).read_bytes()) for fn in REFERENCE_FILES]


def dispatch(client: Any, *, multibody_dir: Path, doc: dict, exp_id: str, principal: str | None,
             budget_s: float, lease_wait_s: int = rigcase.LEASE_WAIT_S, poll_s: float = rigcase.POLL_S,
             progress: Any = None) -> dict:
    """Upload the case's files and queue it on the rig (docker, the solver's registered image), wait within
    budget_s, and pull the case's records back. Ends in one of golden/rigcase.py's honest states."""
    import yaml

    exp = rig_experiment(doc, exp_id)
    text = yaml.safe_dump(exp, sort_keys=False, allow_unicode=True)
    return rigcase.dispatch(client, case_dir=multibody_dir, exp_id=exp_id, principal=principal,
                            budget_s=budget_s, spec_text=text, schema=RIG_SCHEMA,
                            uploads=_uploads(doc, multibody_dir, exp_id),
                            lease_wait_s=lease_wait_s, poll_s=poll_s, progress=progress)
