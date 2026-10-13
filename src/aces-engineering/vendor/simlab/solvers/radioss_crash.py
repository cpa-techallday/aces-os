"""radioss-crash: explicit-dynamics crash tier — OpenCourant (community fork of OpenRadioss).

Two case modes:

  tube   the adapter generates an axial-crush deck for a square thin-walled tube: parametric
         4-node-shell mesh (pure python), /MAT/PLAS_JOHNS (LAW2), /PROP/TYPE1, /INTER/TYPE7
         self-contact (frictionless), a fixed rigid wall at z=0 and a free-flight impactor
         (rigid wall with mass + initial velocity). Reports the impactor reaction force
         history (impulse -> force by numerical differentiation, as the T01 file stores
         impulses — see the Radioss /TH/RWALL reference), mean crush force, crush distance
         (anchor-node displacement), energy balance, and optionally the Wierzbicki-
         Abramowicz closed-form mean force for a square column as a reference
         (Wierzbicki & Abramowicz 1983, J. Appl. Mech. 50(4a):727-734; P_m = 9.56 *
         sigma0 * h^(5/3) * c^(1/3)). The adapter never decides pass/fail — that belongs
         to the registry record.

  deck   user-supplied <name>_0000.rad / <name>_0001.rad run verbatim (starter then
         engine); the adapter parses whatever /TH output the deck writes into global
         energies and rigid-wall forces. No geometry is generated and no closed-form
         reference is applied.

Deck unit system is kg-mm-ms (force kN, stress GPa, energy J, velocity mm/ms = m/s);
case params are SI and converted here. Decks are 2019-format (the format the pinned
OpenCourant 2026-10-11 binaries are QA'd against; the two QA tube decks in
qa-tests/miniqa/CRASH were run end-to-end against this adapter's toolchain choices).

The deck writing and the T01-CSV parsing are pure python and unit-tested on the
workstation; the solver itself only runs where OPENRADIOSS_PATH points at the
binaries (inside the simlab-radioss image).
"""
from __future__ import annotations

import csv
import json
import math
import os
import shutil
import subprocess
import time

SOLVER = "radioss-crash"


class InputError(ValueError):
    pass


class SolverError(RuntimeError):
    """The starter or the engine failed; the message quotes the listing's own error lines."""


# ---------------------------------------------------------------------------
# unit conversion: SI (case params) -> kg-mm-ms (deck)


def si_to_deck(p: dict) -> dict:
    """SI -> kg-mm-ms. Velocities are numerically identical (m/s = mm/ms)."""
    return {
        "width_mm": float(p["width_m"]) * 1e3,
        "length_mm": float(p["length_m"]) * 1e3,
        "thick_mm": float(p["thickness_m"]) * 1e3,
        "E_GPa": float(p["material"]["E_Pa"]) * 1e-9,
        "nu": float(p["material"]["nu"]),
        "rho_kg_mm3": float(p["material"]["rho_kg_m3"]) * 1e-9,
        "sy_GPa": float(p["material"]["yield_Pa"]) * 1e-9,
        "hard_b_GPa": float(p["material"].get("hardening_b_Pa", 0.0)) * 1e-9,
        "hard_n": float(p["material"].get("hardening_n", 1.0)),
        "mass_kg": float(p["impactor"]["mass_kg"]),
        "vel_mm_ms": float(p["impactor"]["velocity_mps"]),
        "end_ms": float(p["end_s"]) * 1e3,
    }


# ---------------------------------------------------------------------------
# parametric square-tube mesh (pure)


def tube_mesh(width_mm: float, length_mm: float, n_side: int, n_z: int):
    """4-node shell mesh of a square tube along +z, section centred on the z axis.
    n_side elements per face per row (corners shared between faces), n_z rows.
    Returns (nodes, elems) with 1-based ids; nodes are (x, y, z) mm."""
    if n_side < 2:
        raise InputError(f"radioss-crash: mesh needs at least 2 elements per side, got {n_side}")
    if n_z < 4:
        raise InputError(f"radioss-crash: mesh needs at least 4 rows along the tube, got {n_z}")
    h = width_mm / 2.0
    ring = 4 * n_side
    nodes = []  # (x, y, z)
    for k in range(n_z + 1):
        z = length_mm * k / n_z
        for s in range(4):
            for j in range(n_side):
                t = j / n_side
                if s == 0:
                    x, y = -h + width_mm * t, h
                elif s == 1:
                    x, y = h, h - width_mm * t
                elif s == 2:
                    x, y = h - width_mm * t, -h
                else:
                    x, y = -h, -h + width_mm * t
                nodes.append((x, y, z))
    elems = []  # (n1, n2, n3, n4), 1-based
    for k in range(n_z):
        b0, b1 = k * ring, (k + 1) * ring
        for s in range(4):
            for j in range(n_side):
                a = s * n_side + j
                b = s * n_side + ((j + 1) % n_side) if j + 1 < n_side else (s + 1) % 4 * n_side
                elems.append((b0 + a + 1, b0 + b + 1, b1 + b + 1, b1 + a + 1))
    return nodes, elems


# ---------------------------------------------------------------------------
# deck writing (pure; the card layouts mirror qa-tests/miniqa/CRASH decks)


def _i(v: int) -> str:
    return f"{int(v):>10d}"


def _f(v: float) -> str:
    return f"{float(v):>20.10g}"


def _comment(text: str) -> list:
    return ["#---1----|----2----|----3----|----4----|----5----|----6----|----7----|----8----|----9----|---10----|",
            f"# {text}"]


def build_starter_deck(name: str, d: dict, nodes, elems, anchor_id: int, gap_mm: float,
                       offset_mm: float, th_node: bool) -> list:
    """The *_0000.rad starter deck for the generated tube. Card field orders follow the
    OpenCourant QA decks qa-tests/miniqa/CRASH/crashtriangles (TRITUBV4) and
    crashsection/box1_42 (BOX1BL), run against the pinned binaries 2026-10-13."""
    nn = len(nodes)
    L = d["length_mm"]
    lines = ["#RADIOSS STARTER", "/BEGIN", f"{name:<80}", f"{2019:>10d}{0:>10d}",
             f"{'kg':>20s}{'mm':>20s}{'ms':>20s}", f"{'kg':>20s}{'mm':>20s}{'ms':>20s}"]
    lines += _comment("1. CONTROL CARDS")
    lines += ["/TITLE", f"radioss-crash tube: c={d['width_mm']:g} h={d['thick_mm']:g} L={L:g} mm",
              "/ANALY", _i(0) + "                   0         0"]
    lines += _comment("2. MATERIAL: /MAT/PLAS_JOHNS (LAW2), kg-mm-ms")
    lines += ["/MAT/PLAS_JOHNS/1", "tube_material",
              "#              RHO_I", _f(d["rho_kg_mm3"]) + _f(0),
              "#                  E                  NU               IFLAG",
              _f(d["E_GPa"]) + _f(d["nu"]) + _i(0),
              "#                  A                   B                   N           EPS_P_MAX            SIG_MAX0",
              _f(d["sy_GPa"]) + _f(d["hard_b_GPa"]) + _f(d["hard_n"]) + _f(1e30) + _f(1e30),
              "#                  C           EPS_DOT_0       ICC   FSMOOTH               F_CUT               CHARD",
              _f(0) + _f(0) + _i(0) + _i(0) + _f(0) + _f(0),
              "#                  M              T_MELT              RHOCP                 T_R",
              _f(0) + _f(0) + _f(0) + _f(0)]
    lines += _comment("3. NODES")
    lines.append("/NODE")
    for i, (x, y, z) in enumerate(nodes):
        lines.append(_i(i + 1) + _f(x) + _f(y) + _f(z))
    lines.append(_i(anchor_id) + _f(0) + _f(0) + _f(L + gap_mm))  # impactor anchor
    lines += _comment("4. PART")
    lines += ["/PART/1", "tube", _i(1) + _i(1) + _i(0)]
    lines += _comment("5. SHELLS")
    lines.append("/SHELL/1")
    for i, (a, b, c, e) in enumerate(elems):
        lines.append(_i(i + 1) + _i(a) + _i(b) + _i(c) + _i(e))
    lines += _comment("PROPERTY: /PROP/TYPE1 (SHELL), 3 through-thickness points")
    lines += ["/PROP/TYPE1/1", "tube_prop",
              "#   ISHELL    ISMSTR     ISH3N    IDRILL                            P_THICK_FAIL",
              _i(0) + _i(0) + _i(0) + _i(0) + _f(0),
              "#                 HM                  HF                  HR                  DM                  DN",
              _f(0) + _f(0) + _f(0) + _f(0) + _f(0),
              "#        N   ISTRAIN               THICK              ASHEAR              ITHICK     IPLAS",
              _i(3) + _i(0) + _f(d["thick_mm"]) + _f(0) + _f(0) + _i(0)]
    lines += _comment("6. SELF-CONTACT: /INTER/TYPE7, frictionless (W-A assumption)")
    lines += ["/INTER/TYPE7/1", "tube_self",
              "#  SLAV_ID   MAST_ID      ISTF      ITHE      IGAP                IBAG      IDEL     ICURV      IADM",
              _i(2) + _i(3) + _i(0) + _i(0) + _i(0) + _i(0) + _i(0) + _i(0) + _i(0),
              "#          FSCALEGAP             GAP_MAX             FPENMAX",
              _f(0) + _f(0) + _f(0),
              "#              STMIN               STMAX          %MESH_SIZE               DTMIN  IREM_GAP   IREM_I2",
              _f(0) + _f(0) + _f(0) + _f(0) + _i(0) + _i(0),
              "#              STFAC                FRIC              GAPMIN              TSTART               TSTOP",
              _f(0.2) + _f(0) + _f(0) + _f(0) + _f(1e30),
              "#      IBC                        INACTI                VISS                VISF              BUMULT",
              "       000" + _i(0) + _f(0) + _f(0) + _f(0),
              "#    IFRIC    IFILTR               XFREQ     IFORM   SENS_ID   FCT_IDF             ASCALEF   FRIC_ID",
              _i(0) + _i(0) + _i(0) + _i(0) + _i(0) + _i(0) + _f(0) + _i(0)]
    lines += ["/GRNOD/PART/2", "tube_self_secondary", _i(1),
              "/SURF/PART/3", "tube_self_main", _i(1)]
    lines += _comment("7. NODE GROUP: every tube node (the rigid walls' secondary set)")
    lines.append("/GRNOD/NODE/7")
    lines.append("tube_nodes")
    row = []
    for i in range(1, nn + 1):
        row.append(_i(i))
        if len(row) == 10:
            lines.append("".join(row))
            row = []
    if row:
        lines.append("".join(row))
    lines += _comment("8. RIGID WALLS: fixed plane z=0; free-flight impactor above the tube")
    lines += ["/RWALL/PLANE/501", "wall_fixed",
              "#  NODE_ID     SLIDE GRNOD_ID1 GRNOD_ID2",
              _i(0) + _i(0) + _i(7) + _i(0),
              "#           D_SEARCH                FRIC            DIAMETER                FFAC       IFQ",
              _f(offset_mm) + _f(0) + _f(0) + _f(0) + _i(0),
              "#                 XM                  YM                  ZM",
              _f(0) + _f(0) + _f(0),
              "#               X_M1                Y_M1                Z_M1",
              _f(0) + _f(0) + _f(1)]
    lines += ["/RWALL/PLANE/500", "wall_impactor",
              "#  NODE_ID     SLIDE GRNOD_ID1 GRNOD_ID2",
              _i(anchor_id) + _i(0) + _i(7) + _i(0),
              "#           D_SEARCH                FRIC            DIAMETER                FFAC       IFQ",
              _f(offset_mm) + _f(0) + _f(0) + _f(0) + _i(0),
              "#               MASS                VX_0                VY_0                VZ_0",
              _f(d["mass_kg"]) + _f(0) + _f(0) + _f(-d["vel_mm_ms"]),
              "#               X_M1                Y_M1                Z_M1",
              _f(0) + _f(0) + _f(1)]
    lines += _comment("9. TIME HISTORIES")
    lines += ["/TH/PART/1", "part_energies", "#     VAR1      VAR2      VAR3", "DEF       ", _i(1)]
    lines += ["/TH/RWALL/2", "Rigid walls", "#     VAR1      VAR2      VAR3", "DEF       ",
              _i(500) + _i(501)]
    if th_node:
        lines += ["/TH/NODE/3", "impactor_anchor", "#     VAR1      VAR2      VAR3",
                  "D         V         ", _i(anchor_id)]
    lines.append("/END")
    return lines


def build_engine_deck(name: str, end_ms: float, dt_his_ms: float, anim: bool) -> list:
    """The *_0001.rad engine deck (2019 format, as the QA decks)."""
    lines = ["#RADIOSS ENGINE", "/RUN/" + name + "/1", _f(end_ms)]
    lines += ["/TFILE/4", "#            DT_HIS", _f(dt_his_ms)]
    lines += ["/DT", _f(0.9) + _f(0)]
    lines.append("/MON/ON")
    if anim:
        lines += ["/ANIM/DT", _f(0) + _f(end_ms / 20.0)]
    lines.append("/VERS/2019")
    return lines


# ---------------------------------------------------------------------------
# Wierzbicki-Abramowicz closed form (pure)


def wierzbicki_abramowicz(sigma0_GPa: float, h_mm: float, c_mm: float) -> float:
    """Mean crush force [kN] of a square thin-walled column, Wierzbicki & Abramowicz (1983),
    J. Appl. Mech. 50(4a):727-734: P_m = 9.56 sigma0 h^(5/3) c^(1/3). GPa = kN/mm^2, so the
    product is kN. sigma0 is the material's flow stress; for a perfectly plastic LAW2 it is
    the yield stress (the adapter reports which sigma0 it used)."""
    return 9.56 * sigma0_GPa * h_mm ** (5.0 / 3.0) * c_mm ** (1.0 / 3.0)


# ---------------------------------------------------------------------------
# T01 CSV parsing + metrics (pure)


RWALL_VARS = ["FNX", "FNY", "FNZ", "FTX", "FTY", "FTZ"]  # DEF order, /TH/RWALL reference


def parse_th_csv(path: str) -> dict:
    """Parse the th_to_csv output. Returns {"time": [...], "global": {name: [...]},
    "rwall": {wall_id: {var: [...]}}, "node": {colname: [...]}}. Column headers for walls
    look like 'Rigid walls                              500 rw z=10                  var 33';
    the var index maps onto RWALL_VARS in order. Values are floats; the time column is
    keyed "time"."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        raise InputError(f"radioss-crash: TH csv {path} has no data rows")
    header = [h.strip('"') for h in rows[0]]
    cols = list(zip(*[[float(c) if c not in ("", None) else float("nan") for c in r[: len(header)]]
                      for r in rows[1:] if r and r[0]]))
    out = {"time": list(cols[0]), "global": {}, "rwall": {}, "node": {}}
    for j, h in enumerate(header):
        if j == 0:
            continue
        vals = list(cols[j])
        hs = h.strip()
        if hs.startswith("Rigid walls"):
            parts = hs.split()
            try:
                wall_id = int(parts[2])
                var_idx = int(parts[-1])
            except (ValueError, IndexError):
                continue
            base = (var_idx - 33) % len(RWALL_VARS)  # vars 33.. are FNX..FTZ per wall, in blocks
            out["rwall"].setdefault(wall_id, {})[RWALL_VARS[base]] = vals
        elif "  " in hs:
            # per-object columns ("<group title padded>  <VAR>"): node displacements/velocities
            # are needed for the crush distance; per-part energies are filtered out downstream
            tail = hs.split()[-1]
            if tail in ("DX", "DY", "DZ", "VX", "VY", "VZ"):
                out["node"][tail] = vals
            else:
                out["global"][hs] = vals
        else:
            out["global"][hs] = vals
    return out


def differentiate(time_ms, impulse):
    """Impulse [kN.ms] -> force [kN] by central differences (T01 stores impulses on walls)."""
    n = len(time_ms)
    f = [0.0] * n
    for i in range(n):
        i0, i1 = max(0, i - 1), min(n - 1, i + 1)
        dt = time_ms[i1] - time_ms[i0]
        f[i] = (impulse[i1] - impulse[i0]) / dt if dt > 0 else 0.0
    return f


def crush_metrics(time_ms, force_kN, crush_mm):
    """Mean crush force over the steady region: the middle 60% of the crush distance
    (excludes the initial peak and the tail where the impactor may be spent). Returns
    None when fewer than 10 samples lie in the window."""
    samples = [(z, f) for z, f in zip(crush_mm, force_kN) if z > 0]
    if len(samples) < 10:
        return None
    z_max = samples[-1][0]
    lo, hi = 0.2 * z_max, 0.8 * z_max
    mid = [f for z, f in samples if lo <= z <= hi]
    if len(mid) < 5:
        return None
    mean = sum(mid) / len(mid)
    return {"crush_mm": z_max, "p_mean_kN": mean, "p_peak_kN": max(f for _, f in samples),
            "n_samples": len(samples), "window": [lo, hi]}


# ---------------------------------------------------------------------------
# case validation


def validate_case(case: dict, spec) -> dict:
    mode = str(case.get("mode", "tube"))
    if mode not in ("tube", "deck"):
        raise InputError(f"radioss-crash: mode must be 'tube' or 'deck', got {mode!r}")
    p = {"mode": mode, "threads": int(case.get("threads", 4)),
         "timeout_s": float(case.get("timeout_s", 3600.0))}
    if mode == "deck":
        deck = case.get("deck")
        if not deck:
            raise InputError("radioss-crash: deck mode needs 'deck' (path of <name>_0000.rad; "
                             "the engine deck <name>_0001.rad must sit beside it)")
        deck = spec.resolve(str(deck))
        if not deck.endswith("_0000.rad"):
            raise InputError(f"radioss-crash: the starter deck must be named <name>_0000.rad, got {deck}")
        if not os.path.isfile(deck):
            raise InputError(f"radioss-crash: the deck {deck} is not here")
        engine_deck = deck[:-len("_0000.rad")] + "_0001.rad"
        if not os.path.isfile(engine_deck):
            raise InputError(f"radioss-crash: the engine deck {engine_deck} is not here")
        p.update({"deck": deck, "engine_deck": engine_deck})
        return p
    g = case.get("geometry") or {}
    for key in ("width_m", "length_m", "thickness_m"):
        if g.get(key) is None:
            raise InputError(f"radioss-crash: geometry.{key} is missing")
        if float(g[key]) <= 0:
            raise InputError(f"radioss-crash: geometry.{key} must be > 0")
    m = case.get("material") or {}
    for key in ("E_Pa", "nu", "rho_kg_m3", "yield_Pa"):
        if m.get(key) is None:
            raise InputError(f"radioss-crash: material.{key} is missing — an explicit-dynamics run "
                             "without density/stiffness/strength is not a crash model; refusing to run")
    imp = case.get("impactor") or {}
    for key in ("mass_kg", "velocity_mps"):
        if imp.get(key) is None:
            raise InputError(f"radioss-crash: impactor.{key} is missing (free-flight rigid wall; "
                             "its mass and velocity are modelling decisions, never defaulted)")
    if case.get("end_s") is None:
        raise InputError("radioss-crash: end_s is missing (how long to crush is a modelling decision)")
    mesh = case.get("mesh") or {}
    p.update({"geometry": {k: float(g[k]) for k in ("width_m", "length_m", "thickness_m")},
              "material": dict(m), "impactor": {k: float(imp[k]) for k in ("mass_kg", "velocity_mps")},
              "end_s": float(case["end_s"]),
              "elem_m": float(mesh.get("size_m", float(g["width_m"]) / 20.0)),
              "gap_m": float(case.get("gap_m", 0.01)),
              "reference": case.get("reference"), "anim": bool(case.get("anim", False))})
    if p["elem_m"] <= 0:
        raise InputError("radioss-crash: mesh.size_m must be > 0")
    if p["geometry"]["thickness_m"] >= p["geometry"]["width_m"] / 2:
        raise InputError("radioss-crash: a tube with thickness >= width/2 is solid; this mode models "
                         "thin-walled tubes only")
    return p


# ---------------------------------------------------------------------------
# solver invocation (needs OPENRADIOSS_PATH; inside the simlab-radioss image)


def _binary(name: str) -> str:
    root = os.environ.get("OPENRADIOSS_PATH")
    if not root:
        raise SolverError("radioss-crash: OPENRADIOSS_PATH is not set — the OpenCourant binaries are "
                          "only present inside the simlab-radioss image; refusing to guess a path")
    exe = os.path.join(root, "exec", name)
    if not os.path.isfile(exe):
        raise SolverError(f"radioss-crash: {exe} is not here")
    return exe


def _run_tool(argv: list, work: str, log_name: str, timeout_s: float, listing: str,
              ok_marker: str) -> dict:
    t0 = time.time()
    with open(os.path.join(work, log_name), "w", encoding="utf-8") as log:
        rc = subprocess.run(argv, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                            timeout=timeout_s).returncode
    text = ""
    lp = os.path.join(work, listing)
    if os.path.isfile(lp):
        with open(lp, encoding="utf-8", errors="replace") as f:
            text = f.read()
    errors = [ln.strip() for ln in text.splitlines() if "** ERROR" in ln]
    if rc != 0 or errors or (ok_marker not in text):
        detail = "; ".join(errors[:4]) or f"rc={rc} (see {log_name} and {listing})"
        raise SolverError(f"radioss-crash: {os.path.basename(argv[0])} failed: {detail}")
    return {"rc": rc, "wall_s": round(time.time() - t0, 2), "listing": lp,
            "log": os.path.join(work, log_name)}


class Adapter:
    name = SOLVER

    def inputs(self, case: dict, spec) -> list:
        if str(case.get("mode", "tube")) == "deck" and case.get("deck"):
            d = spec.resolve(str(case["deck"]))
            return [d, d[:-len("_0000.rad")] + "_0001.rad"]
        return []

    def run(self, case: dict, spec, run_dir: str) -> dict:
        t0 = time.time()
        p = validate_case(case, spec)
        starter = _binary("starter_linux64_gf")
        engine = _binary("engine_linux64_gf")
        th_to_csv = _binary("th_to_csv_linux64_gf")
        work = os.path.join(os.path.abspath(run_dir), "radioss")
        os.makedirs(work, exist_ok=True)

        if p["mode"] == "deck":
            name = os.path.basename(p["deck"])[:-len("_0000.rad")]
            shutil.copy2(p["deck"], os.path.join(work, name + "_0000.rad"))
            shutil.copy2(p["engine_deck"], os.path.join(work, name + "_0001.rad"))
            d = None
            anchor_id = None
        else:
            name = str(case.get("case_id", "c000")).replace("/", "_")
            d = si_to_deck({**p["geometry"], "material": p["material"], "impactor": p["impactor"],
                            "end_s": p["end_s"]})
            n_side = max(2, int(round(d["width_mm"] / (p["elem_m"] * 1e3))))
            n_z = max(4, int(round(d["length_mm"] / (p["elem_m"] * 1e3))))
            nodes, elems = tube_mesh(d["width_mm"], d["length_mm"], n_side, n_z)
            anchor_id = len(nodes) + 1
            gap_mm = p["gap_m"] * 1e3
            offset_mm = 2.0 * p["elem_m"] * 1e3
            deck = build_starter_deck(name, d, nodes, elems, anchor_id, gap_mm, offset_mm,
                                      th_node=True)
            with open(os.path.join(work, name + "_0000.rad"), "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(deck) + "\n")
            dt_his = max(d["end_ms"] / 2000.0, 1e-3)
            eng = build_engine_deck(name, d["end_ms"], dt_his, p["anim"])
            with open(os.path.join(work, name + "_0001.rad"), "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(eng) + "\n")

        st = _run_tool([starter, "-input", name + "_0000.rad", "-nt", str(p["threads"]), "-nspmd", "1"],
                       work, "starter.log", p["timeout_s"], name + "_0000.out", "0 ERROR(S)")
        en = _run_tool([engine, "-input", name + "_0001.rad", "-nt", str(p["threads"])],
                       work, "engine.log", p["timeout_s"], name + "_0001.out", "NORMAL TERMINATION")

        # time history -> csv (the tool writes <name>T01.csv next to <name>T01)
        t01 = os.path.join(work, name + "T01")
        if not os.path.isfile(t01):
            raise SolverError(f"radioss-crash: the engine wrote no {name}T01 (no /TH in the deck?)")
        with open(os.path.join(work, "th_to_csv.log"), "w", encoding="utf-8") as log:
            rc = subprocess.run([th_to_csv, t01], cwd=work, stdout=log,
                                stderr=subprocess.STDOUT, timeout=300).returncode
        csv_path = t01 + ".csv"
        if rc != 0 or not os.path.isfile(csv_path):
            raise SolverError(f"radioss-crash: th_to_csv failed rc={rc} (see th_to_csv.log)")
        th = parse_th_csv(csv_path)

        results = {"solver": SOLVER, "mode": p["mode"], "case": name,
                   "units": {"deck": "kg-mm-ms (force kN, stress GPa, energy J, velocity mm/ms)",
                             "results": "SI unless suffixed"},
                   "opencourant": {"starter": starter, "engine": engine,
                                   "starter_run": st, "engine_run": en},
                   "time_ms": th["time"],
                   "global_energy_J": {k: v for k, v in th["global"].items()
                                       if k.strip().upper() in ("INTERNAL ENERGY", "KINETIC ENERGY",
                                                                "EXTERNAL WORK", "HOURGLASS ENERGY",
                                                                "CONTACT ENERGY", "PLASTIC WORK")},
                   "artifacts": [os.path.join(work, name + suffix) for suffix in
                                 ("_0000.rad", "_0001.rad", "_0000.out", "_0001.out", "T01.csv")],
                   "wall_s": time.time() - t0}

        walls = {}
        for wid, vars_ in th["rwall"].items():
            forces = {v: differentiate(th["time"], imp) for v, imp in vars_.items()}
            walls[str(wid)] = {"force_kN": forces}
        results["rwall"] = walls

        if p["mode"] == "tube" and "500" in walls:
            imp = walls["500"]["force_kN"].get("FNZ", [])
            crush_mm = None
            if "DZ" in th["node"]:
                crush_mm = [abs(z) for z in th["node"]["DZ"]]
            elif th["time"]:
                crush_mm = [p["impactor"]["velocity_mps"] * (t - th["time"][0]) for t in th["time"]]
            if imp and crush_mm and len(imp) == len(crush_mm):
                met = crush_metrics(th["time"], imp, crush_mm)
                if met:
                    results["crush"] = {"crush_m": met["crush_mm"] * 1e-3,
                                        "p_mean_kN": met["p_mean_kN"],
                                        "p_peak_kN": met["p_peak_kN"],
                                        "window_mm": met["window"],
                                        "note": "mean over the middle 60% of the crush distance; "
                                                "force = d(impulse)/dt of the impactor wall FNZ"}
                    absorbed = None
                    ie = results["global_energy_J"].get("INTERNAL ENERGY")
                    if ie:
                        absorbed = ie[-1] - ie[0]
                    results["energy"] = {"internal_J_final": absorbed,
                                         "kinetic_J_final": (results["global_energy_J"].get("KINETIC ENERGY") or [None])[-1],
                                         "hourglass_J_final": (results["global_energy_J"].get("HOURGLASS ENERGY") or [None])[-1],
                                         "external_work_J_final": (results["global_energy_J"].get("EXTERNAL WORK") or [None])[-1]}
                    ref = p["reference"] or {}
                    if ref.get("kind") == "wierzbicki_abramowicz":
                        pm_ref = wierzbicki_abramowicz(d["sy_GPa"], d["thick_mm"], d["width_mm"])
                        results["reference"] = {
                            "kind": "wierzbicki_abramowicz",
                            "formula": "P_m = 9.56 * sigma0 * h^(5/3) * c^(1/3)",
                            "cite": "Wierzbicki & Abramowicz (1983), J. Appl. Mech. 50(4a):727-734",
                            "sigma0_GPa": d["sy_GPa"],
                            "sigma0_note": "flow stress taken as the LAW2 yield stress (hardening b=0)"
                            if not d["hard_b_GPa"] else "flow stress taken as the LAW2 yield stress; "
                            "hardening is present, so W-A underpredicts — treat the comparison as a bound",
                            "p_mean_ref_kN": pm_ref,
                            "p_mean_rel_diff": (met["p_mean_kN"] - pm_ref) / pm_ref}
        results["wall_s"] = round(time.time() - t0, 2)
        with open(os.path.join(work, "radioss.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in results.items()}, f, indent=2)
        return results
