"""exudyn-multibody: multibody-dynamics tier on Exudyn (University of Innsbruck).

Exudyn 1.13.6 (pinned manylinux wheel, sha256 checked at image build; license BSD-3-clause-
equivalent "EXUDYN General License 1.0", text baked into the image). MBDyn itself is
unpackaged in bookworm/jammy/noble (checked 2026-10-14), so this tier runs Exudyn.

Two case modes:

  iftomm_slider_crank   the iftomm spatial rigid slider-crank benchmark (the standard
                        multibody-dynamics validation problem; mechanism parameters are
                        benchmark-fixed, not modelling decisions — see
                        https://www.iftomm-multibody.org/benchmark/). The model build
                        follows Exudyn's own TestModels/sliderCrank3Dtest.py (markers +
                        GenericJoint axis masks, generalized-alpha index-2/Newmark,
                        spectral radius 0.5). The crank-angle and slider-position
                        trajectories are compared against the three published reference
                        solutions vendored under cases/mbdyn_iftomm/ (Masarati/MBDyn,
                        Masoudi, Chaojie — same files the Exudyn docs verify against).
                        The adapter reports max/RMS errors per reference plus the
                        inter-reference spread; it never decides pass/fail (that belongs
                        to the registry record).

  script                a user-supplied python model file, run verbatim in the container
                        with exudyn importable. The script must write exudyn_results.json
                        in its working directory; the adapter loads it, adds provenance,
                        and refuses if the file is absent. Running user python is the
                        contract — say so in the case description.

The benchmark error metrics and reference interpolation are pure python (numpy) and
unit-tested on the workstation; the solver itself only runs inside the simlab-mbdyn image.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import time

import numpy as np

SOLVER = "exudyn-multibody"


class InputError(ValueError):
    pass


class SolverError(RuntimeError):
    """Exudyn or the model script failed; the message quotes the real error."""


# ---------------------------------------------------------------------------
# iftomm benchmark constants (benchmark-fixed; NOT modelling decisions — do not
# expose them as case params). Source: TestModels/sliderCrank3Dtest.py and the
# iftomm benchmark problem statement.

BENCH = {
    "g": [0.0, 0.0, -9.81],
    "lAB": 0.08,          # crank length [m]
    "lBC": 0.3,           # conrod length [m]
    "theta0": 0.0,
    "omega0": [6.0, 0.0, 0.0],
    "zA": 0.12, "yA": 0.1,
    "mAB": 0.12, "mBC": 0.5, "mSlider": 2.0,
    "iAB": [0.0001, 0.00001, 0.0001],
    "iBC": [0.0004, 0.004, 0.004],
    "iSlider": [0.0001, 0.0001, 0.0001],
    # consistent initial velocities (from the initial value problem with constant
    # crank angular velocity; ~10 digits, as published in the benchmark model)
    "v1Init": [0.12, -0.24, 0.0],
    "omega1Init": [1.6941176470530785, -0.8470588235366621, 0.705882352947701],
    "v2Init": [0.24, 0.0, 0.0],
    "omega2Init": [0.0, 0.0, 0.0],
}

REFERENCES = {
    "Masarati": "Spatial_rigid_slider-crank_mechanism_Masarati.txt",
    "Masoudi": "Spatial_rigid_slider-crank_mechanism_Masoudi.txt",
    "Chaojie": "Spatial_rigid_slider-crank_mechanism_Chaojie.txt",
}


# ---------------------------------------------------------------------------
# reference loading + error metrics (pure; unit-tested)


def load_reference(path: str) -> np.ndarray:
    """Reference trajectory file -> array [t, slider_x, crank_angle]; commas or tabs."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read().replace(",", " ")
    rows = []
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) >= 3:
            try:
                rows.append([float(parts[0]), float(parts[1]), float(parts[2])])
            except ValueError:
                continue
    if not rows:
        raise InputError(f"exudyn-multibody: reference {path} has no data rows")
    a = np.array(rows)
    if np.any(np.diff(a[:, 0]) <= 0):
        raise InputError(f"exudyn-multibody: reference {path} times are not increasing")
    return a


def trajectory_errors(t_sim, x_sim, t_ref, x_ref):
    """max/RMS abs error of the simulated trajectory against the reference, the SIMULATION
    linearly interpolated onto the reference sample times (the references are the coarse,
    irregular grids; interpolating them would alias the oscillatory signal). Reference
    samples outside the simulated span are excluded (and counted)."""
    t_ref = np.asarray(t_ref)
    x_ref = np.asarray(x_ref)
    t_sim = np.asarray(t_sim)
    x_sim = np.asarray(x_sim)
    mask = (t_ref >= t_sim[0]) & (t_ref <= t_sim[-1])
    if int(mask.sum()) < 10:
        raise InputError("exudyn-multibody: fewer than 10 reference samples overlap the simulated span")
    interp = np.interp(t_ref[mask], t_sim, x_sim)
    d = interp - x_ref[mask]
    return {"max_abs": float(np.max(np.abs(d))), "rms": float(np.sqrt(np.mean(d * d))),
            "n": int(mask.sum()), "n_excluded": int((~mask).sum())}


# ---------------------------------------------------------------------------
# the iftomm model (runs in-container; kept close to TestModels/sliderCrank3Dtest.py)


def run_iftomm(end_s: float, n_steps: int, work: str) -> dict:
    import exudyn as exu
    from exudyn.utilities import (RigidBodyInertia, MarkerBodyRigid, GenericJoint,
                                  SensorNode, VObjectJointGeneric, GramSchmidt)

    b = BENCH
    SC = exu.SystemContainer()
    mbs = SC.AddSystem()

    xD = math.sqrt(b["lBC"] ** 2 - b["yA"] ** 2 - (b["zA"] + b["lAB"]) ** 2)
    pA = [0.0, b["yA"], b["zA"]]
    pB = [0.0, b["yA"], b["zA"] + b["lAB"]]
    pC = [xD, 0.0, 0.0]
    vCB = np.array(pC) - np.array(pB)
    xAxis1, zAxis1, _ = GramSchmidt(vCB, pA)
    yAxis1 = -np.cross(xAxis1, zAxis1)
    rotMatBC = np.array([xAxis1, yAxis1, zAxis1]).transpose()

    nodeType = exu.NodeType.RotationEulerParameters
    d0 = mbs.CreateRigidBody(referencePosition=pA, initialAngularVelocity=b["omega0"],
                             inertia=RigidBodyInertia(mass=b["mAB"], inertiaTensor=np.diag(b["iAB"])),
                             gravity=b["g"], nodeType=nodeType, show=False, returnDict=True)
    d1 = mbs.CreateRigidBody(referencePosition=0.5 * (np.array(pB) + np.array(pC)),
                             referenceRotationMatrix=rotMatBC,
                             initialVelocity=b["v1Init"], initialAngularVelocity=b["omega1Init"],
                             inertia=RigidBodyInertia(mass=b["mBC"], inertiaTensor=np.diag(b["iBC"])),
                             gravity=b["g"], nodeType=nodeType, show=False, returnDict=True)
    d2 = mbs.CreateRigidBody(referencePosition=pC, initialVelocity=b["v2Init"],
                             inertia=RigidBodyInertia(mass=b["mSlider"],
                                                      inertiaTensor=np.diag(b["iSlider"])),
                             nodeType=nodeType, show=False, returnDict=True)
    n0, b0 = d0["nodeNumber"], d0["bodyNumber"]
    n1, b1 = d1["nodeNumber"], d1["bodyNumber"]
    n2, b2 = d2["nodeNumber"], d2["bodyNumber"]

    oGround = mbs.CreateGround(show=False)
    mGA = mbs.AddMarker(MarkerBodyRigid(bodyNumber=oGround, localPosition=pA))
    mGD = mbs.AddMarker(MarkerBodyRigid(bodyNumber=oGround, localPosition=[0, 0, 0]))
    mCA = mbs.AddMarker(MarkerBodyRigid(bodyNumber=b0))
    mCB = mbs.AddMarker(MarkerBodyRigid(bodyNumber=b0, localPosition=[0, 0, b["lAB"]]))
    mRB = mbs.AddMarker(MarkerBodyRigid(bodyNumber=b1, localPosition=[-0.5 * b["lBC"], 0, 0]))
    mRC = mbs.AddMarker(MarkerBodyRigid(bodyNumber=b1, localPosition=[0.5 * b["lBC"], 0, 0]))
    mS = mbs.AddMarker(MarkerBodyRigid(bodyNumber=b2))
    mbs.AddObject(GenericJoint(markerNumbers=[mGA, mCA], constrainedAxes=[1, 1, 1, 0, 1, 1]))
    mbs.AddObject(GenericJoint(markerNumbers=[mGD, mS], constrainedAxes=[0, 1, 1, 1, 1, 1]))
    mbs.AddObject(GenericJoint(markerNumbers=[mCB, mRB], constrainedAxes=[1, 1, 1, 0, 0, 0]))
    mbs.AddObject(GenericJoint(markerNumbers=[mS, mRC], constrainedAxes=[1, 1, 1, 0, 0, 1]))

    sAngle = mbs.AddSensor(SensorNode(nodeNumber=n0, storeInternal=True,
                                      outputVariableType=exu.OutputVariableType.Rotation))
    sPos = mbs.AddSensor(SensorNode(nodeNumber=n2, storeInternal=True,
                                    outputVariableType=exu.OutputVariableType.Position))

    mbs.Assemble()
    sim = exu.SimulationSettings()
    sim.timeIntegration.newton.useModifiedNewton = False
    sim.timeIntegration.numberOfSteps = int(n_steps)
    sim.timeIntegration.endTime = float(end_s)
    sim.timeIntegration.verboseMode = 0
    sim.timeIntegration.generalizedAlpha.useIndex2Constraints = True
    sim.timeIntegration.generalizedAlpha.useNewmark = True
    sim.timeIntegration.generalizedAlpha.spectralRadius = 0.5
    sim.timeIntegration.generalizedAlpha.computeInitialAccelerations = True
    sim.solutionSettings.writeSolutionToFile = False
    sim.solutionSettings.sensorsWritePeriod = float(end_s) / int(n_steps)  # store every step
    sim.displayStatistics = False
    mbs.SolveDynamic(sim)

    rot = mbs.GetSensorStoredData(sAngle)
    pos = mbs.GetSensorStoredData(sPos)
    return {"t": rot[:, 0].tolist(), "crank_angle_rad": np.unwrap(rot[:, 1]).tolist(),
            "slider_x_m": pos[:, 1].tolist()}


# ---------------------------------------------------------------------------
# case validation


def validate_case(case: dict, spec) -> dict:
    mode = str(case.get("mode", "iftomm_slider_crank"))
    if mode not in ("iftomm_slider_crank", "script"):
        raise InputError(f"exudyn-multibody: mode must be 'iftomm_slider_crank' or 'script', got {mode!r}")
    p = {"mode": mode, "timeout_s": float(case.get("timeout_s", 1800.0))}
    if mode == "script":
        script = case.get("script")
        if not script:
            raise InputError("exudyn-multibody: script mode needs 'script' (path of the model .py, "
                             "relative to the spec); it must write exudyn_results.json")
        script = spec.resolve(str(script))
        if not os.path.isfile(script):
            raise InputError(f"exudyn-multibody: the script {script} is not here")
        p["script"] = script
        return p
    p["end_s"] = float(case.get("end_s", 1.0))
    p["time_step_s"] = float(case.get("time_step_s", 1e-3))
    if p["end_s"] <= 0 or p["time_step_s"] <= 0:
        raise InputError("exudyn-multibody: end_s and time_step_s must be > 0")
    if p["end_s"] / p["time_step_s"] > 2e5:
        raise InputError(f"exudyn-multibody: end_s/time_step_s = {p['end_s'] / p['time_step_s']:g} steps; "
                         "above 2e5 this is a modelling decision to revisit, not a default")
    ref_dir = case.get("references")
    if not ref_dir:
        raise InputError("exudyn-multibody: 'references' (dir with the vendored iftomm trajectories) "
                         "is missing — the benchmark is meaningless without them")
    ref_dir = spec.resolve(str(ref_dir))
    refs = {}
    for name, fn in REFERENCES.items():
        path = os.path.join(ref_dir, fn)
        if not os.path.isfile(path):
            raise InputError(f"exudyn-multibody: the {name} reference {path} is not here")
        refs[name] = path
    p["references"] = refs
    return p


# ---------------------------------------------------------------------------


class Adapter:
    name = SOLVER

    def inputs(self, case: dict, spec) -> list:
        mode = str(case.get("mode", "iftomm_slider_crank"))
        if mode == "script" and case.get("script"):
            return [spec.resolve(str(case["script"]))]
        if mode == "iftomm_slider_crank" and case.get("references"):
            d = spec.resolve(str(case["references"]))
            return [os.path.join(d, fn) for fn in REFERENCES.values()]
        return []

    def run(self, case: dict, spec, run_dir: str) -> dict:
        t0 = time.time()
        p = validate_case(case, spec)
        try:
            import exudyn as exu  # noqa: F401 — the tier refuses to run anywhere else
        except ImportError as e:
            raise SolverError("exudyn-multibody: exudyn is not importable here — this tier runs "
                              "inside the simlab-mbdyn image; refusing to fake it") from e
        import exudyn as exu
        work = os.path.join(os.path.abspath(run_dir), "exudyn")
        os.makedirs(work, exist_ok=True)

        if p["mode"] == "script":
            import shutil
            dst = os.path.join(work, os.path.basename(p["script"]))
            shutil.copy2(p["script"], dst)
            t1 = time.time()
            with open(os.path.join(work, "script.log"), "w", encoding="utf-8") as log:
                rc = subprocess.run(["python3", dst], cwd=work, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=p["timeout_s"]).returncode
            out_json = os.path.join(work, "exudyn_results.json")
            if rc != 0 or not os.path.isfile(out_json):
                tail = ""
                with open(os.path.join(work, "script.log"), encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-800:]
                raise SolverError(f"exudyn-multibody: the model script failed rc={rc} or wrote no "
                                  f"exudyn_results.json; log tail: {tail}")
            with open(out_json, encoding="utf-8") as f:
                model = json.load(f)
            results = {"solver": SOLVER, "mode": "script",
                       "exudyn": {"version": exu.__version__},
                       "model_results": model,
                       "artifacts": [dst, os.path.join(work, "script.log"), out_json],
                       "wall_s": round(time.time() - t0, 2)}
            with open(os.path.join(work, "exudyn.json"), "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
            return results

        traj = run_iftomm(p["end_s"], int(round(p["end_s"] / p["time_step_s"])), work)
        t = np.array(traj["t"])
        x = np.array(traj["slider_x_m"])
        th = np.array(traj["crank_angle_rad"])

        comparison = {}
        for name, path in p["references"].items():
            ref = load_reference(path)
            comparison[name] = {
                "slider_x_m": trajectory_errors(t, x, ref[:, 0], ref[:, 1]),
                "crank_angle_rad": trajectory_errors(t, th, ref[:, 0], ref[:, 2]),
                "reference_span_s": [float(ref[0, 0]), float(ref[-1, 0])],
                "reference_samples": int(ref.shape[0]),
            }
        # inter-reference spread (the references disagree with each other; say by how much)
        names = list(p["references"])
        spread = {}
        if len(names) >= 2:
            a = load_reference(p["references"][names[0]])
            for other in names[1:]:
                r = load_reference(p["references"][other])
                spread[f"{names[0]}_vs_{other}"] = {
                    "slider_x_m": trajectory_errors(a[:, 0], a[:, 1], r[:, 0], r[:, 1]),
                    "crank_angle_rad": trajectory_errors(a[:, 0], a[:, 2], r[:, 0], r[:, 2])}

        results = {"solver": SOLVER, "mode": "iftomm_slider_crank",
                   "benchmark": {"problem": "iftomm spatial rigid slider-crank",
                                 "url": "https://www.iftomm-multibody.org/benchmark/",
                                 "model": "Exudyn TestModels/sliderCrank3Dtest.py (markers + GenericJoint "
                                          "masks, generalized-alpha index-2 Newmark, spectral radius 0.5)",
                                 "parameters": {k: v for k, v in BENCH.items()}},
                   "exudyn": {"version": exu.__version__},
                   "run": {"end_s": p["end_s"], "time_step_s": p["time_step_s"],
                           "samples": int(len(t))},
                   "trajectory": traj,
                   "comparison": comparison,
                   "inter_reference_spread": spread,
                   "artifacts": [os.path.join(work, "exudyn.json")],
                   "wall_s": round(time.time() - t0, 2)}
        with open(os.path.join(work, "exudyn.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        return results
