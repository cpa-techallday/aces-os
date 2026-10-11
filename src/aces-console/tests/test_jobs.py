"""jobs.py: golden runs and CAD actions started from the console, against tests/fakes/fake_golden.py (reached through
ACES_GOLDEN_CMD, replaying the real VM acceptance run's result) and tests/fakes/fake_aces_cad.py (on PATH, replaying
the real aces-cad records). Starts are human POSTs, confirmed by the project name, ledgered; one golden run at a time;
cancel ends a run; refusals (bad options, a missing spec, a missing tool) never start anything."""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from . import CAD_RECORDS
from .support import ledger_entries
from .support_b import CAD_PROJECT, CAD_RUN, ConsoleB, build_store

FAKES = Path(__file__).resolve().parent / "fakes"


def wait_job(c, job_id: str, timeout: float = 30.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        status, _, body = c.get(f"/api/job/{job_id}")
        if status == 200 and body["data"]["status"] != "running":
            return body["data"]
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} still running after {timeout} s")


class JobsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (CAD_RECORDS / "records" / "golden-handoff.json").is_file():
            raise unittest.SkipTest(f"the VM acceptance records are not in this tree ({CAD_RECORDS})")
        cls.c = ConsoleB(tag="jobs")
        cls.work = build_store("jobs")
        cls.bin = Path(tempfile.mkdtemp(prefix="aces-cad-fake-"))
        cls.log = cls.bin / "argv.jsonl"
        if os.name == "nt":
            (cls.bin / "aces-cad.cmd").write_text(f'@"{sys.executable}" "{FAKES / "fake_aces_cad.py"}" %*\r\n', encoding="utf-8")
        else:
            p = cls.bin / "aces-cad"
            p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES / "fake_aces_cad.py"}" "$@"\n', encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        cls.env = mock.patch.dict(os.environ, {
            "ACES_GOLDEN_CMD": json.dumps([sys.executable, str(FAKES / "fake_golden.py")]),
            "PATH": str(cls.bin) + os.pathsep + os.environ.get("PATH", ""), "FAKE_ACES_CAD_LOG": str(cls.log)})
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.c.close()
        shutil.rmtree(cls.bin, ignore_errors=True)

    def argv_calls(self) -> list[list[str]]:
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()] if self.log.is_file() else []

    # --- golden -----------------------------------------------------------------------------------------------------------

    def test_golden_demo_run_streams_and_returns_the_result(self):
        n0 = len(ledger_entries())
        status, _, body = self.c.post("/api/golden/run", {"demo": "root-mount", "confirm": "golden-demo", "options": {"n": 10, "device": "cpu", "lane": "none"}})
        self.assertEqual(status, 202, body)
        job = wait_job(self.c, body["data"]["job"])
        self.assertEqual((job["status"], job["exit"], job["result_run_id"]), ("done", 0, "20261010T102210Z-bc7391"))
        status, _, full = self.c.get(f"/api/job/{job['id']}")
        texts = [l["text"] for l in full["data"]["line_items"]]
        self.assertIn("fake_golden: stand-in progress 3/3 (verify)", texts)
        self.assertIn("-n", full["data"]["argv"])
        self.assertEqual(full["data"]["argv"][full["data"]["argv"].index("--lane") + 1], "none", "lane none keeps the run off the rig")
        rows = [r for r in ledger_entries()[n0:] if r["kind"] == "console.golden.run"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["payload"]["project"], rows[0]["payload"]["demo"]), ("golden-demo", "root-mount"))
        status, _, body = self.c.get("/api/jobs")
        self.assertIn(job["id"], [j["id"] for j in body["data"]["jobs"]])

    def test_golden_refusals_start_nothing(self):
        n0 = len(ledger_entries())
        cases = [({"demo": "root-mount", "confirm": "nope"}, 400, "confirm-mismatch"),
                 ({"demo": "rocket", "confirm": "golden-demo"}, 400, "bad-request"),
                 ({"demo": "root-mount", "confirm": "golden-demo", "options": {"n": 1}}, 400, "bad-request"),
                 ({"demo": "root-mount", "confirm": "golden-demo", "options": {"shell": "rm"}}, 400, "bad-request"),
                 ({"demo": "root-mount", "confirm": "golden-demo", "options": {"lane": "Bad Lane;"}}, 400, "bad-request"),
                 ({"project": "no-such", "confirm": "no-such"}, 404, "not-found"),
                 ({"project": CAD_PROJECT, "confirm": CAD_PROJECT}, 409, "no-spec"),
                 ({"project": CAD_PROJECT, "step": "../x.step", "confirm": CAD_PROJECT}, 400, "bad-request")]
        for body, status, code in cases:
            st, _, out = self.c.post("/api/golden/run", body)
            self.assertEqual((st, out["code"]), (status, code), body)
        self.assertEqual(len(ledger_entries()), n0)
        self.assertEqual(self.c.post("/api/golden/run", {"demo": "root-mount", "confirm": "golden-demo"}, human=False)[0], 403)

    def test_one_golden_run_at_a_time_and_cancel(self):
        with mock.patch.dict(os.environ, {"FAKE_GOLDEN_SLEEP": "20"}):
            status, _, body = self.c.post("/api/golden/run", {"demo": "bone", "confirm": "golden-demo"})
            self.assertEqual(status, 202)
            jid = body["data"]["job"]
            status, _, out = self.c.post("/api/golden/run", {"demo": "bone", "confirm": "golden-demo"})
            self.assertEqual((status, out["code"]), (409, "busy"))
            status, _, out = self.c.post(f"/api/job/{jid}/cancel", {})
            self.assertEqual(status, 200)
            job = wait_job(self.c, jid)
            self.assertEqual(job["status"], "cancelled")
            self.assertIn("console.job.cancel", [r["kind"] for r in ledger_entries()])

    def test_golden_init_from_a_step(self):
        step = base64.b64encode(b"ISO-10303-21;\nEND-ISO-10303-21;\n").decode("ascii")
        self.assertEqual(self.c.post("/api/golden/init", {"name": "x1", "class": "personal", "step": {"filename": "a.stl", "data_b64": step}})[0], 400)
        self.assertEqual(self.c.post("/api/golden/init", {"name": CAD_PROJECT, "class": "personal", "step": {"filename": "a.step", "data_b64": step}})[0], 409)
        status, _, body = self.c.post("/api/golden/init", {"name": "from-step", "class": "personal", "step": {"filename": "part.step", "data_b64": step}})
        self.assertEqual(status, 200, body)
        self.assertIn("console.golden.init", [r["kind"] for r in ledger_entries()])

    # --- CAD ----------------------------------------------------------------------------------------------------------------

    def run_cad(self, body: dict) -> dict:
        status, _, out = self.c.post("/api/cad/action", body)
        self.assertEqual(status, 202, out)
        return wait_job(self.c, out["data"]["job"])

    def test_every_cad_verb(self):
        n0 = len(ledger_entries())
        job = self.run_cad({"project": CAD_PROJECT, "action": "inspect", "run": CAD_RUN})
        self.assertEqual(job["status"], "done")
        self.assertEqual(self.argv_calls()[-1], ["inspect", "--json", "--project", CAD_PROJECT, CAD_RUN])
        self.run_cad({"project": CAD_PROJECT, "action": "render", "run": "latest", "options": {"size": "small"}})
        self.assertEqual(self.argv_calls()[-1], ["render", "--json", "--project", CAD_PROJECT, "--size", "small", "latest"])
        self.run_cad({"project": CAD_PROJECT, "action": "drawing", "run": CAD_RUN, "options": {"projection": "first-angle"}})
        self.assertIn("first-angle", self.argv_calls()[-1])
        self.run_cad({"project": CAD_PROJECT, "action": "pull", "run": CAD_RUN, "options": {"name": "plate"}})
        self.assertEqual(self.argv_calls()[-1][-3:], ["--name", "plate", CAD_RUN])
        job = self.run_cad({"project": CAD_PROJECT, "action": "rerun", "run": CAD_RUN, "options": {"params": {"thickness": 6, "label": "v2"}, "render": True}})
        self.assertEqual(self.argv_calls()[-1], ["run", "--json", "--project", CAD_PROJECT, "--from", CAD_RUN,
                                                 "--param", "thickness=6", "--param", 'label="v2"', "--render"])
        self.assertIsNotNone(job["result"])
        kinds = [r["kind"] for r in ledger_entries()[n0:]]
        self.assertEqual(kinds, ["console.cad.inspect", "console.cad.render", "console.cad.drawing", "console.cad.pull", "console.cad.rerun"])

    def test_prune_needs_the_words_and_cad_refusals(self):
        status, _, out = self.c.post("/api/cad/action", {"project": CAD_PROJECT, "action": "prune", "options": {"keep": 5}})
        self.assertEqual((status, out["code"]), (400, "confirm-mismatch"))
        job = self.run_cad({"project": CAD_PROJECT, "action": "prune", "options": {"keep": 5}, "confirm": f"prune {CAD_PROJECT}"})
        self.assertEqual(self.argv_calls()[-1], ["prune", "--json", "--project", CAD_PROJECT, "--keep", "5"])
        self.assertEqual(job["status"], "done")
        for body in ({"project": CAD_PROJECT, "action": "run", "run": CAD_RUN},
                     {"project": CAD_PROJECT, "action": "rerun", "run": "latest"},
                     {"project": CAD_PROJECT, "action": "rerun", "run": CAD_RUN, "options": {"params": {"x; rm": 1}}},
                     {"project": CAD_PROJECT, "action": "inspect", "run": "../../etc"}):
            self.assertEqual(self.c.post("/api/cad/action", body)[0], 400, body)
        self.assertEqual(self.c.post("/api/cad/action", {"project": "no-such", "action": "inspect"})[0], 404)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if p != str(self.bin))}):
            if shutil.which("aces-cad") is None:
                status, _, out = self.c.post("/api/cad/action", {"project": CAD_PROJECT, "action": "inspect"})
                self.assertEqual((status, out["code"]), (503, "not-installed"))


if __name__ == "__main__":
    unittest.main()
