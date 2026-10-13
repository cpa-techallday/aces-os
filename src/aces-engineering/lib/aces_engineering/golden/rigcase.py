"""Shared rig-dispatch machinery for the golden Tier-3 solver stages (crash, multibody).

The CFD stage carries its own dispatch in golden/cfd.py (pinned by its tests); this module is the same state
machine generalised for the Tier-3 cases, which upload different files (crash: nothing — the deck is generated
in-container; multibody: the iftomm reference trajectories or the user's model script) and pull the same three
records back. The honest end states are identical:

  delivered       results.json came back and verified (sha256 recomputed by the rig on every read)
  failed          the case ran and failed — the rig's status.json / job error is quoted
  pending         the budget ran out first; the experiment is NOT cancelled (it keeps running on the rig) and
                  the record names exactly what to poll and pull later
  not-delivered   a transport/protocol failure before or during submission; nothing may be queued
  templated       no dispatch at all (no lane, no validated registry row, or the stage flag said template) —
                  the case files sit in the run directory for a manual queue
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

POLL_S = 2.0
LEASE_WAIT_S = 1800          # per-case lease wait on the rig (clamped by the rig to 60..21600)
PULLS = ("status.json", "results.json", "evidence.json")   # pulled back under <case_dir>/rig/c000/ when present


def rig_experiment_id(run_id: str, suffix: str) -> str:
    """A filename-safe rig experiment id (the rig's own id rules: letters, digits, '-', '_')."""
    eid = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{run_id}-{suffix}").strip("-_")
    if not eid:
        raise ValueError(f"no usable rig experiment id from run id {run_id!r}")
    return eid[:64]


def validated_row(session: Any, solver: str) -> dict | None:
    """The rig registry's row for `solver` when its validation status is 'pass', else None. A registry that
    cannot be read is None too — the caller templates instead of dispatching; it never invents a lane."""
    if session is None:
        return None
    try:
        reg = session.client.call("sim_list_solvers") or {}
    except Exception:  # noqa: BLE001 — an unreadable registry means template-only, not a failure
        return None
    row = reg.get(solver)
    if isinstance(row, dict) and (row.get("validation") or {}).get("status") == "pass":
        return row
    return None


def write_templated(case_dir: Path, *, schema: str, experiment: str, why: str) -> dict:
    """The 'templated' dispatch record: case files written, nothing submitted, with the manual queue path named."""
    case_dir = Path(case_dir)
    (case_dir / "rig").mkdir(parents=True, exist_ok=True)
    rec = {"schema": schema, "state": "templated",
           "reason": f"{why}: case files written, nothing submitted. Queue it with the experiment yaml in this "
                     "directory (aces-simlab remote call sim_submit, or simlab enqueue --docker on the rig).",
           "experiment": experiment}
    with open(case_dir / "rig" / "dispatch.json", "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    return rec


def dispatch(client: Any, *, case_dir: Path, exp_id: str, principal: str | None, budget_s: float,
             spec_text: str, schema: str, uploads: list[tuple[str, bytes]] = (),
             lease_wait_s: int = LEASE_WAIT_S, poll_s: float = POLL_S, progress: Any = None) -> dict:
    """Upload `uploads` ((store-relative name, bytes) pairs) and the spec, queue the experiment, wait within
    budget_s, and pull the case's records back under <case_dir>/rig/. The record (also written to
    <case_dir>/rig/dispatch.json) ends in one of the honest states in the module docstring."""
    from .rig import RigError, RigIntegrityError, RigUnreachable
    from . import sha256_bytes, utc_now

    case_dir = Path(case_dir)
    rig_dir = case_dir / "rig"
    rig_dir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {"schema": schema, "experiment": exp_id, "state": "not-delivered",
                           "reason": None, "submitted_utc": None, "spec_path": None, "queue": None, "jobs": [],
                           "pulled": {}, "budget_s": budget_s}

    def finish(state: str, reason: str | None) -> dict:
        rec["state"], rec["reason"] = state, reason
        with open(rig_dir / "dispatch.json", "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        return rec

    def note(text: str) -> None:
        if progress is not None:
            progress.line("  " + text)

    try:
        up = []
        for name, data in uploads:
            r = client.put_file(name, data)
            up.append({"name": name, "sha256": r.get("sha256")})
        rec["uploads"] = up
        (rig_dir / "spec.yaml").write_text(spec_text, encoding="utf-8")
        rec["spec_sha256"] = sha256_bytes(spec_text.encode("utf-8"))
        put = client.put_spec(spec_text, f"{exp_id}.yaml", principal)
        rec["spec_path"] = put.get("spec_path")
        sub = client.submit(rec["spec_path"], exp_id, lease_wait_s)
        rec["submitted_utc"] = utc_now()
        rec["queue"], rec["exp_dir"] = sub.get("queue"), sub.get("exp_dir")
        rec["jobs"] = [str(j) for j in sub.get("jobs") or []]
        note(f"{exp_id}: submitted to the rig ({len(rec['jobs'])} case, lease wait {lease_wait_s} s)")
    except (RigError, RigUnreachable, OSError, ValueError) as exc:
        return finish("not-delivered", str(exc))

    t_end = time.monotonic() + float(budget_s)
    case_state = None
    job_error = ""
    while time.monotonic() < t_end:
        try:
            st = client.call("sim_status", {"experiment_id": exp_id}) or {}
        except RigUnreachable as exc:
            return finish("pending", f"lost contact with the rig mid-run ({exc}); the experiment keeps running: "
                                     f"sim_status {exp_id}")
        counts = st.get("counts") or {}
        jobs = st.get("jobs") or []
        if jobs:
            # the case's verdict is over ALL its stage jobs: any failed job fails the case, every job finished
            # means finished, anything else is still running — jobs[0] is just the first stage
            states = [str(j.get("state") or "") for j in jobs]
            failed_errs = [str(j.get("error")) for j, s in zip(jobs, states) if s == "failed" and j.get("error")]
            if failed_errs:
                job_error = failed_errs[-1]
            elif not job_error:
                job_error = str(jobs[0].get("error") or "")
            case_state = "failed" if "failed" in states else ("finished" if all(s == "finished" for s in states)
                                                              else "running")
        done = int(counts.get("finished") or 0) + int(counts.get("failed") or 0)
        if case_state in ("finished", "failed") or (not jobs and done >= 1):
            break
        time.sleep(poll_s)
    if case_state not in ("finished", "failed") and time.monotonic() >= t_end:
        return finish("pending", f"rig budget {budget_s:g} s spent and the case is still {case_state or 'queued'}; "
                                 f"not cancelled — collect later with sim_status {exp_id} and sim_get_file "
                                 f"{exp_id} c000/results.json")

    for name in PULLS:
        try:
            data, sha, _chunks = client.get_file(exp_id, f"c000/{name}")
        except RigIntegrityError as exc:
            return finish("not-delivered", f"pulled file did not verify: {exc}")
        except (RigError, RigUnreachable):
            continue  # a failed case has no results.json; status.json carries the reason
        out = rig_dir / "c000" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        rec["pulled"][name] = {"sha256": sha, "bytes": len(data), "path": f"rig/c000/{name}"}
    status = {}
    sp = rig_dir / "c000" / "status.json"
    if sp.is_file():
        with open(sp, "r", encoding="utf-8") as f:
            status = json.load(f)
    if status.get("status") == "FINISHED" and "results.json" in rec["pulled"]:
        with open(rig_dir / "c000" / "results.json", "r", encoding="utf-8") as f:
            rec["results"] = json.load(f)
        return finish("delivered", None)
    err = status.get("error") or job_error.strip() or f"case state {case_state or 'unknown'} with no results.json"
    return finish("failed", str(err).splitlines()[-1][:400] if err else err)
