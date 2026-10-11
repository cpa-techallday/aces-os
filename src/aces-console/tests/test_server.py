"""server.py, jsonenc.py, config.py and the fixtures: loopback-only bind, POST guards, the CSP with the computed importmap
hash, no-store on /api/*, the finite masked encoder, the envelope on an upstream that is down, static-file rules, the
error shape, /vocab.json, /api/config without token values, fixtures mode, the fixture MANIFEST and the CLI's url and
status commands. Every upstream of these servers is a closed loopback port (nothing fabricated: the console answers
with its honest absence shapes)."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import secrets as pysecrets
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from aces_console import FIXTURE_STAMP, __version__, cli, config as console_config, jsonenc, server

from . import FIXTURES, ROOT
from .support import Console, closed_port, fresh_state

IMPORTMAP = '{"imports":{"three":"./vendor/three.module.js","three/addons/controls/OrbitControls.js":"./vendor/OrbitControls.js"}}'


def make_static(root: Path) -> Path:
    """A minimal static tree for the server tests (test scaffolding, not console content): an index with an inline
    importmap, one script, one nested stylesheet, a dotfile and a file of a type the allow-list refuses."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html><html><head><meta charset=\"utf-8\"><title>t</title>"
                                     f"<script type=\"importmap\">{IMPORTMAP}</script></head><body></body></html>",
                                     encoding="utf-8")
    (root / "app.js").write_text("export const x = 1;\n", encoding="utf-8")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "x.css").write_text("body{}\n", encoding="utf-8")
    (root / ".hidden").write_text("secret\n", encoding="utf-8")
    (root / "tool.exe").write_bytes(b"MZ")
    return root


class EncoderTest(unittest.TestCase):
    def test_masks_credential_keys_and_finite(self):
        out = json.loads(jsonenc.dumps({
            "token": "abc", "api_key": "k", "apiKey": "k2", "Authorization": "Bearer x", "x-gc-token": "t",
            "password": "p", "client_secret": "s", "count": 3,
            "tokens": {"grid_control": {"configured": True, "source": "ACES_GC_TOKEN"}},
            "token_source": "simlab.env", "token_present": True,
            "nan": float("nan"), "inf": float("inf"), "neg": float("-inf"), "fine": 1.5,
            "path": Path("a") / "b", "when": datetime(2026, 10, 11, tzinfo=timezone.utc),
            "nested": [{"secret_key": "zz", "name": "n"}]}))
        for k in ("token", "api_key", "apiKey", "Authorization", "x-gc-token", "password", "client_secret"):
            self.assertEqual(out[k], "[masked]", k)
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["tokens"]["grid_control"], {"configured": True, "source": "ACES_GC_TOKEN"})
        self.assertEqual(out["token_source"], "simlab.env")
        self.assertIs(out["token_present"], True)
        self.assertIsNone(out["nan"])
        self.assertIsNone(out["inf"])
        self.assertIsNone(out["neg"])
        self.assertEqual(out["fine"], 1.5)
        self.assertEqual(out["path"], str(Path("a") / "b"))
        self.assertEqual(out["when"], "2026-10-11T00:00:00+00:00")
        self.assertEqual(out["nested"][0], {"secret_key": "[masked]", "name": "n"})

    def test_rejects_non_json_types(self):
        with self.assertRaises(TypeError):
            jsonenc.dumps({"s": {1, 2}})

    def test_etag_is_order_independent(self):
        self.assertEqual(jsonenc.etag({"a": 1, "b": [1, 2]}), jsonenc.etag({"b": [1, 2], "a": 1}))
        self.assertEqual(len(jsonenc.etag({"a": 1})), 12)
        self.assertNotEqual(jsonenc.etag({"a": 1}), jsonenc.etag({"a": 2}))


class RouterTest(unittest.TestCase):
    def test_patterns_and_methods(self):
        calls = []
        server.register("GET", "/t/router/{project}/{run_id}", lambda req: calls.append(req))
        server.register("GET", "/t/router/file/{project}/{rel:path}", lambda req: calls.append(req))
        route, params, known = server.match("GET", "/t/router/cad-vm/20261010T101628Z-0ffb")
        self.assertIsNotNone(route)
        self.assertEqual(params, {"project": "cad-vm", "run_id": "20261010T101628Z-0ffb"})
        route, params, known = server.match("GET", "/t/router/file/cad-gold/figures/a/b.png")
        self.assertIsNotNone(route)
        self.assertEqual(params, {"project": "cad-gold", "rel": "figures/a/b.png"})
        route, params, known = server.match("POST", "/t/router/cad-vm/x")
        self.assertIsNone(route)
        self.assertTrue(known, "the path exists for GET, so POST is 405 not 404")
        route, params, known = server.match("GET", "/t/router")
        self.assertIsNone(route)
        self.assertFalse(known)

    def test_csp_hash_matches_importmap_body(self):
        html = f'<html><head><script type="importmap">{IMPORTMAP}</script></head></html>'
        expected = base64.b64encode(hashlib.sha256(IMPORTMAP.encode("utf-8")).digest()).decode("ascii")
        self.assertEqual(server.importmap_hash(html), expected)
        self.assertEqual(server.csp_for(html), server.CSP_TEMPLATE.format(hash=f" 'sha256-{expected}'"))
        self.assertEqual(server.csp_for("<html></html>"), server.CSP_TEMPLATE.format(hash=""))


class ServerTest(unittest.TestCase):
    """One console with every upstream on a closed loopback port and a minimal static tree."""

    @classmethod
    def setUpClass(cls):
        cls.static = make_static(ROOT / "static-servertest")
        cls.c = Console(static_dir=cls.static, tag="server")

    @classmethod
    def tearDownClass(cls):
        cls.c.close()

    def test_health_envelope_and_headers(self):
        status, hdrs, body = self.c.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["cache-control"], "no-store")
        self.assertEqual(hdrs["x-content-type-options"], "nosniff")
        self.assertEqual(hdrs["referrer-policy"], "no-referrer")
        self.assertNotIn("access-control-allow-origin", hdrs)
        self.assertTrue(body["ok"])
        for key in ("source", "fetched_utc", "age_s", "ttl_s", "stale", "ok", "error", "next_try_s", "fixture"):
            self.assertIn(key, body["meta"])
        d = body["data"]
        self.assertEqual(d["version"], __version__)
        self.assertEqual(d["bind"], "127.0.0.1")
        self.assertEqual(d["origin"], self.c.url)
        self.assertEqual(d["modules"]["rig"], "installed")
        for name in ("sim", "records", "ledgers", "system"):
            self.assertIn(d["modules"][name], ("not installed", "installed"), name)
        self.assertIn("rig.status", d["sources"])
        self.assertEqual(set(d["counters"]), {"refused_posts", "suppressed_refusals", "nonce_failures"})
        self.assertIsNone(d["switch_outcome_pending"])
        self.assertIsNone(d["fixture"])
        self.assertIsInstance(d["static_manifest_sha256"], str)

    def test_page_carries_the_csp_with_the_importmap_hash(self):
        status, hdrs, body = self.c.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(hdrs["content-type"].startswith("text/html"))
        expected = base64.b64encode(hashlib.sha256(IMPORTMAP.encode("utf-8")).digest()).decode("ascii")
        self.assertEqual(hdrs["content-security-policy"],
                         "default-src 'self'; script-src 'self' 'sha256-" + expected + "'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; font-src 'self'; connect-src 'self'; worker-src 'none'; object-src 'none'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.assertEqual(hdrs["x-content-type-options"], "nosniff")
        self.assertIn(b"importmap", body)

    def test_static_rules(self):
        status, hdrs, body = self.c.request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertTrue(hdrs["content-type"].startswith("text/javascript"))
        self.assertNotIn("content-disposition", hdrs)
        etag = hdrs["etag"]
        status, hdrs2, _ = self.c.request("GET", "/static/app.js", headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        status, _, body = self.c.get("/static/sub/x.css")
        self.assertEqual(status, 200)
        status, _, body = self.c.get("/static/.hidden")
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])
        status, _, body = self.c.get("/static/sub/../app.js")
        self.assertEqual(status, 400)
        status, _, body = self.c.get("/static/missing.js")
        self.assertEqual(status, 404)
        status, _, body = self.c.get("/static/tool.exe")
        self.assertEqual(status, 415)
        status, hdrs, body = self.c.request("GET", "/favicon.ico")
        self.assertEqual(status, 204)
        self.assertEqual(hdrs["x-content-type-options"], "nosniff")

    def test_unknown_route_and_wrong_method(self):
        status, hdrs, body = self.c.get("/api/nothing/here")
        self.assertEqual(status, 404)
        self.assertEqual(set(body), {"ok", "error", "code", "source", "status", "next_try_s"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "not-found")
        self.assertEqual(hdrs["cache-control"], "no-store")
        status, _, body = self.c.post("/api/health", {})
        self.assertEqual(status, 405)
        self.assertEqual(body["code"], "method-not-allowed")

    def test_post_guards(self):
        before = self.c.get("/api/health")[2]["data"]["counters"]["refused_posts"]
        status, _, body = self.c.post("/api/fabric/refresh", {}, human=False)
        self.assertEqual((status, body["code"]), (403, "refused"))
        status, _, body = self.c.post("/api/fabric/refresh", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual((status, body["code"]), (403, "refused"))
        status, _, body = self.c.post("/api/fabric/refresh", {}, headers={"X-ACES-Intent": "agent"})
        self.assertEqual((status, body["code"]), (403, "refused"))
        status, _, body = self.c.post("/api/fabric/refresh", {}, headers={"Content-Type": "text/plain"})
        self.assertEqual((status, body["code"]), (403, "refused"))
        status, _, body = self.c.post("/api/fabric/refresh", None, raw=b"[1, 2]")
        self.assertEqual((status, body["code"]), (403, "refused"))
        status, _, body = self.c.post("/api/fabric/refresh", None, raw=b"not json")
        self.assertEqual((status, body["code"]), (403, "refused"))
        after = self.c.get("/api/health")[2]["data"]["counters"]["refused_posts"]
        self.assertEqual(after - before, 6)
        big = b'{"pad": "' + b"x" * (64 * 1024) + b'"}'
        status, _, body = self.c.post("/api/fabric/refresh", None, raw=big)
        self.assertEqual((status, body["code"]), (413, "too-large"))
        self.assertEqual(self.c.get("/api/health")[2]["data"]["counters"]["refused_posts"], after, "413 is not a guard refusal")

    def test_envelope_when_the_upstream_is_down(self):
        status, hdrs, body = self.c.get("/api/rig/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsNone(body["data"])
        m = body["meta"]
        self.assertFalse(m["ok"])
        self.assertFalse(m["stale"])
        self.assertTrue(m["error"].startswith("unreachable: "), m["error"])
        self.assertTrue(m["source"].endswith("/api/status"))
        self.assertIsInstance(m["next_try_s"], (int, float))
        self.assertEqual(m["ttl_s"], 3.0)
        self.assertIsNone(m["fixture"])
        # rig-state: its own sentence
        status, _, body = self.c.get("/api/rig/snapshot")
        self.assertEqual(status, 200)
        self.assertIsNone(body["data"])
        self.assertTrue(body["meta"]["error"].startswith("rig-state unreachable: "), body["meta"]["error"])
        # the two parts of /api/rig/modes carry their own errors
        status, _, body = self.c.get("/api/rig/modes")
        self.assertEqual(status, 200)
        self.assertIsNone(body["data"]["gridcontrol_modes"])
        self.assertIsNone(body["data"]["sim_modes"])
        self.assertTrue(body["data"]["errors"]["sim_modes"].startswith("SIMLAB console unreachable at http://127.0.0.1:"))
        # telemetry keeps its (empty) ring and names the missing seed
        status, _, body = self.c.get("/api/rig/telemetry")
        self.assertEqual(status, 200)
        self.assertIsNone(body["data"]["telemetry"])
        self.assertEqual(body["data"]["history"], [])
        self.assertTrue(any(s.startswith("console history unavailable") for s in body["data"]["sources_used"]))

    def test_vocab_served(self):
        status, hdrs, body = self.c.get("/vocab.json")
        self.assertEqual(status, 200)
        self.assertEqual(set(body["vocabularies"]), {"console", "rq", "meta", "run", "case", "cad", "endpoint", "registry", "mc"})
        self.assertEqual(body["inks"], ["chalk", "cyan", "amber", "fault"])
        for name, v in body["vocabularies"].items():
            words = v["chalk"] + v["cyan"] + v["amber"] + v["fault"]
            self.assertEqual(len(words), len(set(words)), f"{name}: a word in two inks")
            self.assertIn(v["label"], ("rq", "meta", "case", "run", "cad", "console", "registry", "endpoint", "mc"))
        self.assertNotIn("green", json.dumps(body["inks"]))

    def test_config_reports_token_sources_never_values(self):
        token = pysecrets.token_hex(16)
        with mock.patch.dict(os.environ, {"ACES_GC_TOKEN": token}):
            status, _, body = self.c.get("/api/config")
            self.assertEqual(status, 200)
            d = body["data"]
            self.assertEqual(d["tokens"]["grid_control"], {"configured": True, "source": "ACES_GC_TOKEN"})
            self.assertEqual(d["tokens"]["simlab"], {"configured": False, "source": "not set"})
            self.assertEqual(set(d["server"]), {"name", "host", "grid_control", "rig_state", "simlab_mcp", "simlab_console",
                                                "mlflow", "webodm", "timeout_s"})
            self.assertEqual(d["console"]["origin"], self.c.url)
            self.assertIn("work", d["paths"])
            self.assertIn("python", d["platform"])
            self.assertNotIn(token, json.dumps(body))
            for path in ("/api/health", "/api/rig/status", "/api/rig/modes", "/api/rig/telemetry", "/api/rig/snapshot",
                         "/api/rig/switch", "/api/rig/decisions", "/api/fabric", "/api/events?plate=x"):
                st, hdrs, raw = self.c.request("GET", path) if "events" not in path else (0, {}, b"")
                self.assertNotIn(token.encode(), raw, path)
                self.assertNotIn(token, json.dumps(hdrs), path)
        self.assertNotIn(token, self.c.log_text())
        status, _, body = self.c.get("/api/config")
        self.assertEqual(body["data"]["tokens"]["grid_control"], {"configured": False, "source": "not set"})

    def test_decisions_without_token_is_an_honest_absence(self):
        # 200 with data null and the reason in meta (a 503 would be a browser console error on every Ops visit)
        status, _, body = self.c.get("/api/rig/decisions")
        self.assertEqual(status, 200)
        self.assertIsNone(body["data"])
        self.assertEqual(body["meta"]["ok"], False)
        self.assertTrue(body["meta"]["error"], "the reason is given: no token and the rig's read proxy unreachable")

    def test_server_log_has_method_path_status_only(self):
        self.c.get("/api/health?secret=in-query")
        text = self.c.log_text()
        self.assertIn("GET /api/health 200", text)
        self.assertNotIn("in-query", text)


class BindRefusalTest(unittest.TestCase):
    def test_non_loopback_bind_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            console_config.load(overrides={"console": {"bind": "192.168.1.5", "port": 0}})
        self.assertIn("loopback only", str(ctx.exception.code))
        settings = console_config.load(overrides={"console": {"bind": "127.0.0.1", "port": 0}})
        settings.bind = "0.0.0.0"
        with self.assertRaises(SystemExit):
            server.make_server(settings)

    def test_fixtures_need_the_environment_variable(self):
        env = dict(os.environ)
        env.pop("ACES_CONSOLE_ALLOW_FIXTURES", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                console_config.load(fixtures=str(FIXTURES), overrides={"console": {"port": 0}})
        self.assertIn("ACES_CONSOLE_ALLOW_FIXTURES", str(ctx.exception.code))

    def test_console_defaults(self):
        settings = console_config.load(overrides={"console": {"port": 0}})
        self.assertEqual(settings.bind, "127.0.0.1")
        self.assertTrue(str(settings.state_dir).endswith("console"))
        self.assertIn(settings.static_source, ("config", "source-tree"))


class NoStaticTreeTest(unittest.TestCase):
    def test_missing_static_tree_is_reported(self):
        empty = ROOT / "static-empty"
        empty.mkdir(exist_ok=True)
        c = Console(static_dir=empty, tag="nostatic")
        try:
            status, _, body = c.get("/")
            self.assertEqual(status, 503)
            self.assertEqual(body["code"], "no-static")
            self.assertIn("static tree not found", body["error"])
            self.assertIsNone(c.get("/api/health")[2]["data"]["static_manifest_sha256"])
        finally:
            c.close()


class FixturesModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = Console(fixtures=FIXTURES, gc="http://127.0.0.1:1", rigstate="http://127.0.0.1:2",
                        simlab_console="http://127.0.0.1:3", tag="fixtures")

    @classmethod
    def tearDownClass(cls):
        cls.c.close()

    def test_replays_captures_and_stamps_every_reply(self):
        status, _, body = self.c.get("/api/rig/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["meta"]["fixture"], FIXTURE_STAMP)
        d = body["data"]
        self.assertEqual(d["queue"], {"queued": None, "running": None})
        self.assertEqual(d["queue_note"], "GRID CONTROL reported -1")
        self.assertEqual(d["raw_queue"], {"queued": -1, "running": -1})
        self.assertEqual(d["mode"], "sim-lab-4")
        self.assertEqual(len(d["gpus"]), 8)
        status, _, body = self.c.get("/api/rig/snapshot")
        self.assertEqual(body["meta"]["fixture"], FIXTURE_STAMP)
        self.assertEqual(len(body["data"]["degraded"]), 5)
        by_name = {(r["kind"], r["name"]): r for r in body["data"]["classification"]}
        self.assertEqual(by_name[("endpoint", "apodex-1")]["verdict"], "stopped by mode sim-lab-4 (by design)")
        self.assertEqual(by_name[("endpoint", "embeddings")]["word"], "reachable")
        status, _, body = self.c.get("/api/health")
        self.assertEqual(body["data"]["fixture"], FIXTURE_STAMP)
        self.assertEqual(body["meta"]["fixture"], FIXTURE_STAMP)
        status, _, body = self.c.get("/api/nothing")
        self.assertEqual(body["fixture"], FIXTURE_STAMP)

    def test_switch_and_refresh_refused_in_fixture_mode(self):
        status, _, body = self.c.post("/api/rig/switch/prepare", {"mode": "sim-lab-8"})
        self.assertEqual((status, body["code"]), (503, "fixture"))
        status, _, body = self.c.post("/api/fabric/refresh", {})
        self.assertEqual((status, body["code"]), (503, "fixture"))
        self.assertEqual(self.c.app.counters["suppressed_refusals"], 0)

    def test_sse_first_record_is_the_fixture_event(self):
        status, hdrs, records, text = self.c.sse("fabric", seconds=0.6)
        self.assertEqual(status, 200)
        self.assertTrue(hdrs["content-type"].startswith("text/event-stream"))
        self.assertTrue(text.startswith("retry: 3000"))
        events = [r for r in records if "event" in r]
        self.assertEqual(events[0]["event"], "fixture")
        self.assertEqual(events[0]["data"], {"fixture": FIXTURE_STAMP})


class FixtureManifestTest(unittest.TestCase):
    def test_every_fixture_is_listed_with_its_sha256(self):
        manifest = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
        listed = {row["file"]: row for row in manifest}
        files = sorted(p.name for p in FIXTURES.iterdir() if p.is_file() and p.name not in ("MANIFEST.json", ".gitattributes"))
        self.assertEqual(sorted(listed), files)
        for name in files:
            row = listed[name]
            digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
            self.assertEqual(digest, row["sha256"], name)
            self.assertEqual(row["origin_host"], "192.168.1.65")
            self.assertTrue(row["captured_utc"].startswith("2026-10-1"))
            self.assertIn("origin_route", row)


class CliTest(unittest.TestCase):
    """url, status and the usage exit against a port nothing listens on (the scratch user aces.toml sets [console].port)."""

    def setUp(self):
        self.port = closed_port()
        self.user_toml = ROOT / "cfg" / "aces.toml"
        self.user_toml.write_text(f"[console]\nport = {self.port}\n", encoding="utf-8")

    def tearDown(self):
        self.user_toml.unlink(missing_ok=True)

    def test_url_status_and_usage(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["url"]), 0)
        self.assertEqual(out.getvalue().strip(), f"http://127.0.0.1:{self.port}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["status"]), 1)
        self.assertTrue(out.getvalue().startswith("not running"))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--help"]), 2)
            self.assertEqual(cli.main(["--bogus"]), 2)
            self.assertEqual(cli.main(["stop"]), 0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["video_studio"]), 1)
        self.assertIn("no URL configured for server.video_studio", err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.dict(os.environ, {"ACES_CONSOLE_ALLOW_FIXTURES": ""}):
            self.assertEqual(cli.main(["serve", "--fixtures", str(FIXTURES), "--port", str(self.port)]), 2)
        self.assertIn("ACES_CONSOLE_ALLOW_FIXTURES", err.getvalue())

    def test_server_command_shape(self):
        cmd = cli.server_command(["--port", "1"])
        self.assertEqual(cmd[:2], [sys.executable, "-I"])
        if "-m" in cmd:
            self.assertEqual(cmd[2:], ["-m", "aces_console", "serve", "--port", "1"])
        else:
            self.assertEqual(cmd[2], "-c")
            self.assertIn("from aces_console.cli import main", cmd[3])
            self.assertIn('"serve", "--port", "1"', cmd[3])

    def test_start_status_stop_round_trip(self):
        """A real detached server on the scratch port: start waits for health, status reports it, stop ends it."""
        settings = console_config.load()
        pid = None
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["start"]), 0)
            m = re.search(r"started: pid (\d+) at (\S+)", out.getvalue())
            self.assertIsNotNone(m, out.getvalue())
            pid = int(m.group(1))
            self.assertEqual(m.group(2), f"http://127.0.0.1:{self.port}")
            data = cli.health(settings)
            self.assertIsNotNone(data)
            self.assertEqual(data["pid"], pid)
            self.assertEqual(settings.pid_file.read_text(encoding="utf-8").strip(), str(pid))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["status"]), 0)
            self.assertTrue(out.getvalue().startswith(f"running: pid {pid} at http://127.0.0.1:{self.port}"), out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["start"]), 0)
            self.assertTrue(out.getvalue().startswith("already running"), out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["stop"]), 0)
            self.assertEqual(out.getvalue().strip(), f"stopped pid {pid}")
            self.assertIsNone(cli.health(settings))
            self.assertFalse(settings.pid_file.exists())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["status"]), 1)
        finally:
            if pid is not None and cli._alive(pid):
                import signal

                os.kill(pid, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
