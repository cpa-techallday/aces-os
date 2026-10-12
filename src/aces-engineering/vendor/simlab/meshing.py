"""CPU mesh stage for the CFD path: a body inside a box, external flow, tetrahedral mesh with a
distance-based refinement field, physical groups named for the PyFR boundary conditions, written
as Gmsh MSH 2.2 (the format PyFR's reader takes).

Two geometry paths, by file type:

  STEP (box_minus_step)  true B-reps: gmsh OCC importShapes + boolean cut of the body out of the box.
  STL  (box_minus_stl)   mesh-derived parts (e.g. golden's winner, whose STEP is a faceted AP214
                         FACETED_BREP that OCC cannot sew into a solid): gmsh's built-in kernel
                         reclassifies the facets into surfaces and the box volume takes the body
                         surface loop as a hole — no boolean.

    from simlab.meshing import box_minus_step, box_minus_stl
    info = box_minus_step("sphere.step", "sphere.msh", box=[-4, -4, -4, 12, 4, 4], lc_body=0.08, lc_far=0.8)
    info = box_minus_stl("winner.stl", "winner.msh", box=[-80, -90, -80, 260, 190, 120], lc_body=0.8, lc_far=40)

Boundary naming (physical surfaces): inlet (x min), outlet (x max), farfield (the other four box
faces), wall (every surface of the imported body). Volume physical group: fluid.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Sequence


def box_minus_step(step_path: str, out_msh: str, box: Sequence[float], lc_body: float, lc_far: float,
                   dist_min: Optional[float] = None, dist_max: Optional[float] = None, msh_version: float = 2.2,
                   algorithm3d: int = 10, optimize: bool = True, order: int = 1, threads: int = 0,
                   names: Optional[Dict[str, str]] = None, log: Optional[str] = None) -> dict:
    """Mesh (box \\ body). Returns a summary dict (counts, physical groups, timings)."""
    import gmsh

    names = dict({"inlet": "inlet", "outlet": "outlet", "farfield": "farfield", "wall": "wall", "fluid": "fluid"}, **(names or {}))
    x0, y0, z0, x1, y1, z1 = [float(v) for v in box]
    os.makedirs(os.path.dirname(os.path.abspath(out_msh)), exist_ok=True)
    t0 = time.time()
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        if threads:
            gmsh.option.setNumber("General.NumThreads", int(threads))
        gmsh.model.add("external_flow")
        occ = gmsh.model.occ
        body = occ.importShapes(os.path.abspath(step_path))
        body_vols = [d for d in body if d[0] == 3]
        if not body_vols:
            raise ValueError(f"no solids in {step_path} (a faceted STEP is mesh-derived: use box_minus_stl on its STL)")
        occ.synchronize()
        body_surfs_before = set(s[1] for s in gmsh.model.getBoundary(body_vols, combined=False, oriented=False, recursive=False))
        bx = occ.addBox(x0, y0, z0, x1 - x0, y1 - y0, z1 - z0)
        fluid, _ = occ.cut([(3, bx)], body_vols, removeObject=True, removeTool=True)
        occ.synchronize()
        fluid_vols = [d for d in fluid if d[0] == 3]
        # classify the surfaces of the fluid volume by their bounding boxes
        eps = 1e-6 * max(x1 - x0, y1 - y0, z1 - z0)
        groups: Dict[str, List[int]] = {"inlet": [], "outlet": [], "farfield": [], "wall": []}
        for dim, tag in gmsh.model.getBoundary(fluid_vols, combined=True, oriented=False, recursive=False):
            bb = gmsh.model.getBoundingBox(dim, tag)
            if abs(bb[0] - x0) < eps and abs(bb[3] - x0) < eps:
                groups["inlet"].append(tag)
            elif abs(bb[0] - x1) < eps and abs(bb[3] - x1) < eps:
                groups["outlet"].append(tag)
            elif (abs(bb[1] - y0) < eps and abs(bb[4] - y0) < eps) or (abs(bb[1] - y1) < eps and abs(bb[4] - y1) < eps) \
                    or (abs(bb[2] - z0) < eps and abs(bb[5] - z0) < eps) or (abs(bb[2] - z1) < eps and abs(bb[5] - z1) < eps):
                groups["farfield"].append(tag)
            else:
                groups["wall"].append(tag)
        if not groups["wall"]:
            raise ValueError("no wall surfaces found after the cut; is the body inside the box?")
        for key, tags in groups.items():
            if tags:
                pg = gmsh.model.addPhysicalGroup(2, tags)
                gmsh.model.setPhysicalName(2, pg, names[key])
        pv = gmsh.model.addPhysicalGroup(3, [v[1] for v in fluid_vols])
        gmsh.model.setPhysicalName(3, pv, names["fluid"])
        # size field: lc_body on the wall, growing to lc_far beyond dist_max
        dmin = dist_min if dist_min is not None else 2.0 * lc_body
        dmax = dist_max if dist_max is not None else 0.25 * min(x1 - x0, y1 - y0, z1 - z0)
        f_dist = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(f_dist, "SurfacesList", groups["wall"])
        gmsh.model.mesh.field.setNumber(f_dist, "Sampling", 200)
        f_thr = gmsh.model.mesh.field.add("Threshold")
        gmsh.model.mesh.field.setNumber(f_thr, "InField", f_dist)
        gmsh.model.mesh.field.setNumber(f_thr, "SizeMin", lc_body)
        gmsh.model.mesh.field.setNumber(f_thr, "SizeMax", lc_far)
        gmsh.model.mesh.field.setNumber(f_thr, "DistMin", dmin)
        gmsh.model.mesh.field.setNumber(f_thr, "DistMax", dmax)
        gmsh.model.mesh.field.setAsBackgroundMesh(f_thr)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.option.setNumber("Mesh.Algorithm3D", int(algorithm3d))
        gmsh.option.setNumber("Mesh.ElementOrder", int(order))
        gmsh.option.setNumber("Mesh.MshFileVersion", float(msh_version))
        gmsh.option.setNumber("Mesh.Binary", 0)
        gmsh.model.mesh.generate(3)
        if optimize:
            gmsh.model.mesh.optimize("Netgen")
        t_mesh = time.time() - t0
        ntypes = {}
        for dim in (2, 3):
            types, tags, _nodes = gmsh.model.mesh.getElements(dim)
            for ty, tg in zip(types, tags):
                name = gmsh.model.mesh.getElementProperties(ty)[0]
                ntypes[name] = ntypes.get(name, 0) + len(tg)
        nnodes = len(gmsh.model.mesh.getNodes()[0])
        gmsh.write(os.path.abspath(out_msh))
        if log:
            with open(log, "w", encoding="utf-8") as f:
                f.write("\n".join(gmsh.logger.get()) if gmsh.logger else "")
        return {"msh": os.path.abspath(out_msh), "msh_version": msh_version, "nodes": nnodes, "elements": ntypes,
                "physical_surfaces": {k: len(v) for k, v in groups.items()}, "box": list(box),
                "lc_body": lc_body, "lc_far": lc_far, "dist_min": dmin, "dist_max": dmax, "wall_s": t_mesh}
    finally:
        gmsh.finalize()


def box_minus_stl(stl_path: str, out_msh: str, box: Sequence[float], lc_body: float, lc_far: float,
                  dist_min: Optional[float] = None, dist_max: Optional[float] = None, msh_version: float = 2.2,
                  algorithm3d: int = 10, optimize: bool = True, order: int = 1, threads: int = 0,
                  names: Optional[Dict[str, str]] = None, log: Optional[str] = None,
                  angle_deg: float = 40.0) -> dict:
    """Mesh (box \\ STL body) with gmsh's built-in kernel — the path for mesh-derived parts whose STEP is a
    faceted AP214 FACETED_BREP that OCC cannot sew into a solid. The STL is reclassified into discrete
    surfaces (angle_deg feature angle) and the box volume takes the body surface loop as a hole, so no
    boolean is needed. Returns the same summary dict as box_minus_step, plus "geometry": "stl"."""
    import math

    import gmsh

    names = dict({"inlet": "inlet", "outlet": "outlet", "farfield": "farfield", "wall": "wall", "fluid": "fluid"}, **(names or {}))
    x0, y0, z0, x1, y1, z1 = [float(v) for v in box]
    os.makedirs(os.path.dirname(os.path.abspath(out_msh)), exist_ok=True)
    t0 = time.time()
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        if threads:
            gmsh.option.setNumber("General.NumThreads", int(threads))
        gmsh.model.add("external_flow_stl")
        gmsh.merge(os.path.abspath(stl_path))
        # reclassify the facets into discrete surfaces and reparametrise them as built-in-kernel geometry;
        # forReparametrization=True is required — False makes createGeometry raise
        gmsh.model.mesh.classifySurfaces(angle_deg * math.pi / 180.0, boundary=True, forReparametrization=True)
        gmsh.model.mesh.createGeometry()
        body_surfs = [t for d, t in gmsh.model.getEntities(2)]
        if not body_surfs:
            raise ValueError(f"no surfaces classified from {stl_path} (check the STL is a closed solid)")
        # farfield box in the built-in kernel; discrete entities from createGeometry already occupy the low
        # tags, so every tag below comes from the add* return values — never hardcoded
        geo = gmsh.model.geo
        p = [geo.addPoint(x, y, z) for x, y, z in
             [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]]
        edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
        ln = [geo.addLine(p[a], p[b]) for a, b in edges]
        # closed chains: bottom p0p1p2p3, top p4p5p6p7, xmin p0p3p7p4, xmax p1p2p6p5, ymin p0p1p5p4, ymax p3p2p6p7
        faces = {"bottom": [ln[0], ln[1], ln[2], ln[3]], "top": [ln[4], ln[5], ln[6], ln[7]],
                 "xmin": [-ln[3], ln[11], ln[7], -ln[8]], "xmax": [ln[1], ln[10], -ln[5], -ln[9]],
                 "ymin": [ln[0], ln[9], -ln[4], -ln[8]], "ymax": [-ln[2], ln[10], ln[6], -ln[11]]}
        box_surfs = {}
        for name, edges_ in faces.items():
            box_surfs[name] = geo.addPlaneSurface([geo.addCurveLoop(edges_)])
        vol = geo.addVolume([geo.addSurfaceLoop(list(box_surfs.values())), geo.addSurfaceLoop(body_surfs)])
        geo.synchronize()
        groups: Dict[str, List[int]] = {"inlet": [box_surfs["xmin"]], "outlet": [box_surfs["xmax"]],
                                        "farfield": [box_surfs[k] for k in ("bottom", "top", "ymin", "ymax")],
                                        "wall": body_surfs}
        for key, tags in groups.items():
            if tags:
                pg = gmsh.model.addPhysicalGroup(2, tags)
                gmsh.model.setPhysicalName(2, pg, names[key])
        pv = gmsh.model.addPhysicalGroup(3, [vol])
        gmsh.model.setPhysicalName(3, pv, names["fluid"])
        # size field: lc_body on the wall, growing to lc_far beyond dist_max (same semantics as box_minus_step)
        dmin = dist_min if dist_min is not None else 2.0 * lc_body
        dmax = dist_max if dist_max is not None else 0.25 * min(x1 - x0, y1 - y0, z1 - z0)
        f_dist = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(f_dist, "SurfacesList", body_surfs)
        gmsh.model.mesh.field.setNumber(f_dist, "Sampling", 200)
        f_thr = gmsh.model.mesh.field.add("Threshold")
        gmsh.model.mesh.field.setNumber(f_thr, "InField", f_dist)
        gmsh.model.mesh.field.setNumber(f_thr, "SizeMin", lc_body)
        gmsh.model.mesh.field.setNumber(f_thr, "SizeMax", lc_far)
        gmsh.model.mesh.field.setNumber(f_thr, "DistMin", dmin)
        gmsh.model.mesh.field.setNumber(f_thr, "DistMax", dmax)
        gmsh.model.mesh.field.setAsBackgroundMesh(f_thr)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.option.setNumber("Mesh.Algorithm3D", int(algorithm3d))
        gmsh.option.setNumber("Mesh.ElementOrder", int(order))
        gmsh.option.setNumber("Mesh.MshFileVersion", float(msh_version))
        gmsh.option.setNumber("Mesh.Binary", 0)
        gmsh.model.mesh.generate(3)
        if optimize:
            gmsh.model.mesh.optimize("Netgen")
        t_mesh = time.time() - t0
        ntypes = {}
        for dim in (2, 3):
            types, tags, _nodes = gmsh.model.mesh.getElements(dim)
            for ty, tg in zip(types, tags):
                name = gmsh.model.mesh.getElementProperties(ty)[0]
                ntypes[name] = ntypes.get(name, 0) + len(tg)
        nnodes = len(gmsh.model.mesh.getNodes()[0])
        gmsh.write(os.path.abspath(out_msh))
        if log:
            with open(log, "w", encoding="utf-8") as f:
                f.write("\n".join(gmsh.logger.get()) if gmsh.logger else "")
        return {"msh": os.path.abspath(out_msh), "msh_version": msh_version, "nodes": nnodes, "elements": ntypes,
                "physical_surfaces": {k: len(v) for k, v in groups.items()}, "box": list(box),
                "lc_body": lc_body, "lc_far": lc_far, "dist_min": dmin, "dist_max": dmax, "wall_s": t_mesh,
                "geometry": "stl"}
    finally:
        gmsh.finalize()


def write_sphere_step(path: str, radius: float = 0.5, centre=(0.0, 0.0, 0.0)) -> str:
    from build123d import Location, Sphere, export_step

    s = Sphere(radius).moved(Location(tuple(centre)))
    export_step(s, path)
    return path


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="external-flow tet mesh: STEP body inside a box -> MSH 2.2 with PyFR boundary names")
    ap.add_argument("step")
    ap.add_argument("out_msh")
    ap.add_argument("--box", nargs=6, type=float, required=True, metavar=("x0", "y0", "z0", "x1", "y1", "z1"))
    ap.add_argument("--lc-body", type=float, required=True)
    ap.add_argument("--lc-far", type=float, required=True)
    a = ap.parse_args()
    print(json.dumps(box_minus_step(a.step, a.out_msh, a.box, a.lc_body, a.lc_far), indent=2))
