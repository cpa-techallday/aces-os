"""ccx-thermal: steady-state heat conduction in a part.stl with CalculiX ccx *HEAT TRANSFER.

A member of the CalculiX adapter family next to ccx_check.py, self-contained like ccx_modal.py:
the case names its STL, mesh size, conductivity and boundaries directly. Meshing is the aces
golden conforming mesher inside the aces-golden image (imported in run(), never at module level);
the deck, the .dat parsing and the 1-D slab reference are pure Python in ccx_common, unit-tested
on the workstation.

Units: the caller's consistent set; the validation case works in mm-W-K, i.e. conductivity in
W/(mm K) and flux in W/mm^2 (50 W/(m K) = 0.05 W/(mm K)).

Case params (paths resolve against the spec file):
  stl                path of the part's STL (watertight, one body; required)
  mesh               {size_mm (required), attempts (optional, default ABCD)}
  material           {conductivity} — mandatory: a *HEAT TRANSFER deck without *CONDUCTIVITY is
                     not a model, and this adapter refuses to run it
  boundaries         [{name, box [x0 y0 z0 x1 y1 z1], ...}] on the part's surface (each box planar
                     on exactly one axis), each either a temperature boundary (temperature: value)
                     or a heat flux into the body (flux: value, same consistent unit set).
                     At least one temperature boundary is required: steady-state conduction with
                     only fluxes is singular (the temperature level is undetermined). Faces with
                     no boundary are insulated.
  initial_temperature  field start value (default 0.0; irrelevant to a linear steady state, kept
                     because ccx heat transfer wants initial conditions)
  threads, timeout_s ccx solve resources (defaults 4 / 900)
  reference          optional {kind: slab_conduction_1d, L_mm, hot: <flux boundary name>, cold:
                     <temperature boundary name>}: delta T = q L / k against the difference of the
                     two boundaries' face-mean temperatures; reported, never self-judged

results.json: t_min/t_max/t_mean over all nodes, per-boundary face temperature statistics, mesh
and ccx provenance, and the reference comparison when asked for.
"""
from __future__ import annotations

import json
import os
import time

from . import ccx_common as cc
from .ccx_common import InputError

SOLVER = "ccx-thermal"


def build_deck(nodes, tets, boundaries, conductivity: float, initial_temperature: float,
               heading: str) -> list:
    """The steady-state *HEAT TRANSFER deck as a list of lines (pure; unit-tested). `boundaries`
    are [{name, kind ('temperature'|'flux'), node_ids or dflux rows, value}], already mapped."""
    lines = ["*HEADING", heading.replace("\n", " ")[:120]]
    lines += cc.mesh_lines(nodes, tets)
    for i, b in enumerate(boundaries):
        if b["kind"] == "temperature":
            lines += cc.nset_lines(f"TEMP_{i + 1}", b["node_ids"])
    lines += ["*MATERIAL, NAME=MAT", "*CONDUCTIVITY", cc.fnum(conductivity),
              "*SOLID SECTION, ELSET=EALL, MATERIAL=MAT",
              "*INITIAL CONDITIONS, TYPE=TEMPERATURE", f"NALL, {cc.fnum(initial_temperature)}",
              "*STEP", "*HEAT TRANSFER, STEADY STATE"]
    lines.append("*BOUNDARY")
    for i, b in enumerate(boundaries):
        if b["kind"] == "temperature":
            lines.append(f"TEMP_{i + 1}, 11, 11, {cc.fnum(b['value'])}")
    fluxes = [b for b in boundaries if b["kind"] == "flux"]
    if fluxes:
        lines.append("*DFLUX")
        for b in fluxes:
            for elem, face in b["dflux"]:
                lines.append(f"{int(elem) + 1}, S{int(face)}, {cc.fnum(b['value'])}")
    lines += ["*NODE PRINT, NSET=NALL", "NT", "*END STEP"]
    return lines


def validate_case(case: dict, spec) -> dict:
    """The run parameters, checked before anything is meshed; every refusal names what is absent."""
    stl = case.get("stl")
    if not stl:
        raise InputError("ccx-thermal: the case has no 'stl' (path of the part, relative to the spec)")
    stl = spec.resolve(str(stl))
    if not os.path.isfile(stl):
        raise InputError(f"ccx-thermal: the STL {stl} is not here")
    mesh = case.get("mesh") or {}
    if not mesh.get("size_mm"):
        raise InputError("ccx-thermal: mesh.size_mm is required (the element size is a modelling "
                         "decision; it is never defaulted silently)")
    mat = case.get("material") or {}
    cond = mat.get("conductivity", mat.get("conductivity_W_mmK"))
    if cond is None:
        raise InputError("ccx-thermal: material.conductivity is missing — steady-state conduction "
                         "without conductivity is not a model; refusing to run")
    if not float(cond) > 0:
        raise InputError(f"ccx-thermal: material.conductivity {cond} is not positive")
    bounds = []
    for i, b in enumerate(case.get("boundaries") or []):
        name = b.get("name") or f"boundary{i + 1}"
        box = [float(v) for v in (b.get("box") or [])]
        if len(box) != 6:
            raise InputError(f"ccx-thermal: boundary {name}: box must have 6 numbers")
        cc.planar_axis(box)  # raises unless planar on exactly one axis
        has_t, has_f = b.get("temperature") is not None, b.get("flux") is not None
        if has_t == has_f:
            raise InputError(f"ccx-thermal: boundary {name} needs exactly one of temperature / flux "
                             f"(it has {'both' if has_t else 'neither'})")
        bounds.append({"name": name, "box": box, "kind": "temperature" if has_t else "flux",
                       "value": float(b["temperature"] if has_t else b["flux"])})
    if not bounds:
        raise InputError("ccx-thermal: the case has no boundaries")
    if not any(b["kind"] == "temperature" for b in bounds):
        raise InputError("ccx-thermal: no temperature boundary — steady-state conduction with only "
                         "heat fluxes is singular (the temperature level is undetermined); refusing to run")
    return {"stl": stl, "size_mm": float(mesh["size_mm"]), "attempts": str(mesh.get("attempts", "ABCD")),
            "conductivity": float(cond), "boundaries": bounds,
            "initial_temperature": float(case.get("initial_temperature", 0.0)),
            "threads": int(case.get("threads", 4)), "timeout_s": float(case.get("timeout_s", 900.0)),
            "reference": case.get("reference")}


def map_boundaries(nodes, tets, boundaries: list, window_mm: float) -> list:
    """Boundaries on the mesh: the surface faces of each planar box (their nodes for a temperature
    boundary, (elem, ccx face) rows for a flux boundary). A boundary that selects no face is an
    InputError naming it."""
    faces = cc.boundary_faces(nodes, tets)
    out = []
    for b in boundaries:
        sel = cc.faces_on_plane(faces, b["box"], window_mm)
        if not sel:
            a = cc.planar_axis(b["box"])
            raise InputError(f"ccx-thermal: boundary {b['name']}: no surface of the part within "
                             f"{window_mm:g} mm of {cc.AXES[a]}={b['box'][a]:g} inside its box")
        area = sum(f["area"] for f in sel)
        rec = {**b, "faces": len(sel), "area": area}
        if b["kind"] == "temperature":
            rec["node_ids"] = cc.face_node_ids(sel)
            rec["nodes"] = len(rec["node_ids"])
        else:
            rec["dflux"] = [(f["elem"], f["face"]) for f in sel]
        out.append(rec)
    return out


class Adapter:
    name = SOLVER

    def inputs(self, case: dict, spec) -> list:
        stl = case.get("stl")
        return [spec.resolve(str(stl))] if stl else []

    def run(self, case: dict, spec, run_dir: str) -> dict:
        t0 = time.time()
        p = validate_case(case, spec)                       # refuses honestly before any tooling
        conforming, av = cc.require_ccx_stack(SOLVER)       # gmsh + ccx, or an honest raise
        work = os.path.join(os.path.abspath(run_dir), "ccx")
        os.makedirs(work, exist_ok=True)
        cc.clean_case_outputs(work, "thermal")
        case_id = str(case.get("case_id", "c000"))

        tm0 = time.time()
        mesh_rec = cc.mesh_stl_tet10(conforming, p["stl"], work, p["size_mm"], case_id, p["attempts"])
        t_mesh = time.time() - tm0
        nodes, tets = cc.load_mesh_npz(work)

        boundaries = map_boundaries(nodes, tets, p["boundaries"], 0.5 * p["size_mm"])
        heading = f"ccx-thermal {case_id}: gmsh C3D10 (second-order linear) of {os.path.basename(p['stl'])}"
        deck = build_deck(nodes, tets, boundaries, p["conductivity"], p["initial_temperature"], heading)
        inp = os.path.join(work, "thermal.inp")
        with open(inp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(deck) + "\n")

        solve = cc.run_ccx(av["ccx"], work, "thermal", p["threads"], p["timeout_s"])
        temps = cc.parse_temperatures_dat(cc.read_text(os.path.join(work, "thermal.dat")))

        vals = list(temps.values())
        bstats = []
        for b in boundaries:
            rec = {k: v for k, v in b.items() if k not in ("node_ids", "dflux")}
            if b["kind"] == "temperature":
                tv = [temps[n + 1] for n in b["node_ids"] if n + 1 in temps]
                if tv:
                    rec.update(t_mean=sum(tv) / len(tv), t_min=min(tv), t_max=max(tv))
            else:
                tv = [temps[n + 1] for f in b["dflux"] for n in _face_nodes(nodes, tets, f) if n + 1 in temps]
                if tv:
                    rec.update(t_mean=sum(tv) / len(tv), t_min=min(tv), t_max=max(tv))
            bstats.append(rec)

        results = {
            "solver": SOLVER,
            "t_min": min(vals), "t_max": max(vals), "t_mean": sum(vals) / len(vals),
            "n_nodes": len(vals),
            "boundaries": bstats,
            "material": {"conductivity": p["conductivity"]},
            "initial_temperature": p["initial_temperature"],
            "mesh": {"tool": "gmsh", "version": mesh_rec.get("gmsh_version"), "element": "C3D10",
                     "second_order_linear": True, "size_mm": p["size_mm"], "route": mesh_rec.get("route"),
                     "nodes": mesh_rec.get("nodes"), "elements": mesh_rec.get("elements"),
                     "volume_mm3": mesh_rec.get("volume_mm3"),
                     "volume_rel_err": mesh_rec.get("volume_rel_err"), "min_sicn": mesh_rec.get("min_sicn"),
                     "wall_s": t_mesh},
            "ccx": {"tool": "CalculiX ccx", "version": av.get("ccx_version"), "path": av.get("ccx"),
                    "sha256": av.get("ccx_sha256"), "threads": p["threads"], **solve},
            "wall_s": time.time() - t0,
            "artifacts": [inp, os.path.join(work, "thermal.dat"), os.path.join(work, "thermal.log"),
                          os.path.join(work, "tetmesh.json"), os.path.join(work, "mesh.npz")],
        }
        ref = p["reference"]
        if ref and ref.get("kind") == "slab_conduction_1d":
            hot = next((b for b in bstats if b["name"] == ref.get("hot")), None)
            cold = next((b for b in bstats if b["name"] == ref.get("cold")), None)
            if hot is None or cold is None:
                raise InputError(f"ccx-thermal: reference names boundaries {ref.get('hot')!r} / "
                                 f"{ref.get('cold')!r} that are not in the case")
            if hot.get("kind") != "flux" or cold.get("kind") != "temperature":
                raise InputError("ccx-thermal: the slab reference needs hot = a flux boundary and "
                                 "cold = a temperature boundary")
            dt_ref = cc.slab_conduction_delta_t(hot["value"], float(ref["L_mm"]), p["conductivity"])
            dt_meas = hot["t_mean"] - cold["t_mean"]
            results["reference"] = {"kind": "slab_conduction_1d", "L_mm": float(ref["L_mm"]),
                                    "flux": hot["value"], "conductivity": p["conductivity"],
                                    "formula": "delta T = q L / k",
                                    "delta_t_ref": dt_ref, "delta_t_measured": dt_meas,
                                    "rel_err": (dt_meas - dt_ref) / dt_ref,
                                    "t_hot_mean": hot["t_mean"], "t_cold_mean": cold["t_mean"]}
        with open(os.path.join(work, "thermal.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in results.items()}, f, indent=2)
        return results


def _face_nodes(nodes, tets, dflux_row):
    """The six nodes (0-based) of the boundary face (elem, ccx face number) — for the flux-side
    temperature read-out."""
    elem, face = dflux_row
    tet = tets[elem]
    loc = cc.FACES[face - 1]
    mids = [tet[cc.MID[frozenset((loc[0], loc[1]))]], tet[cc.MID[frozenset((loc[1], loc[2]))]],
            tet[cc.MID[frozenset((loc[2], loc[0]))]]]
    return [tet[i] for i in loc] + mids
