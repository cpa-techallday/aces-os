"""system.py: the link allow-list and its reach classification (127.0.0.1:3100 reported by the rig -> rig-loopback, not
openable; 192.168.1.65:3000 -> lan), open (404, 403, the ledgered launch through a recorder, never the URL in the
ledger), stop (400 without the exact confirm, the ledger entry and the exit), doctor verbatim (run with --offline so
no probe leaves the loopback), packages and entries on this host, the policy, and the fixtures-mode refusals."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest import mock

from aces_console import system

from . import FIXTURES
from .fakes.fake_console import FakeConsole
from .fakes.fake_gc import FakeGC
from .support import ledger_entries, ledger_text, wait_until
from .support_b import ConsoleB


class SystemRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gc = FakeGC()
        cls.fc = FakeConsole()
        cls.c = ConsoleB(gc=cls.gc.url, simlab_console=cls.fc.url, tag="system")
        cls.opened: list[tuple[str, str]] = []
        system._state(cls.c.app).launcher = lambda kind, target: (cls.opened.append((kind, target)) or (True, "recorder"))

    @classmethod
    def tearDownClass(cls):
        cls.c.close()
        cls.gc.close()
        cls.fc.close()

    def test_links_classification(self):
        status, _, body = self.c.get("/api/system/links")
        self.assertEqual(status, 200)
        links = {l["id"]: l for l in body["data"]["links"]}
        mc = links["mc_console"]
        self.assertEqual((mc["url"], mc["reach"], mc["openable"], mc["note"]),
                         ("http://127.0.0.1:3100", "rig-loopback", False, "rig-local address; not reachable from this desktop"))
        gw = links["gridwatch"]
        self.assertEqual((gw["url"], gw["reach"], gw["openable"], gw["note"]), ("http://192.168.1.65:3000", "lan", True, None))
        ml = links["mlflow_console"]
        self.assertEqual((ml["url"], ml["reach"], ml["openable"]), ("http://127.0.0.1:5000", "rig-loopback", False))
        self.assertEqual((links["grid_control"]["url"], links["grid_control"]["reach"], links["grid_control"]["openable"]),
                         (self.gc.url, "desktop-loopback", False), "GRID CONTROL is replaced by this console: listed, not opened")
        self.assertEqual(links["grid_control"]["replaced_by"], "Fabric, Ops and Agents plates")
        self.assertEqual(links["simlab_console"]["url"], self.fc.url)
        self.assertEqual((links["webodm"]["url"], links["webodm"]["reach"]), ("http://127.0.0.1:8090", "desktop-loopback"))
        self.assertEqual(system.reach_of("http://host.docker.internal:3200", True), ("rig-loopback", False, system.RIG_LOCAL_NOTE))
        self.assertEqual(system.reach_of("http://192.168.1.65:3000", True), ("lan", True, None))
        self.assertEqual(system.reach_of("http://127.0.0.1:8090", False), ("desktop-loopback", True, None))
        self.assertEqual(system.reach_of(None, True), (None, False, "not reported"))
        self.assertEqual(system.reach_of("ftp://x/y", False), (None, False, "not an http(s) URL"))

    def test_open(self):
        before = len(ledger_entries())
        status, _, body = self.c.post("/api/system/open", {"id": "nope"})
        self.assertEqual((status, body["code"]), (404, "not-found"))
        status, _, body = self.c.post("/api/system/open", {"id": "mc_console"})
        self.assertEqual((status, body["code"]), (403, "not-openable"))
        self.assertEqual(body["error"], "rig-local address; not reachable from this desktop")
        status, _, body = self.c.post("/api/system/open", {})
        self.assertEqual(status, 400)
        status, _, body = self.c.post("/api/system/open", {"id": "gridwatch"}, human=False)
        self.assertEqual((status, body["code"]), (403, "refused"))
        self.assertEqual(len(ledger_entries()), before, "guard refusals are never ledgered")
        self.assertEqual(self.opened, [])
        status, _, body = self.c.post("/api/system/open", {"id": "gridwatch"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["data"]["opened"], body["data"]["id"], body["data"]["target_kind"], body["data"]["via"]),
                         (True, "gridwatch", "url", "recorder"))
        self.assertEqual(self.opened, [("url", "http://192.168.1.65:3000")])
        es = ledger_entries()
        self.assertEqual(len(es), before + 1)
        self.assertEqual(es[-1]["kind"], "console.system.open")
        self.assertEqual({k: es[-1]["payload"][k] for k in ("id", "target_kind", "ok", "via")},
                         {"id": "gridwatch", "target_kind": "url", "ok": True, "via": "recorder"})
        self.assertNotIn("192.168.1.65:3000", ledger_text(), "the ledger never carries the URL")
        self.assertEqual(body["data"]["ledger_n"], before + 1)

    def test_stop(self):
        status, _, body = self.c.post("/api/system/stop", {"confirm": "nope"})
        self.assertEqual((status, body["code"]), (400, "bad-confirm"))
        status, _, body = self.c.post("/api/system/stop", {})
        self.assertEqual(status, 400)
        c = ConsoleB(tag="system-stop")
        try:
            before = len(ledger_entries())
            status, _, body = c.post("/api/system/stop", {"confirm": "stop"})
            self.assertEqual(status, 200, body)
            self.assertEqual((body["data"]["stopping"], body["data"]["ledger_n"]), (True, before + 1))
            es = ledger_entries()
            self.assertEqual(es[-1]["kind"], "console.server.stop")
            self.assertEqual(set(es[-1]["payload"]), {"peer", "clients", "uptime_s", "user_agent", "ui", "pid"})
            self.assertTrue(c.app.stopping)

            def gone():
                try:
                    c.get("/api/health")
                    return False
                except OSError:
                    return True

            self.assertTrue(wait_until(gone, timeout=10), "the server stops answering")
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001 (already shut down)
                pass

    def test_doctor_verbatim(self):
        status, _, body = self.c.get("/api/doctor")
        self.assertEqual(status, 200)
        self.assertEqual((body["data"]["text"], body["data"]["note"], body["data"]["running"]),
                         (None, "doctor has not been run in this session", False))
        with mock.patch.object(system, "DOCTOR_ARGS", ["doctor", "--offline"]):
            status, _, body = self.c.post("/api/doctor/run", {})
            self.assertEqual(status, 202, body)
            self.assertEqual((body["data"]["started"], body["data"]["command"]), (True, ["aces", "doctor", "--offline"]))
            st = system._state(self.c.app)
            self.assertTrue(wait_until(lambda: not st.doctor["running"], timeout=90), "doctor finished")
            cmd = system.aces_command(["doctor", "--offline"])
        self.assertIsNotNone(cmd)
        direct = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
        self.c.app.hub.refresh("doctor")
        status, _, body = self.c.get("/api/doctor")
        d = body["data"]
        self.assertIsNotNone(d["text"])
        self.assertIsNotNone(d["ran_at"])
        self.assertEqual(d["exit"], direct.returncode)
        names = [ln.split()[0] for ln in d["text"].splitlines() if ln.strip()]
        for row in ("python", "config", "policy", "ledger", "fabric", "server", "docker", "gpu", "omarchy", "agent", "skills", "work", "golden"):
            self.assertIn(row, names, d["text"])
        # docker and gpu rows carry probe timings, the fabric row the manifest age in seconds: two runs a second apart differ there
        varying = ("docker", "gpu", "fabric")
        route_lines = [ln for ln in d["text"].splitlines() if ln.split()[:1] and ln.split()[0] not in varying]
        direct_lines = [ln for ln in (direct.stdout + direct.stderr).splitlines() if ln.split()[:1] and ln.split()[0] not in varying]
        self.assertEqual(route_lines, direct_lines, "the route shows aces doctor's own text")
        self.assertEqual([ln.split()[:2] for ln in d["text"].splitlines() if ln.strip()],
                         [ln.split()[:2] for ln in (direct.stdout + direct.stderr).splitlines() if ln.strip()], "same rows, same statuses")
        self.assertIn("offline", d["text"])
        with st.lock:
            st.doctor["running"] = True
        try:
            status, _, body = self.c.post("/api/doctor/run", {})
            self.assertEqual((status, body["code"]), (409, "busy"))
        finally:
            with st.lock:
                st.doctor["running"] = False

    def test_packages_entries_policy(self):
        status, _, body = self.c.get("/api/system/packages")
        self.assertEqual(status, 200)
        d = body["data"]
        self.assertIsInstance(d["packages"], list)
        self.assertTrue(d["queried_utc"].endswith("Z"))
        if system._pacman() is None:
            self.assertEqual(d["packages"], [])
            self.assertIn("pacman not found on this host", d["note"])
        else:
            self.assertTrue(all(set(p) == {"name", "version"} for p in d["packages"]))
        status, _, body = self.c.get("/api/system/entries")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["data"]["entries"], list)
        if not system.APPLICATIONS_DIR.is_dir() or system._pacman() is None:
            self.assertEqual(body["data"]["entries"], [])
            self.assertIsNotNone(body["data"]["note"])
        status, _, body = self.c.get("/api/policy")
        self.assertEqual(status, 200)
        d = body["data"]
        self.assertEqual(d["policy"]["mode"], "trial")
        self.assertEqual(d["policy"]["classes"], ["personal", "open", "commercial", "sovereign", "controlled"])
        self.assertEqual(d["policy"]["overrides"], [])
        self.assertEqual((d["override"]["present"], d["override"]["mode"]), (False, None))
        self.assertTrue(d["override"]["path"].endswith("policy.json"))
        self.assertEqual(system._parse_desktop.__name__, "_parse_desktop")

    def test_aces_command_and_run_capped(self):
        cmd = system.aces_command(["ledger", "verify", "--json"])
        self.assertIsNotNone(cmd)
        res = system.run_capped(cmd, 60.0)
        self.assertEqual(res["exit"], 0, res)
        doc = json.loads(res["stdout"].strip().splitlines()[-1])
        self.assertTrue(doc["ok"])
        self.assertFalse(res["timed_out"])
        res = system.run_capped(["/no/such/program-xyz"], 5.0)
        self.assertEqual(res["exit"], 127)
        self.assertTrue(res["error"].startswith("not found"))


class FixturesModeBTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = ConsoleB(fixtures=FIXTURES, gc="http://127.0.0.1:1", rigstate="http://127.0.0.1:2", simlab_console="http://127.0.0.1:3",
                         simlab_mcp="http://127.0.0.1:4/mcp", mlflow="http://127.0.0.1:5", tag="fixtures-b")

    @classmethod
    def tearDownClass(cls):
        cls.c.close()

    def test_replays_and_refusals(self):
        status, _, body = self.c.get("/api/sim/registry")
        self.assertEqual((status, body["data"]["n"], body["meta"]["fixture"]), (200, 14, "FIXTURE 2026-10-11"))
        status, _, body = self.c.get("/api/sim/whoami")
        self.assertEqual((status, body["data"]["principal"], body["data"]["server"]["name"]), (200, "anonymous", "aces-console fixtures replay"))
        status, _, body = self.c.get("/api/sim/status/aces-cal-air-03d")
        self.assertIsNone(body["data"])
        self.assertIn("not captured in fixtures", body["meta"]["error"])
        status, _, body = self.c.get("/api/ledger/simlab")
        self.assertEqual((status, body["data"]["n"]), (200, 13))
        status, _, body = self.c.get("/api/mlflow/run/97cfbb614b98494b8d3aaa63e46cfd9e")
        self.assertEqual((status, body["data"]["run"]["info"]["experiment_id"]), (200, "11"))
        status, _, body = self.c.post("/api/doctor/run", {})
        self.assertEqual((status, body["code"]), (503, "fixture"))
        status, _, body = self.c.post("/api/system/open", {"id": "gridwatch"})
        self.assertEqual((status, body["code"]), (503, "fixture"))
        self.assertEqual(os.environ.get("ACES_CONSOLE_ALLOW_FIXTURES"), "1")


if __name__ == "__main__":
    unittest.main()
