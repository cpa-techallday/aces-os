"""ACES console dev/preview launcher: `npm run dev [-- --port N --host H]`.

Runs the console from this source tree with the environment the engineering platform needs:
- PYTHONPATH spans aces-console, aces-base and aces-engineering (lib + vendor) so the server and
  every subprocess it spawns import from this checkout
- ACES_GOLDEN_CMD / ACES_CMD point golden jobs at an interpreter with the engineering deps
  (build123d, pydantic, yaml): Python 3.11 when present, else this interpreter
- tokens come from ~/.config/aces (gridcontrol.env) per aces_console.secrets; no fixtures

Exit code is the server's. Args may arrive as `--port N`, `--port=N`, `--host H`, `--bind H`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # src/aces-console
SRC = HERE.parent                               # src
PY311 = Path(r"C:\Users\shedm\AppData\Local\Programs\Python\Python311\python.exe")


def main() -> int:
    eng = SRC / "aces-engineering"
    parts = [str(eng / "lib"), str(eng / "vendor"), str(SRC / "aces-base")]
    if os.environ.get("PYTHONPATH"):
        parts.append(os.environ["PYTHONPATH"])
    os.environ["PYTHONPATH"] = ";".join(parts)
    worker = str(PY311) if PY311.is_file() else sys.executable
    os.environ.setdefault("ACES_GOLDEN_CMD", json.dumps([worker, "-m", "aces_engineering.golden_cli"]))
    os.environ.setdefault("ACES_CMD", json.dumps([worker, "-m", "aces"]))

    bind, port = "127.0.0.1", "8811"
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a in ("--port", "-p") and i + 1 < len(args):
            port = args[i + 1]
        elif a in ("--host", "--bind") and i + 1 < len(args):
            bind = args[i + 1]
        elif a.startswith("--port="):
            port = a.split("=", 1)[1]
        elif a.startswith(("--host=", "--bind=")):
            bind = a.split("=", 1)[1]

    proc = subprocess.run([sys.executable, "-m", "aces_console", "serve",
                           "--bind", bind, "--port", str(port),
                           "--static", str(HERE / "static")],
                          cwd=str(HERE))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
