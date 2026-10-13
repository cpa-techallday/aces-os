"""The engineering loop: a conversational thread that turns a plain-language request into a visible plan,
editable golden spec drafts, and — only after explicit confirmation — a real `aces golden run` job.

The playbook is deterministic: constraint parsing is unit-pattern based (kg, N, g, °C, m/s, km/h, mph, mm), physics are
inferred from named keywords, and the spec draft is an `aces-golden-spec/1` skeleton whose unfilled fields are
named in `missing`. The agent never invents geometry: a draft without a design space says so and names the
accepted sources (a STEP upload, a built-in demo, an existing project). Runs go through the same one-at-a-time
golden job machinery as the Evidence plate, and every mutation lands in the operator ledger.

Routes:
  POST /api/loop/message        {thread?, text}                       -> thread with the reply + plan card appended
  POST /api/loop/upload         {thread?, name, data_b64}             -> thread with the upload recorded (base64 JSON;
                                                                         this path alone gets a larger body cap)
  GET  /api/loop/file/{tid}/{name}                                    -> the stored upload's bytes (photos render inline)
  GET  /api/loop/thread/{id}                                          -> the thread
  GET  /api/loop/threads                                              -> recent threads (newest first, 20)
  POST /api/loop/plan/approve   {thread, keep: [item keys]}           -> spec drafts for the kept items
  POST /api/loop/spec/confirm   {thread, item, spec, confirm}         -> aces golden run job (confirm = project name)

Loop state lives in <state>/loop/<thread>.json (atomic writes); uploads under <state>/loop/uploads/<thread>/.
A STEP upload is real geometry: plan drafts point domain.source=step at it. Nothing here calls the rig.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
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

# uploads: the composer's attach button. JSON + base64 (the POST guard admits application/json only), so the
# body cap for this one route is ~1.37x the file cap; every other route keeps the 64 KiB default.
UPLOAD_FILE_CAP = 48 * 1024 * 1024
UPLOAD_BODY_CAP = 68 * 1024 * 1024
UPLOAD_KINDS = {".step": "model", ".stp": "model", ".stl": "model",
                ".png": "photo", ".jpg": "photo", ".jpeg": "photo", ".webp": "photo", ".gif": "photo",
                ".pdf": "doc", ".txt": "doc", ".md": "doc", ".csv": "doc", ".json": "doc"}
UPLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,119}$")
STORED_RE = re.compile(r"^[0-9a-f]{8}-[A-Za-z0-9._-]{1,120}$")
FILE_CTYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif", ".pdf": "application/pdf", ".stl": "model/stl",
               ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
               ".csv": "text/csv; charset=utf-8", ".json": "application/json"}

# -------------------------------------------------------------------------------------------------- the playbook

G = 9.80665

MATERIALS = {
    # keyword: (material record — properties are typical room-temperature values, named as such in the draft)
    "6061": {"name": "aluminium-6061-T6", "E_MPa": 68900.0, "nu": 0.33, "rho_kg_mm3": 2.70e-6,
             "allowable_vm_MPa": 276.0, "source": "typical handbook values (loop draft)"},
    "aluminium": "6061", "aluminum": "6061", "alloy": "6061",
    "ti64": {"name": "titanium-Ti-6Al-4V", "E_MPa": 113800.0, "nu": 0.342, "rho_kg_mm3": 4.43e-6,
             "allowable_vm_MPa": 880.0, "source": "typical handbook values (loop draft)"},
    "titanium": "ti64",
    "steel": {"name": "steel-S355", "E_MPa": 210000.0, "nu": 0.30, "rho_kg_mm3": 7.85e-6,
              "allowable_vm_MPa": 355.0, "source": "typical handbook values (loop draft)"},
    "abs": {"name": "abs-printed", "E_MPa": 2000.0, "nu": 0.35, "rho_kg_mm3": 1.04e-6,
            "allowable_vm_MPa": 40.0, "source": "typical handbook values (loop draft)"},
    "nylon": {"name": "pa12-printed", "E_MPa": 1700.0, "nu": 0.39, "rho_kg_mm3": 1.01e-6,
              "allowable_vm_MPa": 45.0, "source": "typical handbook values (loop draft)"},
    "petg": {"name": "petg-printed", "E_MPa": 2100.0, "nu": 0.36, "rho_kg_mm3": 1.27e-6,
             "allowable_vm_MPa": 50.0, "source": "typical handbook values (loop draft)"},
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
    # kg mentions are classified by direction: a payload word before the number ("holds 15 kg", "carrying 200 kg")
    # means payload; a vehicle word after it ("1500 kg off-roader") means the vehicle's own mass, which is not a
    # payload. A kg number with no context either way falls back to payload ("15 kg bracket").
    VEHICLE_CTX = r"\b(?:vehicle|rover|off-?roader|curb|gross|buggy|truck|car|chassis)"
    PAYLOAD_CTX = r"\b(?:hold|carry|carrying|payload|load|lift|bear|tow)"
    for m0 in re.finditer(rf"\b{NUM}\s*(?:kg|kilogram)s?\b", t):
        before, after = t[max(0, m0.start() - 25):m0.start()], t[m0.end():m0.end() + 25]
        val = float(m0.group(1))
        if re.search(PAYLOAD_CTX, before) or re.search(r"\bpayload\b", after):
            c.setdefault("payload_kg", val)
        elif re.search(VEHICLE_CTX, after) or re.search(VEHICLE_CTX, before):
            c.setdefault("vehicle_mass_kg", val)
        elif "payload_kg" not in c:
            c["payload_kg"] = val
    m = re.search(rf"\b{NUM}\s*kn\b", t)
    if m:
        c["load_N"] = float(m.group(1)) * 1000.0
    m = re.search(rf"\b{NUM}\s*g\b", t)
    if m:
        c["shock_g"] = float(m.group(1))
    m = re.search(rf"(?:under|below|max|<=?)\s*{NUM}\s*(?:°c|degc|c\b)", t)
    if m:
        c["max_temp_C"] = float(m.group(1))
    # velocity: m/s canonical, km/h and mph converted; the raw number and unit are recorded alongside
    m, unit = re.search(rf"\b{NUM}\s*m/s\b", t), "m/s"
    if m is None:
        m, unit = re.search(rf"\b{NUM}\s*km/?h\b", t), "km/h"
    if m is None:
        m, unit = re.search(rf"\b{NUM}\s*mph\b", t), "mph"
    if m is not None:
        raw = float(m.group(1))
        c["velocity_ms"] = raw if unit == "m/s" else (raw / 3.6 if unit == "km/h" else raw * 0.44704)
        c["velocity_unit"] = unit
        c["velocity_raw"] = raw
    if re.search(r"\bwater\b", t):
        c["flow_medium"] = "water"   # golden's flow block defaults to air; only water is named explicitly
    m = re.search(rf"\b{NUM}\s*kwh\b", t)
    if m:
        c["battery_kWh"] = float(m.group(1))
    m = re.search(rf"\b{NUM}\s*[x×]\s*{NUM}\s*[x×]\s*{NUM}\s*mm\b", t)
    if m:
        c["envelope_mm"] = [float(m.group(i)) for i in (1, 2, 3)]
    # physics inference
    if any(k in c for k in ("payload_kg", "load_N", "shock_g", "vehicle_mass_kg")) or out["kind"] in KINDS:
        out["physics"].add("structures")
    if "max_temp_C" in c or "cooling" in t or "thermal" in t or "heat" in t:
        out["physics"].add("thermal")
    if "velocity_ms" in c or "drag" in t or "airflow" in t or "aero" in t or "flow_medium" in c:
        out["physics"].add("cfd")
    # whole words only: "rf" as a substring matches "airflow"
    if re.search(r"\b(?:antenna|radar|rf|s11)\b", t) or "gain db" in t:
        out["physics"].add("em")
    for phys, words in UNWIRED_PHYSICS.items():
        if any(w in t for w in words):
            out["physics"].add(phys)
    return out


WIRED = {"structures": "golden (SIMP sweep → surrogate optimise → verified FEA; ccx conforming check validated 2026-10-10)",
         "cfd": "PyFR 3.1 on the rig — golden stage cfd (flow: block → templated case → rig dispatch) wired 2026-10-12; loop drafts carry the flow block when a speed is named (Tier 1)",
         "thermal": "CalculiX steady-state thermal — adapter not written yet (Tier 1)",
         "em": "Palace / gprMax EM (aces-tile-em) — registered on the rig, wr90_through rig smoke PASS 2026-10-12; loop dispatch not wired yet (Tier 1)"}
# what the loop can actually run today: structures, and cfd when the flow block is fully known (a speed under the
# incompressible gate). Everything else is named and tracked, never promised.
LOOP_WIRED = {"structures"}


def _flow_refusal(velocity_mps: float, medium: str) -> str | None:
    """The refusal golden's Flow._incompressible (aces-engineering golden/spec.py) gives for this flow block, or None
    when golden would accept it. aces-engineering is not a console dependency, so the validator's message is mirrored
    here byte-identically — if golden's message changes, change this one too."""
    if medium == "air" and velocity_mps > 100.0:
        return (f"flow.velocity_mps: {velocity_mps:g} m/s in air is Mach "
                f"~{velocity_mps / 343.0:.2f}; ac-navier-stokes is incompressible, so a drag "
                "number past ~100 m/s (Mach 0.3) would be fiction — this tier refuses it")
    return None


def _cfd_item(req: dict[str, Any], kind: str) -> dict[str, Any]:
    """The cfd plan item: wired only when the flow block golden needs is fully known (a speed, and one the
    incompressible tier accepts); otherwise tracked with a reason naming exactly what is missing."""
    c = req["constraints"]
    v = c.get("velocity_ms")
    medium = c.get("flow_medium", "air")
    item: dict[str, Any] = {"key": f"{kind}-cfd", "title": f"{kind} — cfd", "physics": ["cfd"],
                            "answers": "drag / pressure drop / max velocity"}
    if v is None:
        item.update(wired=False, reason="name a speed to run CFD (e.g. “30 m/s”, “120 km/h”, “65 mph”) — "
                                        "the golden cfd stage is wired but its flow block needs a velocity — tracked")
        return item
    refusal = _flow_refusal(v, medium)
    if refusal is not None:
        item.update(wired=False, reason=f"incompressible tier refuses air over 100 m/s — {refusal} — tracked")
        return item
    shown = c.get("velocity_raw", v)
    unit = c.get("velocity_unit", "m/s")
    item.update(wired=True, reason=WIRED["cfd"],
                flow={"velocity_mps": v, "medium": medium,
                      "_as_said": f"{shown:g} {unit}" + ("" if unit == "m/s" else f" = {v:g} m/s")})
    return item


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
        # a vehicle that names a speed gets its cfd item like any other request (drag matters at speed)
        if "cfd" in physics:
            items.append(_cfd_item(req, "vehicle"))
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
        # non-structural physics the request named: wired when the loop can truly run it, tracked items otherwise
        for p in physics:
            if p in LOOP_WIRED or p in UNWIRED_PHYSICS:
                continue
            if p == "cfd":
                items.append(_cfd_item(req, kind))
                continue
            answers = {"thermal": "temperature rise and thermal margin",
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


def spec_draft(req: dict[str, Any], item: dict[str, Any], slug: str, uploads: list[dict] | None = None) -> dict[str, Any]:
    """An aces-golden-spec/1 skeleton with what the parser found; `missing` names what is still needed.
    `uploads` are the thread's upload records (each with an `abs` path): the latest STEP becomes the
    design space (domain.source = "step") — real geometry, named with its file, not a demo stand-in."""
    c = req["constraints"]
    mat = req["material"] or MATERIALS["6061"]
    load_N = c.get("load_N") or (c.get("payload_kg", 0.0) * G * (c.get("shock_g", 1.0)) if c.get("payload_kg") else None)
    load_basis = None
    if load_N is None and item.get("key") == "chassis" and c.get("vehicle_mass_kg"):
        # a chassis carries the vehicle: the vehicle's own mass is the service load, named as such
        load_N = c["vehicle_mass_kg"] * G * c.get("shock_g", 1.0)
        load_basis = f"vehicle mass {c['vehicle_mass_kg']:g} kg x {c.get('shock_g', 1.0):g} g (loop draft)"
    spec = {
        "schema": "aces-golden-spec/1",
        "name": slug[:48],
        "description": f"Loop draft for “{item['title']}”. Generated from the thread; edit before confirming.",
        "units": "mm-N-MPa-kg",
        "hypothesis": None,
        "material": mat,
        "objective": {"output": "mass_kg", "sense": "min"},
        "generator": {"kind": "simp-organic",
                      # rmin lo honours golden's under-resolution gate: rmin_mm >= 1.5 x gen_mm (2.0) = 3.0 mm
                      "variables": {"volfrac": {"lo": 0.08, "hi": 0.35}, "rmin_mm": {"lo": 3.0, "hi": 6.0}},
                      "fixed": {"penal": 3.0}, "iterations": 25},
        "resolution": {"gen_mm": 2.0, "fea_mm": 1.0},
        "sweep": {"n": 10, "seed": 0},
    }
    missing = []
    if load_N is not None:
        load = {"name": "service+shock", "box": None, "force": [0.0, 0.0, -load_N],
                "_note": "placement box unresolved until the design space is known"}
        if load_basis:
            load["_basis"] = load_basis
        spec["loads"] = [load]
    else:
        spec["loads"] = []
        missing.append("loads: no load was parsed from the message — say e.g. “holds 15 kg” or “2 kN”")
    spec["supports"] = []
    missing.append("supports: where is it held/bolted? (needs the design space)")
    step_up = next((u for u in reversed(uploads or []) if u.get("name", "").lower().endswith((".step", ".stp"))
                    and u.get("abs")), None)
    if step_up is not None:
        spec["domain"] = {"source": "step", "step_file": step_up["abs"]}
        spec["_design_space"] = (f"your upload {step_up['name']} (sha256 {str(step_up.get('sha256'))[:12]}…) is the "
                                 "design space — origin/envelope derive from its bounding box")
    else:
        missing.append("domain: the design space — attach a STEP (the paperclip below), pick a demo "
                       "(root-mount, bone), or name an envelope in mm")
    if item.get("wired") and item.get("physics") == ["cfd"]:
        # a wired cfd item carries its flow block into the draft so confirm-to-run dispatches golden's cfd stage;
        # refuse at draft time — with golden's own validator message — rather than hand out a doomed draft
        v = c.get("velocity_ms")
        medium = c.get("flow_medium", "air")
        if v is None:
            raise ValueError("a wired cfd draft needs a parsed velocity: name a speed to run CFD")
        refusal = _flow_refusal(v, medium)
        if refusal is not None:
            raise ValueError(refusal)
        spec["flow"] = {"velocity_mps": v, "medium": medium}
    spec["_missing"] = missing
    spec["_constraints_not_in_structural_spec"] = {
        k: v for k, v in c.items()
        if k in ("max_temp_C", "velocity_ms", "battery_kWh") and not (k == "velocity_ms" and "flow" in spec)}
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
          "messages": [], "plans": [], "drafts": {}, "runs": [], "uploads": []}
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
        lines.append("Before anything runs I need the design space: attach a STEP with the paperclip below, "
                     "pick a demo (root-mount, bone), or name an envelope like 200x150x40 mm.")
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


# -------------------------------------------------------------------------------------------------- uploads

def _fmt_size(n: int) -> str:
    return (f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.1f} KB") if n >= 1024 else f"{n} B"


def _upload_ack(rec: dict) -> str:
    """What the upload means for the work, said plainly. Photos are context for the conversation — the loop
    does not see into images, so it asks for the words that matter."""
    name, kind = rec["name"], rec["kind"]
    if rec["ext"] in (".step", ".stp"):
        return (f"Got {name} ({_fmt_size(rec['size'])}). This is real geometry: approve a plan and the spec "
                "drafts will use it as the design space (domain.source = step). Tell me the part's job — "
                "loads, supports, what 'good' means.")
    if rec["ext"] == ".stl":
        return (f"Got {name} ({_fmt_size(rec['size'])}). An STL is a mesh, not a B-rep: the generator cannot "
                "grow a design inside it, but it can be the body in a CFD run. If you have the STEP, upload "
                "that too — it unlocks the design space.")
    if kind == "photo":
        return (f"Got {name} ({_fmt_size(rec['size'])}) — kept in the thread as context, visible below. "
                "I can't see into images yet, so tell me in words what matters in it: dimensions, features, "
                "what to copy or avoid.")
    return (f"Got {name} ({_fmt_size(rec['size'])}) — kept in the thread. Reference it in your next message "
            "and I'll carry its contents into the plan where it fits.")


def h_upload(req: Request) -> Response:
    app = req.app
    body = req.json or {}
    name, data_b64, tid = body.get("name"), body.get("data_b64"), body.get("thread")
    try:
        if not isinstance(name, str) or not UPLOAD_NAME_RE.match(name) or Path(name).name != name:
            raise ValueError("name must be a plain file name (letters, digits, . _ - and spaces, no paths)")
        ext = Path(name).suffix.lower()
        if ext not in UPLOAD_KINDS:
            raise ValueError(f"*{ext or '.*'} is not accepted — models (.step .stp .stl), photos "
                             "(.png .jpg .webp .gif), documents (.pdf .txt .md .csv .json)")
        if not isinstance(data_b64, str) or not data_b64:
            raise ValueError("data_b64 must be the file's bytes, base64")
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("data_b64 is not valid base64")
        if not raw:
            raise ValueError("the file is empty")
        if len(raw) > UPLOAD_FILE_CAP:
            raise ValueError(f"the file is {_fmt_size(len(raw))}; the cap is {_fmt_size(UPLOAD_FILE_CAP)}")
        if tid is not None and (not isinstance(tid, str) or not THREAD_RE.match(tid)):
            raise ValueError("thread must be a thread id like t-1a2b3c4d")
    except ValueError as e:
        return error(400, str(e), "bad-request")
    sha = hashlib.sha256(raw).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    stored = f"{sha[:8]}-{safe}"
    with LOCK:
        th = load_thread(app, tid) if tid else None
        if tid and th is None:
            return error(404, f"no thread {tid}", "not-found")
        if th is None:
            th = new_thread(app)
        udir = _dir(app) / "uploads" / th["id"]
        udir.mkdir(parents=True, exist_ok=True)
        (udir / stored).write_bytes(raw)
        rec = {"id": f"{th['id']}/{stored}", "name": name, "ext": ext, "kind": UPLOAD_KINDS[ext],
               "size": len(raw), "sha256": sha,
               "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        th.setdefault("uploads", []).append(rec)
        th["messages"].append(_msg("user", "upload", f"uploaded {name} ({_fmt_size(len(raw))})",
                                   card={"upload": rec}))
        th["messages"].append(_msg("loop", "note", _upload_ack(rec)))
        save_thread(app, th)
    n = jobs._record(app, "console.loop.upload", {"thread": th["id"], "name": name, "size": len(raw),
                                                  "sha256": sha[:12]})
    return ok({"thread": th, "upload": rec, "ledger_n": n}, status=201)


def h_file(req: Request) -> Response:
    """Serve a stored upload back (photos render in the thread; models download). Path traversal is refused
    by the two regexes, not by trust."""
    tid, stored = req.params.get("tid", ""), req.params.get("name", "")
    if not THREAD_RE.match(tid) or not STORED_RE.match(stored):
        return error(400, "file must be a thread id and a stored upload name", "bad-request")
    p = _dir(req.app) / "uploads" / tid / stored
    if not p.is_file():
        return error(404, f"no upload {tid}/{stored}", "not-found")
    ext = p.suffix.lower()
    ctype = FILE_CTYPES.get(ext, "application/octet-stream")
    disp = "inline" if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf") else "attachment"
    original = stored[9:]  # strip the sha8- prefix for the download name
    return Response(status=200, body=p.read_bytes(), content_type=ctype,
                    headers={"Content-Disposition": f'{disp}; filename="{original}"',
                             "Cache-Control": "private, max-age=3600"})


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
        ups = [dict(u, abs=str(_dir(app) / "uploads" / u["id"])) for u in th.get("uploads") or [] if u.get("id")]
        for key in keep:
            item = known.get(key)
            if item is None:
                return error(400, f"plan has no item {key!r}", "bad-request")
            if not item["wired"]:
                return error(409, f"item {key!r} is not wired ({item.get('reason', 'unwired')}); it cannot produce a spec draft", "unwired")
            try:
                drafts[key] = spec_draft(parsed or {"constraints": {}, "physics": {"structures"}, "kind": key,
                                                    "material": None, "objective_light": False},
                                         item, slug=f"{tid[2:]}-{key}", uploads=ups)
            except ValueError as e:
                return error(409, f"item {key!r} cannot produce a runnable draft: {e}", "draft-refused")
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
    register("POST", "/api/loop/upload", h_upload)
    server.allow_body("/api/loop/upload", UPLOAD_BODY_CAP)
    register("GET", "/api/loop/file/{tid}/{name}", h_file)
    register("GET", "/api/loop/threads", h_threads)
    register("GET", "/api/loop/thread/{id}", h_thread)
    register("POST", "/api/loop/plan/approve", h_plan_approve)
    register("POST", "/api/loop/spec/confirm", h_spec_confirm)
