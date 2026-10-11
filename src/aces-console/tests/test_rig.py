"""rig.py against the fakes and the 2026-10-11 captures: the queue normalisation (-1 -> null with the reporting sentence),
the 11 modes with their pending texts, the snapshot classification under sim-lab-4, the telemetry ring, the switch
status, decisions with and without a token, the two-step switch (nonce lifecycle, gates, ledger entries, the recorded
POST body and token header, the outcome watcher), the refusal ledger with its 60 s window, the fabric manifest and its
refresh with the 60 s rate limit, and the lost-outcome write at restart. No request leaves the loopback."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as pysecrets
import time
import unittest
from unittest import mock

from aces import ledger

from aces_console import rig

from . import FIXTURES
from .fakes.fake_gc import FakeGC, load_fixture
from .fakes.fake_rigstate import FakeRigState
from .support import Console, ledger_entries, ledger_text, wait_until

TOKEN = pysecrets.token_hex(16)


def _kinds():
    return [e["kind"] for e in ledger_entries()]


class ClassifyTest(unittest.TestCase):
    """The pure function on the captured snapshot and the captured sim pools."""

    def test_sim_lab_4(self):
        snapshot = load_fixture("rigstate-snapshot.json")["result"]["structuredContent"]
        sim_modes = load_fixture("simlab-capacity.json")["modes"]
        rows = rig.classify(snapshot, "sim-lab-4", sim_modes, switching=None)
        by = {(r["kind"], r["name"]): r for r in rows}
        self.assertEqual(len(rows), 6 + 6)
        for name in ("apodex-1", "apodex-2-primary", "apodex-3", "strategist"):
            r = by[("endpoint", name)]
            self.assertEqual((r["word"], r["verdict"]), ("by-design", "stopped by mode sim-lab-4 (by design)"), name)
            self.assertEqual(r["rule"], "stop_services")
        self.assertEqual(by[("endpoint", "apodex-2-primary")]["matched"], "apodex-2")
        for name in ("apodex-1", "apodex-2", "apodex-3", "strategist"):
            self.assertEqual(by[("service", name)]["word"], "by-design", name)
        self.assertEqual(by[("endpoint", "embeddings")]["word"], "reachable")
        self.assertEqual(by[("service", "embeddings")]["word"], "reachable")
        tools = by[("endpoint", "tools-8b")]
        self.assertEqual((tools["word"], tools["verdict"]), ("unreachable", "unreachable (not explained by mode sim-lab-4)"))
        self.assertEqual((tools["matched"], tools["rule"]), ("tools", "keep_services"))
        self.assertEqual(by[("service", "strategist")]["state"], "failed")
        # the two other states of the vocabulary
        rows = rig.classify(snapshot, "sim-lab-4", sim_modes, switching="sim-lab-8")
        self.assertEqual({r["word"] for r in rows if r["name"] == "tools-8b"}, {"transition"})
        rows = rig.classify(snapshot, None, sim_modes, mode_error="unreachable: refused")
        self.assertIn("mode not known: unreachable: refused", [r["verdict"] for r in rows if r["name"] == "apodex-1"][0])
        rows = rig.classify(snapshot, "general-fleet-v2", sim_modes)
        self.assertIn("has no stop list", [r["verdict"] for r in rows if r["name"] == "apodex-1"][0])
        words = {r["word"] for r in rig.classify(snapshot, "sim-lab-4", sim_modes)}
        self.assertTrue(words <= {"by-design", "reachable", "transition", "unreachable"})

    def test_normalisations(self):
        st = rig.normalise_status({"mode": "m", "queue": {"queued": 3, "running": 1}})
        self.assertEqual((st["queue"], st["queue_note"]), ({"queued": 3, "running": 1}, None))
        st = rig.normalise_status({"mode": "m", "queue": "n/a"})
        self.assertEqual((st["queue"], st["queue_note"]), ({"queued": None, "running": None}, "GRID CONTROL reported n/a"))
        st = rig.normalise_status({"mode": "m"})
        self.assertEqual(st["queue_note"], "GRID CONTROL reported no queue")
        modes = rig.normalise_modes({"a": {"label": "A", "switchable": True, "pending": None}, "b": {"label": "B"}})
        self.assertEqual(modes["b"], {"label": "B", "switchable": False, "switchable_reported": False, "pending": None})
        self.assertTrue(modes["a"]["switchable_reported"])
        sw = rig.normalise_switch(load_fixture("gc-switch-status.json"))
        self.assertEqual((sw["idle"], sw["target"], sw["done"], sw["ok"], len(sw["log_tail"]), sw["log_lines"]),
                         (None, "sim-lab-4", True, True, 3, 49))


class RigRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {"ACES_GC_TOKEN": TOKEN})
        cls.env.start()
        cls.gc = FakeGC(token=TOKEN)
        cls.rs = FakeRigState(sse=True)
        cls.c = Console(gc=cls.gc.url, rigstate=cls.rs.url, tag="rig")

    @classmethod
    def tearDownClass(cls):
        cls.c.close()
        cls.gc.close()
        cls.rs.close()
        cls.env.stop()

    def setUp(self):
        self.gc.clear()
        with self.gc.lock:
            self.gc.requests.clear()
        rig.NONCE_TTL_S = 30.0
        st = rig._state(self.c.app)
        with st.lock:
            st.refusals.clear()
            st.nonces.clear()
            st.pending = None
        self.c.app.switch_outcome_pending = None
        self.c.app.counters["nonce_failures"] = 0
        self.c.app.counters["suppressed_refusals"] = 0

    def _fresh(self):
        for topic in ("rig.status", "rig.modes", "rig.switch"):
            self.c.app.hub.refresh(topic)

    def test_status_is_normalised_and_carries_the_token_header_upstream(self):
        self.c.app.hub.refresh("rig.status")  # a fresh upstream call inside this test's recording window
        status, _, body = self.c.get("/api/rig/status")
        self.assertEqual(status, 200)
        d = body["data"]
        self.assertEqual(d["queue"], {"queued": None, "running": None})
        self.assertEqual(d["queue_note"], "GRID CONTROL reported -1")
        self.assertEqual(d["raw_queue"], {"queued": -1, "running": -1})
        self.assertEqual((d["mode"], d["detected_mode"], d["auto_enabled"], d["switching"]), ("sim-lab-4", "transition", False, None))
        self.assertEqual(d["last_switch_ts"], 1791355467907)
        self.assertEqual((d["mc_console"], d["gridwatch"]), ("http://127.0.0.1:3100", "http://192.168.1.65:3000"))
        self.assertEqual(len(d["gpus"]), 8)
        self.assertTrue(body["meta"]["ok"])
        self.assertEqual(body["meta"]["source"], self.gc.url + "/api/status")
        reqs = self.gc.requests_for("GET", "/api/status")
        self.assertTrue(reqs)
        self.assertTrue(all(r["headers"].get("x-gc-token") == TOKEN for r in reqs), "every GC request carries the token")

    def test_modes_11_with_pending_texts(self):
        status, _, body = self.c.get("/api/rig/modes")
        self.assertEqual(status, 200)
        gm = body["data"]["gridcontrol_modes"]
        self.assertEqual(len(gm), 11)
        self.assertEqual(gm["rag-training"]["pending"], "embed/reranker/vl-embed services installed; QLoRA training env "
                                                       "(unsloth) still not installed - training jobs remain manual")
        self.assertEqual(gm["batch-overnight"]["pending"], "8 throughput workers installed (8040-8047); redis-rq queue "
                                                          "provisioning still pending")
        self.assertTrue(all(m["switchable"] and m["switchable_reported"] for m in gm.values()))
        self.assertEqual(gm["sim-lab-4"]["label"], "Sim Lab 4 (GPUs 0-3 simulation; GLM-Flash on 4,5 + GPT-OSS on 7; 6 rests)")
        self.assertEqual(body["data"]["current"], "sim-lab-4")
        self.assertIsNone(body["data"]["sim_modes"], "the SIMLAB console is a closed port in this test")
        self.assertTrue(body["data"]["errors"]["sim_modes"].startswith("SIMLAB console unreachable at "))
        self.assertIn("parts", body["meta"])

    def test_snapshot_route_with_sse_shaped_mcp_reply(self):
        status, _, body = self.c.get("/api/rig/snapshot")
        self.assertEqual(status, 200)
        d = body["data"]
        self.assertEqual(d["capturedAtIso"], "2026-10-10T22:55:16.788Z")
        self.assertEqual(len(d["degraded"]), 5)
        self.assertFalse(d["allHealthy"])
        self.assertEqual(len(d["endpoints"]), 6)
        by = {(r["kind"], r["name"]): r for r in d["classification"]}
        self.assertEqual(by[("endpoint", "strategist")]["word"], "unreachable",
                         "no sim pools (console down): the stop list is unknown, nothing is explained by the mode")
        self.assertEqual(d["classification_basis"]["mode"], "sim-lab-4")
        self.assertTrue(d["classification_basis"]["sim_modes_error"].startswith("SIMLAB console unreachable"))
        req = self.rs.requests[-1]
        self.assertEqual(req["body"]["method"], "tools/call")
        self.assertEqual(req["body"]["params"]["name"], "rig_snapshot")
        self.assertIn("text/event-stream", req["headers"].get("accept", ""))

    def test_telemetry_ring(self):
        self.c.get("/api/rig/telemetry")
        status, _, body = self.c.get("/api/rig/telemetry")
        d = body["data"]
        self.assertEqual(len(d["telemetry"]["gpus"]), 8)
        self.assertEqual(d["telemetry"]["cpu"]["cores"], 192)
        self.assertGreaterEqual(len(d["history"]), 1)
        self.assertEqual(d["history"][-1]["src"], "gc")
        self.assertEqual(d["history"][-1]["gpus"][2], {"i": 2, "u": 100, "m": 2654, "p": 238, "c": 57})
        self.assertEqual(d["ring"]["cap"], 240)
        self.assertIn("gc", d["sources_used"])
        self.assertTrue(any(s.startswith("console history unavailable") for s in d["sources_used"]))

    def test_switch_status_route(self):
        status, _, body = self.c.get("/api/rig/switch")
        d = body["data"]
        self.assertEqual((d["target"], d["phase"], d["done"], d["ok"], d["idle"]), ("sim-lab-4", "done", True, True, None))
        self.assertEqual(len(d["log_tail"]), 3)
        self.assertEqual(d["log_tail"][-1], "[2026-10-07T06:44:27.907Z] SWITCH COMPLETE -> sim-lab-4 in 1s")
        self.assertEqual(len(d["log"]), 49)
        self.assertIsNone(d["outcome_pending"])

    def test_decisions(self):
        status, _, body = self.c.get("/api/rig/decisions")
        self.assertEqual(status, 200)
        self.assertIsNone(body["data"])
        self.assertEqual(body["meta"]["error"], "HTTP 404")
        self.assertEqual(self.gc.requests_for("GET", "/api/decisions")[-1]["headers"].get("x-gc-token"), TOKEN)
        with mock.patch.dict(os.environ, {"ACES_GC_TOKEN": ""}):
            # without a token the decisions are read through the SIMLAB console's /gc read proxy; here that console
            # is a closed port, so the reply is an honest absence with the reason, never a 5xx
            self.c.app.cache.invalidate("rig.decisions")
            status, _, body = self.c.get("/api/rig/decisions")
            self.assertEqual((status, body["data"], body["meta"]["ok"]), (200, None, False))
            self.assertTrue(body["meta"]["error"])

    def test_prepare_refusals_are_ledgered_once_per_window(self):
        n0 = len(ledger_entries())
        for _ in range(3):
            status, _, body = self.c.post("/api/rig/switch/prepare", {"mode": "sim-lab-8"})
            self.assertEqual((status, body["code"]), (409, "transition"))
            self.assertEqual(body["error"], "detected_mode is transition")
        refused = [e for e in ledger_entries()[n0:] if e["kind"] == "console.switch.refused"]
        self.assertEqual(len(refused), 1)
        p = refused[0]["payload"]
        self.assertEqual((p["to"], p["code"], p["peer"], p["repeats_suppressed_in_window"]), ("sim-lab-8", "transition", "127.0.0.1", 0))
        self.assertEqual(p["gates"]["detected_mode"], "transition")
        self.assertEqual(self.c.app.counters["suppressed_refusals"], 2)
        status, _, body = self.c.post("/api/rig/switch/prepare", {"mode": "no-such-mode"})
        self.assertEqual((status, body["code"], body["error"]), (400, "unknown-mode", "unknown mode no-such-mode"))
        status, _, body = self.c.post("/api/rig/switch/prepare", {"mode": "sim-lab-4"})
        self.assertEqual((status, body["code"], body["error"]), (409, "current-mode", "sim-lab-4 is the current mode"))
        kinds = [e["kind"] for e in ledger_entries()[n0:]]
        self.assertEqual(kinds, ["console.switch.refused"] * 3)
        self.assertEqual([e["payload"]["code"] for e in ledger_entries()[n0:]], ["transition", "unknown-mode", "current-mode"])
        self.assertNotIn(TOKEN, ledger_text())
        self.assertEqual(self.gc.requests_for("POST", "/api/switch"), [])

    def test_auto_enabled_and_switching_gates(self):
        n0 = len(ledger_entries())
        self.gc.apply_scenario("/api/status", "gc-status-auto-enabled.json")
        status, _, body = self.c.post("/api/rig/switch/prepare", {"mode": "sim-lab-8"})
        self.assertEqual((status, body["code"]), (409, "auto-enabled"))
        self.assertEqual(body["error"], "auto mode is on; switching by hand is refused")
        self.gc.apply_scenario("/api/status", "gc-status-switching.json")
        status, _, body = self.c.post("/api/rig/switch/prepare", {"mode": "sim-lab-8"})
        self.assertEqual((status, body["code"], body["error"]), (409, "switching", "switching in progress (sim-lab-8)"))
        self.assertEqual([e["payload"]["code"] for e in ledger_entries()[n0:]], ["auto-enabled", "switching"])
        self.assertEqual(self.gc.requests_for("POST", "/api/switch"), [])

    def test_prepare_confirm_lifecycle_and_outcome(self):
        n0 = len(ledger_entries())
        self.gc.apply_scenario("/api/status", "gc-status-settled.json")
        status, _, body = self.c.post("/api/rig/switch/prepare", {"mode": "sim-lab-8"})
        self.assertEqual(status, 200, body)
        d = body["data"]
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", d["nonce"]))
        self.assertEqual((d["expires_s"], d["from"], d["to"]), (30, "sim-lab-4", "sim-lab-8"))
        self.assertEqual(d["label"], "Sim Lab 8 (ALL GPUs simulation; overnight sweeps + surrogate training; no local LLM)")
        self.assertIsNone(d["pending"])
        self.assertIsNone(d["sim_pool"], "no sim pools without the SIMLAB console")
        self.assertEqual(d["gates"], {"detected_mode": "sim-lab-4", "switching": None, "auto_enabled": False,
                                      "switch_idle": None, "switch_done": True})
        self.assertEqual(d["warning"], "LLM reloads have 2400 s health gates; a full refill takes many minutes")
        nonce = d["nonce"]
        # a wrong confirm string never spends the nonce
        status, _, body = self.c.post("/api/rig/switch/confirm", {"mode": "sim-lab-8", "nonce": nonce, "confirm": "sim-lab-4"})
        self.assertEqual((status, body["code"]), (400, "confirm-mismatch"))
        # a nonce bound to another peer or mode is void
        status, _, body = self.c.post("/api/rig/switch/confirm", {"mode": "sim-lab-4", "nonce": nonce, "confirm": "sim-lab-4"})
        self.assertEqual((status, body["code"]), (410, "nonce"))
        self.assertEqual(self.c.app.counters["nonce_failures"], 1)
        # the real confirmation
        t_req = time.time()
        status, _, body = self.c.post("/api/rig/switch", {"mode": "sim-lab-8", "nonce": nonce, "confirm": "sim-lab-8"})
        self.assertEqual(status, 200, body)
        d = body["data"]
        self.assertTrue(d["started"])
        self.assertEqual(d["upstream_status"], 200)
        self.assertIsInstance(d["ledger"]["request_n"], int)
        self.assertEqual(d["ledger"]["result_n"], d["ledger"]["request_n"] + 1)
        posts = self.gc.requests_for("POST", "/api/switch")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"], {"mode": "sim-lab-8"})
        self.assertEqual(posts[0]["headers"].get("x-gc-token"), TOKEN)
        self.assertEqual(posts[0]["headers"].get("content-type"), "application/json")
        self.assertEqual(self.gc.requests_for("POST", "/api/auto"), [], "POST /api/auto is never called")
        entries = ledger_entries()[n0:]
        self.assertEqual([e["kind"] for e in entries], ["console.switch.request", "console.switch.result"])
        req_p, res_p = entries[0]["payload"], entries[1]["payload"]
        self.assertEqual(req_p["nonce_sha12"], hashlib.sha256(nonce.encode()).hexdigest()[:12])
        self.assertEqual((req_p["from"], req_p["to"], req_p["ui"], req_p["peer"]), ("sim-lab-4", "sim-lab-8", "aces-console 0.1.0", "127.0.0.1"))
        self.assertEqual(set(req_p["status_snapshot"]), {"mode", "detected_mode", "switching", "last_switch_ts", "auto_enabled"})
        self.assertEqual(set(req_p["switch_snapshot"]), {"idle", "done", "ok", "target"})
        self.assertEqual((res_p["to"], res_p["upstream_status"], res_p["ok"]), ("sim-lab-8", 200, True))
        self.assertLessEqual(len(res_p["reply_excerpt"]), 2048)
        self.assertNotIn(TOKEN, ledger_text())
        self.assertNotIn(nonce, ledger_text())
        ok, n, msg = ledger.verify()
        self.assertTrue(ok, msg)
        # single use
        status, _, body = self.c.post("/api/rig/switch/confirm", {"mode": "sim-lab-8", "nonce": nonce, "confirm": "sim-lab-8"})
        self.assertEqual((status, body["code"], body["error"]), (410, "nonce", "nonce expired or spent"))
        self.assertEqual(self.c.app.counters["nonce_failures"], 2)
        # outcome pending: the engine still reports the old switch (target sim-lab-4), nothing is written yet
        self.assertEqual(self.c.get("/api/health")[2]["data"]["switch_outcome_pending"]["to"], "sim-lab-8")
        self.c.app.hub.refresh("rig.switch")
        self.assertEqual([e["kind"] for e in ledger_entries()[n0:]], ["console.switch.request", "console.switch.result"])
        self.assertEqual(self.c.get("/api/rig/switch")[2]["data"]["outcome_pending"]["to"], "sim-lab-8")
        # the engine reports done for sim-lab-8, started after the request: the outcome is written once
        self.gc.apply_scenario("/api/switch/status", "gc-switch-status-sim-lab-8-done.json",
                               extra={"startedAt": int(t_req * 1000) + 10})
        self.c.app.hub.refresh("rig.switch")
        self.c.app.hub.refresh("rig.switch")
        entries = ledger_entries()[n0:]
        self.assertEqual([e["kind"] for e in entries], ["console.switch.request", "console.switch.result", "console.switch.outcome"])
        out = entries[2]["payload"]
        self.assertEqual((out["to"], out["ok"], out["phase"], out["reason"]), ("sim-lab-8", True, "done", None))
        self.assertEqual(len(out["log_tail"]), 3)
        self.assertIsNone(self.c.app.switch_outcome_pending)
        latest = self.c.app.broker.latest("switch.outcome")
        self.assertEqual((latest["payload"]["to"], latest["payload"]["pending"]), ("sim-lab-8", False))
        self.assertTrue(ledger.verify()[0])
        self.assertNotIn(TOKEN, self.c.log_text())

    def test_nonce_expires(self):
        self.gc.apply_scenario("/api/status", "gc-status-settled.json")
        rig.NONCE_TTL_S = 0.05
        status, _, body = self.c.post("/api/rig/switch/prepare", {"mode": "sim-lab-8"})
        self.assertEqual(status, 200, body)
        nonce = body["data"]["nonce"]
        time.sleep(0.12)
        status, _, body = self.c.post("/api/rig/switch/confirm", {"mode": "sim-lab-8", "nonce": nonce, "confirm": "sim-lab-8"})
        self.assertEqual((status, body["code"], body["error"]), (410, "nonce", "nonce expired or spent"))
        self.assertEqual(self.gc.requests_for("POST", "/api/switch"), [])

    def test_nonce_is_bound_to_last_switch_ts(self):
        body = self.gc.apply_scenario("/api/status", "gc-status-settled.json")
        status, _, out = self.c.post("/api/rig/switch/prepare", {"mode": "sim-lab-8"})
        self.assertEqual(status, 200, out)
        nonce = out["data"]["nonce"]
        self.gc.apply_scenario("/api/status", "gc-status-settled.json", extra={"last_switch_ts": body["last_switch_ts"] + 1})
        status, _, out = self.c.post("/api/rig/switch/confirm", {"mode": "sim-lab-8", "nonce": nonce, "confirm": "sim-lab-8"})
        self.assertEqual(status, 410)
        self.assertIn("last_switch_ts changed", out["error"])
        self.assertEqual(self.gc.requests_for("POST", "/api/switch"), [])

    def test_upstream_switch_failure_is_502_with_both_ledger_entries(self):
        n0 = len(ledger_entries())
        self.gc.apply_scenario("/api/status", "gc-status-settled.json")
        self.gc.switch_status = 500
        try:
            status, _, body = self.c.post("/api/rig/switch/prepare", {"mode": "sim-lab-8"})
            nonce = body["data"]["nonce"]
            status, _, body = self.c.post("/api/rig/switch/confirm", {"mode": "sim-lab-8", "nonce": nonce, "confirm": "sim-lab-8"})
            self.assertEqual((status, body["code"], body["status"]), (502, "upstream", 500))
            self.assertIn("HTTP 500", body["error"])
        finally:
            self.gc.switch_status = 200
        entries = ledger_entries()[n0:]
        self.assertEqual([e["kind"] for e in entries], ["console.switch.request", "console.switch.result"])
        self.assertEqual((entries[1]["payload"]["ok"], entries[1]["payload"]["upstream_status"]), (False, 500))
        self.assertIsNone(self.c.app.switch_outcome_pending)

    def test_fabric_absent_then_refreshed_then_rate_limited(self):
        status, _, body = self.c.get("/api/fabric")
        self.assertEqual(status, 200)
        d = body["data"]
        self.assertIsNone(d["manifest"])
        self.assertEqual(d["reason"], "no manifest yet; run aces fabric refresh")
        self.assertTrue(d["stale"])
        self.assertEqual(d["max_age_s"], 900)
        self.assertTrue(d["path"].endswith("fabric.json"))
        self.assertIn("sim-lab-4", d["modes_table"], "the shipped modes.json was copied into the scratch etc")
        n0 = len(ledger_entries())
        status, _, body = self.c.post("/api/fabric/refresh", {})
        self.assertEqual((status, body["data"]), (202, {"started": True}))
        self.assertTrue(wait_until(lambda: not self.c.get("/api/fabric")[2]["data"]["refresh"]["running"], 20.0))
        status, _, body = self.c.get("/api/fabric")
        d = body["data"]
        self.assertIsNotNone(d["manifest"])
        self.assertEqual(d["manifest"]["health"]["grid_control"], "ok")
        self.assertEqual(d["manifest"]["mode"]["name"], "sim-lab-4")
        self.assertFalse(d["stale"])
        self.assertIsNone(d["reason"])
        entries = ledger_entries()[n0:]
        self.assertEqual([e["kind"] for e in entries], ["console.fabric.refresh"])
        p = entries[0]["payload"]
        self.assertEqual(p["mode"], "sim-lab-4")
        self.assertEqual(set(p), {"duration_s", "health", "mode", "lanes", "notes", "manifest_path", "error"})
        self.assertIn("sim/pool", p["lanes"])
        status, hdrs, body = self.c.post("/api/fabric/refresh", {})
        self.assertEqual((status, body["code"]), (429, "rate-limited"))
        self.assertTrue(re.fullmatch(r"refreshed \d+ s ago", body["error"]), body["error"])
        self.assertIn("retry-after", hdrs)
        self.assertEqual(len(ledger_entries()), n0 + 1)
        stamp = self.c.app.broker.latest("fabric.manifest")
        self.assertFalse(stamp["payload"]["stale"])


class RestartOutcomeTest(unittest.TestCase):
    def test_result_without_outcome_gets_one_at_start(self):
        gc = FakeGC()
        c = Console(gc=gc.url, tag="restart")
        try:
            ledger.record("console.switch.result", {"to": "sim-lab-8", "upstream_status": 200, "ok": True,
                                                    "reply_excerpt": "", "elapsed_ms": 1, "error": None})
        finally:
            c.close()
        c2 = Console(gc=gc.url, state=c.state, tag="restart2")
        try:
            kinds = [e["kind"] for e in ledger_entries()]
            self.assertEqual(kinds[-2:], ["console.switch.result", "console.switch.outcome"])
            out = ledger_entries()[-1]["payload"]
            self.assertEqual((out["to"], out["ok"], out["reason"]), ("sim-lab-8", None, rig.RESTART_REASON))
            c3_entries = len(ledger_entries())
        finally:
            c2.close()
        c3 = Console(gc=gc.url, state=c.state, tag="restart3")
        try:
            self.assertEqual(len(ledger_entries()), c3_entries, "an outcome is written once")
        finally:
            c3.close()
            gc.close()


if __name__ == "__main__":
    unittest.main()
