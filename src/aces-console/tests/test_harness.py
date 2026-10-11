"""Builder C: the front-end shell, its three plates and the harness, exercised against the real captures.

Everything here runs the fixture devserver (tests/harness/devserver.py) on an ephemeral loopback port and reads what it
serves; nothing contacts the rig and nothing is written except the harness findings in a temporary directory. When a
copied work store is available (ACES_CONSOLE_VM_WORK, or the integrator's scratchpad copy on this machine) the Projects
routes are checked against its real records; otherwise the honest absence shape is checked instead.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
STATIC = os.path.join(PKG, "static")
HARNESS = os.path.join(HERE, "harness")
SCRATCH_VM_WORK = r"C:\Users\shedm\AppData\Local\Temp\claude\C--Users-shedm\3c52762a-0b3b-4f55-9f39-96d315f13241\scratchpad\vm-work"


def vm_work_dir():
    p = os.environ.get("ACES_CONSOLE_VM_WORK") or SCRATCH_VM_WORK
    return p if os.path.isdir(os.path.join(p, "Work")) else None


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def post(url, body, headers=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class DevServerProcess:
    def __init__(self, extra=None):
        cmd = [sys.executable, "-I", os.path.join(HARNESS, "devserver.py"), "--port", "0", "--quiet"] + (extra or [])
        vm = vm_work_dir()
        if vm:
            cmd += ["--vm-work", vm]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline().strip()
        if not line.startswith("listening "):
            err = self.proc.stderr.read()
            self.proc.kill()
            raise RuntimeError(f"devserver did not start: {line!r} {err[-800:]}")
        self.url = line.split(" ", 1)[1]
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                st, _, _ = get(self.url + "/api/health")
                if st == 200:
                    return
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        raise RuntimeError("devserver health never answered")

    def stop(self):
        self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (self.proc.stdout, self.proc.stderr):
            if pipe:
                pipe.close()


class TestStaticTree(unittest.TestCase):
    def test_styles_css_edits(self):
        with open(os.path.join(STATIC, "styles.css"), encoding="utf-8") as fh:
            css = fh.read()
        self.assertNotIn("EBGaramond-italic-500", css, "edit 1: the duplicate italic-500 face must be gone")
        self.assertIn("EBGaramond-italic-400.woff2", css)
        for rule in (".stamp.stale", ".stamp.fixture", ".state.notrun", ".state.by-design"):
            self.assertIn(rule, css, f"edit 2 must add {rule}")
        self.assertIn("@media (max-width: 420px)", css, "the 390 px rules of spec 4.6")
        self.assertIn("--chalk: #dbe7f3;", css, "the token block is untouched")
        self.assertNotRegex(css, r"#[0-9a-f]{3,6}\b.*green", "no green")

    def test_fonts_copied_minus_italic_500(self):
        fonts = sorted(os.listdir(os.path.join(STATIC, "vendor", "fonts")))
        self.assertEqual(fonts, ["EBGaramond-OFL.txt", "EBGaramond-italic-400.woff2", "EBGaramond-normal-500.woff2", "JetBrainsMono-Bold.woff2", "JetBrainsMono-Italic.woff2", "JetBrainsMono-Medium.woff2", "JetBrainsMono-OFL.txt", "JetBrainsMono-Regular.woff2"])

    def test_vendored_modules(self):
        with open(os.path.join(STATIC, "vendor", "three.module.js"), encoding="utf-8") as fh:
            head = fh.read(400000)
        self.assertIn("SPDX-License-Identifier: MIT", head[:400])
        self.assertIn("REVISION = '170'", head)
        self.assertEqual(os.path.getsize(os.path.join(STATIC, "vendor", "OrbitControls.js")), 32134)
        with open(os.path.join(STATIC, "viewer.js"), encoding="utf-8") as fh:
            viewer = fh.read()
        self.assertIn('from "./vendor/OrbitControls.js"', viewer)
        self.assertNotIn("/static/", viewer, "imports made relative")
        for name in ("sky.js", "loaders.js", "app.js", "lib/api.js", "lib/vocab.js", "lib/format.js", "lib/dom.js"):
            with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
                self.assertNotRegex(fh.read(), r"from\s+\"/static/", f"{name} must import relatively")

    def test_index_shell(self):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        for pid, sprite in (("fabric", "i-fleet"), ("projects", "i-projects"), ("pipeline", "i-pipeline"), ("solvers", "i-solvers"), ("objects", "i-objects"), ("evidence", "i-evidence"), ("ops", "i-ops")):
            self.assertIn(f'data-plate="{pid}"', html)
            self.assertIn(f'<symbol id="{sprite}"', html)
            self.assertIn(f'id="plate-{pid}"', html)
        self.assertEqual(len(re.findall(r'role="tab"', html)), 8)
        self.assertEqual(len(re.findall(r'role="tabpanel"', html)), 8)
        scripts = re.findall(r"<script[^>]*>", html)
        self.assertEqual(sorted(scripts), sorted(['<script type="importmap">', '<script type="module" src="/static/app.js">']), "only the importmap is inline")
        self.assertIn('aria-live="polite"', html)
        self.assertIn('role="tablist"', html)
        self.assertIn('class="skip"', html)
        for plate in ("fabric", "projects", "pipeline", "solvers", "objects", "evidence", "agents", "ops"):
            self.assertTrue(os.path.isfile(os.path.join(STATIC, "plates", plate + ".js")))

    def test_vocab_json_matches_spec_4_4(self):
        with open(os.path.join(HARNESS, "vocab.json"), encoding="utf-8") as fh:
            v = json.load(fh)["vocabularies"]
        self.assertEqual(sorted(v), ["cad", "case", "console", "endpoint", "mc", "meta", "registry", "rq", "run"])
        self.assertEqual(v["mc"]["fault"], ["failed", "exhausted", "error"])
        self.assertEqual(v["console"]["fault"], ["FAILED"])
        self.assertEqual(v["rq"]["cyan"], ["started"])
        self.assertEqual(v["meta"]["chalk"], ["queued", "waiting_for_gpu", "finished"])
        self.assertEqual(v["run"]["amber"], ["interrupted"])
        self.assertEqual(v["case"]["chalk"], ["FINISHED", "NOT_RUN"])
        self.assertEqual(v["cad"]["fault"], ["error", "unreadable"])
        self.assertEqual(v["registry"]["chalk"], ["pass", "not recorded"])
        for e in v.values():
            self.assertNotIn("green", json.dumps(e))

    def test_fixture_copies_match_their_manifest(self):
        import hashlib
        d = os.path.join(HARNESS, "fixtures")
        with open(os.path.join(d, "MANIFEST.json"), encoding="utf-8") as fh:
            m = json.load(fh)
        self.assertEqual(m["stamp"], "FIXTURE 2026-10-11")
        for row in m["files"]:
            with open(os.path.join(d, row["file"]), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), row["sha256"], row["file"])
        # the repo root normalises every text file to LF; the captures are CRLF as taken, so without this attribute a
        # fresh checkout (the Linux target) would change their bytes and every hash above
        with open(os.path.join(d, ".gitattributes"), encoding="utf-8") as fh:
            self.assertIn("*.json -text", fh.read())


class TestDevServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = DevServerProcess()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def api(self, path):
        st, headers, body = get(self.srv.url + path)
        return st, headers, json.loads(body.decode("utf-8"))

    def test_page_headers(self):
        st, headers, body = get(self.srv.url + "/")
        self.assertEqual(st, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        csp = headers.get("Content-Security-Policy", "")
        self.assertRegex(csp, r"script-src 'self' 'sha256-[A-Za-z0-9+/=]+'")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn(b'<script type="importmap">', body)
        st, headers, _ = get(self.srv.url + "/static/styles.css")
        self.assertEqual(st, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))
        st, _, _ = get(self.srv.url + "/static/vendor/fonts/JetBrainsMono-Regular.woff2")
        self.assertEqual(st, 200)
        st, _, _ = get(self.srv.url + "/static/../tests/harness/devserver.py")
        self.assertIn(st, (403, 404))
        st, headers, _ = get(self.srv.url + "/api/health")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_health_and_config_carry_no_token_values(self):
        st, _, h = self.api("/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(h["ok"])
        self.assertEqual(h["bind"], "127.0.0.1")
        self.assertEqual(h["fixture"], "FIXTURE 2026-10-11")
        st, _, c = self.api("/api/config")
        self.assertEqual(c["meta"]["fixture"], "FIXTURE 2026-10-11")
        self.assertEqual(c["data"]["tokens"]["grid_control"], {"configured": False, "source": "not set"})
        self.assertNotIn("token_value", json.dumps(c))

    def test_rig_status_normalises_minus_one(self):
        st, _, r = self.api("/api/rig/status")
        self.assertEqual(st, 200)
        self.assertEqual(r["meta"]["fixture"], "FIXTURE 2026-10-11")
        self.assertEqual(r["meta"]["fetched_utc"], "2026-10-10T22:55:13Z", "fetched_utc is the capture time, never a fresh one")
        d = r["data"]
        self.assertEqual(d["mode"], "sim-lab-4")
        self.assertEqual(d["detected_mode"], "transition")
        self.assertIsNone(d["queue"]["queued"])
        self.assertIsNone(d["queue"]["running"])
        self.assertEqual(d["queue_note"], "GRID CONTROL reported -1")
        self.assertEqual(d["raw_queue"], {"queued": -1, "running": -1})
        self.assertEqual(len(d["gpus"]), 8)

    def test_rig_modes_keeps_pending_text(self):
        st, _, r = self.api("/api/rig/modes")
        gm = r["data"]["gridcontrol_modes"]
        self.assertEqual(len(gm), 11)
        self.assertTrue(gm["rag-training"]["pending"].startswith("embed/reranker/vl-embed services installed"))
        self.assertTrue(gm["batch-overnight"]["pending"].startswith("8 throughput workers installed"))
        self.assertTrue(all(v["switchable_reported"] for v in gm.values()))
        self.assertEqual(r["data"]["sim_modes"]["sim-lab-4"]["rest"], [6])
        self.assertEqual(r["data"]["current"], "sim-lab-4")

    def test_snapshot_classification(self):
        st, _, r = self.api("/api/rig/snapshot")
        cls = {(c["kind"], c["name"]): c["verdict"] for c in r["data"]["classification"]}
        for name in ("apodex-1", "apodex-2-primary", "apodex-3", "strategist"):
            self.assertEqual(cls[("endpoint", name)], "stopped by mode sim-lab-4 (by design)", name)
        self.assertEqual(cls[("endpoint", "embeddings")], "reachable")
        self.assertEqual(cls[("endpoint", "tools-8b")], "transition", "unreachable, not in the stop list, detected_mode transition")
        self.assertEqual(cls[("service", "strategist")], "stopped by mode sim-lab-4 (by design)")
        self.assertEqual(len(r["data"]["degraded"]), 5)

    def test_capacity_activity_labels(self):
        st, _, r = self.api("/api/sim/capacity")
        g = {x["index"]: x for x in r["data"]["gpus"]}
        self.assertEqual(g[2]["activity"], "outside the queue (operator hand-run)")
        self.assertEqual(g[2]["processes"][0]["pid"], 279309)
        self.assertEqual(g[2]["processes"][0]["usedMemMiB"], 2628)
        self.assertEqual(g[6]["activity"], "rests by mode sim-lab-4")
        self.assertEqual(g[0]["activity"], "idle")
        self.assertEqual(g[4]["activity"], "not in the sim pool")
        self.assertIsNone(r["data"]["gridcontrol_queue"]["queued"])
        self.assertEqual(r["data"]["queue_note"], "GRID CONTROL reported -1")

    def test_switch_status_tail(self):
        st, _, r = self.api("/api/rig/switch")
        self.assertEqual(r["data"]["target"], "sim-lab-4")
        self.assertTrue(r["data"]["done"])
        self.assertTrue(r["data"]["ok"])
        self.assertEqual(len(r["data"]["log_tail"]), 3)
        self.assertTrue(r["data"]["log_tail"][-1].endswith("SWITCH COMPLETE -> sim-lab-4 in 1s"))

    def test_decisions_need_a_token(self):
        # a GET answers 200 with data:null and the reason in meta.error (spec 3.1); never a 4xx/5xx the browser would log
        st, _, r = self.api("/api/rig/decisions")
        self.assertEqual(st, 200)
        self.assertIsNone(r["data"])
        self.assertFalse(r["meta"]["ok"])
        self.assertIn("GRID CONTROL token", r["meta"]["error"])
        self.assertTrue(r["meta"]["error"].startswith("not configured"))

    def test_vocab_served(self):
        st, _, v = self.api("/vocab.json")
        self.assertEqual(sorted(v["vocabularies"]), ["cad", "case", "console", "endpoint", "mc", "meta", "registry", "rq", "run"])

    def test_projects(self):
        st, _, r = self.api("/api/projects")
        self.assertEqual(st, 200)
        d = r["data"]
        if vm_work_dir():
            self.assertIsNone(d["reason"])
            by = {p["name"]: p for p in d["projects"]}
            self.assertEqual(sorted(by), ["cad-gold", "cad-vm", "golden-demo"])
            self.assertEqual(by["cad-vm"]["class"], "sovereign")
            self.assertEqual(by["cad-vm"]["counts"]["cad_runs"], 16)
            self.assertEqual(by["cad-gold"]["counts"]["golden_runs"], 1)
            self.assertEqual(by["golden-demo"]["counts"]["golden_runs"], 3)
            self.assertEqual(by["golden-demo"]["class"], "personal")
            self.assertEqual(by["cad-vm"]["onshape_gate"], "refused for this class")
            self.assertEqual(by["golden-demo"]["onshape_gate"], "allowed")
            st, _, p = self.api("/api/projects/golden-demo")
            runs = p["data"]["golden_runs"]
            self.assertEqual([x["run_id"] for x in runs], ["20261010T084920Z-b3ac99", "20261010T084129Z-b3ac99", "20261009T144006Z-b3ac99"])
            self.assertEqual(runs[0]["grade"], "rig-verified")
            self.assertEqual(runs[0]["status"], "done")
            self.assertEqual(runs[0]["wall_s"], 83.806)
            self.assertTrue(runs[0]["has_result"])
            st, _, p = self.api("/api/projects/cad-vm")
            cad = p["data"]["cad_runs"]
            self.assertEqual(len(cad), 16)
            self.assertEqual(cad[-1]["run_id"], "20261010T101628Z-0ffb")
            self.assertEqual(cad[-1]["status"], "ok")
            self.assertEqual(cad[-1]["sandbox"], {"mode": "strict", "confined": True, "platform": "linux-x86_64"})
            self.assertTrue(any(e["kind"] == "project.new" for e in self.api("/api/projects/golden-demo")[2]["data"]["ledger"]))
            st, _, nf = self.api("/api/projects/no-such-project")
            self.assertEqual(st, 404)
            self.assertIn("no project at", nf["error"])
        else:
            self.assertEqual(d["projects"], [])
            work = os.environ.get("ACES_WORK", "")
            if work and os.path.isdir(work):
                # the tests' scratch work store exists and is empty (makepkg's check(), the installed tests): no projects,
                # and no reason, since the store is there
                self.assertIsNone(d["reason"])
            else:
                self.assertRegex(d["reason"], r"does not exist$")

    def test_post_guards(self):
        st, r = post(self.srv.url + "/api/fabric/refresh", {})
        self.assertEqual(st, 403)
        self.assertEqual(r["code"], "refused")
        headers = {"Origin": self.srv.url, "X-ACES-Intent": "human", "Content-Type": "application/json"}
        st, r = post(self.srv.url + "/api/fabric/refresh", {}, headers)
        self.assertEqual(st, 503)
        self.assertEqual(r["code"], "fixture")
        st, r = post(self.srv.url + "/api/rig/switch/prepare", {"mode": "sim-lab-8"}, headers)
        self.assertEqual(st, 503, "the switch is never performed against a replay")
        st, r = post(self.srv.url + "/api/system/stop", {"confirm": "no"}, headers)
        self.assertEqual(st, 400)
        st, r = post(self.srv.url + "/api/doctor/run", {}, dict(headers, Origin="http://evil.example"))
        self.assertEqual(st, 403)
        st, _, h = self.api("/api/health")
        self.assertGreaterEqual(h["counters"]["refused_posts"], 2)

    def test_sse_first_record_is_the_fixture_event(self):
        req = urllib.request.Request(self.srv.url + "/api/events?plate=fabric")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.headers.get("Content-Type"), "text/event-stream; charset=utf-8")
            chunk = b""
            t0 = time.time()
            while b'data: {"topic"' not in chunk and time.time() - t0 < 10:
                chunk += r.readline()
        text = chunk.decode("utf-8")
        self.assertIn("retry: 3000", text)
        self.assertIn('event: fixture\ndata: {"fixture": "FIXTURE 2026-10-11"}', text)
        self.assertIn("event: stamp", text)
        stamp = json.loads([ln for ln in text.splitlines() if ln.startswith("data: {\"topic\"")][0][6:])
        self.assertIn(stamp["topic"], ("rig.status", "rig.switch", "fabric.manifest", "console.health", "rig.modes", "rig.telemetry", "rig.snapshot", "rig.decisions", "sim.capacity"))
        self.assertEqual(stamp["fixture"], "FIXTURE 2026-10-11")
        self.assertTrue(stamp["changed"])


class TestNoSSEFallback(unittest.TestCase):
    def test_events_refused_with_503(self):
        srv = DevServerProcess(["--no-sse"])
        try:
            st, _, body = get(srv.url + "/api/events?plate=fabric")
            self.assertEqual(st, 503)
            self.assertEqual(json.loads(body)["code"], "too-many-streams")
        finally:
            srv.stop()


class TestVerifyUI(unittest.TestCase):
    """Runs the browser harness against a devserver; SKIP (exit 0) when no browser exists is a pass."""

    def test_verify_ui(self):
        shots = os.environ.get("ACES_CONSOLE_SHOTS") or tempfile.mkdtemp(prefix="aces-console-shots-")
        cmd = [sys.executable, "-I", os.path.join(HARNESS, "verify_ui.py"), "--shots", shots]
        vm = vm_work_dir()
        if vm:
            cmd += ["--vm-work", vm]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        tail = "\n".join(proc.stdout.splitlines()[-60:])
        sys.stderr.write("\n--- verify_ui.py ---\n" + tail + "\n" + proc.stderr[-2000:] + "\n")
        self.assertEqual(proc.returncode, 0, tail + "\n" + proc.stderr[-2000:])
        if "SKIP:" not in proc.stdout:
            self.assertIn("RESULT: PASS", proc.stdout)
            with open(os.path.join(shots, "findings.json"), encoding="utf-8") as fh:
                f = json.load(fh)
            self.assertEqual(f["fixture"], "FIXTURE 2026-10-11")
            for vp in f["viewports"]:
                self.assertEqual(vp["console_errors"], [])
                self.assertEqual(vp["page_errors"], [])
                self.assertEqual(vp["requests_failed"], [])
                self.assertTrue(vp["fixture_stamped"], vp.get("stamp"))
                self.assertTrue(vp["tabs"]["all_seven"], vp["tabs"])
                self.assertEqual(sorted(k for k in vp["plates"] if ":" not in k), sorted(["fabric", "projects", "pipeline", "solvers", "objects", "evidence", "agents", "ops"]))
                for plate, d in vp["plates"].items():
                    self.assertTrue(d["overflow"]["ok"], (vp["viewport"], plate, d["overflow"]))
                    self.assertEqual(d["vocab"]["unknown"], [], (vp["viewport"], plate))
                    self.assertEqual(d["vocab"]["labels_wrong"], [], (vp["viewport"], plate))
                    self.assertEqual(d["colours"]["n_offenders"], 0, (vp["viewport"], plate, d["colours"]["offenders"]))
                    self.assertEqual(d["text"]["hits"], [], (vp["viewport"], plate))
                self.assertIn("endpoint:reachable", vp["plates"]["fabric"]["status_words"])
                self.assertIn("endpoint:stopped by mode sim-lab-4 (by design)", vp["plates"]["fabric"]["status_words"])
                if vm:
                    detail = [d for k, d in vp["plates"].items() if k.startswith("projects:")]
                    self.assertTrue(detail, "the projects deep link must have been checked")
                    self.assertIn("run:done", detail[0]["status_words"])
                    self.assertEqual(detail[0]["text"]["hits"], [], detail[0]["project"])
                    self.assertIn("cad:ok", detail[0]["status_words"]) if detail[0]["project"] == "cad-vm" else None
                    self.assertTrue(detail[0]["grade_sentence"]["present"])
            pc = f.get("polling_fallback") or {}
            self.assertTrue(pc.get("polling"), pc)
            self.assertTrue(pc.get("mode_rendered"), pc)
            self.assertEqual(pc.get("page_errors"), [])


if __name__ == "__main__":
    unittest.main()
