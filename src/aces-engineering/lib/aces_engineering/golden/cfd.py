"""Tier-1 CFD: template a PyFR case from the golden run's exported part and its `flow:` block.

The chain is the validated one (registry solver `pyfr`, image simlab-pyfr:0.3, validation 2026-10-05): gmsh meshes
box-minus-body around the exported STL with PyFR boundary names (inlet at x-min, outlet at x-max, farfield, wall),
the .ini is the sphere3d smoke deck with the flow's constants substituted, and the simlab experiment carries the
adapter's case params (D, U, rho, ref_area, force_bc) so Cd / Cl rms / Strouhal come out normalised.

The geometry uploaded is the winner's **STL**, not the faceted STEP: the STEP is an AP214 FACETED_BREP that OCC's
reader cannot sew into a solid (every CFD dispatch died at "no solids in winner.step"), while the rig's mesh stage
meshes an STL directly with gmsh's built-in kernel (box_minus_stl) — the STL is also the byte-exact source of the
STEP, so nothing is lost.

Units, stated once and honestly: the exported STL is in **mm** (golden is mm-N-MPa-kg), so the mesh and the PyFR
run are in mm — nu is scaled to mm^2/s and U to mm/s, which leaves Re identical (dimensionless) and makes the
adapter's force normalisation self-consistent with rho = 1 (Cd needs no density; SI drag force is recovered as
Cd * 0.5 * rho_kgm3 * velocity_mps^2 * reference_area_m2). Time stays in seconds.

What this is not: a converged drag value. The defaults trade wall time for a same-day answer (order 2, single
precision, dt from the validated sphere smoke's ratio); a number to engineer to wants the double-precision,
finer-mesh convergence rerun the report points at. And ac-navier-stokes is incompressible — the spec's Flow block
already refuses air past Mach 0.3.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MEDIA = {
    "air": {"nu_m2s": 1.48e-5, "rho_kgm3": 1.225,
            "source": "dry air, 15 C, sea level (nu 1.48e-5 m^2/s, rho 1.225 kg/m^3 — standard atmosphere)"},
    "water": {"nu_m2s": 1.00e-6, "rho_kgm3": 998.0,
              "source": "fresh water, 20 C (nu 1.00e-6 m^2/s, rho 998 kg/m^3); no cavitation modelled"},
}

TEMPLATE_NOTE = ("templated PyFR ac-NS case (incompressible, order {order}, {precision} precision, dt = lc_body/(12 U) "
                 "— the validated sphere3d smoke's ratio): an engineering estimate, not a converged drag value; "
                 "validation quality wants the finer-mesh double-precision rerun")

INI_TEMPLATE = """\
[backend]
precision = {precision}

[constants]
nu = {nu_mm2s:.6g}
Uin = {u_mms:.6g}
Vin = 0.0
Win = 0.0
Pc = 1.0
ac-zeta = 2.5

[solver]
system = ac-navier-stokes
order = {order}

[solver-time-integrator]
formulation = dual
scheme = sdirk33
pseudo-scheme = rk45
controller = none
pseudo-controller = local-pi
tstart = 0.0
tend = {tend_s:.6g}
dt = {dt_s:.17g}
pseudo-dt = {pseudo_dt_s:.6g}
pseudo-niters-min = 3
pseudo-niters-max = 12
pseudo-resid-norm = l2
pseudo-resid-tol = 1e-6
atol = 1e-6
pseudo-dt-max-mult = 2.5

[solver-dual-time-integrator-multip]
pseudo-dt-fact = 1.75
cycle = [(2, 1), (1, 1), (0, 2), (1, 1), (2, 4)]

[solver-interfaces]
riemann-solver = rusanov
ldg-beta = 0.5
ldg-tau = 0.1

[solver-interfaces-tri]
flux-pts = williams-shunn

[solver-elements-tet]
soln-pts = shunn-ham

[soln-plugin-nancheck]
nsteps = 1

[soln-plugin-pseudostats]
flushsteps = 20
file = residual.csv
header = true

[soln-plugin-writer]
dt-out = {writer_dt_s:.17g}
basedir = .
basename = {stem}-{{t:.2f}}

[soln-bcs-wall]
type = no-slp-wall

[soln-bcs-inlet]
type = ac-char-riem-inv
ac-zeta = 180
p = Pc
u = Uin
v = Vin
w = Win

[soln-bcs-outlet]
type = ac-char-riem-inv
ac-zeta = 180
p = Pc
u = Uin
v = Vin
w = Win

[soln-bcs-farfield]
type = ac-char-riem-inv
ac-zeta = 180
p = Pc
u = Uin
v = Vin
w = Win

[soln-ics]
u = Uin
v = Vin
w = Win
p = Pc
"""


def medium(flow: Any) -> dict:
    """Effective fluid properties: the named medium's presets unless the spec overrides them."""
    base = dict(MEDIA[flow.medium])
    if flow.nu_m2s is not None:
        base["nu_m2s"] = float(flow.nu_m2s)
        base["source"] += "; nu overridden by the spec"
    if flow.rho_kgm3 is not None:
        base["rho_kgm3"] = float(flow.rho_kgm3)
        base["source"] += "; rho overridden by the spec"
    return base


def derived_case(flow: Any, bbox_mm: list[float]) -> dict:
    """Everything the case needs, derived from the flow block and the part's bounding box (mm, part frame).

    bbox_mm is [x0, y0, z0, x1, y1, z1]. The farfield box follows the mesh stage's naming: inlet on x-min,
    outlet on x-max — the flow is +x by construction (see spec.Flow).
    """
    ext = [float(bbox_mm[i + 3]) - float(bbox_mm[i]) for i in range(3)]
    if min(ext) <= 0:
        raise ValueError(f"derived_case: degenerate bbox {bbox_mm}")
    D_m = float(flow.reference_length_m) if flow.reference_length_m else ext[0] / 1000.0
    A_m2 = float(flow.reference_area_m2) if flow.reference_area_m2 else ext[1] * ext[2] / 1e6
    A_note = None if flow.reference_area_m2 else "bbox frontal area (y x z extents) — an approximation of the projected area"
    med = medium(flow)
    nu, rho = med["nu_m2s"], med["rho_kgm3"]
    U = float(flow.velocity_mps)
    Re = U * D_m / nu
    up, down, lat = (float(x) for x in flow.farfield)
    cx = [(float(bbox_mm[i]) + float(bbox_mm[i + 3])) / 2.0 for i in range(3)]
    box_mm = [float(bbox_mm[0]) - up * D_m * 1000.0,
              cx[1] - lat * D_m * 1000.0,
              cx[2] - lat * D_m * 1000.0,
              float(bbox_mm[3]) + down * D_m * 1000.0,
              cx[1] + lat * D_m * 1000.0,
              cx[2] + lat * D_m * 1000.0]
    lc_body_m = float(flow.lc_body_m) if flow.lc_body_m else D_m / 8.0
    lc_far_m = float(flow.lc_far_m) if flow.lc_far_m else D_m
    dist_max_m = float(flow.dist_max_m) if flow.dist_max_m else 2.0 * D_m
    dt_s = float(flow.dt_s) if flow.dt_s else lc_body_m / (12.0 * U)
    tend_s = float(flow.tend_s) if flow.tend_s else 20.0 * D_m / U
    # PyFR refuses a writer dt-out that is not an integer multiple of dt ("Plugin call times must be
    # multiples of dt"): tend/20 almost never is, so snap it to the nearest multiple of dt_s
    writer_dt_s = dt_s * max(1, round(tend_s / 20.0 / dt_s))
    return {
        "velocity_mps": U, "nu_m2s": nu, "rho_kgm3": rho, "medium": med["source"],
        "reference_length_m": D_m, "reference_area_m2": A_m2, "reference_area_note": A_note,
        "reynolds": Re, "flow_direction": "+x (the mesh stage names inlet/outlet by the box's x faces)",
        "box_mm": box_mm, "lc_body_mm": lc_body_m * 1000.0, "lc_far_mm": lc_far_m * 1000.0,
        "dist_max_mm": dist_max_m * 1000.0,
        "nu_mm2s": nu * 1e6, "u_mms": U * 1000.0,
        "order": int(flow.order), "precision": str(flow.precision),
        "dt_s": dt_s, "pseudo_dt_s": dt_s / 20.0, "tend_s": tend_s, "writer_dt_s": writer_dt_s,
        "honesty": TEMPLATE_NOTE.format(order=flow.order, precision=flow.precision),
    }


def template_ini(d: dict, stem: str) -> str:
    """The validated sphere3d smoke deck with the flow's constants substituted."""
    return INI_TEMPLATE.format(precision=d["precision"], nu_mm2s=d["nu_mm2s"], u_mms=d["u_mms"],
                               order=d["order"], tend_s=d["tend_s"], dt_s=d["dt_s"],
                               pseudo_dt_s=d["pseudo_dt_s"], writer_dt_s=d["writer_dt_s"], stem=stem)


def experiment_doc(d: dict, *, exp_id: str, project: str, owner: str, label: str,
                   step_rel: str, case_dir_rel: str, hypothesis: str | None = None) -> dict:
    """A simlab experiment (physics: cfd, solver: pyfr) for the templated case, as the spec's dict."""
    return {
        "id": exp_id, "project": project, "owner": owner,
        "physics": "cfd", "solver": "pyfr",
        "description": (f"Golden Tier-1 CFD on {label}: PyFR ac-navier-stokes order {d['order']} "
                        f"({d['precision']}), Re {d['reynolds']:.4g}, {d['velocity_mps']:g} m/s "
                        f"{d['medium']}. {d['honesty']}."),
        "hypothesis": hypothesis or f"drag of {label} at Re {d['reynolds']:.4g} is measurable by the validated pyfr chain",
        "expected": "finite forces for the whole window; Cd_mean / Cl_rms / Strouhal with the templated-case caveat",
        "params": {
            "case_dir": case_dir_rel,
            "mesh": {"from_step": step_rel, "box": d["box_mm"],
                     "lc_body": d["lc_body_mm"], "lc_far": d["lc_far_mm"], "dist_max": d["dist_max_mm"]},
            "backend": "cuda", "precision": d["precision"],
            "tend": d["tend_s"], "dt": d["dt_s"],
            "pseudo_dt": d["pseudo_dt_s"], "pseudo_niters_min": 3, "pseudo_niters_max": 12,
            "nu": d["nu_mm2s"],
            "D": d["reference_length_m"] * 1000.0,   # mesh units (mm) — see module docstring
            "U": d["u_mms"],
            "rho": 1.0,                              # kinematic normalisation; SI force from Cd (see docstring)
            "ref_area": d["reference_area_m2"] * 1e6,
            "force_bc": "wall", "st_window": 0.5, "nancheck": 1,
        },
        "outputs": ["Re", "Cd_mean", "Cd_last", "Cl_rms", "St", "mesh_nodes", "wall_s_mesh", "wall_s_run"],
        "resources": {"precision": "float32" if d["precision"] == "single" else "float64", "vram_gb": 4},
        "tags": {"kind": "golden-tier1-cfd", "system": "golden"},
    }


def write_case(cfd_dir: Path, *, name: str, label: str, flow: Any, stl_path: Path, bbox_mm: list[float],
               project: str, owner: str, hypothesis: str | None = None) -> dict:
    """Write cfd/<label>/case/<label>.ini, the simlab experiment.yaml and cfd_case.json; return the case document.

    Paths inside the experiment are relative to the experiment file, as simlab's spec.resolve expects.
    """
    import yaml  # lazy: importing golden.cfd stays stdlib-only

    cfd_dir = Path(cfd_dir)
    case_dir = cfd_dir / "case"
    case_dir.mkdir(parents=True, exist_ok=True)
    d = derived_case(flow, bbox_mm)
    (case_dir / f"{label}.ini").write_text(template_ini(d, label), encoding="utf-8")
    stl_rel = _posix_rel(stl_path, cfd_dir)
    exp = experiment_doc(d, exp_id=name, project=project, owner=owner, label=label,
                         step_rel=stl_rel, case_dir_rel="case", hypothesis=hypothesis)
    exp_path = cfd_dir / "experiment.yaml"
    with open(exp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(exp, f, sort_keys=False, allow_unicode=True)
    doc = {"schema": "aces-golden-cfd/1", "label": label, "solver": "pyfr",
           "experiment": _posix_rel(exp_path, cfd_dir), "stl": stl_rel, **d}
    with open(cfd_dir / "cfd_case.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    doc["experiment_doc"] = exp              # in-memory only: dispatch rewrites its paths for the rig's spec store
    doc["stl_abs"] = str(stl_path)           # in-memory only: the bytes dispatch uploads
    return doc


def _posix_rel(path: Path, root: Path) -> str:
    import os
    return os.path.relpath(os.path.abspath(path), os.path.abspath(root)).replace(os.sep, "/")


# ----------------------------------------------------------------------------------------------------------- rig dispatch

POLL_S = 2.0
LEASE_WAIT_S = 1800          # per-case GPU lease wait on the rig (clamped by the rig to 60..21600)
RIG_PULLS = ("status.json", "results.json", "evidence.json")   # pulled back under cfd/<label>/rig/c000/ when present


def rig_experiment_id(run_id: str) -> str:
    """A filename-safe rig experiment id for the run's CFD case (the rig's own id rules: letters, digits, '-', '_')."""
    import re
    eid = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{run_id}-cfd").strip("-_")
    if not eid:
        raise ValueError(f"no usable rig experiment id from run id {run_id!r}")
    return eid[:64]


def rig_experiment(doc: dict, exp_id: str) -> dict:
    """The run-dir experiment rewritten for the rig's spec store: the case's .ini and STL upload next to the spec
    (<store>/<principal>/<exp_id>/...), so case_dir and mesh.from_step point there."""
    import copy
    exp = copy.deepcopy(doc["experiment_doc"])
    exp["id"] = exp_id
    exp["params"]["case_dir"] = f"{exp_id}/case"
    exp["params"]["mesh"]["from_step"] = f"{exp_id}/{doc['label']}.stl"  # from_step names the STL; the mesh stage branches on the suffix
    return exp


def dispatch(client: Any, *, cfd_dir: Path, doc: dict, exp_id: str, principal: str | None,
             budget_s: float, lease_wait_s: int = LEASE_WAIT_S, poll_s: float = POLL_S,
             progress: Any = None) -> dict:
    """Upload the templated case to the rig's spec store, queue it (docker, the solver's registered image), wait within
    budget_s, and pull the case's records back. The record (also written to cfd/<label>/rig/dispatch.json) ends in one
    of three honest states:

      delivered       results.json came back and verified (sha256 recomputed by the rig on every read)
      failed          the case ran and failed — the rig's status.json error is quoted
      pending         the budget ran out first; the experiment is NOT cancelled (it keeps running on the rig) and the
                      record names exactly what to poll and pull later
      not-delivered   a transport/protocol failure before or during submission; nothing may be queued
    """
    import time

    import yaml

    from .rig import RigError, RigIntegrityError, RigUnreachable
    from . import sha256_bytes, utc_now

    cfd_dir = Path(cfd_dir)
    rig_dir = cfd_dir / "rig"
    rig_dir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {"schema": "aces-golden-cfd-rig/1", "experiment": exp_id, "state": "not-delivered",
                           "reason": None, "submitted_utc": None, "spec_path": None, "queue": None, "jobs": [],
                           "pulled": {}, "budget_s": budget_s}

    def finish(state: str, reason: str | None) -> dict:
        rec["state"], rec["reason"] = state, reason
        with open(rig_dir / "dispatch.json", "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        return rec

    def note(text: str) -> None:
        if progress is not None:
            progress.line("  " + text)

    try:
        ini = (cfd_dir / "case" / f"{doc['label']}.ini").read_bytes()
        stl = Path(doc["stl_abs"]).read_bytes()
        up1 = client.put_file(f"{exp_id}/case/{doc['label']}.ini", ini)
        up2 = client.put_file(f"{exp_id}/{doc['label']}.stl", stl)
        rec["uploads"] = [{"name": f"{exp_id}/case/{doc['label']}.ini", "sha256": up1.get("sha256")},
                          {"name": f"{exp_id}/{doc['label']}.stl", "sha256": up2.get("sha256")}]
        exp = rig_experiment(doc, exp_id)
        text = yaml.safe_dump(exp, sort_keys=False, allow_unicode=True)
        (rig_dir / "spec.yaml").write_text(text, encoding="utf-8")
        rec["spec_sha256"] = sha256_bytes(text.encode("utf-8"))
        put = client.put_spec(text, f"{exp_id}.yaml", principal)
        rec["spec_path"] = put.get("spec_path")
        sub = client.submit(rec["spec_path"], exp_id, lease_wait_s)
        rec["submitted_utc"] = utc_now()
        rec["queue"], rec["exp_dir"] = sub.get("queue"), sub.get("exp_dir")
        rec["jobs"] = [str(j) for j in sub.get("jobs") or []]
        note(f"cfd: {exp_id} submitted to the rig ({len(rec['jobs'])} case, lease wait {lease_wait_s} s)")
    except (RigError, RigUnreachable, OSError, ValueError) as exc:
        return finish("not-delivered", str(exc))

    t_end = time.monotonic() + float(budget_s)
    case_state = None
    job_error = ""
    while time.monotonic() < t_end:
        try:
            st = client.call("sim_status", {"experiment_id": exp_id}) or {}
        except RigUnreachable as exc:
            return finish("pending", f"lost contact with the rig mid-run ({exc}); the experiment keeps running: "
                                     f"sim_status {exp_id}")
        counts = st.get("counts") or {}
        jobs = st.get("jobs") or []
        if jobs:
            case_state = str(jobs[0].get("state") or case_state)
            job_error = str(jobs[0].get("error") or job_error)
        done = int(counts.get("finished") or 0) + int(counts.get("failed") or 0)
        if done >= 1 or case_state in ("finished", "failed"):
            break
        time.sleep(poll_s)
    else:
        pass
    if case_state not in ("finished", "failed") and time.monotonic() >= t_end:
        return finish("pending", f"rig budget {budget_s:g} s spent and the case is still {case_state or 'queued'}; "
                                 f"not cancelled — collect later with sim_status {exp_id} and sim_get_file "
                                 f"{exp_id} c000/results.json")

    for name in RIG_PULLS:
        try:
            data, sha, _chunks = client.get_file(exp_id, f"c000/{name}")
        except RigIntegrityError as exc:
            return finish("not-delivered", f"pulled file did not verify: {exc}")
        except (RigError, RigUnreachable):
            continue  # a failed case has no results.json; status.json carries the reason
        out = rig_dir / "c000" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        rec["pulled"][name] = {"sha256": sha, "bytes": len(data), "path": f"rig/c000/{name}"}
    status = {}
    sp = rig_dir / "c000" / "status.json"
    if sp.is_file():
        with open(sp, "r", encoding="utf-8") as f:
            status = json.load(f)
    if status.get("status") == "FINISHED" and "results.json" in rec["pulled"]:
        with open(rig_dir / "c000" / "results.json", "r", encoding="utf-8") as f:
            rec["results"] = json.load(f)
        return finish("delivered", None)
    err = status.get("error") or job_error.strip() or f"case state {case_state or 'unknown'} with no results.json"
    return finish("failed", str(err).splitlines()[-1][:400] if err else err)
