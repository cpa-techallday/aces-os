"""ops.py: the in-thread ops agent's honesty contract. Every intent is answered from the console's own state
(jobs, $ACES_WORK records, the hub's cached upstream reads); what it cannot know it says; design requests fall
through to the playbook (ops.reply is None). The HTTP class drives the real routes on an in-process console."""
from __future__ import annotations

import base64
import types
import unittest

from aces_console import ops

from .support import Console
from .support_b import GOLDEN_RUNS, copy_golden_run, fresh_work, new_project


def stub_app() -> types.SimpleNamespace:
    """The least app the read intents need: a jobs state dict and no hub (reported as 'not running')."""
    return types.SimpleNamespace(state={})


class RouterTest(unittest.TestCase):
    def test_design_requests_fall_through(self):
        for text in ("draft a 6061 bracket for 200 N", "build me a duct for 30 m/s airflow",
                     "a titanium antenna housing 200x150x40 mm", "make it lighter"):
            self.assertIsNone(ops.reply(stub_app(), text, inbox_dir=None), text)

    def test_help(self):
        r = ops.reply(stub_app(), "what can you do", inbox_dir=None)
        self.assertEqual(r["card"]["title"], "What I can do")
        self.assertTrue(any("transfer" in line for line in r["card"]["lines"]))

    def test_unknown_question_gets_the_capability_list(self):
        r = ops.reply(stub_app(), "what is the meaning of the mesh convergence study?", inbox_dir=None)
        self.assertEqual(r["card"]["title"], "I can't do that yet")
        self.assertTrue(any("summarise" in line for line in r["card"]["lines"]))

    def test_status_without_jobs_or_hub_is_plain(self):
        r = ops.reply(stub_app(), "what's going on?", inbox_dir=None)
        self.assertEqual(r["card"]["title"], "What's going on")
        self.assertIn("Nothing is running on this desktop right now.", r["card"]["lines"])
        self.assertTrue(any("Rig: not known" in line for line in r["card"]["lines"]))

    def test_queue_explains_the_rule(self):
        r = ops.reply(stub_app(), "why is there a build queue?", inbox_dir=None)
        self.assertEqual(r["card"]["title"], "Why there is a queue")
        self.assertTrue(any("one at a time" in line for line in r["card"]["lines"]))
        self.assertTrue(any("slot is free" in line for line in r["card"]["lines"]))

    def test_transfer_without_names_shows_usage(self):
        r = ops.reply(stub_app(), "transfer the file please", inbox_dir=None)
        self.assertEqual(r["card"]["title"], "Transfer a file")
        self.assertTrue(any("which file and which rig experiment" in line for line in r["card"]["lines"]))


class RecordsIntentsTest(unittest.TestCase):
    """Find / result / summarise over a fresh work root with one real fixture run copied in byte-exact."""

    @classmethod
    def setUpClass(cls):
        cls.work = fresh_work("ops")
        new_project("cad-gold", "sovereign")
        new_project("aeris-radar", "personal")
        cls.run_dir = copy_golden_run("cad-gold", GOLDEN_RUNS["cad-gold"], cls.work)

    def _reply(self, text):
        return ops.reply(stub_app(), text, inbox_dir=None)

    def test_find_by_name_token(self):
        r = self._reply("find the project I started about aeris")
        self.assertEqual(r["card"]["title"], "Found 1 project")
        self.assertIn("aeris-radar", r["text"])
        self.assertEqual(r["card"]["links"][0]["href"], "?plate=projects&project=aeris-radar")

    def test_find_without_match_lists_what_exists(self):
        r = self._reply("find the project about hyperspace")
        self.assertIn("No project matches", r["text"])
        self.assertIn("cad-gold", r["text"])  # names the projects it can see

    def test_latest_result_is_the_fixture_run(self):
        r = self._reply("show the latest result")
        rid = GOLDEN_RUNS["cad-gold"]
        self.assertEqual(dict(r["card"]["kv"]).get("verdict"), "PASS")
        self.assertEqual(dict(r["card"]["kv"]).get("grade"), "verified-desktop")
        self.assertEqual(r["card"]["links"][0]["href"], f"?plate=evidence&project=cad-gold&run={rid}")

    def test_result_scoped_to_a_named_project(self):
        r = self._reply("show the latest result for aeris-radar")
        self.assertIn("no golden runs for project aeris-radar", r["text"])

    def test_summarise_reads_the_runs_own_numbers(self):
        r = self._reply("summarise the cad-gold run")
        kv = dict(r["card"]["kv"])
        self.assertEqual(kv.get("grade"), "verified-desktop")
        self.assertAlmostEqual(kv.get("winner.metrics.mass_kg"), 0.176986, places=5)
        self.assertAlmostEqual(kv.get("winner.metrics.vm_max_MPa"), 64.490431, places=4)
        self.assertIn("nothing is estimated", r["text"])
        self.assertEqual(r["card"]["links"][0]["label"], "open in Evidence")

    def test_summarise_without_runs_says_so(self):
        r = self._reply("summarise the aeris-radar run")
        self.assertIn("nothing to summarise", r["text"].lower())


class HttpTest(unittest.TestCase):
    """The real routes: an ops question lands in the thread as an ops card; a design request still makes a plan;
    the inbox route serves pulled files and refuses traversal."""

    @classmethod
    def setUpClass(cls):
        cls.c = Console(tag="ops-http")

    @classmethod
    def tearDownClass(cls):
        cls.c.close()

    def _message(self, text):
        st, _, body = self.c.post("/api/loop/message", {"text": text})
        self.assertEqual(st, 201, body)
        return body["data"]["thread"]

    def test_ops_question_appends_an_ops_card(self):
        th = self._message("what's going on?")
        m = th["messages"][-1]
        self.assertEqual(m["role"], "ops")
        self.assertEqual(m["card"]["ops"]["title"], "What's going on")

    def test_design_request_still_makes_a_plan(self):
        th = self._message("draft a 6061 bracket for 200 N")
        m = th["messages"][-1]
        self.assertEqual(m["role"], "loop")
        self.assertIn("plan", m["card"])

    def test_transfer_against_a_dead_rig_fails_plainly(self):
        th = self._message("pull report.html from exp-2026-test")
        m = th["messages"][-1]
        self.assertEqual(m["card"]["ops"]["title"], "Transfer a file")
        self.assertIn("pull failed", m["text"].lower())
        self.assertIn("Nothing was written", m["text"])

    def test_inbox_route_serves_and_guards(self):
        state = self.c.settings.state_dir
        inbox = state / "loop" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "exp-1--report.html").write_bytes(b"<html>pulled</html>")
        st, hdrs, body = self.c.get("/api/loop/inbox/exp-1--report.html")
        self.assertEqual(st, 200)
        self.assertEqual(body, b"<html>pulled</html>")
        self.assertIn('filename="report.html"', hdrs.get("content-disposition", ""))
        st, _, _ = self.c.get("/api/loop/inbox/..%2F..%2Fledger.jsonl")
        self.assertIn(st, (400, 404))  # decoded slashes miss the route; undecoded misses the name regex
        st, _, _ = self.c.get("/api/loop/inbox/no-such-file")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
