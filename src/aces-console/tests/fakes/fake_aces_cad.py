"""A stand-in for `aces-cad` for tests: replays the real `--json` records of the aces-cad VM acceptance
(rehearsal/reports/2026-10-10-aces-cad-vm/records: inspect-plate.json, render-plate.json, drawing-plate.json,
pull-plate.json, run-plate-2.json) for inspect, render, drawing, pull and run --from; prune prints a labelled
stand-in. It records its argv in FAKE_ACES_CAD_LOG (one JSON line per call) so a test can assert the console's
command line. Nothing is computed and nothing is written to a project."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RECORDS = Path(__file__).resolve().parents[4] / "rehearsal" / "reports" / "2026-10-10-aces-cad-vm" / "records"
REPLAY = {"inspect": "inspect-plate.json", "render": "render-plate.json", "drawing": "drawing-plate.json",
          "pull": "pull-plate.json", "run": "run-plate-2.json"}


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_ACES_CAD_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")
    verb = argv[0] if argv else ""
    if verb == "prune":
        print(json.dumps({"ok": True, "pruned": [], "note": "fake_aces_cad stand-in: nothing deleted"}))
        return 0
    name = REPLAY.get(verb)
    if name is None or "--json" not in argv:
        print(json.dumps({"ok": False, "error": f"fake_aces_cad: {verb} is not replayed"}))
        return 2
    print(f"fake_aces_cad: replaying {name}", file=sys.stderr)
    sys.stdout.write((RECORDS / name).read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
