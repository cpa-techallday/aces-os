"""aces golden: the honest STEP. The winner is an organic mesh (marching cubes of an implicit field); a true B-rep
of it does not exist. What CAD can take is a faceted STEP: AP214, one FACETED_BREP, vertices and edges deduplicated,
one FACE_SURFACE (PLANE) per triangle, labelled mesh-derived everywhere it appears. Fusion/FreeCAD import it as a
faceted body — good for measuring, referencing and clearance checks, not for pretending the part is analytic.

Written only under the face cap: past it the file is huge and useless in CAD, and the STL is the deliverable.
Pure stdlib (struct + text); no trimesh, no numpy: the export stage runs where the run ran.
"""
from __future__ import annotations

import struct
import time
from pathlib import Path

FACE_CAP = 30_000  # faceted STEP entities run ~9.5 per face; past the cap the STL is the deliverable


def read_stl(path: Path) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Binary or ASCII STL -> (vertices deduplicated, triangles as index triples). Normals are recomputed by the
    CAD side from the winding; the STL's stored normals are ignored."""
    data = Path(path).read_bytes()
    tris_raw: list[tuple[tuple[float, float, float], ...]] = []
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + n * 50 == len(data):
            off = 84
            for _ in range(n):
                vals = struct.unpack_from("<12f", data, off)
                tris_raw.append((vals[3:6], vals[6:9], vals[9:12]))
                off += 50
    if not tris_raw:
        text = data.decode("utf-8", "replace")
        cur = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0].lower() == "vertex":
                cur.append((float(parts[1]), float(parts[2]), float(parts[3])))
                if len(cur) == 3:
                    tris_raw.append(tuple(cur))
                    cur = []
    if not tris_raw:
        raise ValueError(f"{path}: no triangles read (not a binary or ASCII STL?)")
    verts: list[tuple[float, float, float]] = []
    vmap: dict[tuple[float, float, float], int] = {}
    tris: list[tuple[int, int, int]] = []
    for tri in tris_raw:
        idx = []
        for v in tri:
            key = (round(v[0], 6), round(v[1], 6), round(v[2], 6))
            i = vmap.get(key)
            if i is None:
                i = len(verts)
                vmap[key] = i
                verts.append(v)
            idx.append(i)
        tris.append((idx[0], idx[1], idx[2]))
    return verts, tris


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(v):
    n = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
    return (v[0] / n, v[1] / n, v[2] / n) if n else (0.0, 0.0, 1.0)


def write_faceted_step(stl_path: Path, step_path: Path, *, name: str = "part") -> dict:
    """STL -> AP214 FACETED_BREP at step_path. Returns the stats dict; raises ValueError over the face cap."""
    verts, tris = read_stl(stl_path)
    if len(tris) > FACE_CAP:
        raise ValueError(f"{len(tris)} faces is over the faceted STEP cap of {FACE_CAP}: the STL is the deliverable")
    edges: dict[tuple[int, int], int] = {}
    for t in tris:
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            key = (a, b) if a < b else (b, a)
            edges.setdefault(key, len(edges))

    lines: list[str] = []
    add = lines.append
    n = 0

    def nid() -> int:
        nonlocal n
        n += 1
        return n

    def fmt(v) -> str:
        return f"({v[0]:.6g},{v[1]:.6g},{v[2]:.6g})"

    safe = name.replace("'", "")
    add("ISO-10303-21;")
    add("HEADER;")
    add("FILE_DESCRIPTION(('ACES golden export — mesh-derived faceted STEP, not a B-rep'),'2;1');")
    add(f"FILE_NAME('{safe}.step','{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}',('aces golden'),"
        f"('aces-os'),'aces_engineering.golden.step_export','','');")
    add("FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));")
    add("ENDSEC;")
    add("DATA;")

    # vertices: CARTESIAN_POINT + VERTEX_POINT per deduplicated vertex
    cp_ids, vp_ids = [], []
    for v in verts:
        c, p = nid(), nid()
        add(f"#{c}=CARTESIAN_POINT('',{fmt(v)});")
        add(f"#{p}=VERTEX_POINT('',#{c});")
        cp_ids.append(c)
        vp_ids.append(p)

    # edges: DIRECTION + VECTOR + LINE + EDGE_CURVE per deduplicated undirected edge
    edge_ids: dict[tuple[int, int], int] = {}
    for a, b in edges:
        d_, v_, l_, e_ = nid(), nid(), nid(), nid()
        add(f"#{d_}=DIRECTION('',{fmt(_norm(_sub(verts[b], verts[a])))});")
        add(f"#{v_}=VECTOR('',#{d_},1.);")
        add(f"#{l_}=LINE('',#{cp_ids[a]},#{v_});")
        add(f"#{e_}=EDGE_CURVE('',#{vp_ids[a]},#{vp_ids[b]},#{l_},.T.);")
        edge_ids[(a, b)] = e_

    # faces: 3 ORIENTED_EDGE + EDGE_LOOP + FACE_OUTER_BOUND + PLANE(+AXIS2_PLACEMENT_3D+DIRECTION) + FACE_SURFACE
    face_ids = []
    for t in tris:
        oes = []
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            key = (a, b) if a < b else (b, a)
            oe = nid()
            add(f"#{oe}=ORIENTED_EDGE('',*,*,#{edge_ids[key]},{'.T.' if key == (a, b) else '.F.'});")
            oes.append(oe)
        el_, fb_, dn, ax, pl, fs = nid(), nid(), nid(), nid(), nid(), nid()
        add(f"#{el_}=EDGE_LOOP('',({','.join('#' + str(x) for x in oes)}));")
        add(f"#{fb_}=FACE_OUTER_BOUND('',#{el_},.T.);")
        add(f"#{dn}=DIRECTION('',{fmt(_norm(_cross(_sub(verts[t[1]], verts[t[0]]), _sub(verts[t[2]], verts[t[0]]))))});")
        add(f"#{ax}=AXIS2_PLACEMENT_3D('',#{cp_ids[t[0]]},#{dn},$);")
        add(f"#{pl}=PLANE('',#{ax});")
        add(f"#{fs}=FACE_SURFACE('',#{fb_},#{pl},.T.);")
        face_ids.append(fs)

    # solid
    cs, fb = nid(), nid()
    add(f"#{cs}=CLOSED_SHELL('',({','.join('#' + str(f) for f in face_ids)}));")
    add(f"#{fb}=FACETED_BREP('{safe}',#{cs});")

    # units + geometric context
    unc, u1, u2, u3, ctx = nid(), nid(), nid(), nid(), nid()
    add(f"#{unc}=UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(0.001),#{u1},"
        f"'DISTANCE_ACCURACY_VALUE','mesh-derived faceted solid');")
    add(f"#{u1}=( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) );")
    add(f"#{u2}=( NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.) );")
    add(f"#{u3}=( NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT() );")
    add(f"#{ctx}=( GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{unc})) "
        f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{u1},#{u2},#{u3})) REPRESENTATION_CONTEXT('{safe}','3D') );")

    # product plumbing
    sr, pdf, prc, pr1, pd, pdfc, pd_, p, sdr = (nid() for _ in range(9))
    add(f"#{sr}=SHAPE_REPRESENTATION('{safe}',(#{fb}),#{ctx});")
    add(f"#{pdf}=APPLICATION_CONTEXT('automotive design');")
    add(f"#{prc}=PRODUCT_CONTEXT('',#{pdf},'mechanical');")
    add(f"#{pr1}=PRODUCT('{safe}','{safe}','mesh-derived faceted solid from aces golden',(#{prc}));")
    add(f"#{pd}=PRODUCT_DEFINITION_FORMATION('','',#{pr1});")
    add(f"#{pdfc}=PRODUCT_DEFINITION_CONTEXT('part definition',#{pdf},'design');")
    add(f"#{pd_}=PRODUCT_DEFINITION('design','',#{pd},#{pdfc});")
    add(f"#{p}=PRODUCT_DEFINITION_SHAPE('','faceted solid, mesh-derived — not a B-rep',#{pd_});")
    add(f"#{sdr}=SHAPE_DEFINITION_REPRESENTATION(#{p},#{sr});")

    add("ENDSEC;")
    add("END-ISO-10303-21;")
    Path(step_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(step_path), "faces": len(tris), "vertices": len(verts), "edges": len(edges),
            "entities": n, "faceted": True}
