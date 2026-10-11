"""mc.py: Mission Control, the red-team VM, model staging and the auto-switch from the console, against tests/fakes/fake_mc
(stand-in Mission Control replies, captured red-team and staging files). Reads with the token go to GRID CONTROL with
x-gc-token; without it they go through the rig's /gc read proxy; every write needs the token, the POST guards, and is
ledgered with counts and hashes, never the text; the token never appears in a reply or the log."""
from __future__ import annotations

import base64
import json
import os
import secrets as pysecrets
import unittest
from unittest import mock

from .fakes.fake_mc import FakeMC
from .support import Console, ledger_entries

TOKEN = pysecrets.token_hex(16)


class McTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mc = FakeMC(token=TOKEN)
        cls.c = Console(gc=cls.mc.url, simlab_console=cls.mc.url, tag="mc")

    @classmethod
    def tearDownClass(cls):
        cls.c.close()
        cls.mc.close()

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"ACES_GC_TOKEN": TOKEN})
        self.env.start()
        for key in list(self.c.app.cache.keys()):
            self.c.app.cache.invalidate(key)

    def tearDown(self):
        self.env.stop()

    def no_token(self):
        return mock.patch.dict(os.environ, {"ACES_GC_TOKEN": ""})

    # --- reads -----------------------------------------------------------------------------------------------------------

    def test_reads_with_the_token_go_to_grid_control(self):
        status, _, body = self.c.get("/api/mc/agents")
        self.assertEqual(status, 200)
        d = body["data"]
        self.assertEqual((d["n"], d["enabled"], d["via"]), (2, 1, "token"))
        a = d["agents"][0]
        self.assertNotIn("systemPrompt", a)
        self.assertEqual((a["tools"], a["max_output"], a["system_prompt_chars"]), (2, 4096, 49))
        self.assertIsNone(d["agents"][1]["tools"], "toolsJson that is not JSON is reported as unreadable, not as 0")
        req = self.mc.requests_for("GET", "/api/mc/trpc/agents.list")[-1]
        self.assertEqual(req["headers"].get("x-gc-token"), TOKEN)
        status, _, body = self.c.get("/api/mc/runs")
        runs = body["data"]["runs"]
        self.assertEqual(runs[0]["usage"], {"prompt": 120, "completion": 30}, "token counts survive the encoder's masking")
        self.assertNotIn("promptTokens", runs[0])
        self.assertEqual(body["data"]["counts"], {"succeeded": 1, "running": 1})
        self.assertEqual(body["data"]["note"], "agent-reported by Mission Control, not compute truth")

    def test_details(self):
        status, _, body = self.c.get("/api/mc/run/11")
        self.assertEqual((status, body["data"]["steps_total"], body["data"]["usage"]["prompt"]), (200, 1, 120))
        status, _, body = self.c.get("/api/mc/session/3")
        self.assertEqual((status, body["data"]["messages_total"]), (200, 2))
        status, _, body = self.c.get("/api/mc/missions")
        m = body["data"]["missions"][0]
        self.assertEqual((m["steps"], m["steps_done"], m["steps_by_status"]), (2, 1, {"done": 1, "review": 1}))
        status, _, body = self.c.get("/api/mc/mission/7")
        self.assertEqual((status, sorted(body["data"]["runs"])), (200, ["11"]))
        self.assertEqual(self.c.get("/api/mc/run/abc")[0], 400)

    def test_reads_without_a_token_go_through_the_rig_read_proxy(self):
        with self.no_token():
            n0 = len(self.mc.requests)
            status, _, body = self.c.get("/api/mc/missions")
            self.assertEqual((status, body["data"]["via"]), (200, "proxy"))
            self.assertIn("read proxy", body["data"]["via_note"])
            new = self.mc.requests[n0:]
            self.assertTrue(all(r["path"].startswith("/gc/") for r in new), [r["path"] for r in new])
            self.assertTrue(all("x-gc-token" not in r["headers"] for r in new), "no token leaves this desktop")
            status, _, body = self.c.get("/api/rig/redteam")
            self.assertEqual((status, body["data"]["via"], body["data"]["tor_transproxy"]), (200, "proxy", True))
            status, _, body = self.c.get("/api/rig/decisions")
            self.assertEqual(status, 200)

    def test_downloads_and_bytes(self):
        status, _, body = self.c.get("/api/rig/downloads")
        self.assertEqual((status, body["data"]["total"], body["data"]["completed"]), (200, 23, 23))
        status, hdrs, data = self.c.request("GET", "/api/mc/artifact/5")
        self.assertEqual((status, data), (200, b"abc"))
        self.assertEqual(hdrs.get("content-security-policy"), "sandbox")
        self.assertTrue(hdrs.get("content-disposition", "").startswith("attachment"))

    # --- writes ----------------------------------------------------------------------------------------------------------

    def test_writes_without_a_token_are_refused_before_the_rig_and_not_ledgered(self):
        with self.no_token():
            n0, r0 = len(ledger_entries()), len(self.mc.requests)
            for path, body in (("/api/mc/session", {}), ("/api/mc/session/3/message", {"content": "hi"}),
                               ("/api/mc/mission", {"title": "t", "objective": "o"}), ("/api/rig/auto", {"enabled": True, "confirm": "enable auto-switch"}),
                               ("/api/rig/redteam/vm", {"action": "up", "confirm": "start mission vm"})):
                status, _, out = self.c.post(path, body)
                self.assertEqual((status, out["code"]), (503, "not-configured"), path)
            self.assertEqual(len(ledger_entries()), n0)
            self.assertEqual([r for r in self.mc.requests[r0:] if r["method"] == "POST"], [])

    def test_post_guards(self):
        status, _, out = self.c.post("/api/mc/session", {}, human=False)
        self.assertEqual((status, out["code"]), (403, "refused"))

    def test_chat_and_missions_are_ledgered_without_their_text(self):
        n0 = len(ledger_entries())
        status, _, out = self.c.post("/api/mc/session", {"title": "a test session"})
        self.assertEqual((status, out["data"]["result"]["id"]), (200, 4))
        status, _, out = self.c.post("/api/mc/session/3/message", {"content": "a private sentence", "uploads": [9]})
        self.assertEqual(status, 200)
        sent = self.mc.requests_for("POST", "/api/mc/trpc/chat.sendMessage")[-1]
        self.assertEqual(sent["body"], {"json": {"sessionId": 3, "content": "a private sentence", "uploads": [9]}})
        status, _, out = self.c.post("/api/mc/mission", {"title": "M", "objective": "do it", "launch": True,
                                                          "files": [{"id": 9, "filename": "a.txt"}]})
        self.assertEqual((status, out["data"]["id"], out["data"]["launched"]), (200, 8, True))
        created = self.mc.requests_for("POST", "/api/mc/trpc/missions.create")[-1]["body"]["json"]
        self.assertTrue(created["objective"].endswith("[context files]\n- #9 a.txt (fetch: /api/uploads/9)"))
        self.assertEqual(self.mc.requests_for("POST", "/api/mc/trpc/missions.launch")[-1]["body"], {"json": {"id": 8}})
        self.assertEqual(self.c.post("/api/mc/mission/7/retryFailed", {})[0], 200)
        self.assertEqual(self.c.post("/api/mc/mission/7/explode", {})[0], 400)
        self.assertEqual(self.c.post("/api/mc/item/71/approve", {})[0], 200)
        rows = ledger_entries()[n0:]
        self.assertEqual([r["kind"] for r in rows], ["console.mc.session", "console.mc.message", "console.mc.mission",
                                                     "console.mc.mission", "console.mc.approve"])
        text = json.dumps(rows)
        self.assertNotIn("a private sentence", text)
        self.assertNotIn("do it", text)
        msg = rows[1]["payload"]
        self.assertEqual((msg["chars"], len(msg["sha256"]), msg["uploads"], msg["ok"]), (18, 64, 1, True))

    def test_upload(self):
        data = b"\x00\x01binary"
        status, _, out = self.c.post("/api/mc/upload", {"filename": "a b.bin", "mime": "application/octet-stream",
                                                         "data_b64": base64.b64encode(data).decode("ascii")})
        self.assertEqual((status, out["data"]["result"]["id"]), (200, 9))
        req = self.mc.requests_for("POST", "/api/mc/uploads")[-1]
        self.assertEqual(req["body"]["bytes"], len(data))
        self.assertEqual(req["headers"]["x-filename"], "a%20b.bin")
        self.assertEqual(self.c.post("/api/mc/upload", {"filename": "x", "data_b64": "!!"})[0], 400)
        self.assertEqual(self.c.post("/api/mc/upload", {"filename": "../x", "data_b64": "AA=="})[0], 400)

    def test_redteam_and_auto_need_the_typed_words(self):
        n0 = len(ledger_entries())
        status, _, out = self.c.post("/api/rig/redteam/vm", {"action": "down", "confirm": "end mission"})
        self.assertEqual((status, out["code"]), (400, "confirm-mismatch"))
        status, _, out = self.c.post("/api/rig/redteam/vm", {"action": "down", "confirm": "END MISSION"})
        self.assertEqual(status, 200)
        self.assertEqual(self.mc.requests_for("POST", "/api/redteam/vm")[-1]["body"], {"action": "down"})
        status, _, out = self.c.post("/api/rig/auto", {"enabled": True, "confirm": "yes"})
        self.assertEqual((status, out["code"]), (400, "confirm-mismatch"))
        status, _, out = self.c.post("/api/rig/auto", {"enabled": True, "confirm": "enable auto-switch"})
        self.assertEqual(status, 200)
        req = self.mc.requests_for("POST", "/api/auto")[-1]
        self.assertEqual((req["body"], req["headers"].get("x-gc-token")), ({"enabled": True}, TOKEN))
        self.assertEqual([r["kind"] for r in ledger_entries()[n0:]], ["console.redteam.vm", "console.auto"])

    def test_the_token_never_leaves_the_backend(self):
        for path in ("/api/mc/agents", "/api/mc/runs", "/api/mc/missions", "/api/rig/redteam", "/api/health", "/api/config"):
            _, _, body = self.c.get(path)
            self.assertNotIn(TOKEN, json.dumps(body), path)
        self.assertNotIn(TOKEN, self.c.log_text())
        self.assertNotIn(TOKEN, json.dumps(ledger_entries()))


if __name__ == "__main__":
    unittest.main()
