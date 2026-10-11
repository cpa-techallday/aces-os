"""A stand-in for `aces-golden` for tests, reached through ACES_GOLDEN_CMD (src/aces-base/aces/cli.py:218-231).

    python fake_golden.py verify <run_dir> --json --no-model
    python fake_golden.py run ... --json --progress lines
    python fake_golden.py init NAME --step STEP --class CLS

`verify` prints the real `aces golden verify --json` record of the VM acceptance of 2026-10-10 verbatim
(rehearsal/reports/2026-10-10-aces-cad-vm/records/golden-handoff-verify.json: 13 checks, 11 ok, rig and resolve not
run) and exits 0. `run` writes three progress lines to stderr (labelled as this fake's) and prints the real result of
that acceptance's golden run (records/golden-handoff.json, run 20261010T102210Z-bc7391) on stdout, exit 0; with
FAKE_GOLDEN_SLEEP=<s> it waits that long first (so a test can cancel it). `init` prints one line and exits 0 without
creating anything. Nothing is generated: the records are the VM's, as recorded. Any other invocation exits 2.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

RECORDS = Path(__file__).resolve().parents[4] / "rehearsal" / "reports" / "2026-10-10-aces-cad-vm" / "records"
RECORD = RECORDS / "golden-handoff-verify.json"
RUN_RECORD = RECORDS / "golden-handoff.json"


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "verify" and "--json" in argv:
        if not RECORD.is_file():
            print(f"fake_golden: the real verify record {RECORD} is absent", file=sys.stderr)
            return 2
        sys.stdout.write(RECORD.read_text(encoding="utf-8"))
        return 0
    if argv and argv[0] == "run" and "--json" in argv:
        if not RUN_RECORD.is_file():
            print(f"fake_golden: the real run record {RUN_RECORD} is absent", file=sys.stderr)
            return 2
        for line in ("fake_golden: stand-in progress 1/3 (sweep)", "fake_golden: stand-in progress 2/3 (surrogate)"):
            print(line, file=sys.stderr, flush=True)
        time.sleep(float(os.environ.get("FAKE_GOLDEN_SLEEP", "0") or 0))
        print("fake_golden: stand-in progress 3/3 (verify)", file=sys.stderr, flush=True)
        sys.stdout.write(RUN_RECORD.read_text(encoding="utf-8"))
        return 0
    if argv and argv[0] == "init" and "--step" in argv:
        print(f"fake_golden: init {argv[1] if len(argv) > 1 else '?'} accepted (nothing created)")
        return 0
    print("fake_golden: only verify, run and init are replayed", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
