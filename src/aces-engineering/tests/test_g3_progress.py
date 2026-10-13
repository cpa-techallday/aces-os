"""golden.progress: the line and jsonl formats, events.jsonl (including events from before the run directory existed),
the heartbeat reading progress.json beacons, mode selection, and the fd-level StdioGuard (in a child process, since it
re-points the process's own stdout and stderr). Standard library only; every row and beacon is a test input."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from aces_engineering.golden.progress import (MODES, Progress, beacon_summary, beacon_text,  # noqa: E402
                                              format_design_line, make_progress, read_beacon, terminal_columns)

EVENT_KEYS = {"t", "ts", "kind", "stage", "case_id", "i", "n", "msg", "data"}
ROW_OK = {"case_id": "d003", "role": "sweep", "status": "FINISHED", "design_status": "ok",
          "params": {"volfrac": 0.23456, "rmin_mm": 8.1234},
          "metrics": {"mass_kg": 0.12345, "umax_mm": 0.0213, "vm_p99_MPa": 41.25, "vm_max_MPa": 80.0, "compliance_Nmm": 1.5},
          "wall_s": 4.21, "device": "cpu", "error": None}
ROW_FAILED = {"case_id": "d004", "role": "sweep", "status": "FAILED", "design_status": "detached-load",
              "params": {"volfrac": 0.13, "rmin_mm": 6.0}, "metrics": None, "wall_s": 3.0, "device": "cpu", "error": "x"}


class FormatTests(unittest.TestCase):
    def test_design_lines(self) -> None:
        self.assertEqual(format_design_line(ROW_OK),
                         "d003  volfrac 0.2346 rmin_mm 8.123 -> mass 0.1235 kg · vm_p99 41.25 MPa · umax 0.0213 mm · ok · 4.2 s")
        self.assertEqual(format_design_line(ROW_FAILED), "d004  volfrac 0.13 rmin_mm 6 -> detached-load · 3.0 s")

    def test_beacons(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="g3-beacon-"))
        try:
            (tmp / "d003").mkdir()
            (tmp / "d004").mkdir()
            (tmp / "d003" / "progress.json").write_text(json.dumps({"phase": "simp", "step": 12, "total": 25}), encoding="utf-8")
            (tmp / "d004" / "progress.json").write_text(json.dumps({"phase": "pcg", "step": 340, "total": 20000, "relres": 3.2e-6}), encoding="utf-8")
            (tmp / "bad.json").write_text("{half", encoding="utf-8")
            b3, b4 = read_beacon(tmp / "d003" / "progress.json"), read_beacon(tmp / "d004" / "progress.json")
            self.assertEqual(beacon_text(b3), "d003 simp 12/25")
            self.assertEqual(beacon_text(b4), "d004 pcg 340/20000 relres 3.2e-06")
            self.assertEqual(beacon_text({"case": "x001", "phase": "post"}), "x001 post")
            self.assertIsNone(read_beacon(tmp / "bad.json"))
            self.assertIsNone(read_beacon(tmp / "missing.json"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_beacon_summary(self) -> None:
        """The bar postfix stays short with ten workers in flight: one case keeps its text, several are grouped."""
        one = [{"case": "x002", "phase": "pcg", "step": 700, "total": 20000, "relres": 3.2e-7}]
        self.assertEqual(beacon_summary(one), "x002 pcg 700/20000 relres 3.2e-07")
        many = [{"case": f"d00{i}", "phase": "simp", "step": 12 + i, "total": 25} for i in range(4)]
        many += [{"case": "d008", "phase": "pcg", "step": 100, "total": 20000},
                 {"case": "d009", "phase": "done", "step": 1, "total": 1}]
        self.assertEqual(beacon_summary(many), "simp 12-15/25 x4 · pcg 100/20000 x1 · done 1/1 x1")
        self.assertEqual(beacon_summary([]), "")

    def test_terminal_columns_without_a_terminal(self) -> None:
        self.assertIsNone(terminal_columns(io.StringIO()))
        fd, name = tempfile.mkstemp(prefix="g3-cols-")
        try:
            with open(fd, "w", encoding="utf-8") as fh:  # a regular file has no terminal size
                self.assertIsNone(terminal_columns(fh))
        finally:
            os.unlink(name)


class ProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="g3-progress-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def events(self) -> list[dict]:
        return [json.loads(ln) for ln in (self.tmp / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_lines_mode_and_events(self) -> None:
        err = io.StringIO()
        p = Progress("lines", err)
        p.line("aces-golden 0.1.0 · loading the engineering stack ...")  # before the run directory exists
        p.attach(self.tmp / "events.jsonl")
        p.stage(4, "sweep", "10 computed designs")
        bar = p.designs(2, "sweep")
        bar.done(ROW_OK)
        bar.done(ROW_FAILED)
        bar.close()
        p.note("only 1 of 2 designs finished", "warning")
        p.event("result", msg="done", outcome="pass", exit_code=0)
        p.close()
        lines = err.getvalue().splitlines()
        self.assertEqual(lines[0], "aces-golden 0.1.0 · loading the engineering stack ...")
        self.assertEqual(lines[1], "[4/11] sweep  10 computed designs")
        self.assertEqual(lines[2], format_design_line(ROW_OK))
        self.assertEqual(lines[3], format_design_line(ROW_FAILED))
        self.assertEqual(lines[4], "warning: only 1 of 2 designs finished")
        ev = self.events()
        self.assertEqual([e["kind"] for e in ev], ["note", "stage", "case", "case", "note", "result"])
        self.assertTrue(all(set(e) == EVENT_KEYS for e in ev))
        self.assertEqual((ev[1]["stage"], ev[1]["i"], ev[1]["n"]), ("sweep", 4, 11))
        self.assertEqual((ev[2]["case_id"], ev[2]["i"], ev[2]["n"], ev[2]["stage"]), ("d003", 1, 2, "sweep"))
        self.assertEqual(ev[2]["data"]["metrics"]["mass_kg"], 0.12345)
        self.assertEqual(ev[5]["data"], {"outcome": "pass", "exit_code": 0})
        self.assertEqual(ev[5]["msg"], "done")
        self.assertTrue(all(ev[k]["t"] <= ev[k + 1]["t"] for k in range(len(ev) - 1)))

    def test_jsonl_and_none_modes(self) -> None:
        err = io.StringIO()
        p = Progress("jsonl", err, events_path=self.tmp / "events.jsonl")
        p.stage(1, "compute", "cpu: test")
        p.designs(1, "sweep").done(ROW_OK)
        p.close()
        printed = [json.loads(ln) for ln in err.getvalue().splitlines()]
        self.assertEqual([e["kind"] for e in printed], ["stage", "case"])
        self.assertEqual(printed, self.events())
        err2 = io.StringIO()
        q = Progress("none", err2, events_path=self.tmp / "events2.jsonl")
        q.stage(1, "compute", "cpu: test")
        q.line("x")
        q.close()
        self.assertEqual(err2.getvalue(), "")
        self.assertEqual(len((self.tmp / "events2.jsonl").read_text(encoding="utf-8").splitlines()), 2)
        with self.assertRaises(ValueError):
            Progress("loud", err2)
        self.assertEqual(MODES, ("bar", "lines", "jsonl", "none"))

    def test_heartbeat_reads_beacons(self) -> None:
        (self.tmp / "d003").mkdir()
        beacon = self.tmp / "d003" / "progress.json"
        beacon.write_text(json.dumps({"phase": "simp", "step": 12, "total": 25, "compliance_Nmm": 3.1}), encoding="utf-8")
        err = io.StringIO()
        p = Progress("lines", err, events_path=self.tmp / "events.jsonl", tick_s=0.05, quiet_after_s=0.3)
        with p.watch(lambda: [beacon], "sweep"):
            p.stage(4, "sweep", "1 design")
            deadline = time.monotonic() + 5.0
            while "heartbeat:" not in err.getvalue() and time.monotonic() < deadline:
                time.sleep(0.05)
        p.close()
        hb = [ln for ln in err.getvalue().splitlines() if ln.startswith("heartbeat:")]
        self.assertTrue(hb, err.getvalue())
        self.assertRegex(hb[0], r"^heartbeat: d003 simp 12/25 \(\d+ s\)$")
        kinds = [e["kind"] for e in self.events()]
        self.assertIn("heartbeat", kinds)
        hb_ev = next(e for e in self.events() if e["kind"] == "heartbeat")
        self.assertEqual(hb_ev["data"]["beacons"][0]["step"], 12)

    def test_heartbeat_without_beacons_and_its_rate(self) -> None:
        err = io.StringIO()
        p = Progress("lines", err, tick_s=60.0, quiet_after_s=0.2)  # the thread never ticks; tick() is driven here
        p.stage(5, "loop", "surrogate")
        self.assertIsNone(p.tick())  # something was just printed
        time.sleep(0.25)
        text = p.tick()
        self.assertRegex(text or "", r"^\.\.\. still in loop \(\d+ s\)$")
        self.assertIsNone(p.tick())  # at most one heartbeat per quiet period
        p.close()
        self.assertIn(text, err.getvalue())

    def test_bar_mode(self) -> None:
        try:
            import tqdm  # noqa: F401
        except ImportError:
            self.skipTest("tqdm is not installed")
        err = io.StringIO()
        p = Progress("bar", err, events_path=self.tmp / "events.jsonl")
        p.stage(4, "sweep", "2 designs")
        bar = p.designs(2, "sweep")
        bar.done(ROW_OK)
        p.tick()  # refreshes the bar; never raises
        bar.done(ROW_FAILED)
        bar.close()
        p.close()
        out = err.getvalue()
        self.assertIn("[4/11] sweep  2 designs", out)
        self.assertIn(format_design_line(ROW_OK), out)
        self.assertIn("2/2", out)

    def test_make_progress(self) -> None:
        class Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        self.assertEqual(make_progress("auto", io.StringIO(), json_mode=False, quiet=False).mode, "lines")
        self.assertEqual(make_progress("auto", Tty(), json_mode=True, quiet=False).mode, "lines")
        self.assertEqual(make_progress("lines", Tty(), json_mode=False, quiet=True).mode, "none")
        self.assertEqual(make_progress("jsonl", io.StringIO(), json_mode=True, quiet=False).mode, "jsonl")
        try:
            import tqdm  # noqa: F401
            self.assertEqual(make_progress("auto", Tty(), json_mode=False, quiet=False).mode, "bar")
        except ImportError:
            self.assertEqual(make_progress("auto", Tty(), json_mode=False, quiet=False).mode, "lines")


GUARD_CHILD = r"""
import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
from aces_engineering.golden.progress import StdioGuard
tmp_log, final_log = sys.argv[2], sys.argv[3]
g = StdioGuard(tmp_log, json_mode=True).start()
print("noise: python print to stdout")
print("noise: python print to stderr", file=sys.stderr)
os.write(1, b"noise: raw write to fd 1\n")
os.write(2, b"noise: raw write to fd 2\n")
subprocess.run([sys.executable, "-c", "print('noise: a child process')"])
g.err.write("ERR-ONLY · progress\n")
g.move(final_log)
print("noise: after the move")
try:
    import warp  # its banner (printed on init) must land in the log
    warp.init()
except Exception:
    pass
g.out.write('{"only": "this · document"}\n')
g.stop()
print("after-stop")
"""


class StdioGuardTests(unittest.TestCase):
    def test_fd_level_guard_in_a_child(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="g3-guard-"))
        try:
            tmp_log, final_log = tmp / "tmp.log", tmp / "run" / "log.txt"
            cp = subprocess.run([sys.executable, "-c", GUARD_CHILD, str(LIB), str(tmp_log), str(final_log)],
                                capture_output=True, timeout=180)
            out, err = cp.stdout.decode("utf-8"), cp.stderr.decode("utf-8", "replace")
            self.assertEqual(cp.returncode, 0, err)
            self.assertEqual(out.replace("\r\n", "\n"), '{"only": "this · document"}\nafter-stop\n')
            self.assertIn("ERR-ONLY · progress", err)
            self.assertNotIn("noise:", err)
            self.assertFalse(tmp_log.exists())
            log = final_log.read_text(encoding="utf-8", errors="replace")
            for needle in ("noise: python print to stdout", "noise: python print to stderr", "noise: raw write to fd 1",
                           "noise: raw write to fd 2", "noise: a child process", "noise: after the move"):
                self.assertIn(needle, log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
