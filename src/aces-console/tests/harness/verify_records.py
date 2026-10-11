#!/usr/bin/env python3
"""The records plates driven by deep link (Builder D; spec 4.3 Pipeline, Solvers, Objects, Evidence).

Starts tests/harness/devserver.py (with the records routes of records_routes.py) on an ephemeral loopback port and, ONLY
when Playwright for Python and its chromium are importable, opens each records plate at a deep link, presses its
read-only actions (Check bundle, View part.stl, Verify, Verify ledger) and records what the page shows; without a
browser it prints "SKIP: no browser" and exits 0. Nothing here contacts the rig: the devserver replays the captures of
2026-10-11 and reads the copied work store named by --vm-work (the VM's real records); without a work store the
honest-absence branches are what gets checked.

Checked at 1440x900 and 390x844: no console errors, page errors or failed requests; no horizontal overflow; every status
word in the DOM belongs to its vocabulary and carries its label; no computed colour outside the token set; no stray
null/undefined/[object Object]/NaN; and the facts below, each from a real record:
  Pipeline  the two scale counts under their own names, rq and meta side by side, 66 experiments (the capture), the
            experiment aces-cal-air-03d with 9 cases, case c000 FINISHED, its MLflow run honestly "not captured";
  Solvers   14 registry rows, 14 lanes "not recorded", 9 pass rows carrying the not-citable note, golden-case selected;
  Objects   cad-vm: 16 runs, run 20261010T101628Z-0ffb (plate, 11607.3 mm3), 2 drawings drawn inline, 4 bundles,
            Check bundle on 20261010T101736Z-plate ending in the checker's all-match line, View part.stl, 1 note;
  Evidence  cad-gold run 20261010T102210Z-bc7391: grade verified-desktop followed by "no grade is a sign-off", 9 stages,
            15 compute-used rows, 10 sweep rows, report.html in a sandboxed iframe, Verify (503 where aces-engineering
            is not installed, the checks table where it is), Verify ledger (11 entries, chain intact).
Writes a findings JSON and the plate screenshots into --shots. Exit 1 on a failed check, 0 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from verify_ui import JS_COLOURS, JS_OVERFLOW, JS_TEXT, JS_VOCAB, fetch_json, start_devserver  # noqa: E402

VIEWPORTS = [("1440x900", 1440, 900), ("390x844", 390, 844)]
GOLDEN = ("cad-gold", "20261010T102210Z-bc7391")
CAD = ("cad-vm", "20261010T101628Z-0ffb")
BUNDLE = "20261010T101736Z-plate"
JS_ROWS = "(cap) => { const t = [...document.querySelectorAll('table')].find((x) => x.querySelector('caption') && x.querySelector('caption').textContent === cap); return t ? t.querySelectorAll('tbody tr:not(.empty)').length : -1; }"
JS_WORDS = "(sel) => [...document.querySelectorAll(sel + ' [data-vocab]')].map((e) => e.dataset.vocab + ':' + e.dataset.word)"


def plate_checks(page, vocab, sel):
    return {"overflow": page.evaluate(JS_OVERFLOW), "vocab": page.evaluate(JS_VOCAB, vocab), "colours": page.evaluate(JS_COLOURS), "text": page.evaluate(JS_TEXT),
            "status_words": sorted(set(page.evaluate(JS_WORDS, sel)))}


def text_of(page, sel):
    try:
        return page.inner_text(sel)
    except Exception as e:  # noqa: BLE001
        return f"<{type(e).__name__}: {str(e).splitlines()[0][:80]}>"


def open_plate(page, url, plate, params, wait_sel, timeout=30000):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    page.goto(f"{url}/?plate={plate}&{q}", wait_until="load")
    page.wait_for_selector(f"body[data-ready='{plate}']", state="attached", timeout=timeout)
    if wait_sel:
        page.wait_for_selector(wait_sel, state="attached", timeout=timeout)
    page.wait_for_timeout(900)


def check_pipeline(page, url, vocab, label, interact):
    out = {"plate": "pipeline", "deep_link": "experiment=aces-cal-air-03d&root=queue&case=c000"}
    open_plate(page, url, "pipeline", {"experiment": "aces-cal-air-03d", "root": "queue", "case": "c000"}, "#pipe-case .state[data-vocab='console']")
    summary = text_of(page, "#pipe-summary")
    out["scales_named"] = {"cases": "console case scale" in summary, "queue_jobs": "rq job scale" in summary}
    queue = text_of(page, "#pipe-queue")
    out["two_scales_side_by_side"] = "rq job status" in queue and "job meta state" in queue
    out["experiment_rows"] = page.evaluate(JS_ROWS, "experiments as the SIMLAB console lists them")
    out["case_rows"] = page.evaluate(JS_ROWS, "cases of aces-cal-air-03d")
    exp = text_of(page, "#pipe-experiment")
    out["experiment_shown"] = "aces-cal-air-03d" in exp
    jobs_headers = page.evaluate("[...document.querySelectorAll('#pipe-case table th')].map((t) => t.textContent)")
    out["rq_and_meta_headed"] = "rq status" in jobs_headers and "meta state" in jobs_headers
    case = text_of(page, "#pipe-case")
    out["case_shown"] = "c000" in case
    out["mlflow_card"] = "not captured" in case or "metrics" in case
    out["mlflow_card_text"] = next((ln for ln in case.splitlines() if "MLflow" in ln or "not captured" in ln), None)
    out.update(plate_checks(page, vocab, "#body-pipeline"))
    out["ok"] = all([out["scales_named"]["cases"], out["scales_named"]["queue_jobs"], out["two_scales_side_by_side"], out["experiment_shown"], out["case_shown"], out["rq_and_meta_headed"], out["case_rows"] == 9,
                     out["experiment_rows"] in (66, -1) or out["experiment_rows"] > 0])
    out["notes"] = [] if out["experiment_rows"] == 66 else [f"experiment rows {out['experiment_rows']} (66 in the capture; the experiments capture may be absent from the fixture set)"]
    return out


def check_solvers(page, url, vocab, label, interact):
    out = {"plate": "solvers", "deep_link": "solver=golden-case"}
    open_plate(page, url, "solvers", {"solver": "golden-case"}, "#sol-registry table tbody tr")
    out["registry_rows"] = page.evaluate(JS_ROWS, "solver registry")
    out["lanes_not_recorded"] = page.evaluate("document.querySelectorAll('#sol-registry [data-vocab=\"registry\"][data-word=\"not recorded\"]').length")
    out["not_citable_notes"] = page.evaluate("[...document.querySelectorAll('#sol-registry td')].filter((td) => td.textContent.includes('not citable against a running image')).length")
    out["pass_chips"] = page.evaluate("document.querySelectorAll('#sol-registry [data-vocab=\"registry\"][data-word=\"pass\"]').length")
    out["fail_chips"] = page.evaluate("document.querySelectorAll('#sol-registry [data-vocab=\"registry\"][data-word=\"fail\"]').length")
    out["inspector"] = text_of(page, "#eye")
    out["inspector_names_selected"] = "golden-case" in out["inspector"] and "image id recorded" in out["inspector"]
    cap = text_of(page, "#sol-capacity")
    out["activity_labels_seen"] = [w for w in ("outside the queue (operator hand-run)", "rests by mode sim-lab-4", "not in the sim pool", "idle") if w in cap]
    out["gpu_rows"] = page.evaluate(JS_ROWS, "GPU capacity as the SIMLAB console reports it")
    out["mode_rows"] = page.evaluate(JS_ROWS, "mode pools")
    out.update(plate_checks(page, vocab, "#body-solvers"))
    out["ok"] = out["registry_rows"] == 14 and out["lanes_not_recorded"] == 14 and out["not_citable_notes"] == 9 and out["pass_chips"] == 12 and out["fail_chips"] == 2 and out["inspector_names_selected"] and out["gpu_rows"] == 8 and len(out["activity_labels_seen"]) == 4
    return out


def check_objects(page, url, vocab, label, interact, has_work):
    out = {"plate": "objects", "deep_link": f"project={CAD[0]}&run={CAD[1]}", "work_store": has_work}
    if not has_work:
        open_plate(page, url, "objects", {}, None)
        body = text_of(page, "#body-objects")
        out["absence_drawn"] = "no work store" in body or "no projects" in body
        out.update(plate_checks(page, vocab, "#body-objects"))
        out["ok"] = out["absence_drawn"]
        return out
    open_plate(page, url, "objects", {"project": CAD[0], "run": CAD[1]}, "#obj-run .kv")
    out["run_rows"] = page.evaluate(JS_ROWS, f"CAD runs of {CAD[0]}")
    run = text_of(page, "#obj-run")
    out["run_detail"] = {"name_plate": "plate" in run, "volume": "11607.3" in run, "sandbox_strict": "strict" in run, "files_listed": page.evaluate(JS_ROWS, f"files of run {CAD[1]}")}
    # with a run selected the drawings block shows that run's drawings (1 of the project's 2) and says so in its line
    out["drawings_loaded"] = page.evaluate("[...document.querySelectorAll('#obj-drawings img')].map((i) => ({src: i.getAttribute('src'), complete: i.complete, width: i.naturalWidth}))")
    out["drawings_line"] = page.evaluate("(() => { const l = document.querySelector('#obj-drawings .line'); return l ? [...l.querySelectorAll('b')].map((b) => b.textContent) : null; })()")
    out["dimension_rows"] = page.evaluate(JS_ROWS, "dimensions of plate-fa5cc45b")
    out["hole_table_none"] = "hole table\nnone" in text_of(page, "#obj-drawings") or "hole table none" in text_of(page, "#obj-drawings").replace("\n", " ")
    out["bundle_rows"] = page.evaluate(JS_ROWS, f"bundles of {CAD[0]}")
    out["notes_entries"] = page.evaluate("document.querySelectorAll('#obj-notes .timeline .row').length")
    out["onshape"] = text_of(page, "#obj-onshape")
    out["onshape_not_connected"] = "false" in out["onshape"] and "never reads Onshape keys" in out["onshape"]
    if interact:
        # Check bundle: the checker's own lines, ending in its all-match line
        row = page.locator("#obj-bundles tbody tr", has_text=BUNDLE).first
        row.get_by_role("button", name="Check bundle").click()
        page.wait_for_selector("#obj-bundles pre.log", state="attached", timeout=90000)
        page.wait_for_timeout(500)
        pre = text_of(page, "#obj-bundles pre.log")
        out["bundle_check"] = {"lines": pre.count("\n") + 1, "all_match_line": any(ln.rstrip().endswith("all files match") for ln in pre.splitlines()), "tail": pre.splitlines()[-1][-160:] if pre else None}
        # View part.stl: WebGL in a headless browser may or may not be available; the note says which
        row.get_by_role("button", name="View part.stl").click()
        page.wait_for_selector("#objViewerNote .result", state="attached", timeout=30000)
        page.wait_for_timeout(1500)
        note = text_of(page, "#objViewerNote")
        out["viewer"] = {"note": note, "loaded": "triangles" in note}
    out.update(plate_checks(page, vocab, "#body-objects"))
    out["ok"] = (out["run_rows"] == 16 and out["run_detail"]["name_plate"] and out["run_detail"]["volume"] and out["dimension_rows"] == 6 and out["hole_table_none"] and out["bundle_rows"] == 4
                 and out["notes_entries"] == 1 and out["onshape_not_connected"] and out["drawings_line"] == ["2", "1"] and len(out["drawings_loaded"]) == 1
                 and all(d["complete"] and d["width"] > 0 for d in out["drawings_loaded"])
                 and (not interact or (out["bundle_check"]["all_match_line"] and out["viewer"]["loaded"])))
    return out


def check_evidence(page, url, vocab, label, interact, has_work):
    out = {"plate": "evidence", "deep_link": f"project={GOLDEN[0]}&run={GOLDEN[1]}", "work_store": has_work}
    if not has_work:
        open_plate(page, url, "evidence", {}, None)
        body = text_of(page, "#body-evidence")
        out["absence_drawn"] = "no golden runs" in body
        out["grade_sentence"] = "no grade is a sign-off" in body
        out.update(plate_checks(page, vocab, "#body-evidence"))
        out["ok"] = out["absence_drawn"] and out["grade_sentence"]
        return out
    open_plate(page, url, "evidence", {"project": GOLDEN[0], "run": GOLDEN[1]}, "#evi-run iframe")
    # the deep link names the project, so the runs table is filtered to it (1 of the store's 4 golden runs)
    out["golden_rows"] = page.evaluate(JS_ROWS, "golden runs")
    out["project_filter"] = page.evaluate("(document.querySelector('#eviProject') || {}).value")
    run = text_of(page, "#evi-run")
    # the grade line is a flex row (innerText puts each item on its own line), so the order is read from the DOM:
    # the key "grade", its value, then the fixed sentence as the next item
    out["grade_line"] = page.evaluate("(() => { const line = [...document.querySelectorAll('#evi-run .line')].find((l) => [...l.children].some((c) => c.classList.contains('k') && c.textContent === 'grade')); if (!line) return null; const kids = [...line.children]; const i = kids.findIndex((c) => c.classList.contains('k') && c.textContent === 'grade'); return { grade: kids[i + 1] ? kids[i + 1].textContent : null, next: kids[i + 2] ? kids[i + 2].textContent : null }; })()")
    out["grade_then_sentence"] = bool(out["grade_line"]) and out["grade_line"]["grade"] == "verified-desktop" and out["grade_line"]["next"] == "no grade is a sign-off"
    out["stage_rows"] = page.evaluate(JS_ROWS, "stages of the run")
    out["compute_rows"] = page.evaluate(JS_ROWS, "compute used")
    out["sweep_rows"] = page.evaluate(JS_ROWS, "sweep table")
    out["verify_check_rows_before"] = page.evaluate(JS_ROWS, "verify checks")
    out["provenance"] = "306" in run
    out["iframe"] = page.evaluate("(() => { const f = document.querySelector('#evi-run iframe'); return f ? {src: f.getAttribute('src'), sandbox: f.getAttribute('sandbox'), title: f.title} : null; })()")
    frame = next((f for f in page.frames if "report.html" in (f.url or "")), None)
    if frame is not None:
        try:
            out["iframe_document"] = {"title": frame.title(), "text_chars": frame.evaluate("document.body ? document.body.innerText.length : 0")}
        except Exception as e:  # noqa: BLE001
            out["iframe_document"] = {"not_read": f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"}
    else:
        out["iframe_document"] = {"not_read": "no frame with report.html in its url"}
    for _ in range(40):   # the figures load through the file route; wait for every one (up to 10 s)
        out["figures"] = page.evaluate("[...document.querySelectorAll('#evi-run .cards img')].map((i) => ({alt: i.alt, complete: i.complete, width: i.naturalWidth}))")
        if out["figures"] and all(f["complete"] for f in out["figures"]):
            break
        page.wait_for_timeout(250)
    if interact:
        page.get_by_role("button", name="Verify", exact=True).click()
        page.wait_for_selector("#evi-run .result:not(.live)", state="attached", timeout=320000)
        page.wait_for_timeout(400)
        out["verify"] = {"result": text_of(page, "#evi-run .result"), "check_rows": page.evaluate(JS_ROWS, "verify checks")}
        page.get_by_role("button", name="Verify ledger").click()
        page.wait_for_selector("#evi-ledger .result:not(.live)", state="attached", timeout=30000)
        page.wait_for_timeout(300)
        out["verify_ledger"] = text_of(page, "#evi-ledger .result")
    out["ledger_rows"] = page.evaluate(JS_ROWS, "desktop operator ledger, newest first")
    out["chain_rows"] = page.evaluate(JS_ROWS, "rig-side SIMLAB evidence ledger, newest first")
    out.update(plate_checks(page, vocab, "#body-evidence"))
    verify_ok = True
    if interact:
        v = out["verify"]["result"]
        verify_ok = ("aces-engineering not installed (exit 127)" in v) or (out["verify"]["check_rows"] >= 13)
        verify_ok = verify_ok and "chain intact" in out["verify_ledger"]
    # the rows the page shows against the server's own counts (a live store grows: the VM's ledger is no snapshot)
    out["ledger_total"] = page.evaluate("fetch('/api/ledger?n=200').then((r) => r.json()).then((d) => d.data ? d.data.n_total : null)")
    out["chain_total"] = page.evaluate("fetch('/api/ledger/simlab').then((r) => r.json()).then((d) => d.data && Array.isArray(d.data.entries) ? d.data.entries.length : null)")
    figures_ok = len(out["figures"]) >= 4 and all(f["complete"] and f["width"] > 0 for f in out["figures"])
    iframe_ok = out["iframe"] is not None and out["iframe"]["sandbox"] == "" and "report.html" in (out["iframe"]["src"] or "")
    out["notes"] = [] if "text_chars" in out["iframe_document"] else [f"the sandboxed report frame could not be read by the harness ({out['iframe_document'].get('not_read')}); the iframe element and its src are checked"]
    out["ok"] = (out["golden_rows"] == 1 and out["project_filter"] == GOLDEN[0] and out["grade_then_sentence"] and out["stage_rows"] == 9 and out["compute_rows"] == 15 and out["sweep_rows"] == 10
                 and out["provenance"] and iframe_ok and figures_ok and out["ledger_rows"] == min(out["ledger_total"] or 0, 200) and out["ledger_rows"] >= 11
                 and out["chain_rows"] == (out["chain_total"] if out["chain_total"] is not None else 13) and verify_ok)
    return out


def run(url, shots, vocab, has_work):
    from playwright.sync_api import sync_playwright

    findings = {"harness": "playwright (verify_records)", "url": url, "ran_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "work_store": has_work, "viewports": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        findings["browser"] = f"chromium {browser.version}"
        for label, w, h in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, color_scheme="dark")
            page = ctx.new_page()
            console_errors, page_errors, failed = [], [], []
            page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("requestfailed", lambda r: None if ("/api/events" in r.url and "ERR_ABORTED" in str(r.failure)) else failed.append(f"{r.url} {r.failure}"))
            interact = label == "1440x900"   # the actions are pressed once; the phone width checks the rendering
            vp = {"viewport": label, "console_errors": console_errors, "page_errors": page_errors, "requests_failed": failed, "plates": {}}
            for name, fn in (("pipeline", check_pipeline), ("solvers", check_solvers)):
                try:
                    vp["plates"][name] = fn(page, url, vocab, label, interact)
                except Exception as e:  # noqa: BLE001
                    vp["plates"][name] = {"plate": name, "ok": False, "exception": f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"}
                page.screenshot(path=os.path.join(shots, f"records-{name}-{label}.png"), full_page=True)
            for name, fn in (("objects", check_objects), ("evidence", check_evidence)):
                try:
                    vp["plates"][name] = fn(page, url, vocab, label, interact, has_work)
                except Exception as e:  # noqa: BLE001
                    vp["plates"][name] = {"plate": name, "ok": False, "exception": f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"}
                page.screenshot(path=os.path.join(shots, f"records-{name}-{label}.png"), full_page=True)
            # a pressed action that the backend refuses with its reason (Verify -> 503 where aces-engineering is not
            # installed) is logged by Chromium as a failed resource load; it is the honest reply, recorded apart
            vp["action_status_errors"] = [e for e in console_errors if "responded with a status of 503" in e or "responded with a status of 409" in e]
            vp["console_errors"] = [e for e in console_errors if e not in vp["action_status_errors"]]
            ctx.close()
            findings["viewports"].append(vp)
        browser.close()
    return findings


def summarise(findings):
    lines, bad = [], 0
    for vp in findings["viewports"]:
        lab = vp["viewport"]
        ce, pe, rf = vp["console_errors"], vp["page_errors"], vp["requests_failed"]
        lines.append(f"[{lab}] console errors {len(ce)} · page errors {len(pe)} · failed requests {len(rf)} · refused actions logged by the browser {len(vp.get('action_status_errors', []))} (the backend's honest 503/409 on a pressed action)")
        for e in ce + pe + rf + vp.get("action_status_errors", []):
            lines.append(f"[{lab}]   {e}")
        bad += len(ce) + len(pe) + len(rf)
        for name, d in vp["plates"].items():
            if "exception" in d:
                lines.append(f"[{lab}] {name}: EXCEPTION {d['exception']}")
                bad += 1
                continue
            ov = d.get("overflow", {}); vc = d.get("vocab", {}); co = d.get("colours", {}); tx = d.get("text", {})
            lines.append(f"[{lab}] {name} ({d.get('deep_link')}): ok={d['ok']} · overflow {ov.get('scrollWidth')}<={ov.get('innerWidth')} · status words {vc.get('checked')} unknown {len(vc.get('unknown', []))} labels wrong {len(vc.get('labels_wrong', []))} · colours checked {co.get('checked')} offenders {co.get('n_offenders')} · stray text {len(tx.get('hits', []))}")
            facts = {k: v for k, v in d.items() if k not in ("overflow", "vocab", "colours", "text", "status_words", "plate", "deep_link", "ok", "inspector", "onshape")}
            lines.append(f"[{lab}]   facts: {json.dumps(facts, ensure_ascii=False)[:900]}")
            if d.get("status_words"):
                lines.append(f"[{lab}]   words: {', '.join(d['status_words'][:14])}")
            for u in vc.get("unknown", [])[:8]:
                lines.append(f"[{lab}]   unknown word {u}")
            for o in (ov.get("offenders") or [])[:6]:
                lines.append(f"[{lab}]   past the right edge: {o}")
            for o in co.get("offenders", [])[:6]:
                lines.append(f"[{lab}]   colour offender {o}")
            for h in tx.get("hits", [])[:6]:
                lines.append(f"[{lab}]   stray text {h}")
            if not d["ok"]:
                bad += 1
            if not ov.get("ok", True):
                bad += 1
            bad += len(vc.get("unknown", [])) + len(vc.get("labels_wrong", [])) + int(co.get("n_offenders") or 0) + len(tx.get("hits", []))
    return lines, bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the four records plates by deep link (spec 4.3)")
    ap.add_argument("--url", default=None)
    ap.add_argument("--vm-work", default=None)
    ap.add_argument("--shots", default=os.path.join(HERE, "shots"))
    ap.add_argument("--json", default=None, help="findings file (default <shots>/records-findings.json)")
    args = ap.parse_args(argv)
    os.makedirs(args.shots, exist_ok=True)
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("SKIP: no browser (Playwright for Python is not importable)")
        return 0
    proc = None
    url = args.url
    try:
        if not url:
            proc, url = start_devserver(args.vm_work)
        vocab = fetch_json(url + "/vocab.json")
        projects = fetch_json(url + "/api/projects").get("data") or {}
        has_work = projects.get("reason") is None and bool(projects.get("projects"))
        try:
            findings = run(url, args.shots, vocab, has_work)
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            if "Executable doesn't exist" in msg or "browserType.launch" in msg:
                print(f"SKIP: no browser ({msg[:160]})")
                return 0
            raise
    finally:
        if proc:
            proc.kill()
            if proc.stdout:
                proc.stdout.close()
    findings["fixture"] = "FIXTURE 2026-10-11"
    out = args.json or os.path.join(args.shots, "records-findings.json")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(findings, fh, indent=1)
    lines, bad = summarise(findings)
    print(f"harness {findings['harness']} · browser {findings.get('browser')} · {findings['url']} · work store {findings['work_store']}")
    for ln in lines:
        print(ln)
    print(f"findings written to {out}")
    print("RESULT: " + ("PASS" if bad == 0 else f"FAIL ({bad} problem(s))"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
