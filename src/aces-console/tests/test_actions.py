"""The actions that replace GRID CONTROL, the SIMLAB console and MLflow: every simlab-mcp tool from the console
(read tools run; write tools need their name typed and are ledgered as console.sim.tool with a hash of the arguments),
the MLflow browser (metric history, artifact listing, artifact bytes, against the 2026-10-11 captures), the links of
the replaced consoles (listed, never opened) and the CLI keys that now open this console."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from unittest import mock

from aces_console import cli

from .fakes.fake_mlflow import CAPTURED_RUN, FakeMlflow
from .fakes.fake_simlab_mcp import FakeSimlabMcp
from .support import ledger_entries
from .support_b import ConsoleB


class ActionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp = FakeSimlabMcp()
        cls.ml = FakeMlflow()
        cls.c = ConsoleB(simlab_mcp=cls.mcp.url, mlflow=cls.ml.url, tag="actions")

    @classmethod
    def tearDownClass(cls):
        cls.c.close()
        for f in (cls.mcp, cls.ml):
            f.server.shutdown()
            f.server.server_close()

    # --- SIMLAB actions ---------------------------------------------------------------------------------------------------

    def test_tools_are_listed_with_their_schemas_and_write_flags(self):
        status, _, body = self.c.get("/api/sim/tools")
        self.assertEqual(status, 200)
        tools = {t["name"]: t for t in body["data"]["tools"]}
        self.assertEqual(len(tools), 17)
        self.assertTrue(tools["sim_submit"]["write"] and tools["sim_sweep"]["write"] and tools["sim_cancel"]["write"])
        self.assertFalse(tools["sim_list_experiments"]["write"] or tools["sim_status"]["write"])
        self.assertEqual(tools["sim_sweep"]["input_schema"]["required"], ["spec_path", "grid"])

    def test_a_read_tool_runs_and_is_not_ledgered(self):
        n0 = len(ledger_entries())
        status, _, body = self.c.post("/api/sim/tool/sim_list_experiments", {"arguments": {}})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["data"]["write"])
        self.assertIn("aces-cal-air-03d", json.dumps(body["data"]["result"]))
        self.assertEqual(len(ledger_entries()), n0)

    def test_a_write_tool_needs_its_name_and_is_ledgered_with_a_hash(self):
        n0 = len(ledger_entries())
        status, _, body = self.c.post("/api/sim/tool/sim_cancel", {"arguments": {"experiment_id": "aces-x"}})
        self.assertEqual((status, body["code"]), (400, "confirm-mismatch"))
        self.assertEqual(self.mcp.calls("sim_cancel"), [], "nothing reached simlab-mcp without the typed name")
        status, _, body = self.c.post("/api/sim/tool/sim_cancel", {"arguments": {"experiment_id": "aces-x"}, "confirm": "sim_cancel"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["result"]["experiment_id"], "aces-x")
        rows = ledger_entries()[n0:]
        self.assertEqual([r["kind"] for r in rows], ["console.sim.tool"])
        p = rows[0]["payload"]
        self.assertEqual((p["tool"], p["arg_keys"], p["ok"], p["experiment"], len(p["args_sha256"])), ("sim_cancel", ["experiment_id"], True, "aces-x", 64))

    def test_unknown_tools_and_bad_arguments(self):
        self.assertEqual(self.c.post("/api/sim/tool/sim_explode", {"arguments": {}})[0], 404)
        self.assertEqual(self.c.post("/api/sim/tool/Bad-Name", {"arguments": {}})[0], 400)
        self.assertEqual(self.c.post("/api/sim/tool/sim_status", {"arguments": [1]})[0], 400)
        self.assertEqual(self.c.post("/api/sim/tool/sim_list_experiments", {"arguments": {}}, human=False)[0], 403)

    # --- MLflow browser ---------------------------------------------------------------------------------------------------

    def test_metric_history(self):
        status, _, body = self.c.get(f"/api/mlflow/metric-history?run_id={CAPTURED_RUN}&key=h_mm")
        self.assertEqual((status, body["data"]["n"], body["data"]["points"][0]["value"]), (200, 1, 2.0))
        self.assertEqual(self.c.get("/api/mlflow/metric-history?run_id=xyz&key=h_mm")[0], 400)

    def test_artifacts_and_bytes(self):
        status, _, body = self.c.get(f"/api/mlflow/artifacts?run_id={CAPTURED_RUN}")
        self.assertEqual(status, 200)
        self.assertIn("ccx.cvg", [f["path"] for f in body["data"]["files"]])
        status, hdrs, data = self.c.request("GET", f"/api/mlflow/artifact?run_id={CAPTURED_RUN}&path=ccx.cvg")
        self.assertEqual(status, 200)
        self.assertEqual(len(data), 274)
        self.assertEqual(hdrs.get("content-security-policy"), "sandbox")
        self.assertEqual(self.c.request("GET", f"/api/mlflow/artifact?run_id={CAPTURED_RUN}&path=../etc/passwd")[0], 400)
        self.assertEqual(self.c.request("GET", f"/api/mlflow/artifact?run_id={CAPTURED_RUN}&path=missing.bin")[0], 404)

    # --- the replaced consoles ----------------------------------------------------------------------------------------------

    def test_replaced_consoles_are_listed_but_not_openable(self):
        status, _, body = self.c.get("/api/system/links")
        links = {l["id"]: l for l in body["data"]["links"]}
        for lid in ("grid_control", "simlab_console", "mlflow"):
            self.assertFalse(links[lid]["openable"], lid)
            self.assertTrue(links[lid]["replaced_by"], lid)
        status, _, body = self.c.post("/api/system/open", {"id": "grid_control"})
        self.assertNotEqual(status, 200)

    def test_cli_keys_of_the_replaced_consoles_open_this_console(self):
        with mock.patch.object(cli, "cmd_open", return_value=0) as op, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["grid_control"]), 0)
            op.assert_called_with([], plate="ops")
            self.assertEqual(cli.main(["mlflow"]), 0)
            op.assert_called_with([], plate="pipeline")
        self.assertIn("replaced by the ACES console", err.getvalue())


if __name__ == "__main__":
    unittest.main()
