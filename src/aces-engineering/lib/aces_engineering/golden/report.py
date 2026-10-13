"""The report of aces golden: one human page (report.md / report.html) and one agent document (report.json).

`assemble(run)` reads every record on disk and returns the report as data, section by section (0 title and verdict,
1 key numbers, 2 inputs and provenance, 3 voxelisation, 4 compute used, 5 sweep, 6 surrogate, 7 optimisation,
8 verification, 9 independent check and device parity, 10 conforming second discretisation, 11 rig, 12 winner or
candidate, 13 evidence, 14 environment, 15 limits and reproduction). It tolerates gaps: a section whose records do not
exist says `not run: <reason>`, with the reason taken from run.json's stage notes or the validation record. Nothing is
invented: every number is copied from a record, and every number in the verdict line and the validation, check,
conforming, rig, compute and evidence tables carries a provenance entry (a JSON pointer into report.json, the source
file and the key) that `aces golden verify` resolves again.

`verdict_line(data)` is the one-line answer (pinned formats, 4 significant digits); `charts(run)` draws the sweep
scatter, the optimisation map, the check-levels plot and, when the conforming check ran, the conforming plot;
`write(run, data)` writes report.md, report.html (through the vendored simlab.report.to_html) and report.json.
"""
from __future__ import annotations

import math
from pathlib import Path

from . import GRADE_PREVIEW, SCHEMA, clean, sha256_file, stage_wall_sum

CHART = {
    "bg": "#061225", "text": "#dbe7f3", "muted": "#8aa0b8", "grid": "#16294a",
    # categorical slots validated together on the #061225 surface (dark band, all pairs: CVD dE 9.4, normal dE 20.9)
    "series": ("#3987e5", "#d95926", "#199e70"),
    "ramp": ("#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4"),
}
SHORT = {"mass_kg": "mass", "mass_voxel_kg": "mass_voxel", "compliance_Nmm": "compliance", "umax_mm": "umax",
         "vm_max_MPa": "vm_max", "vm_p99_MPa": "vm_p99", "vm_p95_MPa": "vm_p95", "mesh_volume_mm3": "mesh_volume",
         "voxel_volume_mm3": "voxel_volume"}
UNIT = {"mass_kg": "kg", "mass_voxel_kg": "kg", "compliance_Nmm": "N mm", "umax_mm": "mm", "vm_max_MPa": "MPa",
        "vm_p99_MPa": "MPa", "vm_p95_MPa": "MPa", "mesh_volume_mm3": "mm3", "voxel_volume_mm3": "mm3"}
SWEEP_COLUMNS = ("mass_kg", "umax_mm", "vm_p99_MPa", "vm_max_MPa", "compliance_Nmm", "pcg_iters", "faces", "genus")
WALL_STAGES = ("compute", "inputs", "domain", "sweep", "loop", "check", "export", "cfd", "flutter", "evidence")
NO_STEP = ("the part is an organic mesh (marching cubes of an implicit field); a STEP would be a faceted pretence of a "
           "B-rep")
CAVITY_NOTE = "enclosed cavities trap powder: add drain holes before printing (not modelled in v0)"
FILTERS_WARNING = ("the vendored gendesign/filters.py calls numpy.fft.irfftn with s= but without axes=, a NumPy 2 "
                   "DeprecationWarning (filtered in the workers; to be fixed upstream in cudax-sim-runtime, never "
                   "patched in vendor/)")


# ----------------------------------------------------------------------------- formatting

def g4(v) -> str:
    """4 significant digits ('-' for None); 1e4 <= |v| < 1e7 in fixed notation (70000, not 7e+04)."""
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return "-"
        if 1e4 <= abs(v) < 1e7:
            return f"{float(f'{v:.4g}'):.0f}"
        return f"{v:.4g}"
    return str(v)


def _list4(v) -> str:
    return "[" + ", ".join(g4(float(x)) if _num(x) else str(x) for x in v) + "]" if isinstance(v, (list, tuple)) else g4(v)


def _shape(region: dict) -> str:
    """A keep-in / keep-out region as text: a box, or 'cylinder z, centre (82, 70), r 9, span 0..6'."""
    if region.get("box") is not None:
        return "box " + _list4(region["box"])
    c = region.get("cyl") or {}
    if c:
        ctr = c.get("center") or [None, None]
        span = c.get("span") or [None, None]
        return (f"cylinder {c.get('axis')}, centre ({g4(ctr[0])}, {g4(ctr[1])}), r {g4(c.get('r'))}, "
                f"span {g4(span[0])}..{g4(span[1])}")
    return "-"


def _pct(v, signed: bool = False) -> str:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
        return "-"
    return f"{100 * float(v):+.4g} %" if signed else f"{100 * float(v):.4g} %"


def _cell(v) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(g4(x) for x in v)
    return g4(v).replace("|", "/").replace("\n", " ")


def _table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return "\n".join(out)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


# ----------------------------------------------------------------------------- reading the run

class _Reader:
    def __init__(self, run):
        self.run = run
        self._cache: dict = {}

    def json(self, rel: str, default=None):
        if rel not in self._cache:
            try:
                self._cache[rel] = self.run.read_json(rel)
            except Exception:  # an unreadable record is a gap, not a crash
                self._cache[rel] = None
        v = self._cache[rel]
        return default if v is None else v

    def exists(self, rel: str) -> bool:
        return Path(self.run.path(*rel.split("/"))).is_file()

    def sha(self, rel: str):
        p = Path(self.run.path(*rel.split("/")))
        return sha256_file(p) if p.is_file() else None

    def cases(self) -> list[dict]:
        try:
            cases = self.run.cases()
        except Exception:
            cases = None
        if cases is None:
            cases = self.json("cases.json", []) or []
        return [c for c in cases if isinstance(c, dict) and c.get("case_id")]


class _Prov:
    """Provenance entries: pointer into report.json -> (source file, key)."""

    def __init__(self):
        self.items: list[dict] = []

    def add(self, pointer: str, source: str, key: str) -> None:
        self.items.append({"pointer": pointer, "source": source, "key": key})

    def walk(self, value, pointer: str, source: str, key: str) -> None:
        """Register every number inside `value` (a copy of source#key placed at pointer)."""
        if isinstance(value, dict):
            for k, v in value.items():
                self.walk(v, f"{pointer}/{_esc(k)}", source, f"{key}.{k}" if key else str(k))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self.walk(v, f"{pointer}/{i}", source, f"{key}.{i}" if key else str(i))
        elif _num(value):
            self.add(pointer, source, key)


def _esc(k) -> str:
    return str(k).replace("~", "~0").replace("/", "~1")


def _stage_why(run_doc: dict, stage: str, default: str) -> str:
    for st in run_doc.get("stages") or []:
        if st.get("name") != stage:
            continue
        status = st.get("status")
        if status in ("skipped", "failed") and st.get("note"):
            note = str(st["note"])
            return note if note.startswith("not run") else f"not run: {note}"
        if status == "failed":
            return f"not run: the {stage} stage failed"
        if status == "running":
            return f"not run: the {stage} stage did not finish"
    if run_doc.get("status") in ("error", "interrupted"):
        msg = run_doc.get("message") or run_doc.get("status")
        return f"not run: the run stopped before the {stage} stage ({msg})"
    return f"not run: {default}"


def _wall(run_doc: dict):
    """(wall seconds, provenance key on run.json): timings.total_s when present, else the sum of the stage wall
    times recorded so far (the report is written before the run finishes)."""
    total = (run_doc.get("timings") or {}).get("total_s")
    if _num(total):
        return float(total), "timings.total_s"
    names = [st.get("name") for st in run_doc.get("stages") or []
             if st.get("name") in WALL_STAGES and _num(st.get("wall_s"))]
    if not names:
        return None, None
    # the provenance check re-sums with the same helper: never sum() here (compensated from Python 3.12 on)
    return stage_wall_sum(run_doc.get("stages"), names), "sum:stages.wall_s:" + ",".join(names)


# ----------------------------------------------------------------------------- rig and conforming sections

def _image12(image_id) -> str | None:
    """The first 12 hex digits of an image id ("sha256:<hex>" or "<hex>")."""
    if not image_id:
        return None
    s = str(image_id).split()[0]
    return s.split(":", 1)[1][:12] if ":" in s else s[:12]


def _conforming_section(R: _Reader, P: _Prov, val, run_doc: dict) -> dict:
    """Section 10 from validation.json `conforming` and the case's conforming.json: mesh, boundary-condition mapping,
    solver, equilibrium, the comparison rows with their bands, the verdict."""
    if not isinstance(val, dict):
        return {"status": _stage_why(run_doc, "check", "no validation.json")}
    cv = val.get("conforming")
    if not isinstance(cv, dict):
        return {"status": "not run: this run's validation record has no conforming check (golden code before 0.2.0)"}
    sec = {k: cv.get(k) for k in ("ran", "passed", "case_id", "host", "mode", "reason", "h_mm", "source_case",
                                  "reference", "bands", "rows", "constraints_met", "record", "rig", "rig_attempt")}
    sec["ran"] = bool(cv.get("ran"))
    sec["check_status"] = cv.get("status")
    if _num(cv.get("h_mm")):
        P.add("/sections/conforming/h_mm", "validation.json", "conforming.h_mm")
    P.walk(cv.get("reference") or {}, "/sections/conforming/reference", "validation.json", "conforming.reference")
    P.walk(cv.get("bands") or {}, "/sections/conforming/bands", "validation.json", "conforming.bands")
    P.walk(cv.get("rows") or [], "/sections/conforming/rows", "validation.json", "conforming.rows")
    rel = cv.get("record")
    cj = R.json(rel) if rel else None
    if isinstance(cj, dict):
        mesh = {k: (cj.get("mesh") or {}).get(k) for k in ("tool", "version", "route", "algorithm_3d", "size_mm",
                                                            "element", "nodes", "elements", "dof", "volume_mm3",
                                                            "volume_rel_err", "min_sicn", "p01_sicn")}
        P.walk(mesh, "/sections/conforming/mesh", rel, "mesh")
        bcs = {"window_mm": (cj.get("bcs") or {}).get("window_mm"), "supports": [], "loads": []}
        if _num(bcs["window_mm"]):
            P.add("/sections/conforming/bcs/window_mm", rel, "bcs.window_mm")
        for kind in ("supports", "loads"):
            for i, b in enumerate((cj.get("bcs") or {}).get(kind) or []):
                e = {k: b.get(k) for k in ("name", "mode", "axis", "plane_mm", "faces", "nodes", "area_mm2",
                                           "projected_mm2", "footprint_mm2", "coverage", "plane_offset_mm")}
                bcs[kind].append(e)
                P.walk(e, f"/sections/conforming/bcs/{kind}/{i}", rel, f"bcs.{kind}.{i}")
        solver = {k: (cj.get("solver") or {}).get(k) for k in ("tool", "version", "linear_solver", "threads", "rc",
                                                              "wall_s", "sha256")}
        P.walk({k: v for k, v in solver.items() if k != "sha256"}, "/sections/conforming/solver", rel, "solver")
        eq = {k: (cj.get("equilibrium") or {}).get(k) for k in ("applied_N", "reaction_N", "rel_residual")}
        P.walk(eq, "/sections/conforming/equilibrium", rel, "equilibrium")
        metrics = dict(cj.get("metrics") or {})
        P.walk(metrics, "/sections/conforming/metrics", rel, "metrics")
        wall = {k: cj.get(k) for k in ("wall_s_mesh", "wall_s_solve", "wall_s_post", "wall_s")}
        P.walk(wall, "/sections/conforming/wall", rel, "")
        stress = {k: (cj.get("stress") or {}).get(k) for k in ("elements_total", "elements_included",
                                                              "volume_included_mm3")}
        P.walk(stress, "/sections/conforming/stress", rel, "stress")
        sec.update(mesh=mesh, bcs=bcs, solver=solver, equilibrium=eq, metrics=metrics, wall=wall, stress=stress,
                   message=cj.get("message"))
    else:
        sec["solver"] = cv.get("solver")
    sec["chart"] = "figures/conforming.png" if R.exists("figures/conforming.png") else None
    return sec


def _rig_section(R: _Reader, P: _Prov, val, comp) -> dict:
    """Section 11 from validation.json `rig`, rig/session.json and each rig case's rig.json: the lane, the server and
    the probe notes, one row per experiment and per case, cancellations and the integrity error."""
    rv = (val or {}).get("rig") if isinstance(val, dict) else None
    session = R.json("rig/session.json")
    if not isinstance(rv, dict) and not isinstance(session, dict):
        return {"status": "not run: no rig lane was used (desktop only)"}
    rv = rv if isinstance(rv, dict) else {}
    ses = session if isinstance(session, dict) else {}
    server = rv.get("server") or ses.get("server") or {}
    probe = ((comp or {}).get("rig") or {}).get("probe") or {}
    sec = {"lane": rv.get("lane") or ses.get("lane"), "url": rv.get("url") or ses.get("url"),
           "principal": rv.get("principal") or ses.get("principal"), "policy": rv.get("policy") or ses.get("policy"),
           "server_version": server.get("version") if isinstance(server, dict) else None,
           "token_source": rv.get("token_source") or ses.get("token_source"),
           "scope": rv.get("scope") or ses.get("scope"), "notes": list(probe.get("notes") or []),
           "budget_s": ses.get("budget_s"), "experiments": [], "cases": [],
           "cancelled": list(ses.get("cancelled") or []),
           "integrity_error": rv.get("integrity_error") if rv else ses.get("integrity_error"),
           "code_identity_ok": (rv.get("code_identity") or {}).get("ok") if rv else None,
           "session": "rig/session.json" if session is not None else None}
    if _num(ses.get("budget_s")):
        P.add("/sections/rig/budget_s", "rig/session.json", "budget_s")
    counts = {e.get("id"): (k, e) for k, e in enumerate(rv.get("experiments") or []) if isinstance(e, dict)}
    for j, e in enumerate(ses.get("experiments") or []):
        if not isinstance(e, dict):
            continue
        i = len(sec["experiments"])
        cases = e.get("cases") or {}
        rows = {}
        for rc in sorted(cases):
            gid = cases[rc]
            rows[rc] = (gid, R.json(f"{gid}/rig.json") or {})
        ent = {"id": e.get("id"), "stage": e.get("stage"), "solver": e.get("solver"), "image_id12": None, "cases": None,
               "finished": None, "failed": None, "gpus": [], "wall_s": e.get("wall_s"), "max_wait_s": None}
        for gid, rj in rows.values():
            if rj.get("image_id") and not ent["image_id12"]:
                ent["image_id12"] = _image12(rj.get("image_id"))
                ent["image"] = rj.get("image")
        if _num(e.get("wall_s")):
            P.add(f"/sections/rig/experiments/{i}/wall_s", "rig/session.json", f"experiments.{j}.wall_s")
        if e.get("id") in counts:
            k, c = counts[e.get("id")]
            for src_key, key in (("n_cases", "cases"), ("n_finished", "finished"), ("n_failed", "failed")):
                ent[key] = c.get(src_key)
                if _num(c.get(src_key)):
                    P.add(f"/sections/rig/experiments/{i}/{key}", "validation.json", f"rig.experiments.{k}.{src_key}")
        seen_gpu = set()
        for gid, rj in rows.values():
            g = rj.get("gpu")
            if _num(g) and g not in seen_gpu:
                seen_gpu.add(g)
                ent["gpus"].append(g)
        ent["gpus"].sort()
        for n, g in enumerate(ent["gpus"]):
            gid = next(gd for gd, rj in rows.values() if rj.get("gpu") == g)
            P.add(f"/sections/rig/experiments/{i}/gpus/{n}", f"{gid}/rig.json", "gpu")
        waits = [(gid, rj.get("wait_s")) for gid, rj in rows.values() if _num(rj.get("wait_s"))]
        if waits:
            gid, w = max(waits, key=lambda t: t[1])
            ent["max_wait_s"] = w
            P.add(f"/sections/rig/experiments/{i}/max_wait_s", f"{gid}/rig.json", "wait_s")
        sec["experiments"].append(ent)
        for rc, (gid, rj) in rows.items():
            n = len(sec["cases"])
            row = {"case_id": gid, "rig_case": rc, "experiment": e.get("id"),
                   "job_id": rj.get("job_id") or (e.get("jobs") or {}).get(rc), "gpu": rj.get("gpu"),
                   "gpu_name": rj.get("gpu_name"), "wait_s": rj.get("wait_s"), "run_s": rj.get("run_s"),
                   "state": rj.get("state"), "design_status": rj.get("design_status"), "reason": rj.get("reason")}
            for key in ("gpu", "wait_s", "run_s"):
                if _num(row[key]):
                    P.add(f"/sections/rig/cases/{n}/{key}", f"{gid}/rig.json", key)
            sec["cases"].append(row)
    return sec


# ----------------------------------------------------------------------------- assemble

def assemble(run) -> dict:
    """The whole report as data (see the module docstring). Tolerates any missing record."""
    R = _Reader(run)
    P = _Prov()
    run_doc = R.json("run.json", {}) or {}
    eff = R.json("inputs/spec.effective.json", {}) or {}
    d = eff.get("derived") or {}
    val = R.json("validation.json")
    sweep = R.json("sweep.json")
    comp = R.json("compute.json")
    ledgers = R.json("evidence/ledgers.json")
    bundle = R.json("evidence/bundle.json")
    name = eff.get("name") or run_doc.get("demo") or "golden"
    winner_label = None
    winner = None
    for label in ("winner", "candidate"):
        w = R.json(f"{label}/{label}.json")
        if isinstance(w, dict):
            winner_label, winner = label, w
            break
    final_case = (val or {}).get("final_case") or (winner or {}).get("case_id")
    final_res = R.json(f"{final_case}/results.json") if final_case else None
    S: dict = {}

    # 0 title
    S["title"] = {"name": name, "description": eff.get("description"), "project": run.project, "run_id": run.run_id,
                  "exp_id": getattr(run, "exp_id", None), "run_dir": str(run.root),
                  "created_utc": run_doc.get("created_utc"), "demo": run_doc.get("demo"),
                  "renders": {k: v for k, v in ((winner or {}).get("renders") or {}).items()
                              if R.exists(v) and ("iso" in k or "stress" in k)}}

    # summary: what the verdict line says, with provenance
    summ: dict = {"name": name, "verdict": (val or {}).get("verdict"), "outcome": (val or {}).get("outcome")
                  or run_doc.get("outcome"), "grade": (val or {}).get("grade") or run_doc.get("grade"),
                  "message": run_doc.get("message"), "reasons": list((val or {}).get("verdict_reasons") or []),
                  "rounds": (val or {}).get("final_round") or len((val or {}).get("rounds") or []) or None}
    if summ["rounds"] is not None and val and val.get("final_round") is not None:
        P.add("/sections/summary/rounds", "validation.json", "final_round")
    if val and val.get("tol") is not None:
        summ["tol"] = val["tol"]
        P.add("/sections/summary/tol", "validation.json", "tol")
    params = (val or {}).get("winner_params") if (val or {}).get("verdict") == "PASS" else None
    if params:
        summ["params"] = dict(params)
        for k in params:
            if _num(params[k]):
                P.add(f"/sections/summary/params/{_esc(k)}", "validation.json", f"winner_params.{k}")
    if final_res and (val or {}).get("verdict") in ("PASS", "FAIL"):
        src = f"{final_case}/results.json"
        summ["case_id"] = final_case
        if _num(final_res.get("mass_kg")):
            summ["mass_kg"] = final_res["mass_kg"]
            P.add("/sections/summary/mass_kg", src, "mass_kg")
        obj = (d.get("objective") or (val or {}).get("objective") or {})
        if obj.get("output") and obj.get("output") != "mass_kg" and _num(final_res.get(obj["output"])):
            summ["objective"] = {"output": obj["output"], "value": final_res[obj["output"]]}
            P.add("/sections/summary/objective/value", src, obj["output"])
        cons = []
        for i, c in enumerate((val or {}).get("constraints") or []):
            v = final_res.get(c["output"])
            cons.append({"output": c["output"], "op": c["op"], "limit": c["limit"], "value": v})
            P.add(f"/sections/summary/constraints/{i}/limit", "validation.json", f"constraints.{i}.limit")
            if _num(v):
                P.add(f"/sections/summary/constraints/{i}/value", src, c["output"])
        summ["constraints"] = cons
    rounds = (val or {}).get("rounds") or []
    if rounds:
        ri = len(rounds) - 1
        best = None
        for j, r in enumerate(rounds[ri].get("rows") or []):
            if r.get("role") in ("objective", "constraint") and _num(r.get("rel_err")):
                if best is None or r["rel_err"] > best[1]:
                    best = (j, r["rel_err"], r["output"])
        if best:
            summ["max_rel_err"] = {"value": best[1], "output": best[2]}
            P.add("/sections/summary/max_rel_err/value", "validation.json", f"rounds.{ri}.rows.{best[0]}.rel_err")
    ic = (val or {}).get("independent_check") or {}
    lv1 = next((lv for lv in ic.get("levels") or [] if lv.get("level") == 1), None)
    if lv1 is not None:
        li = (ic.get("levels") or []).index(lv1)
        chk = {"h_mm": lv1.get("h_mm"), "ran": bool(lv1.get("ran")), "passed": lv1.get("passed"),
               "reason": lv1.get("reason")}
        if _num(lv1.get("h_mm")):
            P.add("/sections/summary/check/h_mm", "validation.json", f"independent_check.levels.{li}.h_mm")
        gating = [(j, r) for j, r in enumerate(lv1.get("rows") or [])
                  if r.get("role") in ("objective", "constraint") and _num(r.get("rel_gap"))]
        if gating:
            j, r = max(gating, key=lambda t: abs(t[1]["rel_gap"]))
            chk["max_gap"] = {"value": r["rel_gap"], "output": r["output"]}
            P.add("/sections/summary/check/max_gap/value", "validation.json",
                  f"independent_check.levels.{li}.rows.{j}.rel_gap")
        chk["constraints_met"] = all(r.get("meets_fine") is True for r in lv1.get("rows") or []
                                     if r.get("role") == "constraint") if lv1.get("rows") else None
        chk["host"] = lv1.get("host")
        rig1 = lv1.get("rig") if isinstance(lv1.get("rig"), dict) else {}
        if lv1.get("host") == "rig" and _num(rig1.get("gpu")):
            chk["gpu"] = rig1["gpu"]
            P.add("/sections/summary/check/gpu", "validation.json", f"independent_check.levels.{li}.rig.gpu")
        summ["check"] = chk
    cv = (val or {}).get("conforming")
    if isinstance(cv, dict):
        cs = {"ran": bool(cv.get("ran")), "passed": cv.get("passed"), "status": cv.get("status"),
              "case_id": cv.get("case_id"), "host": cv.get("host"), "h_mm": cv.get("h_mm"), "reason": cv.get("reason"),
              "solver_version": (cv.get("solver") or {}).get("version"), "constraints_met": cv.get("constraints_met")}
        if _num(cv.get("h_mm")):
            P.add("/sections/summary/conforming/h_mm", "validation.json", "conforming.h_mm")
        gating = [(j, r) for j, r in enumerate(cv.get("rows") or [])
                  if r.get("role") in ("objective", "constraint", "gating") and _num(r.get("rel_gap"))]
        if gating:
            j, r = max(gating, key=lambda t: abs(t[1]["rel_gap"]))
            cs["max_gap"] = {"value": r["rel_gap"], "output": r["output"]}
            P.add("/sections/summary/conforming/max_gap/value", "validation.json", f"conforming.rows.{j}.rel_gap")
        summ["conforming"] = cs
    thin = (val or {}).get("thin")
    if thin:
        summ["thin"] = dict(thin)
        for k in ("n_ok", "n", "need"):
            if _num(thin.get(k)):
                P.add(f"/sections/summary/thin/{k}", "validation.json", f"thin.{k}")
    sim = (ledgers or {}).get("simlab") or {}
    ace = (ledgers or {}).get("aces") or {}
    if sim.get("sha256"):
        summ["bundle_sha256"] = sim["sha256"]
        summ["simlab_entry"] = sim.get("entry")
        if _num(sim.get("entry")):
            P.add("/sections/summary/simlab_entry", "evidence/ledgers.json", "simlab.entry")
    if ace.get("n") is not None:
        summ["aces_entry"] = ace.get("n")
        P.add("/sections/summary/aces_entry", "evidence/ledgers.json", "aces.n")
    if ledgers and not ace.get("n"):
        summ["aces_error"] = ace.get("error") or "not recorded"
    wall, wall_key = _wall(run_doc)
    if wall is not None:
        summ["wall_s"] = wall
        # before the run finishes (stage 9 writes the report) the number is the sum of the stage wall times, not the
        # run's total: the verdict line labels it "stages" so it is never read as result.json wall_s
        summ["wall_basis"] = "total" if wall_key == "timings.total_s" else "stages"
        P.add("/sections/summary/wall_s", "run.json", wall_key)
    S["summary"] = summ

    # 1 key numbers
    kn: dict = {}
    if final_res:
        src = f"{final_case}/results.json"
        for k in ("mass_kg", "mesh_volume_mm3", "voxel_volume_mm3", "mass_voxel_kg", "compliance_Nmm", "umax_mm",
                  "vm_p99_MPa", "vm_p95_MPa", "vm_max_MPa", "pcg_iters"):
            if _num(final_res.get(k)):
                kn[k] = final_res[k]
                P.add(f"/sections/key_numbers/{k}", src, k)
        kn["case_id"] = final_case
    if sweep:
        kn["designs"] = {"n": sweep.get("n"), "n_ok": sweep.get("n_ok")}
        for k in ("n", "n_ok"):
            if _num(sweep.get(k)):
                P.add(f"/sections/key_numbers/designs/{k}", "sweep.json", k)
    S["key_numbers"] = kn or {"status": _stage_why(run_doc, "loop", "no verified or candidate design")}

    # 2 inputs and provenance
    dom_in = R.json("inputs/domain.json", {}) or {}
    dom = R.json("domain/domain.json", {}) or {}
    digest = ((bundle or {}).get("golden") or {}).get("solver_digest")
    if digest is None:
        try:
            from .evidence import golden_code, parse_snapshot, snapshot_path

            snap = parse_snapshot(snapshot_path())
            digest = {"source_commit": snap.get("source_commit"), "content_sha256": snap.get("content_sha256"),
                      "matches_snapshot": None, **golden_code()}
        except Exception:
            digest = None
    S["inputs"] = {
        "spec": {"path": "inputs/spec.json", "sha256": R.sha("inputs/spec.json"),
                 "effective_sha256": R.sha("inputs/spec.effective.json"),
                 "source": f"demo {run_doc.get('demo')}" if run_doc.get("demo") else "spec file"},
        "overrides": d.get("overrides") or {},
        "domain": {"source": (eff.get("domain") or {}).get("source"), "ref": d.get("domain_ref"),
                   "step": "inputs/domain.step", "step_sha256": dom_in.get("step_sha256") or R.sha("inputs/domain.step"),
                   "step_note": dom_in.get("note"), "mask_sha256": dom.get("mask_sha256"),
                   "brep_volume_mm3": dom_in.get("brep_volume_mm3"), "bbox_mm": dom_in.get("bbox_mm")},
        "material": eff.get("material"), "supports": eff.get("supports") or [], "loads": eff.get("loads") or [],
        "keep_in": (eff.get("domain") or {}).get("keep_in") or [],
        "keep_out": (eff.get("domain") or {}).get("keep_out") or [],
        "resolution": {k: d.get(k) for k in ("gen_mm", "fea_mm", "field_h_mm", "check_factor", "check_levels",
                                             "stress_exclude_mm", "patch_keep_in_mm")},
        "grid": d.get("grid"), "stress_exclude": dom.get("stress_exclude") or [],
        "stress_exclude_effective_mm": dom.get("stress_exclude_effective_mm"),
        "stress_exclude_lattice_mm": dom.get("stress_exclude_lattice_mm"), "seed": d.get("seed"),
        "objective": d.get("objective"), "constraints": d.get("constraints"), "variables": d.get("variables"),
        "fixed": d.get("fixed"), "solver_digest": digest, "demo": bool(run_doc.get("demo")),
        "hypothesis": eff.get("hypothesis"),
    } if eff else {"status": _stage_why(run_doc, "inputs", "no inputs/spec.effective.json")}
    for k in ("stress_exclude_effective_mm", "stress_exclude_lattice_mm"):
        if eff and _num(dom.get(k)):
            P.add(f"/sections/inputs/{k}", "domain/domain.json", k)

    # 3 design-space voxelisation
    if dom:
        S["voxel"] = {"stats": dom.get("stats"), "stats_fea": dom.get("stats_fea"), "shape": dom.get("shape"),
                      "h_gen_mm": dom.get("h_gen_mm"), "design_share": dom.get("design_share"),
                      "n_void_boxes": dom.get("n_void_boxes"), "field_h_mm": dom.get("field_h_mm"),
                      "mask_slices": "domain/mask_slices.png" if R.exists("domain/mask_slices.png") else None,
                      "wall_s": dom.get("wall_s")}
    else:
        S["voxel"] = {"status": _stage_why(run_doc, "domain", "no domain/domain.json")}

    # 4 compute used
    used = R.json("compute_used.json")
    if isinstance(used, dict):
        rows = used.get("rows") or []
        S["compute"] = {"rows": rows, "detected_utc": (comp or {}).get("detected_utc"),
                        "plan": (comp or {}).get("plan")}
        P.walk(rows, "/sections/compute/rows", "compute_used.json", "rows")
    else:
        S["compute"] = {"status": _stage_why(run_doc, "export", "no compute_used.json"),
                        "detected": bool(comp), "plan": (comp or {}).get("plan")}

    # 5 sweep
    if isinstance(sweep, dict):
        srows = []
        for i, r in enumerate(sweep.get("rows") or []):
            srows.append({k: r.get(k) for k in ("case_id", "status", "design_status", "error", "wall_s",
                                                *(sweep.get("variables") or {}).keys(), *SWEEP_COLUMNS) if k in r})
        failed = [{"case_id": r.get("case_id"), "design_status": r.get("design_status"),
                   "error": (str(r.get("error") or "").strip().splitlines() or [""])[-1][:200]}
                  for r in sweep.get("rows") or [] if r.get("status") != "FINISHED"]
        S["sweep"] = {"sampler": sweep.get("sampler"), "variables": sweep.get("variables"), "n": sweep.get("n"),
                      "n_ok": sweep.get("n_ok"), "rows": srows, "failed": failed,
                      "csv_sha256": sweep.get("csv_sha256"),
                      "gallery": "figures/sweep_gallery.png" if R.exists("figures/sweep_gallery.png") else None,
                      "scatter": "figures/sweep_scatter.png" if R.exists("figures/sweep_scatter.png") else None}
    else:
        S["sweep"] = {"status": _stage_why(run_doc, "sweep", "no sweep.json")}

    # 6 surrogate, 7 optimisation (per round)
    srounds, orounds = [], []
    k = 1
    while True:
        cv = R.json(f"surrogate/round-{k}/cv.json")
        op = R.json(f"optimise/round-{k}.json")
        if cv is None and op is None:
            break
        if cv is not None:
            srounds.append({"round": k, "source": f"surrogate/round-{k}/cv.json", **{
                key: cv.get(key) for key in ("kind", "folds", "n", "inputs", "outputs", "log_outputs", "cv", "status",
                                             "flags", "gp_warnings", "gp_warning_kinds", "model_sha256", "data_sha256",
                                             "training_cases", "wall_s")}})
        if op is not None:
            orounds.append({"round": k, "source": f"optimise/round-{k}.json", **{
                key: op.get(key) for key in ("method", "n_candidates", "n_excluded", "n_feasible", "kappa", "tol",
                                             "margins", "exclusion_radius", "excluded", "winner", "from_feasible",
                                             "closest_infeasible", "top", "wall_s")}})
        k += 1
    loop_why = _stage_why(run_doc, "loop", "the surrogate loop did not run")
    S["surrogate"] = {"rounds": srounds} if srounds else {"rounds": [], "status": loop_why}
    S["optimise"] = ({"rounds": orounds, "map": "figures/optimise_map.png" if R.exists("figures/optimise_map.png")
                      else None} if orounds else {"rounds": [], "status": loop_why})

    # 8 verification
    if isinstance(val, dict):
        vr = [{key: r.get(key) for key in ("round", "case_id", "params", "from_feasible", "h_fea_mm", "device", "solver",
                                           "rows", "status", "design_status", "converged", "passed", "reasons")}
              for r in rounds]
        S["verification"] = {"rounds": vr, "resolution_mm": val.get("resolution_mm"), "outcome": val.get("outcome"),
                             "tol": val.get("tol"), "kappa": val.get("kappa"), "cv_margin": val.get("cv_margin"),
                             "check_objective": val.get("check_objective")}
        for i, r in enumerate(rounds):
            P.walk(r.get("rows") or [], f"/sections/verification/rounds/{i}/rows", "validation.json",
                   f"rounds.{i}.rows")
        P.walk(val.get("resolution_mm") or {}, "/sections/verification/resolution_mm", "validation.json",
               "resolution_mm")
        if not rounds:
            S["verification"]["status"] = ("not run: thin sweep" if val.get("outcome") == "thin-sweep" else loop_why)
        # 9 check and parity
        parity_why = None
        if not val.get("device_parity"):
            notes = [n for n in (((comp or {}).get("plan") or {}).get("notes") or []) if "parity" in n]
            if val.get("outcome") == "thin-sweep":
                parity_why = "not run: thin sweep"
            elif not any(r.get("passed") for r in rounds):
                parity_why = "not run: verification failed"
            else:
                parity_why = "not run: " + ("; ".join(notes) if notes else "no second device was planned")
        S["check"] = {"independent_check": ic, "device_parity": val.get("device_parity") or [],
                      "parity_reason": parity_why,
                      "grade": val.get("grade"), "grade_reasons": val.get("grade_reasons") or [],
                      "scope": val.get("scope"),
                      "chart": "figures/check_levels.png" if R.exists("figures/check_levels.png") else None}
        P.walk(ic.get("levels") or [], "/sections/check/independent_check/levels", "validation.json",
               "independent_check.levels")
        P.walk(val.get("device_parity") or [], "/sections/check/device_parity", "validation.json", "device_parity")
    else:
        S["verification"] = {"rounds": [], "status": loop_why}
        S["check"] = {"status": _stage_why(run_doc, "check", "no validation.json")}

    # 10 conforming second discretisation, 11 rig
    S["conforming"] = _conforming_section(R, P, val, run_doc)
    S["rig"] = _rig_section(R, P, val, comp)

    # 12 winner or candidate
    if winner:
        ren = {k2: v for k2, v in (winner.get("renders") or {}).items() if R.exists(v)}
        case_dir = winner.get("case_id")
        skin = R.json(f"{case_dir}/skin.json", {}) if case_dir else {}
        S["winner"] = {"label": winner_label, "case_id": winner.get("case_id"), "params": winner.get("params"),
                       "metrics": winner.get("metrics"), "grade": winner.get("grade"), "verdict": winner.get("verdict"),
                       "stl": winner.get("stl"), "density": winner.get("density"), "step": winner.get("step") or
                       {"written": False, "reason": NO_STEP}, "renders": ren, "mass_kg": winner.get("mass_kg"),
                       "voxel": (final_res or {}).get("voxel"),
                       "infill": (skin or {}).get("infill"), "cavities": (skin or {}).get("cavities"),
                       "cavity_note": CAVITY_NOTE if (skin or {}).get("infill") or (skin or {}).get("cavities") else None,
                       "note": winner.get("note")}
    else:
        S["winner"] = {"status": _stage_why(run_doc, "export", "no verified or candidate design was exported")}

    # 13 evidence
    if isinstance(ledgers, dict):
        anchor_p = "evidence/files.sha256"
        n_files = None
        if R.exists(anchor_p):
            n_files = sum(1 for ln in Path(run.path("evidence", "files.sha256")).read_text(encoding="utf-8").splitlines()
                          if ln.strip())
        ev = {"files_sha256": ((bundle or {}).get("golden") or {}).get("files_sha256"), "anchored_files": n_files,
              "bundle_copy": "evidence/bundle.json" if R.exists("evidence/bundle.json") else None,
              "record_copy": "evidence/record.md" if R.exists("evidence/record.md") else None,
              "simlab": sim, "aces": ace, "project": (ledgers or {}).get("project"),
              "commands": [f"aces golden verify {run.run_id}", "aces-evidence verify", "aces ledger verify"]}
        S["evidence"] = ev
        P.walk(sim, "/sections/evidence/simlab", "evidence/ledgers.json", "simlab")
        P.walk(ace, "/sections/evidence/aces", "evidence/ledgers.json", "aces")
    else:
        S["evidence"] = {"status": _stage_why(run_doc, "evidence", "the evidence stage has not run yet"),
                         "commands": [f"aces golden verify {run.run_id}", "aces-evidence verify", "aces ledger verify"]}

    # 14 environment
    env: dict = {}
    if comp:
        cpu = comp.get("cpu") or {}
        env = {"host": comp.get("host"), "platform": comp.get("platform"), "python": comp.get("python"),
               "cpu": {k2: cpu.get(k2) for k2 in ("model", "logical", "usable", "mem_total_gb", "mem_available_gb",
                                                  "numpy")},
               "blas_threads_per_solve": 1, "workers": ((comp.get("plan") or {}).get("sweep") or {}).get("jobs"),
               "gpu": {k2: (comp.get("gpu") or {}).get(k2) for k2 in ("name", "usable", "reason", "cuda_runtime",
                                                                     "mem_total_mib")},
               "warp": {k2: (comp.get("warp") or {}).get(k2) for k2 in ("version", "usable", "reason")}}
    for c in R.cases():
        ev = R.json(f"{c['case_id']}/evidence.json")
        if isinstance(ev, dict) and (ev.get("environment") or {}).get("packages"):
            env["packages"] = ev["environment"]["packages"]
            break
    gp_warn = sum(int(sr.get("gp_warnings") or 0) for sr in srounds)
    env["known_warnings"] = [FILTERS_WARNING,
                             f"{gp_warn} Gaussian-process warnings captured while fitting (counted, not printed)"
                             if srounds else "no surrogate was fitted, so no GP warnings"]
    S["environment"] = env

    # 15 limits and reproduction
    simp = {}
    for c in R.cases():
        if c.get("role") in ("sweep", "verify"):
            gr = ((R.json(f"{c['case_id']}/status.json") or {}).get("diagnostics") or {}).get("generator") or {}
            if gr:
                simp[c["case_id"]] = {k: gr.get(k) for k in ("mnd_pct", "iterations_run", "stopped_at_cap",
                                                             "final_change")}
                P.walk(simp[c["case_id"]], f"/sections/limits/simp/{_esc(c['case_id'])}",
                       f"{c['case_id']}/status.json", "diagnostics.generator")
    conf_sec, rig_sec = S["conforming"], S["rig"]
    not_verified = []
    if not conf_sec.get("ran"):
        why = conf_sec.get("reason") or conf_sec.get("status") or "not in this run's records"
        why = str(why)[len("not run: "):] if str(why).startswith("not run: ") else str(why)
        not_verified.append(f"no conforming-mesh second discretisation ({why})")
    if rig_sec.get("status"):
        not_verified.append("no rig run (desktop only)")
    not_verified += ["no fatigue or buckling check", "nominal material properties (see the material source)"]
    S["limits"] = {
        "grade_meaning": "every desktop solve is preview-grade; verified-desktop means the finer-resolution "
                         "independent check agreed within tol and the constraints held there; rig-verified adds that "
                         "the level-1 check was solved on the rig with a validated image, that the conforming tet10 "
                         "second discretisation ran on the rig and agreed within its bands with every constraint met, "
                         "and that the rig ran this desktop's golden code; a conforming check that violates a "
                         "constraint caps the grade at preview-grade",
        "method": "linear static voxel FEA (H8 elements, fictitious domain with a soft void, Jacobi-PCG, FP64)",
        "generator": "SIMP topology optimisation without projection: grey zones remain in the density field, so the "
                     "organic skin is the iso-surface of a partly grey field (M_nd below, per design)",
        "simp": simp,
        "iterations": (eff.get("generator") or {}).get("iterations"),
        "stress": "the constrained stress is vm_p99 outside the boundary-condition zones; vm_max is recorded but is "
                  "resolution-sensitive and is never constrained",
        "check": (ic.get("reason") if ic else None),
        "not_verified": not_verified,
        "argv": ((run_doc.get("tool") or {}).get("argv")), "seed": d.get("seed"),
        "determinism": "BLAS runs one thread per solve and sweep.csv carries no wall times, so the same seed on the "
                       "same machine reproduces sweep.csv byte for byte for any --jobs",
    }

    data = {"schema": SCHEMA["report"], "run_id": run.run_id, "verdict": summ.get("verdict"),
            "outcome": summ.get("outcome"), "grade": summ.get("grade") or GRADE_PREVIEW, "sections": clean(S),
            "provenance": P.items}
    data["verdict_line"] = verdict_line(data, evidence=True)
    return data


# ----------------------------------------------------------------------------- verdict line

def verdict_line(data: dict, *, evidence: bool = True) -> str:
    """The pinned one-line answer. evidence=False leaves out the bundle and ledger positions (they do not exist yet
    when the bundle's own note is written)."""
    s = (data.get("sections") or {}).get("summary") or {}
    name = s.get("name") or "golden"
    verdict = s.get("verdict") or data.get("verdict")
    grade = s.get("grade") or data.get("grade") or GRADE_PREVIEW
    tail = []
    if evidence:
        if s.get("bundle_sha256"):
            tail.append(f"bundle {s['bundle_sha256'][:12]} simlab #{s.get('simlab_entry')}")
            tail.append(f"aces #{s['aces_entry']}" if s.get("aces_entry") is not None else
                        f"aces not recorded ({s.get('aces_error') or 'no entry'})")
        else:
            tail.append("evidence not recorded")
    if s.get("wall_s") is not None:  # "stages": the sum of the stage times, not result.json wall_s (see assemble)
        tail.append(f"{'stages ' if s.get('wall_basis') == 'stages' else ''}{g4(float(s['wall_s']))} s")
    if verdict == "PASS":
        parts = [f"PASS · {grade} · {name}: " + " ".join(f"{k} {g4(v)}" for k, v in (s.get("params") or {}).items())]
        if s.get("mass_kg") is not None:
            parts.append(f"mass {g4(s['mass_kg'])} kg")
        if s.get("objective"):
            o = s["objective"]
            parts.append(f"{o['output']} {g4(o['value'])}")
        for c in s.get("constraints") or []:
            parts.append(f"{c['output']} {g4(c.get('value'))} {c['op']} {g4(c['limit'])}")
        if s.get("max_rel_err"):
            parts.append(f"predicted within {g4(100 * float(s.get('tol') or 0))} % (max "
                         f"{g4(100 * float(s['max_rel_err']['value']))} % on {s['max_rel_err']['output']})")
        chk = s.get("check")
        if chk:
            h = g4(chk.get("h_mm"))
            where = ""
            if chk.get("host") == "rig":
                where = f" on the rig (GPU {g4(chk['gpu'])})" if chk.get("gpu") is not None else " on the rig"
            if not chk.get("ran"):
                why = str(chk.get("reason") or "not run")
                why = why[len("not run: "):] if why.startswith("not run: ") else why
                parts.append(f"check {h} mm: not run ({why})")
            else:
                gap = chk.get("max_gap")
                gap_txt = (f"max gap {_pct(gap['value'], signed=True)} on {gap['output']}" if gap else "no gaps")
                if chk.get("passed"):
                    parts.append(f"check {h} mm{where}: {gap_txt}, constraints met")
                else:
                    met = "constraints met" if chk.get("constraints_met") else "constraints not met"
                    parts.append(f"check {h} mm{where}: {gap_txt}, "
                                 f"{met if chk.get('constraints_met') is not None else 'failed'}")
        conf = s.get("conforming")
        if conf:
            parts.append(conforming_fragment(conf))
        return " · ".join(parts + tail)
    if verdict == "FAIL":
        first = (s.get("reasons") or ["verification failed"])[0]
        k = s.get("rounds") or 0
        return " · ".join([f"FAIL · {name}: {first} after {k} round(s)"] + tail)
    if verdict == "INFEASIBLE":
        k = s.get("rounds") or 0
        return " · ".join([f"INFEASIBLE · {name}: no candidate predicted feasible with the margins (round {k})"] + tail)
    if verdict == "THIN":
        t = s.get("thin") or {}
        return " · ".join([f"THIN · {name}: {t.get('n_ok')} of {t.get('n')} designs finished (need {t.get('need')})"]
                          + tail)
    outcome = s.get("outcome") or data.get("outcome") or "error"
    msg = s.get("message") or "the run did not reach a verdict"
    return " · ".join([f"{str(outcome).upper()} · {name}: {msg}"] + [t for t in tail if not t.startswith("evidence")])


def conforming_fragment(conf: dict) -> str:
    """The verdict line's conforming fragment: `conforming tet10 2 mm (ccx 2.23, rig): max gap -4.8 % on vm_p99_MPa,
    constraints met`, `... constraints not met`, `...: failed (<status>)`, or `conforming tet10: not run (<reason>)`."""
    if not conf.get("ran"):
        why = str(conf.get("reason") or conf.get("status") or "not run")
        why = why[len("not run: "):] if why.startswith("not run: ") else why
        return f"conforming tet10: not run ({why})"
    who = f"ccx {conf['solver_version']}" if conf.get("solver_version") else "ccx"
    head = f"conforming tet10 {g4(conf.get('h_mm'))} mm ({who}, {conf.get('host')})"
    gap = conf.get("max_gap")
    if not gap:
        return f"{head}: failed ({conf.get('status')})"
    text = f"{head}: max gap {_pct(gap['value'], signed=True)} on {gap['output']}"
    met = conf.get("constraints_met")
    return text + ("" if met is None else ", constraints met" if met else ", constraints not met")


# ----------------------------------------------------------------------------- charts

def chart_figure(rows: int, cols: int, *, size=(8.0, 5.0)):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(rows, cols, figsize=size, squeeze=False, layout="constrained")
    fig.patch.set_facecolor(CHART["bg"])
    flat = list(axes.ravel())
    for ax in flat:
        ax.set_facecolor(CHART["bg"])
    return fig, flat


def chart_axes(ax, xlabel: str, ylabel: str) -> None:
    ax.set_xlabel(xlabel, color=CHART["muted"], fontsize=9, family="monospace")
    ax.set_ylabel(ylabel, color=CHART["muted"], fontsize=9, family="monospace")
    ax.tick_params(colors=CHART["muted"], labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(CHART["grid"])
    ax.grid(True, color=CHART["grid"], linewidth=0.7)
    ax.set_axisbelow(True)


def chart_legend(ax, *, markerscale: float = 1.0) -> None:
    leg = ax.legend(loc="best", fontsize=8, frameon=True, markerscale=markerscale)
    if leg is None:
        return
    leg.get_frame().set_facecolor(CHART["bg"])
    leg.get_frame().set_edgecolor(CHART["grid"])
    for t in leg.get_texts():
        t.set_color(CHART["text"])


def save_chart(fig, path: Path) -> Path:
    import matplotlib.pyplot as plt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130, facecolor=CHART["bg"], bbox_inches="tight", metadata={"Software": None})
    plt.close(fig)
    return path


def _limit_line(ax, value: float, vertical: bool, label: str) -> None:
    if vertical:
        ax.axvline(value, color=CHART["muted"], linewidth=1.2, zorder=2)
        ax.annotate(label, (value, 1.0), xycoords=("data", "axes fraction"), xytext=(4, -12),
                    textcoords="offset points", color=CHART["text"], fontsize=8, family="monospace")
    else:
        ax.axhline(value, color=CHART["muted"], linewidth=1.2, zorder=2)
        ax.annotate(label, (0.0, value), xycoords=("axes fraction", "data"), xytext=(4, 4), textcoords="offset points",
                    color=CHART["text"], fontsize=8, family="monospace")


def _sweep_scatter(R: _Reader, run, eff: dict) -> Path | None:
    from matplotlib.lines import Line2D

    d = eff.get("derived") or {}
    obj = (d.get("objective") or {}).get("output")
    cons = list(d.get("constraints") or [])
    sweep = R.json("sweep.json")
    if not obj or not cons or not isinstance(sweep, dict):
        return None
    ok = [r for r in sweep.get("rows") or [] if r.get("status") == "FINISHED"]
    bad = [r for r in sweep.get("rows") or [] if r.get("status") != "FINISHED"]
    verify = []
    for c in R.cases():
        if c.get("role") == "verify":
            res = R.json(f"{c['case_id']}/results.json")
            if isinstance(res, dict):
                verify.append((c["case_id"], res))
    val = R.json("validation.json") or {}
    win = val.get("winner_case") if val.get("verdict") == "PASS" else None
    n = len(cons)
    fig, axes = chart_figure(1, n, size=(5.2 * n, 4.4))
    for i, con in enumerate(cons):
        ax = axes[i]
        o = con["output"]
        xs = [r.get(o) for r in ok if _num(r.get(o)) and _num(r.get(obj))]
        ys = [r.get(obj) for r in ok if _num(r.get(o)) and _num(r.get(obj))]
        if xs:
            ax.scatter(xs, ys, s=48, c=CHART["series"][0], edgecolors=CHART["bg"], linewidths=1.5, zorder=3,
                       label=f"sweep designs ({len(xs)})")
        vx = [(cid, res) for cid, res in verify if _num(res.get(o)) and _num(res.get(obj))]
        if vx:
            ax.scatter([res[o] for _, res in vx], [res[obj] for _, res in vx], s=170, marker="*", c=CHART["series"][1],
                       edgecolors=CHART["bg"], linewidths=1.0, zorder=4, label=f"verify solves ({len(vx)})")
            for cid, res in vx:
                ax.annotate(cid, (res[o], res[obj]), xytext=(7, 5), textcoords="offset points", color=CHART["text"],
                            fontsize=8, family="monospace")
                if cid == win:
                    ax.scatter([res[o]], [res[obj]], s=380, facecolors="none", edgecolors=CHART["series"][2],
                               linewidths=2.0, zorder=5, label="verified winner")
        _limit_line(ax, float(con["limit"]), True, f"limit {con['op']} {g4(float(con['limit']))}")
        chart_axes(ax, f"{o} (real solve)", f"{obj} (real solve)")
        handles, labels = ax.get_legend_handles_labels()
        if bad:
            handles.append(Line2D([], [], linestyle="none", marker="x", markersize=7, color=CHART["text"]))
            labels.append(f"failed designs ({len(bad)}, no metrics)")
        if i == 0 and handles:
            leg = ax.legend(handles, labels, loc="best", fontsize=8, frameon=True, markerscale=0.6)
            leg.get_frame().set_facecolor(CHART["bg"])
            leg.get_frame().set_edgecolor(CHART["grid"])
            for t in leg.get_texts():
                t.set_color(CHART["text"])
    if bad:
        txt = "failed: " + ", ".join(f"{r.get('case_id')} ({r.get('design_status')})" for r in bad[:8])
        fig.text(0.01, -0.02, txt, color=CHART["muted"], fontsize=8, family="monospace", ha="left", va="top")
    fig.suptitle(f"{eff.get('name')}: objective vs each constrained output, real solves (preview-grade)",
                 color=CHART["text"], fontsize=10, x=0.01, ha="left")
    return save_chart(fig, Path(run.path("figures", "sweep_scatter.png")))


def _check_levels(R: _Reader, run, eff: dict) -> Path | None:
    val = R.json("validation.json") or {}
    ic = val.get("independent_check") or {}
    ran = [lv for lv in ic.get("levels") or [] if lv.get("ran") and lv.get("rows")]
    if not ran:
        return None
    gating = [r["output"] for r in ran[0]["rows"] if r.get("role") in ("objective", "constraint")]
    seen, outs = set(), []
    for o in gating:
        if o not in seen:
            seen.add(o)
            outs.append(o)
    h0 = (val.get("resolution_mm") or {}).get("solve")
    limits = {c["output"]: c for c in val.get("constraints") or []}
    fig, axes = chart_figure(1, len(outs), size=(4.4 * len(outs), 4.0))
    for i, o in enumerate(outs):
        ax = axes[i]
        row0 = next(r for r in ran[0]["rows"] if r["output"] == o)
        hs, vs = [h0], [row0.get("coarse")]
        for lv in ran:
            r = next((x for x in lv["rows"] if x["output"] == o), None)
            if r is not None and _num(r.get("fine")):
                hs.append(lv["h_mm"])
                vs.append(r["fine"])
        pts = [(h, v) for h, v in zip(hs, vs) if _num(h) and _num(v)]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=CHART["series"][0], linewidth=2.0, marker="o",
                    markersize=8, markeredgecolor=CHART["bg"], markeredgewidth=1.5, zorder=3)
            labels = ["verify"] + [f"level {lv['level']}" for lv in ran]
            for k, ((h, v), lab) in enumerate(zip(pts, labels)):
                ha = "left" if k == 0 else "right" if k == len(pts) - 1 else "center"
                ax.annotate(f"{lab}\n{g4(float(v))}", (h, v), xytext=(0, 9), textcoords="offset points", ha=ha,
                            va="bottom", color=CHART["text"], fontsize=8, family="monospace")
            lo_v, hi_v = min(p[1] for p in pts), max(p[1] for p in pts)
            pad = 0.25 * (hi_v - lo_v) or 0.05 * abs(hi_v) or 1.0
            top = hi_v + pad
            if o in limits:
                top = max(top, float(limits[o]["limit"]) + 0.5 * pad)
            ax.set_ylim(lo_v - pad, top)
        if o in limits:
            c = limits[o]
            _limit_line(ax, float(c["limit"]), False, f"limit {c['op']} {g4(float(c['limit']))}")
        ax.invert_xaxis()
        chart_axes(ax, "voxel size h (mm), finer to the right", o)
    fig.suptitle(f"{eff.get('name')}: gating outputs at the solve and check resolutions", color=CHART["text"],
                 fontsize=10, x=0.01, ha="left")
    return save_chart(fig, Path(run.path("figures", "check_levels.png")))


def _conforming_chart(R: _Reader, run, eff: dict) -> Path | None:
    """figures/conforming.png: for each gating output (objective, constraints, compliance) the voxel values against h
    (the verify solve and each check level), the tet10 value as a point at its element size, and the conforming band
    shaded around the reference voxel value. Only when the conforming check ran and produced numbers."""
    val = R.json("validation.json") or {}
    cv = val.get("conforming") or {}
    if not cv.get("ran"):
        return None
    rows = [r for r in cv.get("rows") or [] if r.get("role") in ("objective", "constraint", "gating")
            and _num(r.get("conforming")) and _num(r.get("voxel"))]
    if not rows:
        return None
    ic = val.get("independent_check") or {}
    ran = [lv for lv in ic.get("levels") or [] if lv.get("ran") and lv.get("rows")]
    h0 = (val.get("resolution_mm") or {}).get("solve")
    limits = {c["output"]: c for c in val.get("constraints") or []}
    fig, axes = chart_figure(1, len(rows), size=(4.4 * len(rows), 4.0))
    for i, r in enumerate(rows):
        ax, o = axes[i], r["output"]
        pts, labels = [], []
        first = next((x for x in (ran[0]["rows"] if ran else []) if x.get("output") == o), None)
        if first is not None and _num(first.get("coarse")) and _num(h0):
            pts.append((float(h0), float(first["coarse"])))
            labels.append("verify")
        for lv in ran:
            x = next((x for x in lv["rows"] if x.get("output") == o), None)
            if x is not None and _num(x.get("fine")) and _num(lv.get("h_mm")):
                pts.append((float(lv["h_mm"]), float(x["fine"])))
                labels.append(f"{lv.get('case_id')} ({lv.get('host') or 'desktop'})")
        ref, band = float(r["voxel"]), float(r.get("band") or 0.0)
        lo, hi = ref - band * abs(ref), ref + band * abs(ref)
        ax.axhspan(lo, hi, color=CHART["series"][0], alpha=0.16, zorder=1, label=f"band +-{100 * band:g} %")
        if pts:
            ax.plot([q[0] for q in pts], [q[1] for q in pts], color=CHART["series"][0], linewidth=2.0, marker="o",
                    markersize=8, markeredgecolor=CHART["bg"], markeredgewidth=1.5, zorder=3, label="voxel H8")
            for (h, v), lab in zip(pts, labels):
                ax.annotate(f"{lab}\n{g4(v)}", (h, v), xytext=(0, 9), textcoords="offset points", ha="center",
                            va="bottom", color=CHART["text"], fontsize=8, family="monospace")
        hc, vc = float(cv.get("h_mm") or 0.0), float(r["conforming"])
        ax.scatter([hc], [vc], s=150, marker="D", c=CHART["series"][1], edgecolors=CHART["bg"], linewidths=1.0,
                   zorder=4, label=f"tet10 C3D10 ({cv.get('case_id')}, {cv.get('host')})")
        ax.annotate(f"tet10\n{g4(vc)}", (hc, vc), xytext=(8, -14), textcoords="offset points", ha="left", va="top",
                    color=CHART["text"], fontsize=8, family="monospace")
        vals = [q[1] for q in pts] + [vc, lo, hi]
        if o in limits:
            _limit_line(ax, float(limits[o]["limit"]), False, f"limit {limits[o]['op']} {g4(float(limits[o]['limit']))}")
            vals.append(float(limits[o]["limit"]))
        span = (max(vals) - min(vals)) or abs(ref) or 1.0
        ax.set_ylim(min(vals) - 0.15 * span, max(vals) + 0.3 * span)
        ax.invert_xaxis()
        chart_axes(ax, "element size h (mm), finer to the right", o)
        if i == 0:
            chart_legend(ax, markerscale=0.7)
    fig.suptitle(f"{eff.get('name')}: conforming tet10 against the voxel solves, gating outputs (band shaded)",
                 color=CHART["text"], fontsize=10, x=0.01, ha="left")
    return save_chart(fig, Path(run.path("figures", "conforming.png")))


def _map(R: _Reader, run, eff: dict) -> Path | None:
    from . import optimise as opt_mod
    from . import surrogate as sur_mod

    k = 0
    while R.json(f"surrogate/round-{k + 1}/cv.json") is not None:
        k += 1
    if k == 0 or not Path(run.path("surrogate", f"round-{k}", "model.joblib")).is_file():
        return None
    sur = sur_mod.load(Path(run.path("surrogate", f"round-{k}")))
    names = list(((eff.get("derived") or {}).get("variables") or {}).keys())
    cases = {c["case_id"]: c for c in R.cases()}
    sweep_pts = []
    for r in (R.json("sweep.json") or {}).get("rows") or []:
        p = {v: r.get(v, (cases.get(r.get("case_id")) or {}).get(v)) for v in names}
        if all(_num(x) for x in p.values()):
            sweep_pts.append({"case_id": r.get("case_id"), "params": p, "ok": r.get("status") == "FINISHED"})
    val = R.json("validation.json") or {}
    ver = [{"case_id": r.get("case_id"), "params": r.get("params"), "ok": r.get("status") == "FINISHED",
            "passed": r.get("passed")} for r in val.get("rounds") or [] if r.get("params")]
    win = val.get("winner_params") if val.get("verdict") == "PASS" else None
    return opt_mod.plot_map(sur, eff, run, {"round": k, "fit": R.json(f"surrogate/round-{k}/cv.json"),
                                            "sweep": sweep_pts, "verify": ver, "winner": win})


def charts(run) -> dict:
    """figures/sweep_scatter.png, figures/optimise_map.png, figures/check_levels.png and figures/conforming.png, each
    when its records exist. Returns {name: path} for the charts written."""
    R = _Reader(run)
    eff = R.json("inputs/spec.effective.json", {}) or {}
    out = {}
    for key, fn in (("sweep_scatter", _sweep_scatter), ("optimise_map", _map), ("check_levels", _check_levels),
                    ("conforming", _conforming_chart)):
        p = fn(R, run, eff)
        if p is not None:
            out[key] = Path(p)
    return out


# ----------------------------------------------------------------------------- markdown

def _img(path: str | None, alt: str) -> list[str]:
    return [f"![{alt}]({path})", ""] if path else []


def _status(sec: dict) -> list[str]:
    return [str(sec.get("status")), ""] if sec.get("status") else []


def _conforming_markdown(cf: dict) -> list[str]:
    L = ["## 10. Conforming second discretisation", ""]
    if cf.get("status"):
        return L + _status(cf)
    where = f"on the {cf.get('host')}" if cf.get("host") in ("rig", "desktop") else f"on {cf.get('host') or '-'}"
    if not cf.get("ran"):
        why = str(cf.get("reason") or cf.get("check_status") or "not run")
        L += [why if why.startswith("not run") else f"not run: {why}", "",
              f"Mode `{cf.get('mode') or '-'}`; status `{cf.get('check_status') or '-'}`; "
              f"case `{cf.get('case_id') or '-'}` {where}.",
              ""]
        if isinstance(cf.get("rig_attempt"), dict):
            att = cf["rig_attempt"]
            L += [f"The rig did not deliver {att.get('case_id')} ({att.get('design_status')}: {att.get('reason')}).", ""]
        return L
    m, sv, eq, wall = cf.get("mesh") or {}, cf.get("solver") or {}, cf.get("equilibrium") or {}, cf.get("wall") or {}
    ref = cf.get("reference") or {}
    L += [f"Case `{cf.get('case_id')}` {where} (mode `{cf.get('mode')}`), the part of `{cf.get('source_case')}`: "
          f"{m.get('tool')} {m.get('version')} {m.get('element')} (straight-sided tet10) at {g4(cf.get('h_mm'))} mm, "
          f"{m.get('route')} route (Algorithm3D {m.get('algorithm_3d')}): {m.get('elements')} elements, "
          f"{m.get('nodes')} nodes, {m.get('dof')} DOF; minimum SICN {g4(m.get('min_sicn'))} (1st percentile "
          f"{g4(m.get('p01_sicn'))}); mesh volume {g4(m.get('volume_mm3'))} mm3 ({_pct(m.get('volume_rel_err'), True)} "
          "against the STL).", ""]
    bcs = cf.get("bcs") or {}
    rows = [["support", b.get("name"), b.get("mode"), b.get("faces"), b.get("nodes"), b.get("area_mm2"),
             b.get("projected_mm2"), b.get("footprint_mm2"), b.get("coverage"), b.get("plane_offset_mm")]
            for b in bcs.get("supports") or []]
    rows += [["load", b.get("name"), b.get("mode"), b.get("faces"), b.get("nodes"), b.get("area_mm2"),
              b.get("projected_mm2"), b.get("footprint_mm2"), b.get("coverage"), b.get("plane_offset_mm")]
             for b in bcs.get("loads") or []]
    if rows:
        L += [f"Boundary conditions on the mesh surface (window {g4(bcs.get('window_mm'))} mm, faces facing within 45 "
              "degrees of the plane normal):", "",
              _table(["kind", "name", "mode", "faces", "nodes", "area (mm2)", "projected (mm2)", "footprint (mm2)",
                      "coverage", "plane offset (mm)"], rows), ""]
    L += [f"Solver: {sv.get('tool') or 'CalculiX ccx'} {sv.get('version') or '-'}"
          + (f" ({sv.get('linear_solver')})" if sv.get("linear_solver") else "")
          + f", {sv.get('threads')} threads; mesh {g4(wall.get('wall_s_mesh'))} s, solve {g4(wall.get('wall_s_solve'))} s, "
          f"post {g4(wall.get('wall_s_post'))} s. Equilibrium: applied {_list4(eq.get('applied_N'))} N, reactions "
          f"{_list4(eq.get('reaction_N'))} N, relative residual {g4(eq.get('rel_residual'))}.", ""]
    L += [_table(["output", "role", f"voxel ({ref.get('case_id')}, {g4(ref.get('h_mm'))} mm)", "tet10", "gap", "band",
                  "agree", "limit", "meets on tet10"],
                 [[r.get("output"), r.get("role"), r.get("voxel"), r.get("conforming"), _pct(r.get("rel_gap"), True),
                   _pct(r.get("band")), r.get("agree"), f"{r.get('op')} {g4(r.get('limit'))}" if r.get("op") else "-",
                   r.get("meets_conforming")] for r in cf.get("rows") or []]), ""]
    L += _img(cf.get("chart"), "conforming against voxel")
    if cf.get("passed"):
        verdict = "agrees within its bands and meets every constraint"
    elif cf.get("check_status") != "ok":
        verdict = f"failed: {cf.get('check_status')}" + (f" ({cf.get('message')})" if cf.get("message") else "")
    else:
        verdict = ("does not agree within its bands" if cf.get("constraints_met") is not False
                   else "violates a constraint on the conforming mesh (the grade is capped at preview-grade)")
    L += [f"Verdict: **{verdict}**." + (f" {cf.get('reason')}" if cf.get("reason") and not cf.get("passed") else ""), ""]
    if isinstance(cf.get("rig"), dict):
        rg = cf["rig"]
        L += [f"On the rig: experiment `{rg.get('experiment')}`, rig case `{rg.get('rig_case')}`, job `{rg.get('job_id')}`, "
              f"image `{rg.get('image')}` id {_image12(rg.get('image_id')) or '-'}, ccx-check validation "
              f"{rg.get('solver_validation')}.", ""]
    return L


def _rig_markdown(rg: dict) -> list[str]:
    L = ["## 11. Rig", ""]
    if rg.get("status"):
        return L + _status(rg)
    scope = rg.get("scope") or {}
    sc = ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in scope.items()) if isinstance(scope, dict) else str(scope)
    L += [f"Lane `{rg.get('lane')}` at `{rg.get('url')}` (simlab {rg.get('server_version') or '-'}, principal "
          f"`{rg.get('principal')}`, policy {rg.get('policy')}, token from {rg.get('token_source')}); scope: {sc or '-'}; "
          f"budget {g4(rg.get('budget_s'))} s per experiment; code identity "
          f"{'ok' if rg.get('code_identity_ok') else 'not ok' if rg.get('code_identity_ok') is False else '-'}.", ""]
    if rg.get("notes"):
        L += ["Probe notes: " + "; ".join(str(n) for n in rg["notes"]), ""]
    if rg.get("experiments"):
        L += [_table(["experiment", "stage", "solver", "image id", "cases", "finished", "GPUs", "wall s",
                      "max queue wait s"],
                     [[e.get("id"), e.get("stage"), e.get("solver"), e.get("image_id12") or "-", e.get("cases"),
                       e.get("finished"), ", ".join(g4(g) for g in e.get("gpus") or []) or "-", e.get("wall_s"),
                       e.get("max_wait_s")] for e in rg["experiments"]]), ""]
    if rg.get("cases"):
        L += [_table(["case", "rig case", "job id", "GPU", "wait s", "run s", "state"],
                     [[c.get("case_id"), c.get("rig_case"), c.get("job_id"), c.get("gpu"), c.get("wait_s"),
                       c.get("run_s"), c.get("state") if c.get("state") != "not-run" else
                       f"not delivered: {c.get('design_status')}"] for c in rg["cases"]]), ""]
    cancelled = rg.get("cancelled") or []
    L += [f"Cancelled: {', '.join(str(c) for c in cancelled) if cancelled else 'none'}. Integrity error: "
          f"{rg.get('integrity_error') or 'none'}.", ""]
    return L


def render_markdown(data: dict) -> str:
    S = data.get("sections") or {}
    t = S.get("title") or {}
    L: list[str] = [f"# aces golden: {t.get('name')} ({t.get('run_id')})", "", f"**{data.get('verdict_line')}**", ""]
    L += [f"Project `{t.get('project')}` · run directory `{t.get('run_dir')}` · created {t.get('created_utc') or '-'}"
          + (f" · demo `{t.get('demo')}` (demo inputs)" if t.get("demo") else ""), ""]
    if t.get("description"):
        L += [str(t["description"]), ""]
    for k, v in (t.get("renders") or {}).items():
        L += _img(v, k)

    # 1
    kn = S.get("key_numbers") or {}
    L += ["## 1. Key numbers", ""]
    if kn.get("status"):
        L += _status(kn)
    else:
        rows = [[k, kn[k]] for k in ("mass_kg", "mesh_volume_mm3", "mass_voxel_kg", "voxel_volume_mm3",
                                     "compliance_Nmm", "umax_mm", "vm_p99_MPa", "vm_p95_MPa", "vm_max_MPa", "pcg_iters")
                if k in kn]
        s = S.get("summary") or {}
        for c in s.get("constraints") or []:
            v = c.get("value")
            met = v is not None and ((v <= c["limit"]) if c["op"] == "<=" else v >= c["limit"])
            rows.append([f"constraint {c['output']} {c['op']} {g4(c['limit'])}",
                         f"{g4(v)} ({'met' if met else 'NOT met'})"])
        if kn.get("designs"):
            rows.append(["designs finished", f"{kn['designs'].get('n_ok')} of {kn['designs'].get('n')}"])
        rows.append(["grade", data.get("grade")])
        L += [f"Case `{kn.get('case_id')}` (vm_max is resolution-sensitive and never constrained).", "",
              _table(["quantity", "value"], rows), ""]

    # 2
    inp = S.get("inputs") or {}
    L += ["## 2. Inputs and provenance", ""]
    if inp.get("status"):
        L += _status(inp)
    else:
        sp = inp.get("spec") or {}
        dm = inp.get("domain") or {}
        L += [f"- spec `{sp.get('path')}` sha256 `{sp.get('sha256')}` ({sp.get('source')}); effective spec sha256 "
              f"`{sp.get('effective_sha256')}`",
              f"- overrides: {', '.join(f'{k} = {v}' for k, v in (inp.get('overrides') or {}).items()) or 'none'}",
              f"- design space: {dm.get('source')} `{dm.get('ref')}` -> `{dm.get('step')}` sha256 `{dm.get('step_sha256')}`"
              f" (B-rep volume {g4(dm.get('brep_volume_mm3'))} mm3)",
              "- " + (dm.get("step_note") or "the STEP header carries an export timestamp; the stable geometry digest "
                      "is the mask sha256"),
              f"- voxel mask sha256 `{dm.get('mask_sha256')}`; seed {inp.get('seed')}", ""]
        mat = inp.get("material") or {}
        L += [_table(["material", "E (MPa)", "nu", "density (kg/mm3)", "allowable vm (MPa)"],
                     [[mat.get("name"), mat.get("E_MPa"), mat.get("nu"), mat.get("rho_kg_mm3"),
                       mat.get("allowable_vm_MPa")]]),
              "", f"Material source: {mat.get('source') or 'not given'}", ""]
        bc = [["support", s.get("name"), s.get("box"), f"dofs {s.get('dofs')}"] for s in inp.get("supports") or []]
        bc += [["load", ld.get("name"), ld.get("box"),
                f"force {ld.get('force')} N, axis {ld.get('axis')}, case {ld.get('case')}"]
               for ld in inp.get("loads") or []]
        if bc:
            L += [_table(["kind", "name", "box (mm, part frame)", "detail"], bc), ""]
        regions = [["keep-in", r.get("name"), _shape(r), r.get("round_mm")] for r in inp.get("keep_in") or []]
        regions += [["keep-out", r.get("name"), _shape(r), r.get("round_mm")] for r in inp.get("keep_out") or []]
        if regions:
            L += [_table(["region", "name", "shape (mm)", "round (mm)"], regions), ""]
        res = inp.get("resolution") or {}
        want, got = res.get("stress_exclude_mm"), inp.get("stress_exclude_effective_mm")
        excl = f"{g4(want)} mm"
        if _num(got):
            lat = g4(inp.get("stress_exclude_lattice_mm"))
            excl = (f"{g4(got)} mm on the {lat} mm lattice" if not _num(want) or abs(float(got) - float(want)) <= 1e-9
                    else f"{g4(got)} mm (the spec asks {g4(want)} mm: rounded up to the {lat} mm lattice, so every "
                         "solve resolution excludes the same zone)")
        L += [f"- resolutions (mm): generator {g4(res.get('gen_mm'))}, skin field {g4(res.get('field_h_mm'))}, solve "
              f"{g4(res.get('fea_mm'))}, check levels "
              + (", ".join(f"{lv.get('level')}: {g4(lv.get('h_mm'))}" + (" (GPU only)" if lv.get("gpu_only") else "")
                           for lv in res.get("check_levels") or []) or "none"),
              f"- grid: origin {(inp.get('grid') or {}).get('origin_mm')} mm, envelope "
              f"{(inp.get('grid') or {}).get('envelope_mm')} mm",
              f"- stress-exclusion zones (vm_p99 ignores them): {len(inp.get('stress_exclude') or [])} boxes, {excl}",
              f"- objective {(inp.get('objective') or {}).get('sense')} {(inp.get('objective') or {}).get('output')}; "
              "constraints "
              + ", ".join(f"{c['output']} {c['op']} {g4(c['limit'])}" for c in inp.get("constraints") or []),
              "- design variables: "
              + ", ".join(f"{k} [{g4(v[0])}, {g4(v[1])}]" for k, v in (inp.get("variables") or {}).items())
              + f"; fixed: " + (", ".join(f"{k} {g4(v)}" for k, v in (inp.get("fixed") or {}).items()) or "none")]
        dg = inp.get("solver_digest") or {}
        L += [f"- solver digest: vendored snapshot {str(dg.get('source_commit'))[:12]} content sha256 "
              f"`{dg.get('content_sha256')}`; imported modules match the snapshot: {dg.get('matches_snapshot')}; "
              f"golden code sha256 `{dg.get('golden_code_sha256')}`"]
        if inp.get("hypothesis"):
            L += [f"- hypothesis: {inp['hypothesis']}"]
        if inp.get("demo"):
            L += ["- demo inputs: the material, the allowable and the loads are demonstration values, not a qualified "
                  "design case"]
        L += [""]

    # 3
    vx = S.get("voxel") or {}
    L += ["## 3. Design-space voxelisation", ""]
    if vx.get("status"):
        L += _status(vx)
    else:
        st = vx.get("stats") or {}
        L += [f"step-structures stage_mesh at {g4(vx.get('h_gen_mm'))} mm: {st.get('cells')} cells, {st.get('solid_cells')} "
              f"inside the design space (design share {g4(vx.get('design_share'))}), voxel/B-rep volume "
              f"{g4(st.get('volume_ratio_voxel_over_brep'))}, watertight tessellation {st.get('watertight')}; "
              f"{vx.get('n_void_boxes')} passive void boxes keep the generator inside the design space.", ""]
        L += _img(vx.get("mask_slices"), "design-space mask slices")

    # 4
    cu = S.get("compute") or {}
    L += ["## 4. Compute used", ""]
    if cu.get("status"):
        L += _status(cu)
    else:
        from .compute import rows_markdown

        L += [rows_markdown(cu.get("rows") or [])]

    # 5
    sw = S.get("sweep") or {}
    L += ["## 5. Generative sweep", ""]
    if sw.get("status"):
        L += _status(sw)
    else:
        smp = sw.get("sampler") or {}
        var = sw.get("variables") or {}
        L += [f"{sw.get('n')} designs from a Latin hypercube ({smp.get('optimization')}, seed {smp.get('seed')}) over "
              + ", ".join(f"{k} [{g4(v[0])}, {g4(v[1])}]" for k, v in var.items())
              + f"; {sw.get('n_ok')} finished. Every design is generated (SIMP), skinned (organic implicit surface) and "
              "solved on the real voxel FEA; all are preview-grade.", ""]
        cols = ["case_id", "status", *var.keys(), *SWEEP_COLUMNS]
        L += [_table(cols, [[r.get(c) for c in cols] for r in sw.get("rows") or []]), ""]
        if sw.get("failed"):
            L += ["Failed designs:", ""] + [f"- {f['case_id']}: {f['design_status']}"
                                             + (f" ({f['error']})" if f.get("error") else "")
                                             for f in sw["failed"]] + [""]
        L += _img(sw.get("gallery"), "sweep gallery") + _img(sw.get("scatter"), "sweep scatter")
        L += [f"sweep.csv sha256 `{sw.get('csv_sha256')}`", ""]

    # 6
    su = S.get("surrogate") or {}
    L += ["## 6. Surrogate", ""]
    if su.get("status"):
        L += _status(su)
    for r in su.get("rounds") or []:
        lo = r.get("log_outputs") or {}
        L += [f"Round {r['round']}: {r.get('kind')} on {r.get('n')} real solves, {r.get('folds')}-fold CV; log outputs "
              f"{lo.get('policy')} -> {lo.get('applied')}; status **{r.get('status')}**"
              + (f" ({'; '.join(r.get('flags') or [])})" if r.get("flags") else "")
              + f"; {r.get('gp_warnings')} GP warnings captured; model sha256 `{r.get('model_sha256')}`", ""]
        cv = r.get("cv") or {}
        L += [_table(["output", "RMSE", "MAE", "MAPE %", "R2", "log", "range"],
                     [[o, cv[o].get("rmse"), cv[o].get("mae"), cv[o].get("mape_pct"), cv[o].get("r2"),
                       (lo.get("per_output") or {}).get(o), cv[o].get("y_range")] for o in cv]), ""]

    # 7
    op = S.get("optimise") or {}
    L += ["## 7. Optimisation", ""]
    if op.get("status"):
        L += _status(op)
    for r in op.get("rounds") or []:
        w = r.get("winner") or r.get("closest_infeasible")
        L += [f"Round {r['round']}: {r.get('method')} over {r.get('n_candidates')} candidates ({r.get('n_excluded')} within "
              f"{g4(r.get('exclusion_radius'))} of a verified design excluded: "
              f"{', '.join(r.get('excluded') or []) or 'none'}); "
              f"kappa {g4(r.get('kappa'))}, margins "
              + ", ".join(f"{k} {_pct(v)}" for k, v in (r.get("margins") or {}).items())
              + f"; {r.get('n_feasible')} predicted feasible.", ""]
        if w:
            what = "winner" if r.get("from_feasible") else "closest infeasible (infill)"
            L += [f"{what}: " + ", ".join(f"{k} {g4(v)}" for k, v in (w.get("params") or {}).items()) + " -> "
                  + ", ".join(f"{o} {g4(w['pred'][o])} +- {g4(w['sd'][o])}" for o in w.get("pred") or {}), ""]
        if r.get("top"):
            keys = list((r["top"][0].get("params") or {}).keys())
            outs = list((r["top"][0].get("pred") or {}).keys())
            L += [_table(keys + outs, [[p["params"][k] for k in keys] + [p["pred"][o] for o in outs]
                                       for p in r["top"]]), ""]
    L += _img(op.get("map"), "optimisation map")

    # 8
    ve = S.get("verification") or {}
    L += ["## 8. Verification", ""]
    if ve.get("status"):
        L += _status(ve)
    rm = ve.get("resolution_mm") or {}
    if rm:
        L += [f"Resolutions recorded in the validation record (mm): generator {g4(rm.get('generator'))}, field "
              f"{g4(rm.get('field'))}, solve {g4(rm.get('solve'))}, check "
              f"{', '.join(g4(h) for h in rm.get('check') or []) or 'none'}. "
              f"Tolerance {_pct(ve.get('tol'))}, kappa {g4(ve.get('kappa'))}.", ""]
    for r in ve.get("rounds") or []:
        where = "from the feasible set" if r.get("from_feasible") else "infill"
        L += [f"Round {r.get('round')}: `{r.get('case_id')}` ({where}"
              f", {r.get('device')}, {r.get('solver')}) -> **{'PASS' if r.get('passed') else 'FAIL'}**", ""]
        L += [_table(["output", "role", "predicted", "sd", "solved", "rel err", "agree", "limit", "meets"],
                     [[x.get("output"), x.get("role"), x.get("predicted"), x.get("sd"), x.get("solved"),
                       _pct(x.get("rel_err")), x.get("agree"), f"{x.get('op')} {g4(x.get('limit'))}" if x.get("op") else "-",
                       x.get("meets")] for x in r.get("rows") or []]), ""]
        if r.get("reasons"):
            L += [f"- {why}" for why in r["reasons"]] + [""]

    # 9
    ck = S.get("check") or {}
    L += ["## 9. Independent check and device parity", ""]
    if ck.get("status"):
        L += _status(ck)
    else:
        ic = ck.get("independent_check") or {}
        L += [_table(["level", "case", "h (mm)", "device", "host", "gating", "estimate s", "budget s", "ran", "passed",
                      "reason"],
                     [[lv.get("level"), lv.get("case_id"), lv.get("h_mm"), lv.get("device"), lv.get("host") or "desktop",
                       lv.get("gating"), lv.get("est_s"), lv.get("budget_s"), lv.get("ran"), lv.get("passed"),
                       lv.get("reason")] for lv in ic.get("levels") or []]), ""]
        for lv in ic.get("levels") or []:
            att = lv.get("rig_attempt")
            if isinstance(att, dict):
                L += [f"Level {lv.get('level')} ran on this desktop as {lv.get('case_id')}: the rig did not deliver "
                      f"{att.get('case_id')} ({att.get('design_status')}: {att.get('reason')}).", ""]
        for lv in ic.get("levels") or []:
            if lv.get("rows"):
                L += [f"Level {lv['level']} ({g4(lv.get('h_mm'))} mm):", "",
                      _table(["output", "role", "coarse", "fine", "gap", "agree", "limit", "meets at fine h"],
                             [[x.get("output"), x.get("role"), x.get("coarse"), x.get("fine"), _pct(x.get("rel_gap"), True),
                               x.get("agree"), f"{x.get('op')} {g4(x.get('limit'))}" if x.get("op") else "-",
                               x.get("meets_fine")] for x in lv["rows"]]), ""]
        L += _img(ck.get("chart"), "check levels")
        par = ck.get("device_parity") or []
        if par:
            L += [_table(["case", "device", "backend", "max rel diff", "tol", "passed"],
                         [[p.get("case_id"), p.get("device"), p.get("backend"), p.get("max_rel_diff"), p.get("tol"),
                           p.get("passed")] for p in par]), ""]
        else:
            L += [f"Device parity: {ck.get('parity_reason') or 'not run'}.", ""]
        L += [f"Grade **{ck.get('grade')}**: " + ("; ".join(ck.get("grade_reasons") or []) or "-"), "",
              f"Scope: {ck.get('scope')}", ""]

    # 10
    L += _conforming_markdown(S.get("conforming") or {})

    # 11
    L += _rig_markdown(S.get("rig") or {})

    # 12
    wn = S.get("winner") or {}
    L += ["## 12. " + ("Winner" if wn.get("label") == "winner" else "Candidate" if wn.get("label") else "Winner"), ""]
    if wn.get("status"):
        L += _status(wn)
    else:
        for k, v in (wn.get("renders") or {}).items():
            L += _img(v, k)
        stl = wn.get("stl") or {}
        den = wn.get("density") or {}
        vox = wn.get("voxel") or {}
        L += [f"Case `{wn.get('case_id')}`: " + ", ".join(f"{k} {g4(v)}" for k, v in (wn.get("params") or {}).items())
              + f"; grade {wn.get('grade')}, verdict {wn.get('verdict')}.", ""]
        if stl:
            L += [f"- `{stl.get('path')}` sha256 `{stl.get('sha256')}`: {stl.get('faces')} faces, {stl.get('vertices')} "
                  f"vertices, watertight {stl.get('watertight')}, {stl.get('solids')} solid(s), {stl.get('cavities')} "
                  f"cavities, genus {stl.get('genus')}, area {g4(stl.get('area_mm2'))} mm2, "
                  f"bbox {_list4(stl.get('bbox_mm'))} mm",
                  f"- mesh volume {g4(stl.get('mesh_volume_mm3'))} mm3 vs voxel volume "
                  f"{g4(vox.get('voxel_volume_mm3'))} mm3 (ratio {g4(vox.get('volume_ratio_voxel_over_mesh'))}); mass "
                  f"{g4(wn.get('mass_kg'))} kg from the mesh volume"]
        else:
            L += [f"- no part file: {wn.get('note') or 'the case produced no part'}"]
        if den:
            L += [f"- density field `{den.get('path')}` sha256 `{den.get('sha256')}`"]
        step = wn.get("step") or {}
        if step.get("written"):
            L += [f"- STEP: `{step.get('path')}` sha256 `{step.get('sha256')}` ({step.get('faces')} faces): "
                  f"{step.get('note') or 'mesh-derived faceted STEP'}"]
        else:
            L += [f"- STEP: not written: {step.get('reason') or NO_STEP}"]
        if wn.get("cavity_note"):
            L += [f"- {wn['cavity_note']}"]
        L += [""]

    # 13
    ev = S.get("evidence") or {}
    L += ["## 13. Evidence", ""]
    if ev.get("status"):
        L += _status(ev)
    else:
        sim = ev.get("simlab") or {}
        ace = ev.get("aces") or {}
        pj = ev.get("project") or {}
        sv = sim.get("verify") or {}
        L += [f"- anchor `evidence/files.sha256` ({ev.get('anchored_files')} files) sha256 `{ev.get('files_sha256')}`",
              f"- bundle `{sim.get('path')}` sha256 `{sim.get('sha256')}`, previous "
              f"`{sim.get('previous') or 'none (first entry)'}`; copy "
              f"`{ev.get('bundle_copy')}`, record `{ev.get('record_copy')}`",
              f"- simlab ledger entry {sim.get('entry')} of {sim.get('entries')}: verify "
              f"{'ok' if sv.get('ok') else 'FAILED'}"
              f" ({sv.get('relocated', 0)} relocated)",
              f"- aces ledger entry {ace.get('n')} (golden.run) hash `{ace.get('hash')}`: verify "
              f"{'ok' if (ace.get('verify') or {}).get('ok') else 'FAILED'} ({(ace.get('verify') or {}).get('message')})"
              + (f"; error: {ace.get('error')}; replay: `{ace.get('replay')}`" if ace.get("error") else ""),
              f"- project evidence `{pj.get('evidence_ref')}`; components {', '.join(pj.get('components') or []) or 'none'}"
              + (f"; warnings: {'; '.join(pj.get('messages'))}" if pj.get("messages") else "")]
    L += ["- verify offline: " + ", ".join(f"`{c}`" for c in ev.get("commands") or []), ""]

    # 14
    en = S.get("environment") or {}
    L += ["## 14. Environment", ""]
    cpu = en.get("cpu") or {}
    gpu = en.get("gpu") or {}
    if en.get("host"):
        L += [f"- host {en.get('host')}, {en.get('platform')}, Python {en.get('python')}",
              f"- CPU {cpu.get('model')}: {cpu.get('usable')} of {cpu.get('logical')} usable, "
              f"{g4(cpu.get('mem_available_gb'))} of "
              f"{g4(cpu.get('mem_total_gb'))} GB free; {en.get('workers')} sweep workers; BLAS 1 thread per solve",
              f"- GPU: {gpu.get('name') or 'none'} ({'usable' if gpu.get('usable') else 'not used'}: {gpu.get('reason')})",
              f"- Warp: {(en.get('warp') or {}).get('version') or '-'} ({(en.get('warp') or {}).get('reason')})"]
    if en.get("packages"):
        L += ["- packages: " + ", ".join(f"{k} {v}" for k, v in en["packages"].items())]
    L += ["- known warnings: " + "; ".join(en.get("known_warnings") or []), ""]

    # 15
    lm = S.get("limits") or {}
    L += ["## 15. Limits and reproduction", ""]
    L += [f"- {lm.get('grade_meaning')}", f"- {lm.get('method')}", f"- {lm.get('generator')}"]
    simp = lm.get("simp") or {}
    if simp:
        capped = sum(1 for x in simp.values() if x.get("stopped_at_cap"))
        L += ["- M_nd (percent of grey design elements, 0 % is crisp): "
              + ", ".join(f"{cid} {g4(x.get('mnd_pct'))} %" for cid, x in simp.items()),
              f"- {capped} of {len(simp)} SIMP runs stopped at the {lm.get('iterations')}-iteration cap with the density "
              "change still above change_tol (the generator's own convergence; every part was still solved on the real "
              "FEA)" if capped else f"- all {len(simp)} SIMP runs met change_tol before the iteration cap"]
    L += [f"- {lm.get('stress')}"]
    if lm.get("check"):
        L += [f"- independent check: {lm['check']}"]
    argv = [str(a) for a in (lm.get("argv") or [])]
    if argv and not (argv[0].endswith("aces-golden") or argv[0] == "aces"):
        argv = ["aces", "golden"] + argv  # run.json records the arguments after the command
    L += ["- not verified: " + "; ".join(lm.get("not_verified") or []),
          f"- reproduce: `{' '.join(argv) or 'aces golden run'}` (seed {lm.get('seed')})",
          f"- {lm.get('determinism')}", ""]
    return "\n".join(L).rstrip() + "\n"


def write(run, data: dict) -> dict:
    """Write report.md, report.html and report.json; returns {"md", "html", "json"} paths."""
    from simlab.report import to_html

    md = render_markdown(data)
    md_path = run.write_text("report.md", md)
    html_path = Path(run.path("report.html"))
    title = f"aces golden {((data.get('sections') or {}).get('title') or {}).get('name')} {data.get('run_id')}"
    to_html(md, str(html_path), title)
    with open(html_path, encoding="utf-8", newline="") as fh:  # newline="": keep the line endings as written
        html = fh.read()
    if "\r\n" in html:  # the vendored writer uses text mode: CRLF on Windows; every golden file is LF
        run.write_text("report.html", html)
    doc = {"schema": SCHEMA["report"], "run_id": data.get("run_id"), "verdict_line": data.get("verdict_line"),
           "verdict": data.get("verdict"), "outcome": data.get("outcome"), "grade": data.get("grade"),
           "sections": data.get("sections"), "provenance": data.get("provenance") or []}
    json_path = run.write_json("report.json", doc)
    return {"md": Path(md_path), "html": html_path, "json": Path(json_path)}
