"""flutter-screen: bending-torsion flutter speed of a uniform cantilever wing (p-k, strip theory + Theodorsen).

A screening adapter, pure Python + numpy, no meshing and no external solver: the structural model is the exact
modal basis of a uniform cantilever (Euler-Bernoulli bending modes, St-Venant torsion modes), the aerodynamics
incompressible strip theory with Theodorsen's circulatory function (Jones 2-pole rational approximation), and
the stability solve the p-k method (per-mode reduced-frequency iteration on a companion-matrix eigenproblem).
It answers one question honestly: the speed at which damping of any tracked mode crosses zero, and why
(which modes coalesce). It is incompressible — the Mach number of the answer is reported beside it, and past
Mach ~0.3 the number is indicative, not citable. The registry record decides pass/fail, never the adapter.

Validated against the Goland (1945) wing: the literature band is 135.6-137.2 m/s / ~70 rad/s (Patil et al. 2001
137.16 m/s / 70.2 rad/s; Matter 136.41 m/s / 69.35 rad/s); this adapter's own numbers are in the registry
drop-in (solvers.d/flutter.json) and docs/validation/flutter-*/.

Case params (no files; inputs() is empty):
  wing     {semi_span_m, chord_m, ea_frac (elastic axis, fraction of chord from the leading edge),
            cg_frac (centre of mass, same datum), ei_Nm2 (spanwise bending rigidity),
            gj_Nm2 (torsion rigidity), m_kg_m (mass per unit span), i_ea_kg_m (mass moment of inertia
            about the elastic axis per unit span), rho_kg_m3 (freestream density)}
  modes    {n_bending (default 2), n_torsion (default 2)}
  speeds   {start_mps, stop_mps, step_mps} — the sweep; flutter is bisected between the bracketing speeds
  stations integration stations along the semi-span (default 200)
  reference optional {flutter_mps, tol} — reported beside the answer; never decides anything

results.json: flutter_speed_mps (+ mach, frequency, the two coalescing modes), per-mode V-g/V-f tables,
the modal basis' in-vacuo frequencies, and full provenance (method, approximations, numpy version).
"""
from __future__ import annotations

import json
import math
import os
import time

import numpy as np

SOLVER = "flutter-screen"

# Jones 2-pole rational approximation poles for Theodorsen's C(k)
JONES_B = (0.0455, 0.3)
JONES_A = (0.165, 0.335)

# cosh(bL) cos(bL) = -1: the first eight roots (Euler-Bernoulli cantilever)
BETA_L = (1.8751040687, 4.6940911330, 7.8547574382, 10.9955407349, 14.1371683910, 17.2787595321,
          20.4203522510, 23.5619449019)


class InputError(ValueError):
    """A case the adapter refuses, named plainly."""


# -------------------------------------------------------------------------------------------------- aerodynamics

def theodorsen_jones(k: float) -> complex:
    """C(k) ~= 1 - 0.165 ik/(ik + 0.0455) - 0.335 ik/(ik + 0.3) (Jones, e^{+iwt} convention):
    C(0) = 1, C(inf) = 0.5, and Im C < 0 for k > 0 (the phase lag that carries the aero damping)."""
    if k <= 0.0:
        return 1.0 + 0.0j
    s = 1j * k
    return 1.0 - sum(a * s / (s + b) for a, b in zip(JONES_A, JONES_B))


def strip_matrices(rho: float, U: float, b: float, a: float, ck: complex) -> tuple:
    """Per-unit-span generalised force Q = Q_acc ddot + Q_vel dot + Q_disp for local dof [h, alpha]
    (h plunge positive down, alpha pitch nose-up), test functions [deltah (force, down+), deltaalpha
    (moment about the EA, nose-up+)]: Q_h = -L, Q_alpha = M_EA. Theodorsen, incompressible."""
    pi = math.pi
    qacc = np.array([[-pi * rho * b ** 2, pi * rho * b ** 3 * a],
                     [pi * rho * b ** 3 * a, -pi * rho * b ** 4 * (0.125 + a ** 2)]], dtype=complex)
    qvel = np.array([[-2 * pi * rho * U * b * ck, -pi * rho * b ** 2 * U - 2 * pi * rho * U * b ** 2 * (0.5 - a) * ck],
                     [2 * pi * rho * U * b ** 2 * (a + 0.5) * ck,
                      -pi * rho * b ** 3 * U * (0.5 - a) + 2 * pi * rho * U * b ** 3 * (a + 0.5) * (0.5 - a) * ck]],
                    dtype=complex)
    qdisp = np.array([[0.0, -2 * pi * rho * U ** 2 * b * ck],
                      [0.0, 2 * pi * rho * U ** 2 * b ** 2 * (a + 0.5) * ck]], dtype=complex)
    return qacc, qvel, qdisp


# -------------------------------------------------------------------------------------------------- structure

def bending_shape(y: np.ndarray, L: float, n: int) -> np.ndarray:
    """n-th (1-based) cantilever bending mode shape, tip-normalised to 1."""
    bl = BETA_L[n - 1]
    k = (math.cosh(bl) + math.cos(bl)) / (math.sinh(bl) + math.sin(bl))
    x = bl * y / L
    return (np.cosh(x) - np.cos(x)) - k * (np.sinh(x) - np.sin(x))


def torsion_shape(y: np.ndarray, L: float, n: int) -> np.ndarray:
    """n-th (1-based) cantilever torsion mode shape sin((2n-1) pi y / 2L), tip = 1."""
    return np.sin((2 * n - 1) * math.pi * y / (2.0 * L))


def modal_basis(wing: dict, n_b: int, n_t: int, stations: int) -> dict:
    """The modal model: shapes on the integration grid, in-vacuo frequencies, and the generalised
    mass/stiffness (diagonal blocks exact; bending-torsion coupling through the static unbalance)."""
    L = wing["semi_span_m"]
    EI, GJ = wing["ei_Nm2"], wing["gj_Nm2"]
    m, iea = wing["m_kg_m"], wing["i_ea_kg_m"]
    sa = m * (wing["cg_frac"] - wing["ea_frac"]) * wing["chord_m"]   # static unbalance per unit span
    y = np.linspace(0.0, L, stations + 1)
    phi = np.array([bending_shape(y, L, i + 1) for i in range(n_b)])  # (n_b, stations+1)
    tht = np.array([torsion_shape(y, L, i + 1) for i in range(n_t)])

    wb = np.array([(BETA_L[i] / L) ** 2 * math.sqrt(EI / m) for i in range(n_b)])
    wt = np.array([((2 * i + 1) * math.pi / (2.0 * L)) * math.sqrt(GJ / iea) for i in range(n_t)])

    def integ(f):
        return np.trapezoid(f, y)

    mb = np.diag([integ(m * phi[i] ** 2) for i in range(n_b)])
    mt = np.diag([integ(iea * tht[j] ** 2) for j in range(n_t)])
    mbt = np.array([[integ(sa * phi[i] * tht[j]) for j in range(n_t)] for i in range(n_b)])
    ms = np.block([[mb, mbt], [mbt.T, mt]])
    ks = np.diag(np.concatenate([wb ** 2 * np.diag(mb), wt ** 2 * np.diag(mt)]))
    return {"y": y, "phi": phi, "theta": tht, "omega_bending": wb, "omega_torsion": wt,
            "M": ms, "K": ks, "s_alpha_kg": sa}


def aero_generalised(wing: dict, basis: dict, U: float, k: float) -> tuple:
    """Global aero matrices Q_acc/Q_vel/Q_disp in modal coordinates: strip matrices at each station,
    pushed through the mode shapes (Q[i,j] = sum_s w_s shape[i,:] Qloc shape[j,:]^T) and trapezoid-integrated."""
    b = 0.5 * wing["chord_m"]
    a = 2.0 * wing["ea_frac"] - 1.0        # EA in semichords from midchord
    ck = theodorsen_jones(k)
    phi, tht, y = basis["phi"], basis["theta"], basis["y"]
    n_b, n_t = phi.shape[0], tht.shape[0]
    ns = phi.shape[1]
    shapes = np.zeros((n_b + n_t, 2, ns))
    shapes[:n_b, 0, :] = phi               # h = phi . q_b
    shapes[n_b:, 1, :] = tht               # alpha = theta . q_t
    w = np.full(ns, y[1] - y[0]); w[0] *= 0.5; w[-1] *= 0.5
    out = []
    for qloc in strip_matrices(wing["rho_kg_m3"], U, b, a, ck):
        acc = np.zeros((n_b + n_t, n_b + n_t), dtype=complex)
        for s in range(ns):
            st = shapes[:, :, s]
            acc += w[s] * (st @ qloc @ st.T)
        out.append(acc)
    return tuple(out)


# -------------------------------------------------------------------------------------------------- the p-k solve

def pk_eig(meff: np.ndarray, ceff: np.ndarray, keff: np.ndarray) -> np.ndarray:
    """Eigenvalues p of (p^2 Meff + p Ceff + Keff) q = 0 via the companion form."""
    n = meff.shape[0]
    minv = np.linalg.inv(meff)
    top = np.hstack([np.zeros((n, n)), np.eye(n)])
    bot = np.hstack([-minv @ keff, -minv @ ceff])
    return np.linalg.eigvals(np.vstack([top, bot]))


def pk_solve(basis: dict, wing: dict, U: float, prev: np.ndarray) -> tuple:
    """One airspeed: per-mode reduced-frequency iteration. Returns (p tracked, converged, iterations).
    `prev` carries the previous speed's eigenvalues for mode tracking (or i*omega_in_vacuo at the start)."""
    ms, ks = basis["M"], basis["K"]
    n = ms.shape[0]
    b = 0.5 * wing["chord_m"]
    tracked = np.array(prev, dtype=complex)
    converged, iters = True, 0
    for mode in range(n):
        p_old = tracked[mode]
        for it in range(60):
            iters += 1
            k = abs(p_old.imag) * b / max(U, 1e-6)
            qacc, qvel, qdisp = aero_generalised(wing, basis, U, k)
            meff, ceff, keff = ms - qacc, -qvel, ks - qdisp
            eigs = pk_eig(meff, ceff, keff)
            eigs = eigs[eigs.imag > 1e-9]
            if eigs.size == 0:
                converged = False
                break
            p_new = eigs[np.argmin(np.abs(eigs - p_old))]
            tracked[mode] = p_new
            if abs(p_new - p_old) < 1e-9 * max(1.0, abs(p_old)):
                p_old = p_new
                break
            p_old = p_new
        else:
            converged = False
    return tracked, converged, iters


def sweep(wing: dict, n_b: int, n_t: int, speeds: list, stations: int) -> dict:
    """The V-g/V-f table over the speed list, then bisection on any damping sign change."""
    basis = modal_basis(wing, n_b, n_t, stations)
    n = n_b + n_t
    prev = 1j * np.concatenate([basis["omega_bending"], basis["omega_torsion"]])
    rows, all_conv = [], True
    for U in speeds:
        p, conv, _ = pk_solve(basis, wing, U, prev)
        all_conv = all_conv and conv
        prev = p
        rows.append({"speed_mps": U,
                     "modes": [{"p_re": float(pi.real), "p_im": float(pi.imag),
                                "freq_rad_s": float(abs(pi.imag)),
                                "damping": float(-pi.real / abs(pi)) if abs(pi) > 0 else 0.0}
                               for pi in p]})
    crossing = None
    for r0, r1 in zip(rows, rows[1:]):
        hit = next((m for m in range(n)
                    if r0["modes"][m]["p_re"] < 0.0 <= r1["modes"][m]["p_re"]), None)
        if hit is None:
            continue
        lo, hi = r0["speed_mps"], r1["speed_mps"]
        p_prev = np.array([complex(md["p_re"], md["p_im"]) for md in r0["modes"]])
        p_mid = p_prev
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            p_mid, _, _ = pk_solve(basis, wing, mid, p_prev)
            if p_mid[hit].real < 0.0:
                lo = mid
                p_prev = p_mid
            else:
                hi = mid
            if hi - lo < 0.05:
                break
        crossing = {"speed_mps": 0.5 * (lo + hi), "mode_index": hit,
                    "frequency_rad_s": float(abs(p_mid[hit].imag))}
        break
    return {"basis": basis, "rows": rows, "converged": all_conv, "crossing": crossing}


# -------------------------------------------------------------------------------------------------- the adapter

WING_KEYS = ("semi_span_m", "chord_m", "ea_frac", "cg_frac", "ei_Nm2", "gj_Nm2", "m_kg_m", "i_ea_kg_m",
             "rho_kg_m3")


def validate_case(case: dict, spec=None) -> dict:
    w = case.get("wing") or {}
    for key in WING_KEYS:
        if w.get(key) is None:
            raise InputError(f"flutter-screen: wing.{key} is missing — the screen needs the section "
                             "properties named explicitly; it invents none of them")
    wing = {k: float(w[k]) for k in WING_KEYS}
    if wing["semi_span_m"] <= 0 or wing["chord_m"] <= 0:
        raise InputError("flutter-screen: semi_span_m and chord_m must be > 0")
    for f in ("ea_frac", "cg_frac"):
        if not 0.0 <= wing[f] <= 1.0:
            raise InputError(f"flutter-screen: wing.{f} {wing[f]:g} is outside [0, 1] (fraction of chord "
                             "from the leading edge)")
    for k in ("ei_Nm2", "gj_Nm2", "m_kg_m", "i_ea_kg_m", "rho_kg_m3"):
        if wing[k] <= 0:
            raise InputError(f"flutter-screen: wing.{k} must be > 0")
    modes = case.get("modes") or {}
    n_b, n_t = int(modes.get("n_bending", 2)), int(modes.get("n_torsion", 2))
    if not 1 <= n_b <= 8 or not 1 <= n_t <= 8:
        raise InputError("flutter-screen: n_bending / n_torsion must be in 1..8")
    sp = case.get("speeds") or {}
    try:
        v0, v1, dv = float(sp["start_mps"]), float(sp["stop_mps"]), float(sp["step_mps"])
    except (KeyError, TypeError, ValueError):
        raise InputError("flutter-screen: speeds {start_mps, stop_mps, step_mps} is required — the sweep "
                         "range is a modelling decision and is never defaulted") from None
    if not (0 < v0 < v1 and dv > 0 and (v1 - v0) / dv >= 2):
        raise InputError(f"flutter-screen: speeds start {v0} / stop {v1} / step {dv} do not bracket a sweep")
    stations = int(case.get("stations", 200))
    if stations < 40:
        raise InputError("flutter-screen: stations < 40 integrates strip forces too coarsely; refusing")
    return {"wing": wing, "n_bending": n_b, "n_torsion": n_t,
            "speeds": [v0 + i * dv for i in range(int(round((v1 - v0) / dv)) + 1)],
            "stations": stations, "reference": case.get("reference")}


class Adapter:
    name = SOLVER

    def inputs(self, case: dict, spec) -> list:
        return []  # the section properties are the input; there is no geometry file

    def run(self, case: dict, spec, run_dir: str) -> dict:
        import numpy
        t0 = time.time()
        p = validate_case(case, spec)
        wing = p["wing"]
        out = sweep(wing, p["n_bending"], p["n_torsion"], p["speeds"], p["stations"])
        basis = out["basis"]
        crossing = out["crossing"]

        results = {
            "solver": SOLVER,
            "method": ("p-k eigenvalue iteration; incompressible strip theory with Theodorsen C(k) "
                       "(Jones 2-pole rational approximation); exact uniform-cantilever modal basis "
                       f"({p['n_bending']} bending + {p['n_torsion']} torsion)"),
            "wing": wing,
            "b_m": 0.5 * wing["chord_m"],
            "a_semichords": 2.0 * wing["ea_frac"] - 1.0,
            "s_alpha_kg_per_m": basis["s_alpha_kg"],
            "in_vacuo": {"omega_bending_rad_s": [float(w) for w in basis["omega_bending"]],
                         "omega_torsion_rad_s": [float(w) for w in basis["omega_torsion"]]},
            "vg": out["rows"],
            "pk_converged": out["converged"],
            "numpy": numpy.__version__,
            "wall_s": time.time() - t0,
        }
        if crossing:
            c_a = 343.0  # ISA sea level, indicative only
            results["flutter_speed_mps"] = crossing["speed_mps"]
            results["flutter_frequency_rad_s"] = crossing["frequency_rad_s"]
            results["flutter_mode_index"] = crossing["mode_index"]
            results["flutter_mach"] = crossing["speed_mps"] / c_a
            results["mach_note"] = ("incompressible aerodynamics; at Mach {:.2f} the number is "
                                    "indicative, not citable".format(crossing["speed_mps"] / c_a)
                                    if crossing["speed_mps"] / c_a > 0.3 else
                                    "Mach {:.2f}: within the incompressible assumption".format(
                                        crossing["speed_mps"] / c_a))
        else:
            results["flutter_speed_mps"] = None
            results["flutter_note"] = (f"no damping crossing in {p['speeds'][0]:g}-{p['speeds'][-1]:g} m/s: "
                                       "flutter is outside the sweep, or absent; widen speeds to tell which")
        ref = p["reference"]
        if ref and crossing and ref.get("flutter_mps"):
            vf, vr = crossing["speed_mps"], float(ref["flutter_mps"])
            results["reference"] = {"flutter_mps": vr, "tol": ref.get("tol"),
                                    "rel_err": (vf - vr) / vr}
        work = os.path.join(os.path.abspath(run_dir), "flutter")
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, "flutter.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        results["artifacts"] = [os.path.join(work, "flutter.json")]
        return results
