"""PyFR adapter, two stages:

  mesh  (cpu)  either copy the case's .msh, or build one from a geometry body with gmsh — a STEP B-rep
               (box_minus_step) or an STL of a mesh-derived part (box_minus_stl), chosen by the suffix
               (case["mesh"] = {"from_step": "sphere.step"|"winner.stl", "box": [...], "lc_body": .., "lc_far": ..})
  solve (sim)  patch the .ini (overrides + wall-force plugin), `pyfr import`, `pyfr run -b cuda`,
               post-process the force history to Cd / Cl rms / Strouhal

Case params:
  case_dir         directory with exactly one .ini (and one .msh unless mesh.from_step is given), relative to the spec
  mesh             optional gmsh block: from_step (relative to the spec; names a .step/.stp or .stl file), box
                   [x0 y0 z0 x1 y1 z1], lc_body, lc_far, dist_min, dist_max, algorithm3d (default 10 = HXT),
                   optimize (default true)
  backend          cuda (default) | openmp
  precision        single | double (overrides [backend] precision)
  tend, dt, order  optional overrides of the time integrator / solver order
  pseudo_dt, pseudo_niters_min, pseudo_niters_max, pseudo_resid_tol   dual-time overrides
  nancheck         NaN check interval in steps (default 1; a NaN solution fails the case)
  nu               optional override of [constants] nu
  D, U, rho        reference length, velocity, density for Re and the coefficients (default 1, 1, 1)
  ref_area         reference area for Cd/Cl (default D per unit span, i.e. 2-D); pi D^2 / 4 for a sphere
  force_bc         boundary name carrying the body (default 'wall'), force_nsteps (default 10)
  st_window        fraction of the time series used for the spectrum (default 0.5, the last half)
  writer_dt        override of the solution writer interval (default: keep the case's)
"""
from __future__ import annotations

import configparser
import glob
import json
import math
import os
import shutil
import subprocess
import time

import numpy as np

STAGES = {"mesh": "cpu", "solve": "sim"}


def _cfg(path: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(path)
    return cp


def strouhal(t: np.ndarray, fy: np.ndarray, D: float, U: float, window: float = 0.5) -> dict:
    n = len(t)
    i0 = int(n * (1.0 - window))
    tt, ff = t[i0:], fy[i0:]
    if len(tt) < 16:
        return {"St": float("nan"), "f_Hz": float("nan"), "Cl_rms": float("nan")}
    dt = float(np.median(np.diff(tt)))
    tu = np.arange(tt[0], tt[-1], dt)
    fu = np.interp(tu, tt, ff)
    fu = fu - fu.mean()
    spec = np.abs(np.fft.rfft(fu * np.hanning(len(fu))))
    freqs = np.fft.rfftfreq(len(fu), dt)
    k = int(np.argmax(spec[1:]) + 1)
    if 1 <= k < len(spec) - 1:  # parabolic interpolation of the peak
        a, b, c = spec[k - 1], spec[k], spec[k + 1]
        shift = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
        f = freqs[k] + shift * (freqs[1] - freqs[0])
    else:
        f = freqs[k]
    return {"St": float(f * D / U), "f_Hz": float(f), "Cl_rms": float(np.sqrt(np.mean(fu ** 2)))}


class Adapter:
    name = "pyfr"
    stages = STAGES

    # ---- inputs ----
    def _files(self, case: dict, spec):
        d = spec.resolve(case["case_dir"])
        ini = glob.glob(os.path.join(d, "*.ini"))
        msh = glob.glob(os.path.join(d, "*.msh"))
        from_step = (case.get("mesh") or {}).get("from_step")
        if len(ini) != 1:
            raise FileNotFoundError(f"{d} must contain exactly one .ini")
        if from_step:
            return ini[0], None, spec.resolve(from_step)
        if len(msh) != 1:
            raise FileNotFoundError(f"{d} must contain exactly one .msh (or give mesh.from_step)")
        return ini[0], msh[0], None

    def inputs(self, case: dict, spec) -> list:
        return [p for p in self._files(case, spec) if p]

    def _work(self, run_dir: str) -> str:
        work = os.path.abspath(os.path.join(run_dir, "pyfr"))  # absolute: pyfr runs with cwd=work
        os.makedirs(work, exist_ok=True)
        return work

    # ---- stages ----
    def run_stage(self, stage: str, case: dict, spec, run_dir: str) -> dict:
        return getattr(self, "stage_" + stage)(case, spec, run_dir)

    def stage_mesh(self, case: dict, spec, run_dir: str) -> dict:
        ini_src, msh_src, step = self._files(case, spec)
        work = self._work(run_dir)
        t0 = time.time()
        if step:
            from .. import meshing

            m = case["mesh"]
            stem = os.path.splitext(os.path.basename(step))[0]
            msh = os.path.join(work, stem + ".msh")
            # the from_step key may name an STL (mesh-derived part, e.g. golden's winner): the STL path
            # meshes with gmsh's built-in kernel, a STEP B-rep with OCC — the suffix decides
            fn = meshing.box_minus_stl if step.lower().endswith(".stl") else meshing.box_minus_step
            info = fn(step, msh, m["box"], float(m["lc_body"]), float(m["lc_far"]),
                      dist_min=m.get("dist_min"), dist_max=m.get("dist_max"),
                      algorithm3d=int(m.get("algorithm3d", 10)), optimize=bool(m.get("optimize", True)),
                      names=m.get("names"))
            info.update({"source": "gmsh", "step": step})
        else:
            stem = os.path.splitext(os.path.basename(msh_src))[0]
            msh = os.path.join(work, stem + ".msh")
            shutil.copy(msh_src, msh)
            info = {"source": "case_dir", "msh": msh, "wall_s": time.time() - t0}
        info["stem"] = stem
        info["ini_src"] = ini_src
        with open(os.path.join(run_dir, "mesh.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
        return info

    def stage_solve(self, case: dict, spec, run_dir: str) -> dict:
        with open(os.path.join(run_dir, "mesh.json"), "r", encoding="utf-8") as f:
            mesh = json.load(f)
        work = self._work(run_dir)
        stem = mesh["stem"]
        ini = os.path.join(work, stem + ".ini")
        msh = mesh["msh"]
        pyfrm = os.path.join(work, stem + ".pyfrm")
        cp = _cfg(mesh["ini_src"])
        if "precision" in case:
            cp["backend"]["precision"] = str(case["precision"])
        for key in ("tend", "dt", "tstart"):
            if key in case:
                cp["solver-time-integrator"][key] = str(case[key])
        if "order" in case:
            cp["solver"]["order"] = str(case["order"])
        if "nu" in case:
            cp["constants"]["nu"] = str(case["nu"])
        if "writer_dt" in case:
            cp["soln-plugin-writer"]["dt-out"] = str(case["writer_dt"])
        cp["soln-plugin-writer"]["basedir"] = "."
        bc = case.get("force_bc", "wall")
        sec = f"soln-plugin-fluidforce-{bc}"
        if not cp.has_section(sec):
            cp.add_section(sec)
        cp[sec]["nsteps"] = str(int(case.get("force_nsteps", 10)))
        cp[sec]["file"] = f"{bc}-forces.csv"
        cp[sec]["header"] = "true"
        cp[sec]["quad-deg"] = str(int(case.get("force_quad_deg", 6)))  # required by PyFR 3.x
        # a NaN solution must abort the run, not finish quietly: check every `nancheck` steps (default 1)
        if not cp.has_section("soln-plugin-nancheck"):
            cp.add_section("soln-plugin-nancheck")
        cp["soln-plugin-nancheck"]["nsteps"] = str(int(case.get("nancheck", 1)))
        for key in ("pseudo-dt", "pseudo-niters-min", "pseudo-niters-max", "pseudo-resid-tol"):
            if key.replace("-", "_") in case:
                cp["solver-time-integrator"][key] = str(case[key.replace("-", "_")])
        with open(ini, "w", encoding="utf-8") as f:
            cp.write(f)

        backend = case.get("backend", "cuda")
        env = dict(os.environ)
        # PyFR's csv plugins APPEND to existing files: stale output from an earlier attempt in the same
        # run directory would corrupt the force history, so start clean
        for stale in glob.glob(os.path.join(work, "*.csv")) + glob.glob(os.path.join(work, "*.pyfrs")):
            os.remove(stale)
        log = open(os.path.join(work, "pyfr.log"), "w", encoding="utf-8")
        t0 = time.time()
        subprocess.run(["pyfr", "import", msh, pyfrm], check=True, cwd=work, stdout=log, stderr=subprocess.STDOUT, env=env)
        t_import = time.time() - t0
        t1 = time.time()
        proc = subprocess.Popen(["pyfr", "run", "-b", backend, pyfrm, ini], cwd=work, stdout=log, stderr=subprocess.STDOUT, env=env)
        tend = float(cp["solver-time-integrator"].get("tend", 0) or 0)
        forces_path = os.path.join(work, f"{bc}-forces.csv")
        try:
            from gendesign import progress as _progress
        except Exception:
            _progress = None
        while proc.poll() is None:
            time.sleep(2.0)
            if _progress is not None and _progress.enabled() and os.path.exists(forces_path):
                try:
                    with open(forces_path, "rb") as fh:
                        fh.seek(0, 2)
                        size = fh.tell()
                        fh.seek(max(0, size - 400))
                        tail = fh.read().decode(errors="ignore").strip().splitlines()
                    last = [x for x in tail if x and not x.startswith("t,")][-1]
                    t_now = float(last.split(",")[0])
                    _progress.report("pyfr", step=int(1000 * t_now / tend) if tend else None, total=1000 if tend else None, t=t_now, tend=tend)
                except Exception:
                    pass
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, "pyfr run")
        t_run = time.time() - t1
        log.close()

        forces = os.path.join(work, f"{bc}-forces.csv")
        data = np.genfromtxt(forces, delimiter=",", names=True)
        t = data["t"]
        if not np.all(np.isfinite(data["px"])):
            raise RuntimeError(f"non-finite wall forces in {forces}: the solution diverged (see pyfr.log / residual.csv)")
        cols = data.dtype.names
        fx = data["px"] + (data["vx"] if "vx" in cols else 0.0)
        fy = data["py"] + (data["vy"] if "vy" in cols else 0.0)
        D = float(case.get("D", 1.0))
        U = float(case.get("U", 1.0))
        rho = float(case.get("rho", 1.0))
        area = float(case.get("ref_area", D))  # 2-D: per unit span
        q = 0.5 * rho * U ** 2 * area
        st = strouhal(t, fy, D, U, float(case.get("st_window", 0.5)))
        n = len(t)
        i0 = int(n * (1.0 - float(case.get("st_window", 0.5))))
        nu = float(cp["constants"].get("nu", "nan"))
        Re = U * D / nu if nu == nu and nu > 0 else float("nan")
        m = {
            "Re": Re,
            "St": st["St"],
            "f_shed": st["f_Hz"],
            "Cd_mean": float(fx[i0:].mean() / q),
            "Cd_last": float(fx[-1] / q),
            "Cl_rms": float(st["Cl_rms"] / q),
            "t_end": float(t[-1]),
            "n_force_samples": int(n),
            "mesh_source": mesh.get("source"),
            "mesh_nodes": mesh.get("nodes"),
            "wall_s_mesh": mesh.get("wall_s"),
            "wall_s_import": t_import,
            "wall_s_run": t_run,
            "wall_s": t_import + t_run,
            "artifacts": [forces, ini, os.path.join(work, "pyfr.log"), os.path.abspath(os.path.join(run_dir, "mesh.json"))],
        }
        if Re == Re and 47 < Re < 190 and area == D:
            m["St_williamson"] = 0.2665 - 1.018 / math.sqrt(Re)  # laminar cylinder shedding correlation (2-D only)
            m["St_error_pct"] = 100 * (m["St"] - m["St_williamson"]) / m["St_williamson"]
        res = os.path.join(work, "residual.csv")
        if os.path.exists(res):
            m["artifacts"].append(res)
        return m

    def run(self, case: dict, spec, run_dir: str) -> dict:
        self.stage_mesh(case, spec, run_dir)
        return self.stage_solve(case, spec, run_dir)
