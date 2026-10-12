"""ccx-modal: natural frequencies of a part.stl with CalculiX ccx *FREQUENCY.

A member of the CalculiX adapter family next to ccx_check.py, but self-contained: the case names
its STL, mesh size, material and supports directly (no golden upload envelope). The gmsh C3D10
meshing is the aces golden conforming mesher (aces_engineering.golden.conforming.mesh_stl, inside
the aces-golden image, imported in run(), never at module level: the queue and MCP containers
import every adapter to read its stages and lack aces_engineering). The deck writing, the .dat
parsing and the closed-form references are pure Python in ccx_common and are unit-tested on the
workstation.

Units: mm, N, s, tonne (the deck's *DENSITY is rho_kg_mm3 / 1000), so frequencies are Hz.

Case params (all relative paths resolve against the spec file):
  stl                path of the part's STL (watertight, one body; required)
  mesh               {size_mm: element size for the gmsh C3D10 mesh (required); attempts: subset
                     of ABCD to pin a route (optional, default all four)}
  material           {E_MPa, nu, rho_kg_mm3} — rho_kg_mm3 is mandatory: a *FREQUENCY deck without
                     *DENSITY has no mass matrix, and this adapter refuses to run it
  supports           [{name, box [x0 y0 z0 x1 y1 z1], dofs [0, 1, 2]}]; a box planar on one axis
                     is the surface on that plane, any other box is the nodes inside it. An empty
                     list is a free-free run (the first six modes are rigid-body modes)
  n_modes            number of eigenfrequencies (default 6)
  threads            ccx solver threads (default 4; the mesher always runs single-threaded — the
                     conforming check measured gmsh 4.15.2 multi-thread meshes differ run to run)
  timeout_s          ccx wall-clock limit (default 900)
  reference          optional closed form, {kind: euler_bernoulli_cantilever, L_mm, b_mm, h_mm} —
                     the adapter reports f1_ref_hz and f1_rel_err beside the measured modes; it
                     never decides pass/fail itself (that belongs to the registry record)

results.json: eigenfrequencies_hz, per-mode rows (mode, eigenvalue, omega_rad_per_s,
frequency_hz), mesh and ccx provenance, and the reference comparison when asked for.
"""
from __future__ import annotations

import json
import os
import time

from . import ccx_common as cc
from .ccx_common import InputError

SOLVER = "ccx-modal"


def build_deck(nodes, tets, supports, material: dict, n_modes: int, heading: str) -> list:
    """The *FREQUENCY deck as a list of lines (pure; unit-tested). `supports` are
    [{name, node_ids (0-based), dofs}] with node_ids already mapped onto the mesh."""
    lines = ["*HEADING", heading.replace("\n", " ")[:120]]
    lines += cc.mesh_lines(nodes, tets)
    own = [s for s in supports if s["node_ids"]]
    for i, s in enumerate(own):
        lines += cc.nset_lines(f"SUP_{i + 1}", s["node_ids"])
    # mm-N-s-tonne: *DENSITY in tonne/mm^3 so the eigenfrequencies come out in Hz
    rho_tonne = float(material["rho_kg_mm3"]) / 1000.0
    lines += ["*MATERIAL, NAME=MAT", "*ELASTIC",
              f"{cc.fnum(material['E_MPa'])}, {cc.fnum(material['nu'])}",
              "*DENSITY", cc.fnum(rho_tonne),
              "*SOLID SECTION, ELSET=EALL, MATERIAL=MAT",
              "*STEP", "*FREQUENCY, SOLVER=SPOOLES", str(int(n_modes))]
    if own:
        lines.append("*BOUNDARY")
        for i, s in enumerate(own):
            dofs = sorted(int(d) for d in s["dofs"])
            if dofs == [0, 1, 2]:
                lines.append(f"SUP_{i + 1}, 1, 3")
            else:
                lines += [f"SUP_{i + 1}, {d + 1}, {d + 1}" for d in dofs]
    lines.append("*END STEP")
    return lines


def validate_case(case: dict, spec) -> dict:
    """The run parameters, checked before anything is meshed; every refusal names what is absent."""
    stl = case.get("stl")
    if not stl:
        raise InputError("ccx-modal: the case has no 'stl' (path of the part, relative to the spec)")
    stl = spec.resolve(str(stl))
    if not os.path.isfile(stl):
        raise InputError(f"ccx-modal: the STL {stl} is not here")
    mesh = case.get("mesh") or {}
    if not mesh.get("size_mm"):
        raise InputError("ccx-modal: mesh.size_mm is required (the element size is a modelling decision; "
                         "it is never defaulted silently)")
    mat = case.get("material") or {}
    for key in ("E_MPa", "nu"):
        if mat.get(key) is None:
            raise InputError(f"ccx-modal: material.{key} is missing")
    if mat.get("rho_kg_mm3") is None:
        raise InputError("ccx-modal: material.rho_kg_mm3 is missing — a *FREQUENCY run without density "
                         "has no mass matrix and its 'modes' would be meaningless; refusing to run")
    supports = []
    for i, s in enumerate(case.get("supports") or []):
        box = [float(v) for v in s["box"]]
        if len(box) != 6:
            raise InputError(f"ccx-modal: support {s.get('name') or i}: box must have 6 numbers")
        supports.append({"name": s.get("name") or f"support{i + 1}", "box": box,
                         "dofs": [int(d) for d in (s.get("dofs") if s.get("dofs") is not None else (0, 1, 2))]})
    n_modes = int(case.get("n_modes", 6))
    if n_modes < 1:
        raise InputError(f"ccx-modal: n_modes {n_modes} < 1")
    return {"stl": stl, "size_mm": float(mesh["size_mm"]), "attempts": str(mesh.get("attempts", "ABCD")),
            "material": {"E_MPa": float(mat["E_MPa"]), "nu": float(mat["nu"]),
                         "rho_kg_mm3": float(mat["rho_kg_mm3"])},
            "supports": supports, "n_modes": n_modes, "threads": int(case.get("threads", 4)),
            "timeout_s": float(case.get("timeout_s", 900.0)), "reference": case.get("reference")}


def map_supports(nodes, tets, supports: list, window_mm: float) -> list:
    """Support node sets on the mesh: a planar box is its surface's nodes (corners and midside),
    any other box is the nodes inside it. A support that maps to nothing is an InputError naming
    it — never an empty NSET in the deck."""
    faces = None
    out = []
    for s in supports:
        box = s["box"]
        planar = [a for a in range(3) if abs(box[a] - box[a + 3]) <= 1e-9 * max(1.0, abs(box[a]))]
        if len(planar) == 1:
            if faces is None:
                faces = cc.boundary_faces(nodes, tets)
            sel = cc.faces_on_plane(faces, box, window_mm)
            ids = cc.face_node_ids(sel)
            if not ids:
                raise InputError(f"ccx-modal: support {s['name']}: no surface of the part within "
                                 f"{window_mm:g} mm of {cc.AXES[planar[0]]}={box[planar[0]]:g} inside its box")
            mode = "surface"
        else:
            ids = cc.nodes_in_box(nodes, box)
            if not ids:
                raise InputError(f"ccx-modal: support {s['name']}: no mesh node inside its box {box}")
            mode = "nodes"
        out.append({**s, "node_ids": ids, "nodes": len(ids), "mode": mode})
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
        cc.clean_case_outputs(work, "modal")
        case_id = str(case.get("case_id", "c000"))

        tm0 = time.time()
        mesh_rec = cc.mesh_stl_tet10(conforming, p["stl"], work, p["size_mm"], case_id, p["attempts"])
        t_mesh = time.time() - tm0
        nodes, tets = cc.load_mesh_npz(work)

        supports = map_supports(nodes, tets, p["supports"], 0.5 * p["size_mm"])
        heading = f"ccx-modal {case_id}: gmsh C3D10 (second-order linear) of {os.path.basename(p['stl'])}"
        deck = build_deck(nodes, tets, supports, p["material"], p["n_modes"], heading)
        inp = os.path.join(work, "modal.inp")
        with open(inp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(deck) + "\n")

        solve = cc.run_ccx(av["ccx"], work, "modal", p["threads"], p["timeout_s"])
        modes = cc.parse_eigenfrequencies_dat(cc.read_text(os.path.join(work, "modal.dat")))
        if len(modes) < p["n_modes"]:
            raise cc.ParseError(f"ccx.modal: asked for {p['n_modes']} modes, ccx.dat has {len(modes)}")

        results = {
            "solver": SOLVER,
            "eigenfrequencies_hz": [m["frequency_hz"] for m in modes],
            "modes": modes,
            "n_modes_requested": p["n_modes"],
            "free_free": not p["supports"],
            "material": p["material"],
            "supports": [{k: v for k, v in s.items() if k != "node_ids"} for s in supports],
            "mesh": {"tool": "gmsh", "version": mesh_rec.get("gmsh_version"), "element": "C3D10",
                     "second_order_linear": True, "size_mm": p["size_mm"], "route": mesh_rec.get("route"),
                     "nodes": mesh_rec.get("nodes"), "elements": mesh_rec.get("elements"),
                     "volume_mm3": mesh_rec.get("volume_mm3"),
                     "volume_rel_err": mesh_rec.get("volume_rel_err"), "min_sicn": mesh_rec.get("min_sicn"),
                     "wall_s": t_mesh},
            "ccx": {"tool": "CalculiX ccx", "version": av.get("ccx_version"), "path": av.get("ccx"),
                    "sha256": av.get("ccx_sha256"), "threads": p["threads"], **solve},
            "wall_s": time.time() - t0,
            "artifacts": [inp, os.path.join(work, "modal.dat"), os.path.join(work, "modal.log"),
                          os.path.join(work, "tetmesh.json"), os.path.join(work, "mesh.npz")],
        }
        ref = p["reference"]
        if ref and ref.get("kind") == "euler_bernoulli_cantilever":
            m = p["material"]
            r = cc.beam_rect_reference(float(ref["L_mm"]), float(ref["b_mm"]), float(ref["h_mm"]),
                                       m["E_MPa"], m["rho_kg_mm3"], n=min(len(modes), 3))
            f1, f1_ref = modes[0]["frequency_hz"], r["frequencies_hz"][0]
            results["reference"] = r
            results["f1_hz"] = f1
            results["f1_ref_hz"] = f1_ref
            results["f1_rel_err"] = (f1 - f1_ref) / f1_ref
            results["mesh_mass_kg"] = (mesh_rec.get("volume_mm3") or 0.0) * m["rho_kg_mm3"]
            results["reference_note"] = ("mode 1 vs Euler-Bernoulli first bending; EB neglects shear "
                                         "deformation, so a 3-D solid solve sits slightly below it")
        with open(os.path.join(work, "modal.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in results.items()}, f, indent=2)
        return results
