"""Builder B's test helpers (not a test module): a Console whose simlab-mcp and MLflow upstreams can be pointed at
fakes, a fresh work root per test class, and the assembly of a work store from the committed real records:

    the two record-only golden runs of tests/fixtures/golden/ (cad-gold 20261010T102210Z-bc7391 verified-desktop,
    golden-demo 20261010T084920Z-b3ac99 rig-verified), copied byte-exact under <WORK>/<project>/golden/<run>/;
    the CAD records of REPO/rehearsal/reports/2026-10-10-aces-cad-vm: bundle-plate under its real bundle name
    cad/local/20261010T101736Z-plate/ (outcome.json names it), the plate drawing under the real run id
    cad/local/runs/20261010T101628Z-0ffb/drawings/ (the drawing record's run_id) with no run.json (the fixture
    records/run-plate.json is the op_run result, not a run.json), and cad/NOTES.md.

Projects are created with aces.project.new (the real writer: aces.yaml plus a project.new ledger entry), never by
hand. Nothing here is test data.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from aces import config as aces_config, project as aces_project

from aces_console import config as console_config, server

from . import CAD_RECORDS, FIXTURES, ROOT
from .support import Console, closed_url, fresh_state

GOLDEN_FIXTURES = FIXTURES / "golden"
GOLDEN_RUNS = {"cad-gold": "20261010T102210Z-bc7391", "golden-demo": "20261010T084920Z-b3ac99"}
CAD_PROJECT = "cad-vm"
BUNDLE = "20261010T101736Z-plate"
CAD_RUN = "20261010T101628Z-0ffb"
DRAWING = "plate-fa5cc45b"
_WORK_N = [0]


def fresh_work(tag: str) -> Path:
    """A new, empty ACES_WORK made current for aces and the console."""
    _WORK_N[0] += 1
    work = ROOT / f"work-{tag}-{_WORK_N[0]}"
    work.mkdir(parents=True, exist_ok=True)
    aces_config.WORK = work
    os.environ["ACES_WORK"] = str(work)
    return work


def new_project(name: str, cls: str) -> Path:
    """aces.project.new: writes <WORK>/<name>/aces.yaml and a project.new ledger entry (the real code path)."""
    return aces_project.new(name, cls, None, None, None)


def copy_golden_run(project: str, run_id: str, work: Path) -> Path:
    src = GOLDEN_FIXTURES / project / "golden" / run_id
    dst = work / project / "golden" / run_id
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return dst


def assemble_cad_project(work: Path) -> Path:
    pdir = work / CAD_PROJECT
    bundle = pdir / "cad" / "local" / BUNDLE
    shutil.copytree(CAD_RECORDS / "bundle-plate", bundle, dirs_exist_ok=True)
    drawings = pdir / "cad" / "local" / "runs" / CAD_RUN / "drawings"
    drawings.mkdir(parents=True, exist_ok=True)
    for p in (CAD_RECORDS / "drawing").iterdir():
        if p.is_file() and p.name.startswith(DRAWING):
            shutil.copyfile(p, drawings / p.name)
    shutil.copyfile(CAD_RECORDS / "agent-loop-codex" / "NOTES.md", pdir / "cad" / "NOTES.md")
    return pdir


def build_store(tag: str) -> Path:
    """A fresh work root with the three projects and the real records above; the current ACES_STATE receives the
    three project.new ledger entries."""
    work = fresh_work(tag)
    new_project("cad-gold", "sovereign")
    new_project("golden-demo", "personal")
    new_project(CAD_PROJECT, "sovereign")
    for project, run_id in GOLDEN_RUNS.items():
        copy_golden_run(project, run_id, work)
    assemble_cad_project(work)
    return work


class ConsoleB(Console):
    """support.Console with simlab_mcp and mlflow upstreams too (closed loopback ports when not given)."""

    def __init__(self, *, gc: str | None = None, rigstate: str | None = None, simlab_console: str | None = None,
                 simlab_mcp: str | None = None, mlflow: str | None = None, fixtures: str | Path | None = None,
                 static_dir: str | Path | None = None, bind: str = "127.0.0.1", state: Path | None = None, tag: str = "console-b"):
        import threading

        # the state and work roots current before this console, restored by Console.close()
        self._prev_state = (aces_config.STATE, os.environ.get("ACES_STATE"))
        self._prev_work = (aces_config.WORK, os.environ.get("ACES_WORK"))
        if state is not None:  # a given state (a restart test) is made current like a fresh one
            aces_config.STATE = state
            os.environ["ACES_STATE"] = str(state)
        self.state = state or fresh_state(tag)
        overrides = {"server": {"grid_control": gc or closed_url(), "rig_state": (rigstate or closed_url()) + "/mcp",
                                "simlab_console": simlab_console or closed_url(), "simlab_mcp": simlab_mcp or closed_url("/mcp"),
                                "mlflow": mlflow or closed_url(), "timeout_s": 2},
                     "console": {"bind": bind, "port": 0, "state_dir": str(self.state / "console")}}
        self.settings = console_config.load(static=str(static_dir) if static_dir else None,
                                            fixtures=str(fixtures) if fixtures else None, overrides=overrides)
        self.httpd, self.app = server.make_server(self.settings)
        self.port = self.settings.port
        self.url = self.settings.origin
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.app.hub.start()
