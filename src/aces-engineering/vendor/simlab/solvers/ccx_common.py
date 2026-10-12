"""Shared pure helpers for the CalculiX adapters ccx-modal (ccx_modal.py) and ccx-thermal
(ccx_thermal.py): deck writing, ccx.dat parsing, boundary-face bookkeeping and the closed-form
references the validation cases compare against.

Standard library only, lists in and out: the queue and MCP containers import every adapter module
to read its stages, and the unit tests run on the workstation, which has neither gmsh nor ccx nor
aces_engineering. The gmsh meshing and the ccx run live in the adapters' run(), inside the
aces-golden image.

Units. Mechanics uses the mm-N-s-tonne consistent set: E in MPa (N/mm^2), density in tonne/mm^3
(= rho_kg_mm3 / 1000), so eigenfrequencies come out in Hz. Thermal uses the caller's consistent
set; the validation case works in mm-W-K (conductivity W/(mm K), flux W/mm^2).

Element type is C3D10 (gmsh 10-node tet, straight edges), numbered as in the aces golden
conforming check: corners 0-3, midside nodes 4-9 on the edges (0,1), (1,2), (2,0), (0,3), (1,3),
(2,3). ccx's face numbering for a tet (as unordered corner sets) is S1 = {0,1,2}, S2 = {0,1,3},
S3 = {1,2,3}, S4 = {0,2,3}, which is the order of FACES below.
"""
from __future__ import annotations

import math
import os
import re
import time

EDGES = ((0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3))   # midside nodes 4..9, in this order
FACES = ((0, 1, 2), (0, 1, 3), (1, 2, 3), (0, 2, 3))       # ccx S1..S4 as local corner triples
OPPOSITE = (3, 2, 0, 1)                                    # the corner each face does not contain
MID = {frozenset(e): 4 + k for k, e in enumerate(EDGES)}
AXES = "xyz"


class InputError(RuntimeError):
    """The case cannot be run as asked: a missing input file, a missing material property, a
    boundary that maps to nothing. The message must say exactly what is absent — an adapter that
    cannot run says so, it never invents numbers."""


class ParseError(ValueError):
    """ccx's output does not hold what the deck asked for."""


# -------------------------------------------------------------------------------- deck primitives

def fnum(x: float) -> str:
    """A float for the deck: repr when it fits ccx's 20-character number field, else the longest
    E format that does (ccx reads reals with an F20.0 edit; same rule as the conforming check's
    write_deck)."""
    x = float(x)
    s = repr(x)
    if len(s) <= 20:
        return s
    for p in range(16, 0, -1):
        s = f"{x:.{p}e}"
        if len(s) <= 20:
            return s
    raise ValueError(f"{x!r} does not fit a 20-character field")


def ints(values, per_line: int = 10) -> list:
    """Data lines of integers, 10 per line (ccx allows 16 entries / 132 characters per line)."""
    vals = [str(int(v)) for v in values]
    return [", ".join(vals[i:i + per_line]) for i in range(0, len(vals), per_line)]


def mesh_lines(nodes, tets) -> list:
    """*NODE, NSET=NALL and *ELEMENT, TYPE=C3D10, ELSET=EALL blocks; 0-based tets in, 1-based deck
    out. Nodes and elements are numbered 1..N in input order."""
    lines = ["*NODE, NSET=NALL"]
    lines += [f"{i + 1}, {fnum(x)}, {fnum(y)}, {fnum(z)}" for i, (x, y, z) in enumerate(nodes)]
    lines.append("*ELEMENT, TYPE=C3D10, ELSET=EALL")
    lines += [f"{e + 1}, " + ", ".join(str(int(v) + 1) for v in tets[e]) for e in range(len(tets))]
    return lines


def nset_lines(name: str, node_ids) -> list:
    """*NSET block for 0-based node indices (written 1-based, sorted)."""
    ids = sorted(int(v) + 1 for v in node_ids)
    if not ids:
        raise InputError(f"node set {name} is empty: refusing to write it into the deck")
    return [f"*NSET, NSET={name}"] + ints(ids)


# ----------------------------------------------------------------------------- boundary of a mesh

def boundary_faces(nodes, tets) -> list:
    """The boundary faces of a C3D10 mesh (pure Python): one dict per face that belongs to exactly
    one element, with elem (0-based element index), face (ccx face number 1-4), nodes (the six
    face nodes: 3 corners + 3 midside, 0-based), centroid, area and outward unit normal."""
    count = {}
    where = {}
    for e, tet in enumerate(tets):
        for f, loc in enumerate(FACES):
            tri = tuple(sorted(int(tet[i]) for i in loc))
            count[tri] = count.get(tri, 0) + 1
            where.setdefault(tri, []).append((e, f))
    out = []
    for tri, (e, f) in ((t, w[0]) for t, w in where.items() if count[t] == 1 and len(w) == 1):
        tet = [int(v) for v in tets[e]]
        loc = FACES[f]
        a, b, c = (tet[i] for i in loc)
        opp = tet[OPPOSITE[f]]
        xa, xb, xc, xo = (nodes[a], nodes[b], nodes[c], nodes[opp])
        u = [xb[k] - xa[k] for k in range(3)]
        v = [xc[k] - xa[k] for k in range(3)]
        nrm = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
        if sum(nrm[k] * (xo[k] - xa[k]) for k in range(3)) > 0:
            b, c = c, b
            nrm = [-x for x in nrm]
        norm = math.sqrt(sum(x * x for x in nrm))
        if norm <= 0:
            raise InputError(f"degenerate boundary face on element {e + 1} (zero area)")
        mid = [tet[MID[frozenset((loc[0], loc[1]))]], tet[MID[frozenset((loc[1], loc[2]))]],
               tet[MID[frozenset((loc[2], loc[0]))]]]
        out.append({"elem": e, "face": f + 1, "nodes": [a, b, c] + mid,
                    "centroid": [(nodes[a][k] + nodes[b][k] + nodes[c][k]) / 3.0 for k in range(3)],
                    "area": 0.5 * norm, "normal": [x / norm for x in nrm]})
    return out


def planar_axis(box) -> int:
    """The axis a box is planar on (box[a] == box[a+3]); InputError when it is planar on no axis
    or on more than one."""
    planar = [a for a in range(3) if abs(float(box[a]) - float(box[a + 3])) <= 1e-9 * max(1.0, abs(float(box[a])))]
    if len(planar) != 1:
        raise InputError(f"box {[float(v) for v in box]} must be planar on exactly one axis (it is on {planar})")
    return planar[0]


def faces_on_plane(faces: list, box, window: float) -> list:
    """Boundary faces whose centroids lie within `window` of the plane of a planar box and inside
    its in-plane extent, facing outward (|n[a]| >= 1/sqrt(2), pointing away from the part: -a at
    the box's own plane when the rest of the box is on the + side and vice versa)."""
    a = planar_axis(box)
    p = float(box[a])
    others = [j for j in range(3) if j != a]
    n_min = 0.7071067811865476
    sel = []
    for f in faces:
        c = f["centroid"]
        if abs(c[a] - p) > window:
            continue
        if not all(float(box[j]) - 1e-9 <= c[j] <= float(box[j + 3]) + 1e-9 for j in others):
            continue
        if abs(f["normal"][a]) < n_min:
            continue
        sel.append(f)
    return sel


def face_node_ids(faces: list) -> list:
    """Sorted unique 0-based node ids touched by the faces (corners and midside nodes)."""
    return sorted({n for f in faces for n in f["nodes"]})


def nodes_in_box(nodes, box, tol: float = 1e-6) -> list:
    """0-based indices of the nodes inside a box (inclusive, +-tol)."""
    out = []
    for i, x in enumerate(nodes):
        if all(float(box[a]) - tol <= x[a] <= float(box[a + 3]) + tol for a in range(3)):
            out.append(i)
    return out


# ----------------------------------------------------------------------------------- ccx.dat parsing

_FLOAT = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[EeDd][-+]?\d+|[-+]\d{3})?")


def ffloat(token: str) -> float:
    """A Fortran E-format number: 1.234567E-03, 1.234567D-03, or 1.234567-100 (three-digit
    exponent without the E)."""
    t = token.replace("D", "E").replace("d", "e")
    if "E" not in t and "e" not in t:
        t = re.sub(r"(?<=\d)([-+]\d{3})$", r"E\1", t)
    return float(t)


_BLOCK_HEAD = re.compile(r"^\s*([\w ]+?)\s*(?:\(([^)]*)\))?\s*for set\s+(\S+)\s+and time\s+(\S+)", re.IGNORECASE)


def dat_blocks(text: str) -> list:
    """The labelled blocks of a ccx .dat file: [(kind, set_name, data_lines)] where kind is the
    block's own words lowercased ('temperatures', 'displacements', 'stresses', 'total force')."""
    blocks, cur = [], None
    for raw in text.splitlines():
        m = _BLOCK_HEAD.match(raw)
        if m:
            cur = [m.group(1).strip().lower(), m.group(3).upper(), []]
            blocks.append(cur)
            continue
        if cur is not None:
            if raw.strip():
                cur[2].append(raw)
            elif cur[2]:
                cur = None
    return [(k, s, lines) for k, s, lines in blocks]


def parse_temperatures_dat(text: str, set_name: str = "NALL") -> dict:
    """{node_id (1-based): temperature} from the 'temperatures for set <name>' block of ccx.dat."""
    for kind, name, lines in dat_blocks(text):
        if kind.startswith("temperatures") and name == set_name.upper():
            out = {}
            for ln in lines:
                toks = ln.split()
                if len(toks) != 2:
                    toks = toks[:1] + _FLOAT.findall(" ".join(toks[1:]))
                if len(toks) != 2:
                    raise ParseError(f"temperatures {set_name}: unexpected line {ln.strip()[:80]!r}")
                out[int(toks[0])] = ffloat(toks[1])
            if not out:
                raise ParseError(f"temperatures for set {set_name}: the block is empty")
            return out
    raise ParseError(f"ccx.dat has no temperatures for set {set_name}")


# A mode row of the ccx .dat eigenfrequency table: mode number, eigenvalue, real part (rad/time),
# real part (cycles/time) and, since ccx 2.x writes complex-frequency output, the imaginary part
# (rad/time) — optional for older formats.
_MODE_ROW = re.compile(r"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(\S+))?\s*$")


def parse_eigenfrequencies_dat(text: str) -> list:
    """The eigenfrequency table ccx writes into .dat for a *FREQUENCY step:
    [{mode, eigenvalue, omega_rad_per_s, frequency_hz, omega_imag_rad_per_s}], in mode order.
    The section header is letter-spaced in ccx 2.23 ('E I G E N V A L U E   O U T P U T'), so the
    anchor is matched with spaces removed."""
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        if "EIGENVALUE" in raw.upper().replace(" ", ""):
            modes = []
            for ln in lines[i + 1:]:
                m = _MODE_ROW.match(ln)
                if m:
                    modes.append({"mode": int(m.group(1)), "eigenvalue": ffloat(m.group(2)),
                                  "omega_rad_per_s": ffloat(m.group(3)), "frequency_hz": ffloat(m.group(4)),
                                  "omega_imag_rad_per_s": ffloat(m.group(5)) if m.group(5) else None})
                elif modes:
                    break
            if modes:
                return modes
    raise ParseError("ccx.dat has no eigenfrequency table (no EIGENVALUE header with mode rows)")


# --------------------------------------------------------------------------- closed-form references

# roots of cos(beta) cosh(beta) = -1 (cantilever characteristic equation)
CANTILEVER_BETAS = (1.875104068711961, 4.694091132974175, 7.854757438237613)


def euler_bernoulli_frequencies_hz(L_m: float, E_Pa: float, I_m4: float, rho_kg_m3: float,
                                   A_m2: float, n: int = 1) -> list:
    """Cantilever natural frequencies f_n = beta_n^2 / (2 pi) sqrt(E I / (rho A L^4)), Hz."""
    if n > len(CANTILEVER_BETAS):
        raise InputError(f"closed-form cantilever reference has {len(CANTILEVER_BETAS)} modes, not {n}")
    if not all(v > 0 for v in (L_m, E_Pa, I_m4, rho_kg_m3, A_m2)):
        raise InputError("Euler-Bernoulli reference needs positive L, E, I, rho and A")
    base = math.sqrt(E_Pa * I_m4 / (rho_kg_m3 * A_m2 * L_m ** 4)) / (2.0 * math.pi)
    return [b * b * base for b in CANTILEVER_BETAS[:n]]


def beam_rect_reference(L_mm: float, b_mm: float, h_mm: float, E_MPa: float, rho_kg_mm3: float,
                        n: int = 1) -> dict:
    """Euler-Bernoulli cantilever frequencies of a rectangular beam b x h (bending in the h
    direction), from mm/MPa inputs converted to SI."""
    L_m, b_m, h_m = L_mm / 1000.0, b_mm / 1000.0, h_mm / 1000.0
    I_m4 = b_m * h_m ** 3 / 12.0
    A_m2 = b_m * h_m
    freqs = euler_bernoulli_frequencies_hz(L_m, E_MPa * 1e6, I_m4, rho_kg_mm3 * 1e9, A_m2, n)
    return {"kind": "euler_bernoulli_cantilever", "L_mm": float(L_mm), "b_mm": float(b_mm),
            "h_mm": float(h_mm), "E_MPa": float(E_MPa), "rho_kg_mm3": float(rho_kg_mm3),
            "I_m4": I_m4, "A_m2": A_m2, "frequencies_hz": freqs,
            "formula": "f_n = beta_n^2/(2 pi) sqrt(E I/(rho A L^4)), beta = 1.8751, 4.6941, 7.8548"}


def slab_conduction_delta_t(flux: float, L: float, k: float) -> float:
    """1-D steady conduction across a slab: delta T = q L / k (any consistent unit set)."""
    if not k > 0:
        raise InputError("slab conduction reference needs a positive conductivity")
    if not L > 0:
        raise InputError("slab conduction reference needs a positive thickness")
    return float(flux) * float(L) / float(k)


# ----------------------------------------------------------------- the rig-side stack (lazy imports)

def require_ccx_stack(solver: str):
    """(conforming module, availability dict) inside the aces-golden image; raises with an honest
    message where gmsh or ccx is absent. Imported here, never at module level."""
    try:
        from aces_engineering.golden import conforming
    except ImportError as exc:
        raise InputError(f"{solver}: cannot run here — aces_engineering.golden.conforming is not "
                         f"importable ({exc}); this adapter runs inside the aces-golden image "
                         f"(gmsh + ccx), which this Python is not") from None
    av = conforming.available()
    if not av.get("gmsh"):
        raise InputError(f"{solver}: cannot run here — {av.get('gmsh_reason') or 'gmsh not found'}")
    if not av.get("usable"):
        raise InputError(f"{solver}: cannot run here — {av.get('reason') or 'ccx not found'}")
    return conforming, av


def mesh_stl_tet10(conforming, stl_path: str, out_dir: str, size_mm: float, case_id: str,
                   attempts: str = "ABCD") -> dict:
    """The aces golden conforming mesher on the part's STL (gmsh C3D10, straight edges, always
    single-threaded: multi-thread gmsh meshes differ run to run). Writes mesh.npz + tetmesh.json
    in out_dir; raises InputError with the mesher's own status and message on any failure."""
    rec = conforming.mesh_stl(stl_path, out_dir, size=float(size_mm), threads=1, case_id=case_id,
                              attempts=attempts)
    if rec.get("status") != "ok":
        raise InputError(f"meshing {os.path.basename(str(stl_path))} failed: {rec.get('status')}: "
                         f"{rec.get('message') or 'no message'}")
    return rec


def load_mesh_npz(out_dir: str):
    """(nodes, tets) of out_dir/mesh.npz as plain lists (0-based C3D10 connectivity)."""
    import numpy as np

    with np.load(os.path.join(out_dir, "mesh.npz")) as z:
        nodes = [[float(v) for v in row] for row in z["nodes"]]
        tets = [[int(v) for v in row] for row in z["tets"]]
    if not nodes or not tets:
        raise ParseError(f"{os.path.join(out_dir, 'mesh.npz')} holds no nodes or no elements")
    return nodes, tets


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def clean_case_outputs(work_dir: str, jobname: str) -> None:
    """Remove a previous attempt's deck and outputs: a rerun in the same case directory must never
    read stale results as its own."""
    for name in (f"{jobname}.inp", f"{jobname}.dat", f"{jobname}.log", f"{jobname}.frd",
                 f"{jobname}.sta", f"{jobname}.cvg", f"{jobname}.12d", "spooles.out"):
        try:
            os.remove(os.path.join(work_dir, name))
        except FileNotFoundError:
            pass


def run_ccx(ccx_path: str, work_dir: str, jobname: str, threads: int, timeout_s: float) -> dict:
    """Run `ccx <jobname>` in work_dir (the deck is <jobname>.inp); capture <jobname>.log, honour
    the timeout, and refuse on a non-zero exit, an *ERROR line, or a missing .dat. Returns the
    solver provenance block {rc, wall_s, errors, warnings}."""
    import subprocess

    env = dict(os.environ)
    for k in ("OMP_NUM_THREADS", "CCX_NPROC_EQUATION_SOLVER", "CCX_NPROC_STIFFNESS", "CCX_NPROC_RESULTS"):
        env[k] = str(int(threads))
    log_path = os.path.join(work_dir, jobname + ".log")
    t0 = time.time()
    timed_out = False
    with open(log_path, "w", encoding="utf-8", newline="\n") as log:
        log.write("$ " + " ".join([ccx_path, jobname]) + "\n")
        log.flush()
        try:
            p = subprocess.run([ccx_path, jobname], cwd=work_dir, env=env, stdout=log,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=float(timeout_s))
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc, timed_out = None, True
    wall_s = time.time() - t0
    log_text = read_text(log_path)
    errors = [ln.strip()[:300] for ln in log_text.splitlines() if "*ERROR" in ln][:20]
    warnings = [ln.strip()[:300] for ln in log_text.splitlines() if "*WARNING" in ln][:20]
    out = {"rc": rc, "wall_s": wall_s, "errors": errors, "warnings": warnings, "log": log_path}
    dat = os.path.join(work_dir, jobname + ".dat")
    if timed_out:
        raise RuntimeError(f"ccx did not finish {jobname} within {float(timeout_s):g} s (killed); "
                           f"see {log_path}")
    if rc != 0 or errors or not os.path.isfile(dat):
        why = errors[0] if errors else (f"ccx exited {rc}" if rc else f"ccx wrote no {jobname}.dat")
        raise RuntimeError(f"ccx failed on {jobname}: {why} (see {log_path})")
    return out
