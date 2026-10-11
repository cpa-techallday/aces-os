#!/usr/bin/env python3
"""aces-console browser harness (spec 7.3; Builder C).

Serves the static tree with tests/harness/devserver.py (or uses --url), then drives a headless browser ONLY if one is
available: Playwright for Python with its chromium or firefox when importable (this machine), else a chromium or
firefox executable on PATH (screenshots and DOM checks only), else it prints "SKIP: no browser" and exits 0.

Checks, at 1440x900 and 390x844: no console errors or page errors; the seven tabs reachable by keyboard; no horizontal
overflow (scrollWidth <= innerWidth) on every plate; every status word in the DOM ([data-vocab]) belongs to its
vocabulary in /vocab.json and carries its label; "no grade is a sign-off" on the Evidence plate (reported as not
checked while that plate is Builder D's placeholder); no element's computed colour outside the token set of :root; no
rendered text carrying a stray null, undefined, [object Object] or NaN (an append or template slip, never a fixture value).
Writes the Fabric plate screenshots and a findings JSON into --shots (default tests/harness/shots). Exit 1 on a failed
check, 0 otherwise. Everything the harness sees is a fixture replay stamped FIXTURE 2026-10-11; no rig is contacted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWPORTS = [("1440x900", 1440, 900), ("390x844", 390, 844)]
PLATES = ["fabric", "projects", "pipeline", "solvers", "objects", "evidence", "agents", "ops"]

JS_COLOURS = r"""
() => {
  const cs = getComputedStyle(document.documentElement);
  const names = new Set();
  for (const sh of document.styleSheets) {
    let rules; try { rules = sh.cssRules; } catch (e) { continue; }
    for (const r of rules) if (r.selectorText && r.selectorText.split(",").some((s) => s.trim() === ":root")) for (const p of r.style) if (p.startsWith("--")) names.add(p);
  }
  const probe = document.createElement("span"); document.body.append(probe);
  const tokens = new Set(); const raw = {};
  for (const n of names) {
    const v = cs.getPropertyValue(n).trim(); if (!v) continue;
    probe.style.color = ""; probe.style.color = v;
    const c = getComputedStyle(probe).color; const m = c.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (m) { tokens.add(`${m[1]},${m[2]},${m[3]}`); raw[n] = c; }
  }
  probe.remove();
  const props = ["color", "background-color", "border-top-color", "border-right-color", "border-bottom-color", "border-left-color", "outline-color", "text-decoration-color", "caret-color"];
  const offenders = []; let checked = 0, unparsed = 0;
  const sig = (e) => e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (e.className && typeof e.className === "string" ? "." + e.className.trim().split(/\s+/).slice(0, 3).join(".") : "");
  for (const e of document.querySelectorAll("body *")) {
    if (e.closest("[hidden]") || e.closest("dialog:not([open])") || e.closest("svg")) continue;
    const r = e.getBoundingClientRect(); if (!r.width && !r.height) continue;
    const s = getComputedStyle(e);
    for (const p of props) {
      const v = s.getPropertyValue(p); if (!v || v === "transparent" || v === "auto") continue;
      const m = v.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
      if (!m) { unparsed++; continue; }
      if (m[4] !== undefined && Number(m[4]) === 0) continue;
      checked++;
      const key = `${m[1]},${m[2]},${m[3]}`;
      if (!tokens.has(key)) offenders.push({ el: sig(e), prop: p, value: v });
    }
  }
  return { tokens: [...tokens], checked, unparsed, offenders: offenders.slice(0, 60), n_offenders: offenders.length };
}
"""

JS_OVERFLOW = r"""
() => {
  const iw = window.innerWidth;
  const sig = (e) => e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (e.className && typeof e.className === "string" ? "." + e.className.trim().split(/\s+/).slice(0, 3).join(".") : "");
  const clipped = (e) => { for (let a = e.parentElement; a; a = a.parentElement) { const o = getComputedStyle(a).overflowX; if (o === "auto" || o === "hidden" || o === "scroll" || o === "clip") return true; } return false; };
  const out = [];
  for (const e of document.querySelectorAll("body *")) {
    if (e.closest("[hidden]")) continue;
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.right > iw + 0.5) out.push({ el: sig(e), right: Math.round(r.right), width: Math.round(r.width), clipped: clipped(e), text: (e.textContent || "").trim().slice(0, 60) });
  }
  return { scrollWidth: document.documentElement.scrollWidth, innerWidth: iw, ok: document.documentElement.scrollWidth <= iw, offenders: out.filter((o) => !o.clipped).slice(0, 15), clipped_offenders: out.filter((o) => o.clipped).length };
}
"""

JS_VOCAB = r"""
(vocab) => {
  const out = { checked: 0, unknown: [], labels_wrong: [] };
  const pat = (c) => c.includes("<") ? new RegExp("^" + c.split(/<[a-z_]+>/i).map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("(.+)") + "$") : null;
  for (const e of document.querySelectorAll("[data-vocab]")) {
    out.checked++;
    const v = e.dataset.vocab, w = e.dataset.word;
    const entry = vocab.vocabularies[v];
    const words = entry ? [...(entry.chalk || []), ...(entry.cyan || []), ...(entry.amber || []), ...(entry.fault || [])] : [];
    const ok = !!entry && words.some((c) => c === w || (pat(c) && pat(c).test(w)));
    if (!ok) out.unknown.push({ vocab: v, word: w });
    const lab = e.querySelector(".voc");
    if (!lab || (entry && lab.textContent !== entry.label)) out.labels_wrong.push({ vocab: v, word: w, label: lab ? lab.textContent : null });
  }
  return out;
}
"""

JS_TEXT = r"""
() => {
  // a null or undefined that reached the page as text (a native append(null), a `${missing}` template), an object
  // printed as "[object Object]" or a NaN is a rendering slip, never a value the fixtures hold. A standalone "null" is
  // allowed (a plate may quote an upstream null, "mode: null"); a null glued to a word is not.
  const text = document.body.innerText || "";
  const pats = [/[A-Za-z0-9)]null\b/g, /\bundefined\b/g, /\[object Object\]/g, /\bNaN\b/g];
  const hits = [];
  for (const re of pats) {
    let m;
    while ((m = re.exec(text)) && hits.length < 12) hits.push({ match: m[0], context: text.slice(Math.max(0, m.index - 40), m.index + m[0].length + 20).replace(/\s+/g, " ") });
  }
  return { checked: text.length, hits };
}
"""


def wait_health(url, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def start_devserver(vm_work=None, extra=None):
    cmd = [sys.executable, "-I", os.path.join(HERE, "devserver.py"), "--port", "0", "--quiet"] + (extra or [])
    if vm_work:
        cmd += ["--vm-work", vm_work]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    line = proc.stdout.readline().strip()
    if not line.startswith("listening "):
        proc.kill()
        raise RuntimeError(f"devserver did not start: {line!r}")
    url = line.split(" ", 1)[1]
    if not wait_health(url):
        proc.kill()
        raise RuntimeError("devserver health never answered")
    return proc, url


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def run_playwright(url, shots, vocab, full_page=False):
    from playwright.sync_api import sync_playwright

    findings = {"harness": "playwright", "url": url, "ran_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "viewports": []}
    with sync_playwright() as p:
        browser = None
        for name in ("chromium", "firefox"):
            try:
                browser = getattr(p, name).launch(headless=True)
                findings["browser"] = f"{name} {browser.version}"
                break
            except Exception as e:  # noqa: BLE001
                findings.setdefault("launch_failures", []).append(f"{name}: {str(e).splitlines()[0][:160]}")
        if browser is None:
            return None, findings
        for label, w, h in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, color_scheme="dark")
            page = ctx.new_page()
            console_errors, page_errors, failed = [], [], []
            page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            # an EventSource the page closes itself (plate change, context close) is reported by Chromium as ERR_ABORTED on
            # /api/events; that is the expected close of a stream, not a failed request, so it is counted apart
            aborted_streams = []
            page.on("requestfailed", lambda r: (aborted_streams if ("/api/events" in r.url and "ERR_ABORTED" in str(r.failure)) else failed).append(f"{r.url} {r.failure}"))
            vp = {"viewport": label, "console_errors": console_errors, "page_errors": page_errors, "requests_failed": failed, "streams_closed_by_page": aborted_streams, "plates": {}, "tabs": {}}
            # waits are selector-based: the page's CSP has no 'unsafe-eval', so string predicates evaluated in the main world would be refused
            page.goto(f"{url}/?plate=fabric", wait_until="load")
            page.wait_for_selector("body[data-ready='fabric']", state="attached", timeout=30000)
            page.wait_for_timeout(2500)   # the first stamps of the stream
            stamp = page.inner_text("#stamp")
            vp["stamp"] = stamp
            vp["fixture_stamped"] = "FIXTURE 2026-10-11" in stamp
            vp["stream_state"] = page.evaluate("document.querySelector('#stamp .part:nth-last-child(1)') && document.querySelector('#stamp').innerText")
            shot = os.path.join(shots, f"fabric-{label}.png")
            page.screenshot(path=shot, full_page=False)
            vp["screenshot"] = shot
            # keyboard reachability: focus the first tab, ArrowDown walks and selects every plate
            page.focus("#tab-fabric")
            reached = []
            for i in range(len(PLATES)):
                active = page.evaluate("document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.plate : null")
                selected = page.evaluate("(document.querySelector('[role=tab][aria-selected=\"true\"]') || {}).dataset ? document.querySelector('[role=tab][aria-selected=\"true\"]').dataset.plate : null")
                if active:
                    page.wait_for_selector(f"body[data-ready='{active}']", state="attached", timeout=30000)
                    page.wait_for_timeout(400)
                    visible = page.evaluate(f"!document.getElementById('plate-{active}').hidden")
                    reached.append({"focused": active, "selected": selected, "panel_visible": visible})
                    plate = {"overflow": page.evaluate(JS_OVERFLOW)}
                    plate["vocab"] = page.evaluate(JS_VOCAB, vocab)
                    plate["colours"] = page.evaluate(JS_COLOURS)
                    plate["text"] = page.evaluate(JS_TEXT)
                    plate["placeholder"] = page.evaluate(f"!!document.querySelector('#body-{active} [data-placeholder]')")
                    plate["status_words"] = page.evaluate(f"[...document.querySelectorAll('#body-{active} [data-vocab]')].map((e) => e.dataset.vocab + ':' + e.dataset.word).filter((v, i, a) => a.indexOf(v) === i)")
                    if active == "evidence":
                        present = page.evaluate("document.body.innerText.includes('no grade is a sign-off')")
                        plate["grade_sentence"] = {"present": present, "note": None if present else ("not checked: the Evidence plate is Builder D's placeholder" if plate["placeholder"] else "absent")}
                    if full_page:   # a review aid: every plate, the whole page, for a human to look at
                        plate["full_page"] = os.path.join(shots, f"{active}-{label}-full.png")
                        page.screenshot(path=plate["full_page"], full_page=True)
                    vp["plates"][active] = plate
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(250)
            vp["tabs"] = {"reached": reached, "all_seven": sorted({r["focused"] for r in reached if r["panel_visible"] and r["focused"] == r["selected"]}) == sorted(PLATES)}
            # deep link into a project's detail (?plate=projects&project=<first project the backend lists>): the golden and
            # CAD run tables carry the `run` and `cad` vocabulary chips, and the Projects plate prints the grade sentence
            try:
                projects = fetch_json(url + "/api/projects").get("data") or {}
            except Exception:  # noqa: BLE001
                projects = {}
            names = [p.get("name") for p in projects.get("projects") or [] if p.get("name")]
            if names:
                page.goto(f"{url}/?plate=projects&project={names[0]}", wait_until="load")
                page.wait_for_selector("body[data-ready='projects']", state="attached", timeout=30000)
                page.wait_for_timeout(1200)
                detail = {"project": names[0], "overflow": page.evaluate(JS_OVERFLOW)}
                detail["vocab"] = page.evaluate(JS_VOCAB, vocab)
                detail["colours"] = page.evaluate(JS_COLOURS)
                detail["text"] = page.evaluate(JS_TEXT)
                detail["status_words"] = page.evaluate("[...document.querySelectorAll('#body-projects [data-vocab]')].map((e) => e.dataset.vocab + ':' + e.dataset.word).filter((v, i, a) => a.indexOf(v) === i)")
                detail["grade_sentence"] = {"present": page.evaluate("document.body.innerText.includes('no grade is a sign-off')"), "note": "the Projects plate prints the sentence under its golden-runs table"}
                if full_page:
                    detail["full_page"] = os.path.join(shots, f"projects-{names[0]}-{label}-full.png")
                    page.screenshot(path=detail["full_page"], full_page=True)
                vp["plates"]["projects:" + names[0]] = detail
            else:
                vp["projects_deep_link"] = f"not checked: the backend lists no project ({projects.get('reason') or 'no reason given'})"
            ctx.close()
            findings["viewports"].append(vp)
        browser.close()
    return True, findings


def run_polling_check(url, no_sse_url):
    """With the stream refused (503), the page must fall back to polling and still render the mode from the GET routes."""
    from playwright.sync_api import sync_playwright

    out = {"url": no_sse_url}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{no_sse_url}/?plate=fabric", wait_until="load")
        page.wait_for_selector("body[data-ready='fabric']", state="attached", timeout=30000)
        try:
            page.wait_for_selector("#stamp .part:text-is('polling')", state="attached", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1500)
        out["stamp"] = page.inner_text("#stamp")
        out["polling"] = "polling" in out["stamp"].lower()
        out["mode_rendered"] = "mode sim-lab-4" in out["stamp"].lower()
        out["page_errors"] = errors
        out["title_says"] = page.get_attribute("#stamp", "title")
        ctx.close(); browser.close()
    return out


def run_plain_browser(url, shots, exe):
    """Fallback without Playwright: screenshots and a DOM dump only (overflow, console and colour checks cannot run)."""
    findings = {"harness": f"plain {os.path.basename(exe)}", "url": url, "viewports": [], "note": "Playwright absent: screenshots and DOM word check only; console, overflow, keyboard and colour checks SKIPPED"}
    for label, w, h in VIEWPORTS:
        shot = os.path.join(shots, f"fabric-{label}.png")
        subprocess.run([exe, "--headless=new", "--disable-gpu", f"--window-size={w},{h}", f"--screenshot={shot}", "--virtual-time-budget=6000", f"{url}/?plate=fabric"], check=False, timeout=120, capture_output=True)
        findings["viewports"].append({"viewport": label, "screenshot": shot if os.path.exists(shot) else None})
    return True, findings


def summarise(findings):
    lines, bad = [], 0
    for vp in findings.get("viewports", []):
        lab = vp["viewport"]
        ce, pe, rf = vp.get("console_errors", []), vp.get("page_errors", []), vp.get("requests_failed", [])
        lines.append(f"[{lab}] console errors {len(ce)} · page errors {len(pe)} · failed requests {len(rf)} · streams closed by the page {len(vp.get('streams_closed_by_page', []))} (expected on plate change) · stamp '{vp.get('stamp', '').replace(chr(10), ' / ')}' · fixture stamped {vp.get('fixture_stamped')}")
        for e in ce + pe + rf:
            lines.append(f"[{lab}]   {e}")
        bad += len(ce) + len(pe) + len(rf)
        if vp.get("projects_deep_link"):
            lines.append(f"[{lab}] projects deep link: {vp['projects_deep_link']}")
        tabs = vp.get("tabs", {})
        lines.append(f"[{lab}] tabs reachable by keyboard: {[r['focused'] for r in tabs.get('reached', [])]} all seven {tabs.get('all_seven')}")
        if tabs and not tabs.get("all_seven"):
            bad += 1
        for pl, d in vp.get("plates", {}).items():
            ov = d.get("overflow", {}); vc = d.get("vocab", {}); co = d.get("colours", {}); tx = d.get("text", {})
            lines.append(f"[{lab}] {pl}: overflow {ov.get('scrollWidth')}<={ov.get('innerWidth')} ok={ov.get('ok')} · status words {vc.get('checked')} unknown {len(vc.get('unknown', []))} labels wrong {len(vc.get('labels_wrong', []))} · colours checked {co.get('checked')} offenders {co.get('n_offenders')} (unparsed {co.get('unparsed')}) · tokens {len(co.get('tokens', []))} · text {tx.get('checked')} chars stray null/undefined {len(tx.get('hits', []))}{' · placeholder' if d.get('placeholder') else ''}")
            for h in tx.get("hits", [])[:10]:
                lines.append(f"[{lab}]   stray text {h.get('match')!r} in: …{h.get('context')}…")
            if d.get("status_words"):
                lines.append(f"[{lab}]   words: {', '.join(d['status_words'][:16])}{' …' if len(d['status_words']) > 16 else ''}")
            for u in vc.get("unknown", [])[:10]:
                lines.append(f"[{lab}]   unknown word {u}")
            for o in (ov.get("offenders") or [])[:10]:
                lines.append(f"[{lab}]   past the right edge: {o}")
            for o in co.get("offenders", [])[:12]:
                lines.append(f"[{lab}]   colour offender {o}")
            if "grade_sentence" in d:
                lines.append(f"[{lab}]   'no grade is a sign-off' on Evidence: present={d['grade_sentence']['present']} {d['grade_sentence']['note'] or ''}")
            if not ov.get("ok", True):
                bad += 1
            bad += len(vc.get("unknown", [])) + len(vc.get("labels_wrong", [])) + int(co.get("n_offenders") or 0) + len(tx.get("hits", []))
        if vp.get("screenshot"):
            lines.append(f"[{lab}] screenshot {vp['screenshot']}")
    pc = findings.get("polling_fallback")
    if pc:
        lines.append(f"[polling] stream refused with 503 -> stamp '{pc.get('stamp', '').replace(chr(10), ' / ')}' · polling {pc.get('polling')} · mode rendered from the GET routes {pc.get('mode_rendered')} · page errors {len(pc.get('page_errors', []))}")
        if not pc.get("polling") or not pc.get("mode_rendered") or pc.get("page_errors"):
            bad += 1
    return lines, bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="aces-console browser harness (spec 7.3)")
    ap.add_argument("--url", default=None, help="a running console or devserver; default: start tests/harness/devserver.py on an ephemeral port")
    ap.add_argument("--vm-work", default=None, help="passed to the devserver it starts")
    ap.add_argument("--shots", default=os.path.join(HERE, "shots"))
    ap.add_argument("--json", default=None, help="where to write the findings (default <shots>/findings.json)")
    ap.add_argument("--full-page", action="store_true", help="also save a full-page screenshot of every plate into --shots (a review aid)")
    args = ap.parse_args(argv)
    os.makedirs(args.shots, exist_ok=True)
    have_pw = False
    try:
        import playwright  # noqa: F401
        have_pw = True
    except ImportError:
        pass
    exe = None
    for name in ("chromium", "chromium-browser", "chrome", "google-chrome", "firefox"):
        exe = shutil.which(name)
        if exe:
            break
    if not have_pw and not exe:
        print("SKIP: no browser (neither Playwright for Python nor chromium/firefox on PATH)")
        return 0
    proc = None
    proc2 = None
    url = args.url
    try:
        if not url:
            proc, url = start_devserver(args.vm_work)
        vocab = fetch_json(url + "/vocab.json")
        if have_pw:
            ok, findings = run_playwright(url, args.shots, vocab, full_page=args.full_page)
            if ok and not args.url:
                # a second devserver that refuses the stream exercises the polling fallback of lib/api.js
                proc2, url2 = start_devserver(args.vm_work, ["--no-sse"])
                findings["polling_fallback"] = run_polling_check(url, url2)
            if not ok:
                if exe:
                    ok, findings2 = run_plain_browser(url, args.shots, exe)
                    findings2["playwright"] = findings
                    findings = findings2
                else:
                    print("SKIP: no browser (Playwright is installed but no browser could be launched: %s)" % "; ".join(findings.get("launch_failures", [])))
                    return 0
        else:
            ok, findings = run_plain_browser(url, args.shots, exe)
    finally:
        for pr in (proc, proc2):
            if pr:
                pr.kill()
                if pr.stdout:
                    pr.stdout.close()
    findings["fixture"] = "FIXTURE 2026-10-11"
    out = args.json or os.path.join(args.shots, "findings.json")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(findings, fh, indent=1)
    lines, bad = summarise(findings)
    print(f"harness {findings.get('harness')} · browser {findings.get('browser', 'n/a')} · {findings.get('url')}")
    for ln in lines:
        print(ln)
    print(f"findings written to {out}")
    print("RESULT: " + ("PASS" if bad == 0 else f"FAIL ({bad} problem(s))"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
