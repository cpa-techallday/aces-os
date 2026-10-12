"""The engineering loop: a conversational thread that turns a plain-language request into a visible plan,
editable golden spec drafts, and — only after explicit confirmation — a real `aces golden run` job.

The playbook is deterministic: constraint parsing is unit-pattern based (kg, N, g, °C, m/s, mm), physics are
inferred from named keywords, and the spec draft is an `aces-golden-spec/1` skeleton whose unfilled fields are
named in `missing`. The agent never invents geometry: a draft without a design space says so and names the
accepted sources (a STEP upload, a built-in demo, an existing project). Runs go through the same one-at-a-time
golden job machinery as the Evidence plate, and every mutation lands in the operator ledger.

Routes:
  POST /api/loop/message        {thread?, text}                       -> thread with the reply + plan card appended
  GET  /api/loop/thread/{id}                                          -> the thread
  GET  /api/loop/threads                                              -> recent threads (newest first, 20)
  POST /api/loop/plan/approve   {thread, keep: [item keys]}           -> spec drafts for the kept items
  POST /api/loop/spec/confirm   {thread, item, spec, confirm}         -> aces golden run job (confirm = project name)

Loop state lives in <state>/loop/<thread>.json (atomic writes). Nothing here calls the rig.
"""
from __future__ import annotations

import json
import os
import re
import secrets as pysecrets
import threading
import time
from pathlib import Path
from typing import Any

from . import jobs, server
from .server import Request, Response, error, ok

THREAD_RE = re.compile(r"^t-[0-9a-z]{8}$")
ITEM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
KEEP_THREADS = 20
TEXT_CAP = 8000
LOCK = threading.Lock()

# -------------------------------------------------------------------------------------------------- the playbook

G = 9.80665

MATERIALS = {
    # keyword: (material record — properties are typical room-temperature values, named as such in the draft)
    "6061": {"name": "aluminium-6061-T6", "E_MPa": 68900.0, "nu": 0.33, "rho_kg_mm3": 2.70e-6,
             "yield_MPa": 276.0, "source": "typical handbook values (loop draft)"},
    "aluminium": "6061", "aluminum": "6061", "alloy": "6061",
    "ti64": {"name": "titanium-Ti-6Al-4V", "E_MPa": 113800.0, "nu": 0.342, "rho_kg_mm3": 4.43e-6,
             "yield_MPa": 880.0, "source": "typical handbook values (loop draft)"},
    "titanium": "ti64",
    "steel": {"name": "steel-S355", "E_MPa": 210000.0, "nu": 0.30, "rho_kg_mm3": 7.85e-6,
              "yield_MPa": 355.0, "source": "typical handbook values (loop draft)"},
    "abs": {"name": "abs-printed", "E_MPa": 2000.0, "nu": 0.35, "rho_kg_mm3": 1.04e-6,
            "yield_MPa": 40.0, "source": "typical handbook values (loop draft)"},
    "nylon": {"name": "pa12-printed", "E_MPa": 1700.0, "nu": 0.39, "rho_kg_mm3": 1.01e-6,
              "yield_MPa": 45.0, "source": "typical handbook values (loop draft)"},
    "petg": {"name": "petg-printed", "E_MPa": 2100.0, "nu": 0.36, "rho_kg_mm3": 1.27e-6,
             "yield_MPa": 50.0, "source": "typical handbook values (loop draft)"},
}

KINDS = ("bracket", "mount", "frame", "chassis", "enclosure", "housing", "antenna", "duct", "impeller",
         "arm", "linkage", "panel", "tower", "mast")
VEHICLE_WORDS = ("vehicle", "off-roader", "offroader", "rover", "car", "truck", "buggy", "ugv")
UNWIRED_PHYSICS = {"crash": ("crash",), "multibody": ("multibody", "suspension travel", "articulation"),
                   "aeroelasticity": ("aeroelastic", "flutter")}

NUM = r"(\d+(?:\.\d+)?)"


def parse_request(text: str) -> dict[str, Any]:
    """Unit-pattern parsing of one message into constraints. Everything found is named; nothing is guessed."""
    t = text.lower()
    out: dict[str, Any] = {"text": text, "kind": None, "material": None, "constraints": {}, "physics": set(),
                           "objective_light": bool(re.search(r"light(est)?|minimum mass|minimi[sz]e mass|weight", t)),
                           "vehicle": any(w in t for w in VEHICLE_WORDS)}
    for k in KINDS:
        if re.search(rf"\b{k}\b", t):
            out["kind"] = k
            break
    for kw, mat in MATERIALS.items():
        if re.search(rf"\b{re.escape(kw)}\b", t):
            out["material"] = MATERIALS[mat] if isinstance(mat, str) else mat
            break
    c = out["constraints"]
    m = re.search(rf"\b{NUM}\s*(?:kg|kilogram)", t)
    if m and ("hold" in t or "carry" in t or "load" in t or "kg" in t):
        c["payload_kg"] = float(m.group(1))
    m = re.search(rf"\b{NUM}\s*kn\b", t)
    if m:
        c["load_N"] = float(m.group(1)) * 1000.0
    m = re.search(rf"\b{NUM}\s*g\b", t)
    if m:
        c["shock_g"] = float(m.group(1))
    m = re.search(rf"(?:under|below|max|<=?)\s*{NUM}\s*(?:°c|degc|c\b)", t)
    if m:
        c["max_temp_C"] = float(m.group(1))
    m = re.search(rf"\b{NUM}\s*m/s\b", t)
    if m:
        c["velocity_ms"] = float(m.group(1))
    m = re.search(rf"\b{NUM}\s*kwh\b", t)
    if m:
        c["battery_kWh"] = float(m.group(1))
    m = re.search(rf"\b{NUM}\s*[x×]\s*{NUM}\s*[x×]\s*{NUM}\s*mm\b", t)
    if m:
        c["envelope_mm"] = [float(m.group(i)) for i in (1, 2, 3)]
    m = re.search(rf"\b{NUM}\s*(?:kg|kilogram).*(?:vehicle|rover|off-?roader|curb|gross)", t) or \
        re.search(rf"(?:vehicle|rover|off-?roader|curb|gross).*\b{NUM}\s*(?:kg|kilogram)", t)
    if m:
        c["vehicle_mass_kg"] = float(m.group(1))
    # physics inference
    if any(k in c for k in ("payload_kg", "load_N", "shock_g")) or out["kind"] in KINDS:
        out["physics"].add("structures")
    if "max_temp_C" in c or "cooling" in t or "thermal" in t or "heat" in t:
        out["physics"].add("thermal")
    if "velocity_ms" in c or "drag" in t or "airflow" in t or "aero" in t:
        out["physics"].add("cfd")
    if any(w in t for w in ("antenna", "radar", "rf", "s11", "gain db")):
        out["physics"].add("em")
    for phys, words in UNWIRED_PHYSICS.items():
        if any(w in t for w in words):
            out["physics"].add(phys)
    return out


WIRED = {"structures": "golden (SIMP sweep → surrogate optimise → verified FEA; ccx conforming check validated 2026-10-10)",
         "cfd": "PyFR 3.1 on the rig — golden stage cfd (flow: block → templated case → rig dispatch) wired 2026-10-12; loop drafts don't carry flow blocks yet (Tier 1)",
         "thermal": "CalculiX steady-state thermal — adapter not written yet (Tier 1)",
         "em": "Palace / gprMax EM (aces-tile-em) — registered on the rig, wr90_through rig smoke PASS 2026-10-12; loop dispatch not wired yet (Tier 1)"}
# what the loop can actually run today: structures only. Everything else is named and tracked, never promised.
LOOP_WIRED = {"structures"}


def build_plan(req: dict[str, Any]) -> dict[str, Any]:
    """The plan card: line items with physics, wiring state and the question each answers. Honest about gaps."""
    items: list[dict[str, Any]] = []
    c = req["constraints"]
    physics = sorted(req["physics"])
    unwired = [p for p in physics if p in UNWIRED_PHYSICS]
    kind = req["kind"] or "part"

    if req["vehicle"]:
        mass = c.get("vehicle_mass_kg")
        items.append({"key": "chassis", "title": "chassis frame — tessellated/lattice structure",
                      "physics": ["structures"],
                      "answers": f"frame mass and stiffness{f' for a {mass:g} kg vehicle' if mass else ''}",
                      "wired": True})
        if "battery_kWh" in c:
            items.append({"key": "battery-box", "title": f"battery enclosure — {c['battery_kWh']:g} kWh pack",
                          "physics": ["structures"], "answers": "enclosure mass and strength",
                          "wired": True})
            items.append({"key": "battery-thermal", "title": f"battery pack thermal — {c['battery_kWh']:g} kWh",
                          "physics": ["thermal"], "answers": "thermal margin",
                          "wired": False, "reason": WIRED["thermal"]})
        items.append({"key": "vehicle-integration", "title": "whole-vehicle dynamics",
                      "physics": ["multibody"], "answers": "ride, articulation, rollover",
                      "wired": False, "reason": "multibody dynamics is not wired yet (Tier 3) — tracked"})
    else:
        bits = []
        if "payload_kg" in c:
            bits.append(f"holds {c['payload_kg']:g} kg")
        if "load_N" in c:
            bits.append(f"takes {c['load_N']:g} N")
        if "shock_g" in c:
            bits.append(f"survives {c['shock_g']:g} g shock")
        summary = "; ".join(bits) or "as described"
        items.append({"key": kind, "title": f"{kind} — {summary}",
                      "physics": ["structures"],
                      "answers": "mass, stiffness, stress margin" + (", lightest candidate" if req["objective_light"] else ""),
                      "wired": True})
        # non-structural physics the request named: tracked items, honestly marked
        for p in physics:
            if p in LOOP_WIRED or p in UNWIRED_PHYSICS:
                continue
            answers = {"thermal": "temperature rise and thermal margin", "cfd": "drag / pressure drop / max velocity",
                       "em": "gain, S11, RF performance"}.get(p, p)
            items.append({"key": f"{kind}-{p}", "title": f"{kind} — {p}", "physics": [p], "answers": answers,
                          "wired": False, "reason": WIRED.get(p, f"{p} is not wired yet")})
    for p in unwired:
        items.append({"key": p, "title": f"{p} analysis", "physics": [p], "answers": p,
                      "wired": False, "reason": f"{p} is not wired yet (Tier 3) — tracked"})
    missing = []
    if not any(i["wired"] for i in items):
        missing.append("nothing in this request maps to a wired solver")
    return {"items": items, "wired_count": sum(1 for i in items if i["wired"]),
            "missing_geometry": True,  # a draft must name a design space: STEP upload, demo, or project
            "est": "one golden run per wired item; wall time minutes (desktop) to tens of minutes (rig)"}


def spec_draft(req: dict[str, Any], item: dict[str, Any], slug: str) -> dict[str, Any]:
    """An aces-golden-spec/1 skeleton with what the parser found; `missing` names what is still needed."""
    c = req["constraints"]
    mat = req["material"] or MATERIALS["6061"]
    load_N = c.get("load_N") or (c.get("payload_kg", 0.0) * G * (c.get("shock_g", 1.0)) if c.get("payload_kg") else None)
    spec = {
        "schema": "aces-golden-spec/1",
        "name": slug[:48],
        "description": f"Loop draft for “{item['title']}”. Generated from the thread; edit before confirming.",
        "units": "mm-N-MPa-kg",
        "hypothesis": None,
        "material": mat,
        "objective": {"output": "mass_kg", "sense": "min"},
        "generator": {"kind": "simp-organic",
                      "variables": {"volfrac": {"lo": 0.08, "hi": 0.35}, "rmin_mm": {"lo": 1.5, "hi": 4.0}},
                      "fixed": {"penal": 3.0}, "iterations": 25},
        "resolution": {"gen_mm": 2.0, "fea_mm": 1.0},
        "sweep": {"n": 10, "seed": 0},
    }
    missing = []
    if load_N is not None:
        spec["loads"] = [{"name": "service+shock", "box": None, "force": [0.0, 0.0, -load_N],
                          "_note": "placement box unresolved until the design space is known"}]
    else:
        spec["loads"] = []
        missing.append("loads: no load was parsed from the message — say e.g. “holds 15 kg” or “2 kN”")
    spec["supports"] = []
    missing.append("supports: where is it held/bolted? (needs the design space)")
    missing.append("domain: the design space — attach a STEP, pick a demo (root-mount, bone), or name an envelope in mm")
    spec["_missing"] = missing
    spec["_constraints_not_in_structural_spec"] = {
        k: v for k, v in c.items() if k in ("max_temp_C", "velocity_ms", "battery_kWh")}
    return spec


# -------------------------------------------------------------------------------------------------- thread store

def _dir(app) -> Path:
    d = app.settings.state_dir / "loop"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(app, tid: str) -> Path:
    return _dir(app) / f"{tid}.json"


def load_thread(app, tid: str) -> dict | None:
    p = _path(app, tid)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_thread(app, th: dict) -> None:
    p = _path(app, th["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(th, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def new_thread(app) -> dict:
    tid = "t-" + pysecrets.token_hex(4)
    th = {"id": tid, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "messages": [], "plans": [], "drafts": {}, "runs": []}
    save_thread(app, th)
    return th


def _msg(role: str, kind: str, text: str, card: Any = None) -> dict:
    m = {"role": role, "kind": kind, "text": text, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if card is not None:
        m["card"] = card
    return m


def reply_text(req: dict[str, Any], plan: dict[str, Any]) -> str:
    wired = plan["wired_count"]
    items = plan["items"]
    lines = []
    if wired:
        lines.append(f"Plan: {wired} wired item{'s' if wired != 1 else ''} — " +
                     "; ".join(i["title"] for i in items if i["wired"]) + ".")
    for i in items:
        if not i["wired"]:
            lines.append(f"Not wired yet: {i['title']} ({i['reason']}).")
    if plan["missing_geometry"] and wired:
        lines.append("Before anything runs I need the design space: attach a STEP file, pick a demo "
                     "(root-mount, bone), or name an envelope like 200x150x40 mm.")
    lines.append("Approve the plan to get editable spec drafts; nothing runs until you confirm a spec.")
    return "\n".join(lines)


# -------------------------------------------------------------------------------------------------- handlers

def h_message(req: Request) -> Response:
    app = req.app
    body = req.json or {}
    text = body.get("text")
    tid = body.get("thread")
    try:
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_CAP:
            raise ValueError(f"text must be a non-empty string of at most {TEXT_CAP} characters")
        if tid is not None and (not isinstance(tid, str) or not THREAD_RE.match(tid)):
            raise ValueError("thread must be a thread id like t-1a2b3c4d")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    with LOCK:
        th = load_thread(app, tid) if tid else None
        if tid and th is None:
            return error(404, f"no thread {tid}", "not-found")
        if th is None:
            th = new_thread(app)
        parsed = parse_request(text.strip())
        plan = build_plan(parsed)
        th["messages"].append(_msg("user", "text", text.strip()))
        th["messages"].append(_msg("loop", "plan", reply_text(parsed, plan), card={"plan": plan, "request": {k: (sorted(v) if isinstance(v, set) else v) for k, v in parsed.items() if k != "text"}}))
        th["plans"].append(plan)
        save_thread(app, th)
    n = jobs._record(app, "console.loop.message", {"thread": th["id"], "chars": len(text), "items": len(plan["items"])})
    return ok({"thread": th, "ledger_n": n}, status=201)


def h_thread(req: Request) -> Response:
    tid = req.params.get("id", "")
    if not THREAD_RE.match(tid):
        return error(400, "id must be a thread id like t-1a2b3c4d", "bad-request")
    th = load_thread(req.app, tid)
    if th is None:
        return error(404, f"no thread {tid}", "not-found")
    return ok({"thread": th})


def h_threads(req: Request) -> Response:
    d = _dir(req.app)
    rows = []
    for p in sorted(d.glob("t-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:KEEP_THREADS]:
        try:
            th = json.loads(p.read_text(encoding="utf-8"))
            first = next((m for m in th.get("messages", []) if m.get("role") == "user"), None)
            rows.append({"id": th["id"], "created_utc": th.get("created_utc"),
                         "title": (first["text"][:80] if first else "(empty)"), "messages": len(th.get("messages", []))})
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return ok({"threads": rows})


def h_plan_approve(req: Request) -> Response:
    app = req.app
    body = req.json or {}
    tid, keep = body.get("thread"), body.get("keep")
    try:
        if not isinstance(tid, str) or not THREAD_RE.match(tid):
            raise ValueError("thread must be a thread id")
        if not isinstance(keep, list) or not keep or not all(isinstance(k, str) and ITEM_RE.match(k) for k in keep):
            raise ValueError("keep must be a non-empty list of plan item keys")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    with LOCK:
        th = load_thread(app, tid)
        if th is None:
            return error(404, f"no thread {tid}", "not-found")
        plan = th["plans"][-1] if th["plans"] else None
        if plan is None:
            return error(409, "this thread has no plan to approve", "no-plan")
        parsed = None
        for m in reversed(th["messages"]):
            if m.get("kind") == "plan" and m.get("card"):
                parsed = m["card"]["request"]
                break
        drafts = {}
        known = {i["key"]: i for i in plan["items"]}
        for key in keep:
            item = known.get(key)
            if item is None:
                return error(400, f"plan has no item {key!r}", "bad-request")
            if not item["wired"]:
                return error(409, f"item {key!r} is not wired ({item.get('reason', 'unwired')}); it cannot produce a spec draft", "unwired")
            drafts[key] = spec_draft(parsed or {"constraints": {}, "physics": {"structures"}, "kind": key,
                                                "material": None, "objective_light": False},
                                     item, slug=f"{tid[2:]}-{key}")
        th["drafts"].update(drafts)
        th["messages"].append(_msg("loop", "drafts", f"Spec drafts ready for: {', '.join(drafts)}. "
                                   "Each lists what is still missing (usually the design space); edit and confirm to run.",
                                   card={"drafts": {k: {"missing": v["_missing"]} for k, v in drafts.items()}}))
        save_thread(app, th)
    n = jobs._record(app, "console.loop.plan", {"thread": tid, "keep": keep})
    return ok({"thread": th, "drafts": drafts, "ledger_n": n})


def h_spec_confirm(req: Request) -> Response:
    app = req.app
    body = req.json or {}
    tid, key, spec, confirm = body.get("thread"), body.get("item"), body.get("spec"), body.get("confirm")
    try:
        if not isinstance(tid, str) or not THREAD_RE.match(tid):
            raise ValueError("thread must be a thread id")
        if not isinstance(key, str) or not ITEM_RE.match(key):
            raise ValueError("item must be a plan item key")
        if not isinstance(spec, dict) or spec.get("schema") != "aces-golden-spec/1":
            raise ValueError("spec must be an aces-golden-spec/1 object")
        if not isinstance(confirm, str) or not jobs.NAME_RE.match(confirm):
            raise ValueError("confirm must be the project name (lower case letters, digits, - and _)")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    with LOCK:
        th = load_thread(app, tid)
        if th is None:
            return error(404, f"no thread {tid}", "not-found")
        clean = {k: v for k, v in spec.items() if not k.startswith("_")}
        sdir = _dir(app) / "specs"
        sdir.mkdir(exist_ok=True)
        spath = sdir / f"{tid}-{key}.json"
        spath.write_text(json.dumps(clean, indent=1), encoding="utf-8")
        busy = jobs._running(jobs._state(app), lambda j: j.kind == "golden.run")
        if busy is not None:
            return error(409, f"a golden run is already running ({busy.project}, job {busy.id}); one at a time", "busy")
        # --new only when the project does not exist yet; a re-run lands as the project's next golden run
        from aces import config as aces_config
        exists = (Path(aces_config.WORK) / confirm / "aces.yaml").is_file()
        args = ["golden", "run", "--spec", str(spath), "--project", confirm] + ([] if exists else ["--new"]) + \
               ["--json", "--progress", "lines"]
        argv = jobs._aces(args)
        if argv is None:
            return error(503, "aces CLI not found (aces-base is not installed)", "not-installed")
        job = jobs.Job("golden.run", confirm, f"golden run {confirm} (loop {tid}/{key})",
                       ["aces", "golden", "run", "--project", confirm])
        th["runs"].append({"item": key, "project": confirm, "job": job.id,
                           "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        th["messages"].append(_msg("loop", "run", f"Running “{key}” as project {confirm} (job {job.id}). "
                                   "Stage chips appear in the thread; the result card lands when it finishes.",
                                   card={"run": {"project": confirm, "job": job.id}}))
        save_thread(app, th)
        n = jobs._record(app, "console.loop.run", {"thread": tid, "item": key, "project": confirm, "job": job.id})
        jobs.start_job(app, job, argv, jobs.GOLDEN_WALL_S)
    return ok({"job": job.id, "project": confirm, "ledger_n": n}, status=202)


def install(register) -> None:
    register("POST", "/api/loop/message", h_message)
    register("GET", "/api/loop/threads", h_threads)
    register("GET", "/api/loop/thread/{id}", h_thread)
    register("POST", "/api/loop/plan/approve", h_plan_approve)
    register("POST", "/api/loop/spec/confirm", h_spec_confirm)
