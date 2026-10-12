"""loop.py: the golden loop's honesty contract for CFD — velocity parsing (m/s, km/h, mph), a cfd plan item that is
wired only when golden's flow block is fully known and inside the incompressible gate, spec drafts that carry the
flow block (so confirm-to-run dispatches golden's cfd stage), draft-time refusal with golden's own validator message,
and byte-identical behaviour for structures-only requests. The HTTP class drives the real routes on an in-process
console; the unit classes call parse_request / build_plan / spec_draft directly."""
from __future__ import annotations

import unittest

from aces_console import loop

from .support import Console

G = 9.80665
# golden's Flow._incompressible message (aces-engineering golden/spec.py), mirrored byte-identically in loop.py
AIR_150_REFUSAL = ("flow.velocity_mps: 150 m/s in air is Mach ~0.44; ac-navier-stokes is incompressible, so a drag "
                   "number past ~100 m/s (Mach 0.3) would be fiction — this tier refuses it")


def cfd_item(plan: dict) -> dict | None:
    return next((i for i in plan["items"] if i["physics"] == ["cfd"]), None)


class VelocityParseTest(unittest.TestCase):
    def test_metres_per_second(self):
        c = loop.parse_request("bracket in 30 m/s airflow")["constraints"]
        self.assertEqual(c["velocity_ms"], 30.0)
        self.assertEqual(c["velocity_unit"], "m/s")
        self.assertEqual(c["velocity_raw"], 30.0)

    def test_kilometres_per_hour(self):
        c = loop.parse_request("bracket in 120 km/h crosswind")["constraints"]
        self.assertAlmostEqual(c["velocity_ms"], 120.0 / 3.6)
        self.assertEqual(c["velocity_unit"], "km/h")
        self.assertEqual(c["velocity_raw"], 120.0)

    def test_miles_per_hour(self):
        c = loop.parse_request("mount at 65 mph")["constraints"]
        self.assertAlmostEqual(c["velocity_ms"], 65.0 * 0.44704)
        self.assertEqual(c["velocity_unit"], "mph")
        self.assertEqual(c["velocity_raw"], 65.0)

    def test_no_velocity_no_velocity_keys(self):
        c = loop.parse_request("bracket drag")["constraints"]
        for k in ("velocity_ms", "velocity_unit", "velocity_raw"):
            self.assertNotIn(k, c)

    def test_water_medium_named(self):
        c = loop.parse_request("bracket in water at 2 m/s")["constraints"]
        self.assertEqual(c["flow_medium"], "water")
        self.assertNotIn("flow_medium", loop.parse_request("bracket at 30 m/s")["constraints"],
                         "air is the default; it is not recorded")


class CfdPlanWiringTest(unittest.TestCase):
    def test_wired_when_a_speed_is_known(self):
        plan = loop.build_plan(loop.parse_request("bracket holds 5 kg in 30 m/s airflow"))
        item = cfd_item(plan)
        self.assertIsNotNone(item)
        self.assertTrue(item["wired"])
        self.assertEqual(item["flow"]["velocity_mps"], 30.0)
        self.assertEqual(item["flow"]["medium"], "air")
        self.assertIn("flow: block → templated case → rig dispatch", item["reason"])
        self.assertNotIn("don't carry flow blocks", item["reason"])

    def test_tracked_when_no_speed_is_named(self):
        plan = loop.build_plan(loop.parse_request("bracket drag"))
        item = cfd_item(plan)
        self.assertIsNotNone(item)
        self.assertFalse(item["wired"])
        self.assertIn("name a speed to run CFD", item["reason"])
        self.assertNotIn("flow", item)

    def test_tracked_when_air_is_over_100_mps(self):
        plan = loop.build_plan(loop.parse_request("bracket at 150 m/s"))
        item = cfd_item(plan)
        self.assertFalse(item["wired"])
        self.assertIn("incompressible tier refuses air over 100 m/s", item["reason"])
        self.assertIn(AIR_150_REFUSAL, item["reason"])

    def test_wired_when_water_is_over_100_mps(self):
        plan = loop.build_plan(loop.parse_request("bracket in water at 150 m/s"))
        item = cfd_item(plan)
        self.assertTrue(item["wired"], "the incompressible gate is air-only")
        self.assertEqual(item["flow"]["medium"], "water")

    def test_wired_at_the_100_mps_boundary(self):
        plan = loop.build_plan(loop.parse_request("bracket at 100 m/s"))
        self.assertTrue(cfd_item(plan)["wired"], "100 m/s is the refusal boundary, not over it")


class CfdDraftTest(unittest.TestCase):
    def test_wired_draft_carries_the_flow_block(self):
        req = loop.parse_request("aluminium bracket holds 5 kg in 30 m/s airflow")
        item = cfd_item(loop.build_plan(req))
        draft = loop.spec_draft(req, item, "abc12345-bracket-cfd")
        self.assertEqual(draft["flow"], {"velocity_mps": 30.0, "medium": "air"})
        self.assertEqual(draft["schema"], "aces-golden-spec/1")
        self.assertNotIn("velocity_ms", draft["_constraints_not_in_structural_spec"],
                         "the velocity is in the spec now (the flow block), not outside it")

    def test_draft_refuses_air_over_100_mps_with_goldens_own_message(self):
        # unreachable through build_plan (the item is tracked, never wired), so the draft refuses on its own
        req = loop.parse_request("bracket at 150 m/s")
        item = {"key": "bracket-cfd", "title": "bracket — cfd", "physics": ["cfd"], "wired": True}
        with self.assertRaises(ValueError) as ctx:
            loop.spec_draft(req, item, "abc12345-bracket-cfd")
        self.assertEqual(str(ctx.exception), AIR_150_REFUSAL)

    def test_draft_refuses_a_wired_cfd_item_without_a_speed(self):
        req = loop.parse_request("bracket drag")
        item = {"key": "bracket-cfd", "title": "bracket — cfd", "physics": ["cfd"], "wired": True}
        with self.assertRaises(ValueError) as ctx:
            loop.spec_draft(req, item, "abc12345-bracket-cfd")
        self.assertIn("name a speed to run CFD", str(ctx.exception))


class StructuresOnlyPinnedTest(unittest.TestCase):
    """The structures-only request's parse, plan and draft shape is pinned (the draft carries the corrected
    material key and rmin range — golden's Material model and resolution gate — since the P6 acceptance fixes)."""

    TEXT = "bracket holds 15 kg"

    def test_parse_is_unchanged(self):
        req = loop.parse_request(self.TEXT)
        self.assertEqual(req["constraints"], {"payload_kg": 15.0})
        self.assertEqual(req["physics"], {"structures"})
        self.assertEqual(req["kind"], "bracket")
        self.assertIsNone(req["material"])

    def test_plan_is_unchanged(self):
        plan = loop.build_plan(loop.parse_request(self.TEXT))
        self.assertEqual(plan, {"items": [{"key": "bracket", "title": "bracket — holds 15 kg",
                                           "physics": ["structures"],
                                           "answers": "mass, stiffness, stress margin",
                                           "wired": True}],
                                "wired_count": 1,
                                "missing_geometry": True,
                                "est": "one golden run per wired item; wall time minutes (desktop) to tens of minutes (rig)"})

    def test_draft_shape_is_pinned(self):
        req = loop.parse_request(self.TEXT)
        item = loop.build_plan(req)["items"][0]
        draft = loop.spec_draft(req, item, "abcd1234-bracket")
        self.assertNotIn("flow", draft)
        self.assertEqual(draft, {
            "schema": "aces-golden-spec/1",
            "name": "abcd1234-bracket",
            "description": "Loop draft for “bracket — holds 15 kg”. Generated from the thread; edit before confirming.",
            "units": "mm-N-MPa-kg",
            "hypothesis": None,
            "material": loop.MATERIALS["6061"],
            "objective": {"output": "mass_kg", "sense": "min"},
            "generator": {"kind": "simp-organic",
                          "variables": {"volfrac": {"lo": 0.08, "hi": 0.35}, "rmin_mm": {"lo": 3.0, "hi": 6.0}},
                          "fixed": {"penal": 3.0}, "iterations": 25},
            "resolution": {"gen_mm": 2.0, "fea_mm": 1.0},
            "sweep": {"n": 10, "seed": 0},
            "loads": [{"name": "service+shock", "box": None, "force": [0.0, 0.0, -15.0 * G],
                       "_note": "placement box unresolved until the design space is known"}],
            "supports": [],
            "_missing": ["supports: where is it held/bolted? (needs the design space)",
                         "domain: the design space — attach a STEP, pick a demo (root-mount, bone), or name an envelope in mm"],
            "_constraints_not_in_structural_spec": {}})


class P6AcceptanceFixesTest(unittest.TestCase):
    """The defects the P6 acceptance drive (2026-10-12) found: drafts must confirm unedited against golden's models,
    the vehicle's own mass is not a payload, and "airflow" is not an EM keyword ("rf" inside it)."""

    def test_draft_material_matches_goldens_material_model(self):
        # aces_engineering.golden.spec.Material fields (aces-engineering is not a console dependency: pinned here)
        fields = {"name", "E_MPa", "nu", "rho_kg_mm3", "allowable_vm_MPa", "source"}
        for key, mat in loop.MATERIALS.items():
            if isinstance(mat, str):
                continue
            self.assertEqual(set(mat), fields, key)
            self.assertGreater(mat["allowable_vm_MPa"], 0.0, key)

    def test_draft_honours_goldens_resolution_gate(self):
        # golden refuses rmin_mm lo under 1.5 x gen_mm (golden/spec.py resolution guard)
        draft = loop.spec_draft(loop.parse_request("bracket holds 15 kg"),
                                {"key": "bracket", "title": "bracket — holds 15 kg", "physics": ["structures"]},
                                "abcd1234-bracket")
        rmin_lo = draft["generator"]["variables"]["rmin_mm"]["lo"]
        gen = draft["resolution"]["gen_mm"]
        self.assertGreaterEqual(rmin_lo, 1.5 * gen)

    def test_vehicle_mass_is_not_a_payload(self):
        c = loop.parse_request("a 1500 kg six-wheeled off-roader, military spec, electric drive, "
                               "50 kWh battery")["constraints"]
        self.assertEqual(c.get("vehicle_mass_kg"), 1500.0)
        self.assertNotIn("payload_kg", c)
        self.assertEqual(c.get("battery_kWh"), 50.0)

    def test_vehicle_mass_and_payload_together(self):
        c = loop.parse_request("a 1500 kg off-roader carrying 200 kg of gear")["constraints"]
        self.assertEqual(c.get("vehicle_mass_kg"), 1500.0)
        self.assertEqual(c.get("payload_kg"), 200.0)

    def test_chassis_draft_load_is_the_vehicle_mass(self):
        req = loop.parse_request("a 1500 kg six-wheeled off-roader with a tessellated chassis")
        item = next(i for i in loop.build_plan(req)["items"] if i["key"] == "chassis")
        draft = loop.spec_draft(req, item, "abcd1234-chassis")
        self.assertEqual(len(draft["loads"]), 1)
        self.assertAlmostEqual(-draft["loads"][0]["force"][2], 1500.0 * G)
        self.assertIn("vehicle mass 1500 kg", draft["loads"][0]["_basis"])
        self.assertFalse(any(m.startswith("loads:") for m in draft["_missing"]),
                         "the vehicle mass is the chassis load — it is not missing")

    def test_airflow_is_not_an_em_keyword(self):
        req = loop.parse_request("bracket in 30 m/s airflow")
        self.assertNotIn("em", req["physics"])
        plan = loop.build_plan(req)
        self.assertFalse(any(i["physics"] == ["em"] for i in plan["items"]))
        # and the real EM words still match
        for text in ("antenna for the tile", "radar bracket", "rf front-end mount", "s11 of the array"):
            self.assertIn("em", loop.parse_request(text)["physics"], text)

    def test_vehicle_with_a_speed_gets_a_cfd_item(self):
        plan = loop.build_plan(loop.parse_request("a 1500 kg off-roader, top speed 120 km/h, drag matters"))
        item = next((i for i in plan["items"] if i["physics"] == ["cfd"]), None)
        self.assertIsNotNone(item)
        self.assertTrue(item["wired"])
        self.assertAlmostEqual(item["flow"]["velocity_mps"], 120.0 / 3.6)


class LoopHttpTest(unittest.TestCase):
    """The routes end to end: message → plan card → approve → drafts (flow block included), and the refusals."""

    @classmethod
    def setUpClass(cls):
        cls.c = Console(tag="loop")

    @classmethod
    def tearDownClass(cls):
        cls.c.close()

    def _message(self, text: str) -> dict:
        status, _, body = self.c.post("/api/loop/message", {"text": text})
        self.assertEqual(status, 201, body)
        return body["data"]["thread"]

    def test_approve_a_wired_cfd_item_yields_a_draft_with_the_flow_block(self):
        th = self._message("aluminium bracket holds 5 kg in 30 m/s airflow")
        plan = th["messages"][-1]["card"]["plan"]
        item = cfd_item(plan)
        self.assertTrue(item["wired"])
        # the request card records the velocity as parsed, unit included
        self.assertEqual(th["messages"][-1]["card"]["request"]["constraints"]["velocity_unit"], "m/s")
        status, _, body = self.c.post("/api/loop/plan/approve", {"thread": th["id"], "keep": ["bracket-cfd"]})
        self.assertEqual(status, 200, body)
        draft = body["data"]["drafts"]["bracket-cfd"]
        self.assertEqual(draft["flow"], {"velocity_mps": 30.0, "medium": "air"})
        self.assertEqual(draft["schema"], "aces-golden-spec/1")

    def test_approve_a_tracked_cfd_item_is_refused_with_the_reason(self):
        th = self._message("bracket at 150 m/s")
        item = cfd_item(th["messages"][-1]["card"]["plan"])
        self.assertFalse(item["wired"])
        status, _, body = self.c.post("/api/loop/plan/approve", {"thread": th["id"], "keep": ["bracket-cfd"]})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "unwired")
        self.assertIn("incompressible tier refuses air over 100 m/s", body["error"])
        th = self._message("bracket drag")
        status, _, body = self.c.post("/api/loop/plan/approve", {"thread": th["id"], "keep": ["bracket-cfd"]})
        self.assertEqual(status, 409)
        self.assertIn("name a speed to run CFD", body["error"])

    def test_nothing_claims_cfd_ran(self):
        th = self._message("bracket holds 5 kg in 30 m/s airflow")
        self.assertEqual(th["runs"], [], "a plan and a draft are not a run")
        self.assertNotIn("ran", th["messages"][-1]["text"])


if __name__ == "__main__":
    unittest.main()
