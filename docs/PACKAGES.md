# ACES OS package register

State as of 2026-10-09; the section "Changed for the rig lane and the conforming check" is 2026-10-10 and comes from
a builder's worktree, before integration; the section "Changed for the ACES console" is 2026-10-11 and comes from
Builder D's worktree (branch `console/d`), before integration. Packages are built by `bin/build` inside the `aces-builder` image (Arch
`base-devel`) into the local `[aces]` repo at `repo/x86_64/`, and install-tested by `bin/install-test` in a clean
`archlinux:base` container.
Every package and the repo database are signed with the development key (`keys/aces-pkgs.pub.asc`, fingerprint in
`keys/FINGERPRINT`, ed25519, expires 2028-10-08); the install test trusts that key and installs with
`SigLevel = Required DatabaseRequired`. CI (`.github/workflows/build.yml`) repeats the build and the install test on every
push to the private repo `github.com/cpa-techallday/aces-os`, signing with the same key from a repository secret. The key
is a development key: rotate it before anything is distributed.

## Built and tested

| Package | Version | What it is | Build | Test evidence |
|---|---|---|---|---|
| `aces-base` | 0.1.0-2 | The `aces` CLI (standard-library Python: `fabric`, `policy`, `project`, `ledger`, `lane`, `doctor`) and `/etc/aces` defaults (`aces.toml`, `policy.json`, `modes.json`) | built; the PKGBUILD `check()` self-test passed (project, end-use gate, override, 3-entry ledger chain, offline doctor) | Clean container: `aces --version`, `doctor --offline`, `policy show`, `project new/add-component/classify`, `policy override`, `ledger verify` (chain intact), `fabric refresh` reported every endpoint unreachable and derived **no** lanes. 0.1.0-2 adds the `server.webodm` default (`http://127.0.0.1:8090`) for aces-survey; rebuilt and re-tested 2026-10-09 (same CLI checks pass). On DARKNESS against the real rig: GRID CONTROL answered (mode `sim-lab-4`, 8 GPU rows, auto-switching off), rig-state tool call merged per-GPU processes (GPU 4: `VLLM::EngineCore`, 57.6 GB), MLflow on the rig answered HTTP 200, five lanes derived from `modes.json` (see the lane-test table below), two lane tests recorded in the ledger |
| `aces-creative` | 0.1.0-1 | Meta-package over the seven ArtCraft apps, `status.json` with upstream's status words, `aces-creative-labels` | built | Installed with its seven members; `aces-creative-labels` wrote 7 per-user launcher overrides (`ai.storyteller.*.desktop`) |
| `photocraft-bin` | 0.3.0-1 | Pinned copy of the AUR recipe (GitHub release tarball) | built | installed as an `aces-creative` member; not launched (no display in the container) |
| `vectorcraft-bin` | 0.4.0-1 | same | built | same |
| `filmcraft-bin` | 0.2.1-1 | same | built | same |
| `lightcraft-bin` | 0.2.1-1 | same | built | same |
| `printcraft-bin` | 0.2.1-1 | same (upstream now calls it PdfCraft) | built | same |
| `effectcraft-bin` | 0.4.0-1 | same | built | same |
| `designcraft-bin` | 0.2.1-1 | same | built | same |
| `aces-console` | 0.1.0-2 | **The one desktop console (2026-10-11; replaces `aces-consoles`; on main since a84a4d3, 0.1.0-2 = f7712c4 and fd0c222; accepted on the rehearsal VM, see `rehearsal/reports/2026-10-11-aces-console-vm/`).** 0.1.0-2 adds everything GRID CONTROL, the SIMLAB console and MLflow offered: an Agents plate (Mission Control runs, chats, missions, approvals, uploads), SIMLAB actions (every simlab-mcp tool, write tools confirmed by name and ledgered), an MLflow browser (runs, params, metric history, artifacts), the auto-switch, the red-team VM and model staging on Ops; the three replaced pages are listed, never opened; GRID CONTROL reads go through the rig's `/gc` read proxy when the desktop holds no token, every write needs it. A per-user, loopback-only standard-library Python backend (`aces_console`: one envelope with staleness and error text, SSE stamps with a polling fallback, proxies of GRID CONTROL `:3200`, rig-state `:8010` and the SIMLAB console `:8032` with every token held server-side, MLflow REST, the human-only two-step gated and ledgered mode switch, the fabric refresh; records routes for projects, golden runs, CAD runs, bundles, drawings, notes and the operator ledger; file routes through `safefs` under `CSP: sandbox`) and one static Parameter Sky front-end under `/usr/share/aces/console/static` (seven plates: Fabric, Work, Pipeline, Solvers, Objects, Evidence, Ops; plain ES modules, no build step; Three.js r170 and the fonts vendored); one launcher entry **ACES** (`/usr/share/applications/aces.desktop`, `Exec=aces-console open`); `provides`/`conflicts`/`replaces` `aces-consoles` and keeps its `aces-console <server key>` behaviour; the unit tests, fakes, fixtures (with `MANIFEST.json`) and `install_check.py` installed under `/usr/share/aces/console/tests`; licences MIT (aces_console), MIT (Three.js), OFL-1.1 (fonts), `LicenseRef-ACES-undecided` for the five files copied from cudax-sim-runtime (`LICENSES.md`); operator guide and API table in `docs/CONSOLE.md`; decision record `docs/decisions/2026-10-11-aces-console.md`. Depends `aces-base>=0.1.0-3`, `python>=3.11`, `xdg-utils`; optdepends aces-engineering (verify), aces-cad (bundle check), omarchy, libnotify | see "Changed for the ACES console" below | see "Changed for the ACES console" below |
| `aces-consoles` | 0.1.0-2 | **Replaced by `aces-console` 0.1.0-1 on 2026-10-11 (the PKGBUILD stays this release so `pacman -Su` exercises the replacement in the install test).** `aces-console <key>` launcher (now also `webodm`) (reads the URL from the aces config; Omarchy web-app window or `xdg-open`) and desktop entries for GRID CONTROL, SIMLAB, MLflow | built | Installed; the three desktop entries are present. The launcher itself was not exercised (no browser in the container) |
| `aces-omarchy` | 0.1.0-4 | Overlay for Omarchy: `post-boot.d` and `post-update.d` hooks, the `aces-engineering` agent skill, the `aces-fabric` bar-widget plugin, `aces-omarchy-setup` (links every skill any ACES package ships under `/usr/share/aces/*/skills`, copies the plugin, and puts the widget on the bar: through `omarchy plugin enable` when the shell runs, else by adding `{"id": "aces-fabric"}` to the bar's right section in `~/.config/omarchy/shell.json`, which is where the shell's PluginRegistry reads enabled bar widgets from; `--remove` takes it off again) | built | Installed on a real Omarchy 4.0.4 (the rehearsal VM, 2026-10-09): `aces-omarchy-setup` linked the hooks and the skills into `~/.claude`, `~/.codex`, `~/.pi/agent` and `~/.agents`, copied the plugin, and **`omarchy plugin validate` passed on both the copy and the packaged directory**. `qmllint` against Omarchy's own `qs.Commons`/`qs.Ui` modules found two real defects, fixed in -3 (a `state` property shadowing Item's, and an unresolvable parameter type in the `Process.exited` handler); the remaining warning classes also occur in Omarchy's first-party plugins. -4 adds the bar placement: a copied plugin is discovered but off the bar until `shell.json` names it (`omarchy plugin enable` needs the running shell, which an SSH session does not have). Seen rendering live on the VM's desktop the same evening, once the desktop user signed in at the console (captured with `rehearsal/vm-shot.ps1`) |
| `aces-field` | 0.1.0-1 | Meta-package over the Arch-packaged field tools (RF receive and analysis, firmware analysis, device access over SSH, CAN, serial); AUR-only members are optional until forked | built with `--nodeps` | Not installed (multi-GB); `pacman -Sp aces-field` resolves a 372-package transaction against the official repos, so every hard dependency exists |
| `aces-survey` | 0.1.0-1 | Survey and photogrammetry: `aces-survey-setup` (installs the vendored FlyPath 3.0.0 QGIS plugin into the user's QGIS profile and enables it; creates the WebODM tree), `aces-webodm` (start/stop/status/open/update/logs over the pinned WebODM v3.3.0 tree under Docker, GPU node when nvidia-smi and the Docker nvidia runtime are present), a WebODM desktop entry, README with the DJI Fly mission-loading procedure, LICENSES.md. Depends on `qgis`, `python-shapely`, `python-pyproj`, `docker`, `docker-compose`; `cloudcompare`, `colmap`, `meshroom` (AUR) and `nvidia-container-toolkit` optional. FlyPath zip and WebODM release tarball are fetched at build time with pinned sha256s (34.7 MB package) | built with `--nodeps` (QGIS is not needed to build an `any` package) | Clean container: installed with `-Sdd`; `aces-survey-setup` placed FlyPath 3.0.0 and wrote `[PythonPlugins] FlyPath=true`, created the WebODM 3.3.0 tree; `aces-webodm --help` prints usage and `status` reports that docker is missing instead of crashing; `--remove` cleared both. `pacman -Sp aces-survey` resolves a 256-package transaction. On DARKNESS (Windows, Docker Desktop) the same stack runs outside the package: `GPU_NVIDIA=true ./webodm.sh start --port 8090 --detached` brought up webapp, worker, db, broker and `node-odx-1` (`webodm/nodeodx:gpu`), the node sees the RTX PRO 4000, the console answers HTTP 302 on :8090; FlyPath 3.0.0 imports under QGIS 3.44.15 (OSGeo4W, user-profile install) with shapely 2.1.2 and pyproj 3.8.0 |
| `aces-os` | 0.1.0-4 | The meta-package: `aces-base`, `aces-creative`, `aces-field`, `aces-consoles`, `aces-omarchy`, `aces-survey`, `aces-engineering`, `aces-voxel`; `aces-studio` stays optional until the rig's video lane exists; -4 adds `aces-cad-onshape` as an optional dependency (cloud adapter, not installed by default) | built with `--nodeps` (signed, -4 on 2026-10-09 14:50 UTC) | `pacman -Sp aces-os` resolves in a clean signed container: 508 packages when aces-base, aces-creative, aces-consoles and aces-omarchy are already installed, 529 when aces-base, aces-engineering and aces-voxel are (qgis, docker and the engineering venv's libraries included) |
| `aces-engineering` | 0.1.0-1 | One locked Python 3.14 venv at `/opt/aces/engineering/venv` (76 distributions, each hash-pinned in `requirements.txt`, binary wheels only; 907.6 MiB installed, 255 MB package) with a vendored snapshot of cudax-sim-runtime's `simlab` and `gendesign` (commit `28d92a8`, content sha256 in `SNAPSHOT`) on its path. Commands: `aces-simlab` (validate, local run, report, evidence, surrogate, export, capacity, `remote` MCP client), `aces-evidence` (ledger and bundle verification, relocates ledgers from other machines), `aces-gendesign` (list, selftest, topopt, parametric, nozzle, fusion); `mcp.json.example` for the rig's simlab `:8022` and rig-state `:8010`; LICENSES.md. Not installed: rig-only rq, redis, uvicorn, mcp, starlette, gmsh, rocketcea, physicsnemo, and the GPU packages cupy, warp, torch (mlflow too); the wrappers keep the queue, leases, MCP-server and console commands off the desktop CLI. Depends on `libglvnd` and `libx11` (the OCP wheel links libGL and libX11) | built (x86_64, signed); `check()` = `aces-gendesign selftest` passed | `check()`: 56/56 imports, fea3d element test, the 1 mm cantilever on NumPy reproduces the workstation's GPU record (0.1989054912444315 mm) to 8.3e-13 in 401 PCG iterations, evidence bundle and chain verified, md/html/docx report, 8 SIMP iterations, L-bracket STEP round trip at 26,694 voxels as on the workstation (needs `embreex`; trimesh's rtree fallback gives 26,616, all 78 differences on one 45-degree face). Clean container (own script, `SigLevel = Optional TrustAll`): relocated shebangs, `pyvenv.cfg` and bytecode paths, selftest again, `aces-voxel fea` of the bracket STEP at 2 mm (converged, 1,232 iterations), the workstation's 4-bundle evidence ledger verified (4 entries relocated) and a hand-edited bundle rejected, `aces-simlab remote` against a local mcp 2.0.0 server (JSON and SSE replies, bearer token, 401 on a wrong one), a 10-trial parametric study and a GP surrogate in a per-user registry, `pacman -R` leaves nothing under `/opt/aces`. After the full signed rebuild, `bin/install-test aces-base aces-engineering aces-voxel` passed under `SigLevel = Required DatabaseRequired`: `aces-engineering selftest: all hard checks passed`, `aces-voxel fea --self-test` PASS (STEP round trip at 26,694 voxels in 0.3 s) |
| `aces-voxel` | 0.1.0-1 | `aces-voxel`: `fea` (STEP to voxel H8 FEA through simlab's step-structures stages, or `--self-test`), `topopt` (3-D SIMP), `lattice` (Warp rod lattices, GPU only), `export` (density to STL); `docs/VOXEL_KERNEL.md` (what is standard, what is not, the source modules) in `/usr/share/doc/aces-voxel`; an `aces-voxel` agent skill in `/usr/share/aces/voxel/skills` (linked by `aces-omarchy-setup` 0.1.0-2). Runs in the aces-engineering venv, no second venv | built (any, signed) with its dependency installed (`pkgbuilds/ORDER` no longer marks it `nodeps`), so `check()` runs against the installed aces-engineering: fea self-test, 6 SIMP iterations, export, a build123d bar STEP through `fea`, `lattice` exits 2 without a GPU | Clean container: `--help`, `fea --self-test`, `fea` of the bracket STEP, `export` of a SIMP density, `lattice` exit 2 with its reason |
| `aces-cad-onshape` | 0.1.0-2 | -2 changes two unit tests only: the reply-that-cannot-be-encoded test uses a circular reference instead of a list nested 100,000 deep (json encoded that list on the GitHub runner, whose containers run with an unlimited stack, so CI run 37948580881 failed the package's `check()` while the same test passed on DARKNESS and the VM with the default 8 MB stack). Onshape adapter (cloud; every write, evaluation, export and pull runs the project class gate and is refused for sovereign and controlled projects): standard-library REST client, `aces-onshape` CLI (whoami, documents, create-document, elements, fs read/write, eval, features, add-feature, add-custom-feature, update-feature, delete-feature, version, massprops, export step/stl, pull), `aces-onshape-mcp` stdio MCP server (17 tools; descriptions name every write; changing a feature or Feature Studio it did not create needs confirm_foreign, writing into a public document needs allow_public), the `aces-cad-onshape` agent skill in `/usr/share/aces/cad-onshape/skills`, `mcp.json.example` with this server and the official Onshape FeatureScript MCP Server (Onshape Labs, `https://fs-mcp.labs.onshape.app/mcp`, browser sign-in after an App Store subscription). `aces-onshape pull onshape://<did>/<wid>/<eid> --project NAME` writes `part.step`, `parameters.json`, `source.json` and `outcome.json` under `~/Work/NAME/cad/onshape/` and a `cad.pull` ledger entry with the sha256 of the first three where `aces` offers `ledger record` | built (any, signed) on main, 0.1.0-2 on 2026-10-09 15:14 UTC with `bin/build aces-cad-onshape`: `check()` ran the 69 unit tests (the 62 originals plus the seven review regressions) against the fake Onshape server, all OK on Python 3.14.7, namcap reported nothing; the same 69 pass in the builder image with the runner's unlimited stack (`docker run --ulimit stack=-1:-1`), which is where 0.1.0-1's test 34c had failed in CI. 0.1.0-1 (14:49 UTC, same sources apart from that test) and the earlier unsigned worktree build (62 tests, before the review fixes) are superseded | Clean container, the default `bin/install-test` set (aces-base, aces-creative, aces-consoles, aces-omarchy, aces-cad-onshape) from the signed repo (`SigLevel = Required DatabaseRequired`, 2026-10-09 14:52 UTC, exit 0): `aces-onshape --help`; `aces-onshape whoami` exits 3 with `no Onshape API keys: ...`; the installed tests in `/usr/share/aces/cad-onshape/tests` 69/69 as root against the installed package; `pacman -Sp aces-os` (0.1.0-4) resolves to 508 packages. Earlier, unsigned, with `bin/install-test aces-base aces-omarchy aces-cad-onshape` (62/62 before the review fixes): `aces-omarchy-setup` links the skill as `~/.claude/skills/aces-cad-onshape` and `~/.agents/skills/aces-cad-onshape`, and `--remove` unlinks both. Workstation: 62 tests OK on Python 3.11.9 and 3.13.13 (one skipped there, the POSIX file-mode test), 62/62 on 3.14.7 in `aces-builder`; the stdio MCP server driven through the skill's whole loop against the fake server. Real Omarchy 4.0.4 (the rehearsal VM, 2026-10-09 14:55 UTC): `pacman -Sy aces-cad-onshape aces-os` from the signed LAN repo installed 0.1.0-1 and upgraded aces-os to 0.1.0-4 (pacman lists the new optional dependency as installed); `aces-onshape whoami` exits 3 with the keys message; the installed tests 69/69; `aces-omarchy-setup` linked the skill into `~/.claude/skills` and `~/.agents/skills`; `aces-onshape-mcp` answered `initialize` and `tools/list` (17 tools) over stdio with `ACES_ONSHAPE_PROJECT=golden-demo`; upgraded to 0.1.0-2 from the same repo at 15:16 UTC, installed tests 69/69 again, whoami exit 3. No live Onshape run yet (no credentials on DARKNESS or the VM) |
| `aces-cad` | 0.1.0-1 | Local text-to-CAD (no network, no quota, every project class): agents write build123d Python; `aces-cad-mcp` (stdio MCP server, 8 tools) and the `aces-cad` CLI run it in a sandboxed child of the aces-engineering venv (seccomp, Landlock, rlimits, a scrubbed environment; strict mode by default), report volume, area, bbox, validity and topology, inspect (mass, validity, holes, thickness, draft, radii, interference, section, measure, design space), render PNGs in the golden-path look, draw dimensioned DXF/SVG/PNG, and pull a run into `cad/local/` as STEP + parameters + sha256 with a `cad.pull` ledger entry; the `aces-cad` agent skill; no venv of its own (section "Local CAD" below) | built (any) from branch `cad-local/integration` at 3651c59 (the review fixes, on top of main 954332c) with `bin/build aces-cad` in the integration worktree, **unsigned** (the worktree has no secret key; its repo holds unsigned copies of main's packages), 2026-10-10 09:38 UTC, against main's aces-engineering 0.1.0-4: `check()` ran the 114 tests as the `builder` user on Python 3.14.7 in strict mode (no "strict-mode tests skipped" line), OK in 444.7 s with one skip (S02b: the makepkg tree has no lock file); namcap printed nothing; the package holds 143 files, 57 fixture scripts, 27 checked-hash `.pyc` and the two shims at 755, no `__pycache__` under the tests, and no payload file names the build directory (makepkg's own `.BUILDINFO` does). Earlier builds of the branch, against aces-engineering 0.1.0-2: 032c1d0 (108 tests, OK in 641.9 s) and 6e3f6ef (107, OK in 428.2 s), with the same skip | Clean `archlinux:base`, `bin/install-test aces-base aces-engineering aces-cad` from that unsigned repo (`SigLevel = Optional TrustAll`, 2026-10-10 09:42-09:51 UTC, exit 0), as root, run the way CI runs it (after the engineering step, its golden records moved aside first): `aces-cad doctor` ready, mode **strict**, seccomp active (106 BPF instructions, TSYNC), Landlock ABI 3 (no TCP rules and no scoping at that ABI; no root skipped), 14 of 14 checks passed, memory, CPU and file-size limits enforced, PDEATHSIG on; worker Python 3.14.7, build123d 0.11.1, OCP 7.9.3.1, ezdxf 1.4.4, lib3mf 2.5.0, trimesh 5.1.0, numpy 2.4.6; the ACES plate 11607.300918301276 mm³, counts 1/1/7/15/10, valid, 2.5 s wall (imports 1.6 s), peak 500.9 MB; inspect 0.030991493451864402 kg in AlSi10Mg, one simple through Ø10 hole entering at (30, 20, 5), minimum thickness 5.0 mm (embree), validity ok; iso and front renders at 1024 × 800; drawing texts 60, 5, 40, Ø10 THRU, 30, 20 with 0 audit errors; the pull wrote `part.step`, `part.stl`, `part.3mf`, `parameters.json`, `source.py`, `source.json` and `outcome.json`, `geometry_sha256` `806c9be7a86c8ae60308ee75d9a910f371d5f5aae6fe6517fa5ce6aa5fd1742f` (a run named `plate`), the `cad.pull` ledger entry 7 (class sovereign), the CAD source recorded, `check_bundle.py` all files match, `aces ledger verify` ok (7 entries); the socket script kind sandbox exit 5, the loop kind timeout exit 4 (3.0 s); the installed tests as root 114, OK in 438.0 s (one skip, S02b); the golden block passed (23 s; no CalculiX in this set, so no conforming check). `pacman -Sp aces-os` (0.1.0-6): 522 packages after this set, 521 after the engineering step's set, 510 in the default install test (main's 0.1.0-5 there: 509). Earlier, at 032c1d0 with aces-engineering 0.1.0-2: the same block with 108 installed tests, and as the non-root `builder` user the installed tests, `aces-cad selftest` (13 passed, 0 failed, 0 skipped) and the stdio MCP loop (17 of 17; section "Local CAD") Rebuilt and signed from main 61ac8a4 on 2026-10-10 at 10:13 UTC (`bin/build aces-cad`, then `aces-os` 0.1.0-6), the packages the LAN repo served to the VM; the VM acceptance of the same day passed (the "VM acceptance" paragraph of the Local CAD section and `rehearsal/README.md`). |

## Changed for the golden path

These versions were built in the Arch builder (`bin/build aces-base aces-omarchy aces-engineering aces-voxel`, signed)
and install-tested in a clean container (`bin/install-test` and `bin/install-test aces-base aces-engineering
aces-voxel`) on DARKNESS on 2026-10-09 (UTC), last after the review fixes (the same version-releases: none had been
published); the rows above describe the previous builds. The rehearsal VM has run these versions (the golden-path table
below, run `20261009T144006Z-b3ac99`), and CI has built them and passed both install tests (run 37946810947; only its
log upload failed, on file permissions, fixed with the merge that followed). The rehearsal web root
(`D:\HyperV\aces-rehearsal\serve\www`) had still served aces-base 0.1.0-2, aces-engineering 0.1.0-1, aces-voxel 0.1.0-1
and aces-omarchy 0.1.0-4 and an `aces-install.sh` without the golden block; `rehearsal\serve-repo.ps1` re-synced it
after the last build (signed, the four new versions and their `.sig`, the golden block; checked byte for byte against
`repo\x86_64` before the VM run). Re-run it after any rebuild before the VM step, and upgrade aces-omarchy there too so
the VM's agent skill documents `aces golden`. Before the VM step the VM itself had to be restarted: its virtual disks sit
on a USB SSD that stalled on the evening of 2026-10-09, and the guest's btrfs root had gone read-only (see
`rehearsal/README.md`).

| Package | Version | What changed | Test evidence |
|---|---|---|---|
| `aces-base` | 0.1.0-3 | `aces ledger record KIND --json FILE\|-` (kind and payload checks; `policy.`, `project.`, `lane.`, `ledger.` reserved; 64 KiB payload limit; `--print json`), `aces ledger verify --json`, `aces ledger show --kind K --json` (entries carry their position `n`), an exclusive writer lock around read-last-hash-and-append, `aces project add-evidence` and `add-cad-source`, the `aces golden` passthrough (exit 127 with the package to install when `aces-golden` is missing; `ACES_GOLDEN_CMD` for development; on that non-exec path a Ctrl-C reaches the child from the same console and `aces` waits for its interrupted run records, then returns its 130), a `golden` row in `aces doctor`, `python -m aces`; `check()` adds the unit tests (`tests/test_ledger_record.py`, including two concurrent writers) and inline asserts for the new commands | built (any, signed); `check()` on Python 3.14.7: the 3-entry self-check, 17 unit tests OK, then `ledger record golden.run` as entry 4, `policy.override` and `Bad Kind` refused with exit 2, verify, `project add-evidence`, `ledger show --json`, the passthrough 127 without `aces-golden` and 3 from a stub. Clean container (`SigLevel = Required DatabaseRequired`): `aces ledger verify` ok with 5 entries after the golden run |
| `aces-engineering` | 0.1.0-2 | `aces-golden` (`aces_engineering.golden_cli` and the `aces_engineering.golden` package: spec, demo, domain, generator, organic skin, FEA, cases, sweep, export, renders, compute detection, surrogate, optimiser, verification, evidence, report, run directory, progress, pipeline); depends on `aces-base>=0.1.0-3`; `check()` runs the golden unit tests, `aces golden run --demo` (printing the run's log when it fails, and requiring exactly one JSON document on stdout) and `aces golden verify latest` after the selftest; the selftest imports the golden modules and tqdm, threadpoolctl, psutil. After review: every support and load box face sits on the grid that `gen_mm`, `fea_mm` and the planned check levels share (the refusal names the nearest box; `--demo --res 5` used to fix 42 nodes at 5 mm and 150 at 2.5 mm), a traction plane must be an envelope face, one load case per spec, the seed is a 32-bit unsigned integer, the stress-exclusion zones are rounded up to the solve grid so every resolution excludes the same zone (the demo's 6 mm becomes 8 mm; `domain.json` records it), a failed kernel self-test stops the run before the project and run directory (exit 7), the CPU-only cell warning is emitted, the verdict line labels its stage-time sum, the audit reports the compute table as INFO for a run made by other golden code, and the report's stage-time sum and its provenance check share one plain-addition helper | built (x86_64, signed). The first build failed `check()`: the gyroid cutaway render called `Trimesh.slice_plane`, which imports shapely, and shapely is not in the lock (the workstation venv had it from the system site-packages); the render now uses `trimesh.intersections.slice_faces_plane` (numpy only). The first rebuild after review failed `check()` in `aces golden verify`: the report summed the stage times with `sum()`, which is compensated on Python 3.12 and later, the verifier with a plain loop (29.057 against 29.057000000000002); both now use one helper, and the compute-used sums use `math.fsum`. Rebuilt: selftest all hard checks passed (81/81 imports), 169 golden unit tests OK (2 skipped) on Python 3.14.7, `aces golden run --demo` PASS · verified-desktop (run `20261009T142505Z-b3ac99`, bundle `a24c2ae54277`; 2 mm check vm_p99 56.88 MPa, +4.4 %), `aces golden verify latest` all checks pass. Clean container: selftest passed, golden block in the table below |
| `aces-voxel` | 0.1.0-2 | the skill names the end-to-end generative loop (`aces golden run --demo`); depends on `aces-engineering>=0.1.0-2`, the first version with `aces golden` | built (any, signed); its `check()` ran against the installed aces-engineering 0.1.0-2 (the build after review). Clean container with aces-engineering 0.1.0-2: `aces-voxel fea --self-test` OK |
| `aces-omarchy` | 0.1.0-5 | the aces-engineering skill documents `aces golden` (commands, grades, exit codes including the passthrough's 127, rules); depends on `aces-base>=0.1.0-3`, the first version with the commands the skill documents | built (any, signed); default install test (`bin/install-test`: aces-base, aces-creative, aces-consoles, aces-omarchy) passed, `aces-omarchy-setup` linked the skill into `~/.claude/skills` and `~/.agents/skills` |

## Changed for the rig lane and the conforming check (2026-10-10, branch `golden-rig/packaging`, not yet integrated)

Built and tested by the packaging builder in its own worktree (`aces-os-golden-packaging`, unsigned: there is no key
there), on DARKNESS. The cells hold only what was run there. The integration build and install test (with the
cudax snapshot `rig-golden-1` and the golden rig client merged) and the operator's rig runs come later and get their
own rows.

| Package | Version | What changed | Build | Test evidence |
|---|---|---|---|---|
| `calculix-ccx-bin` | 2.23-1 | New: the CalculiX author's ccx 2.23 binary (`http://www.dhondt.de/ccx_2.23.tar.bz2`, sha256 `35d426fed5eb164fbbaaafa20819d13d22e30bc2b9bc9c6c4ed957e9468355dd`, unpacked with bsdtar in `prepare()`) as `/usr/bin/ccx_2.23` with the symlink `/usr/bin/ccx`; licence GPL-2.0-or-later plus SPOOLES (public domain) and ARPACK/LAPACK/BLAS (BSD-3-Clause) found in the binary; depends glibc, libgcc, libgomp, libquadmath; `check()` requires `ldd` without "not found" and `Version 2.23` from `ccx -v` (which exits 201). `pkgbuilds/ORDER` builds it before aces-engineering | `bash bin/build calculix-ccx-bin`: **failed in `check()`**, `libgfortran.so.4 => not found`. The binary was linked against GCC 7's Fortran runtime (symbol version GFORTRAN_7); Arch ships only `libgfortran.so.5`, and no Arch package provides `.so.4` | Not installable from this repo (`bin/install-test aces-base aces-engineering aces-voxel calculix-ccx-bin`: `error: target not found: calculix-ccx-bin`). The binary itself: with `libgfortran.so.4` from Ubuntu 20.04's `libgfortran4` 7.5.0-6ubuntu2 on the loader path it ran in a throwaway `archlinux:base` container and wrote a `ccx.dat` byte-identical to the Ubuntu run; in a throwaway Ubuntu 24.04 test image with the same runtime, the 35 conforming tests passed, including the bar patch test (exact to 1e-6) and the cantilever (mean tip u_z 0.70 % from Timoshenko's 0.20153 mm) |
| `aces-engineering` | 0.1.0-3 | gmsh 4.15.2 in the lock (77 distributions; psutil, threadpoolctl and tqdm become direct pins at their locked versions); `aces_engineering.golden.conforming` (the C3D10 + CalculiX second discretisation); report sections 10 (conforming) and 11 (rig), the check table's host column and the new verdict-line fragments; the selftest's `conforming` check and the rig and conforming modules in its import list; depends += glu, libxcursor, libxft, libxinerama, libxrender, libxfixes, libxext, fontconfig (what libgmsh needs beyond libGL and libX11); optdepends and checkdepends calculix-ccx-bin; licence += `LicenseRef-Gmsh-GPL-2.0-or-later-with-exceptions`; `build()` first checks the vendored snapshot against two pins (`_snapshot_commit`, `_snapshot_content_sha256`, re-hashing `vendor/` with `sha256sum -b`); `sync.sh --commit REV` copies from a clean clone; `check()`'s demo runs with `--conforming off` | **Not built.** `makepkg -s` cannot install its checkdepends (calculix-ccx-bin, above), and on this branch alone the selftest's import check names nine modules that only arrive with the integration (dev layout: `83/92 import`, missing `aces_engineering.golden.rig`, `.rigside`, `simlab.filestore`, `simlab.specstore`, `simlab.solvers.golden_case`, `.golden_check`, `.ccx_check`, `gendesign.mg`, `gendesign.connectivity`) | `bash src/aces-engineering/lock.sh` (Python 3.14.7, pip 26.2.1): 77 distributions, the diff adds gmsh and moves the three pins with unchanged hashes. Snapshot gate in `aces-builder`: the committed tree passes; one appended line in a vendored file, and a SNAPSHOT naming another commit, are both refused. `sync.sh --commit 28d92a8` reproduced the committed snapshot exactly (51 files, content sha256 `3ed0ee68...`). The locked gmsh wheel in a clean `archlinux:base` with exactly the declared libraries: `ldd` finds every library of `libgmsh.so.4.15`, `import gmsh` reports 4.15.2 on Python 3.14.7, and an OCC box meshes (1,140 tets). Unit tests in the dev layout (workstation, Python 3.11.9): 215 golden tests OK, 5 skipped (2 of them the ccx tests). Ubuntu 24.04 test image with ccx 2.23: the 35 conforming tests OK; the selftest's `conforming` check PASS (539 C3D10, worst relative difference 2.3e-08, equilibrium 5.4e-09), INFO without ccx, FAIL with a ccx that cannot start |
| `aces-omarchy` | 0.1.0-6 | the aces-engineering skill: the grade ladder and `grade_reasons`, `--lane` only on request and never in CI, lanes only from the fabric manifest, mode switching stays human, never a desktop result as a rig result, the new flags and exit codes, the 13 audit checks | built (any, unsigned) with `bash bin/build aces-base aces-omarchy aces-os` | `bash bin/install-test aces-base aces-omarchy` exit 0: `aces-omarchy-setup` linked the skill into `~/.claude/skills` and `~/.agents/skills` (`aces doctor`: `skills ok`) and `--remove` unlinked both; the packaged `SKILL.md` carries the new rules |
| `aces-os` | 0.1.0-5 | depends += `calculix-ccx-bin` | built with `--nodeps` (unsigned) in the same `bin/build` call; `.PKGINFO` lists `depend = calculix-ccx-bin` | `pacman -Sp aces-os` does not resolve in this worktree's repo (most of its members were not built there, and calculix-ccx-bin cannot be): `error: failed to prepare transaction (could not satisfy dependencies)` |

`bin/install-test` gains, for the engineering block: the ccx version when ccx is present; `import gmsh` in the
packaged venv as a hard check; `aces golden compute --lane sim/pool`, which must exit 2 from the container and must
not be an argparse usage error; with calculix-ccx-bin installed, a conforming status other than `ccx-not-found` or
`gmsh-not-found` (printed, not gated on `ran`); `m001/conforming.json` and `m001/ccx.log` copied to `logs/golden/`.
CI's engineering step installs `calculix-ccx-bin` too. The block's logic was exercised with stub commands in five
cases (check ran; no ccx; ccx installed but not found by the check; `compute` without `--lane`; a usable lane), all
as expected; the real block has not run, since aces-engineering 0.1.0-3 was not built.

## Integration build after review (2026-10-10, branch `golden-rig/integration`)

Built and install-tested by the integration fixer in its own worktree (`aces-os-golden-integration`, unsigned: there
is no key there) on DARKNESS, at 6db440f (calculix-ccx-bin and aces-engineering built again after the `bin/build`
change below): the integrated tree (the cudax snapshot `rig-golden-1` and the golden rig client, 30bee52) plus the
fixes for review findings 1 to 4. The cells hold only what was run there; the rows above
stay as the packaging branch measured them. The engineering block of `bin/install-test` ran here from the committed
recipe; the integrator had run it before only with an uncommitted variant of calculix-ccx-bin in a separate worktree.

| Package | Version | What changed | Build | Test evidence |
|---|---|---|---|---|
| `calculix-ccx-bin` | 2.23-1 | Review finding 4: the package brings the GCC 7 Fortran runtime the binary needs. A second pinned source, Ubuntu 20.04's `libgfortran4_7.5.0-6ubuntu2_amd64.deb` (`archive.ubuntu.com`, 492,528 bytes, sha256 `9191a744ea8e1cb877c26bfb1cb6efa06305be49c7053eecfee5f1037a8005d5`), the deb the rig image takes it from; `prepare()` checks its `libgfortran.so.4.0.0` against `3f198ada9cdcda0c6b2fba24d6654c839ea91d0e8246c8ddba5b1b2851dff4b2`, the sha256 the image pins; installed privately as `/usr/lib/calculix-ccx-bin/libgfortran.so.4.0.0` (with the `.so.4` link) behind `/etc/ld.so.conf.d/calculix-ccx-bin.conf` (the reviewer's option (a); the operator's decision 1 named the same deb with a wrapper at `/usr/bin/ccx`, see Known gaps); licence += `GPL-3.0-or-later WITH GCC-exception-3.1`. The ccx binary is unchanged (no wrapper script, no patchelf), so the ccx sha256 a conforming record names is the image's | `bash bin/build calculix-ccx-bin` exit 0: both sources `Passed`, `libgfortran.so.4.0.0: OK`, in `check()` `libgfortran.so.4 => /build/work/src/gf4/libgfortran.so.4` and `This is Version 2.23`, `Finished making: calculix-ccx-bin 2.23-1`. In the package: `usr/bin/ccx_2.23` sha256 `31be21fc2f0902bd9a05acc2651dbac6dc2a2573dabbf235e39a38cb6f458862`, `.PKGINFO` depends glibc, libgcc, libgomp, libquadmath | installed by `makepkg -s` as aces-engineering's checkdepends, and by `bin/install-test aces-base aces-engineering aces-voxel calculix-ccx-bin` in a clean `archlinux:base`: `ccx: /usr/sbin/ccx: This is Version 2.23`; the demo's conforming record names solver sha256 `31be21fc...` (above), `*STATIC, SOLVER=SPOOLES`, rc 0 |
| `aces-engineering` | 0.1.0-3 | the integrated tree with the fixes for review findings 1 (a mesh above `SPOOLES_MAX_DOF`, 800,000 DOF, is `too-large`, not run), 2 and 3 (a file the rig will not hand over, or an internal error in a rig case, is not delivered and falls back to the desktop) | `bash bin/build aces-engineering aces-omarchy aces-os` exit 0 (07:14:43Z to 07:21:39Z), `Finished making: aces-engineering 0.1.0-3`. `check()` with calculix-ccx-bin installed: selftest `92/92 import`, `conforming` PASS (bar patch test, 539 C3D10 at 1 mm on the remesh route, ccx 2.23, worst rel diff 2.3e-08, equilibrium 5.4e-09), `all hard checks passed`; golden tests `Ran 300 tests in 222.753s`, `OK (skipped=2)`; the demo with `--conforming off`, `aces golden verify 20261010T071953Z-b3ac99: all checks pass`; venv 1.1G (78 distributions) | `bin/install-test aces-base aces-engineering aces-voxel calculix-ccx-bin` exit 0: `import gmsh` 4.15.2; `aces golden compute --lane sim/pool` exit 2 (`it is not in the fabric manifest (sim lanes: none); nothing was submitted`); `aces golden run --demo` exit 0 after 53 s, PASS, verified-desktop, `golden: conforming status ok \| agrees within its bands and meets every constraint` (row in the golden path table below); `aces golden verify` all checks pass (11 PASS, 2 INFO: rig, resolve); `aces ledger verify` 5 entries, chain intact; `aces-evidence verify` ok |
| `aces-omarchy` | 0.1.0-6 | no change since the packaging branch; rebuilt in the same call | built (any, unsigned) | `bash bin/install-test` (default set) exit 0: `aces-omarchy-setup` linked the skills into `~/.claude/skills` and `~/.agents/skills`, `aces doctor`: `skills ok`, and `--remove` unlinked them |
| `aces-os` | 0.1.0-5 | no change since the packaging branch; rebuilt with `--nodeps` | built (any, unsigned) | resolves now: `aces-os: resolves, 509 packages in the transaction` (default install test) and 520 (engineering install test), next to `aces-field` 372 and 375 and `aces-survey` 256 and 261 |
| `aces-engineering` | 0.1.0-4 | the vendored cudax snapshot moves from rig-golden-1 (16aedb1) to rig-golden-1.1 (62de981): one file, `simlab/solvers/step_structures.py`, now the version the rig runs (the rig operator's in-place patch of 2026-10-06: per-solid voxel union for non-watertight multi-solid STEPs, `emin`, the `solver` key with default `mg`, merged with the spec's `fea_solver`, which wins); SNAPSHOT and the two PKGBUILD pins updated (content sha256 `da7c28a3...`). Also in this build: the conforming mesher always meshes single-threaded (`MESH_THREADS = 1`, operator decision 3: with more threads gmsh 4.15.2 gives a different mesh each run), the case's thread count applying to the ccx solve alone, both counts recorded | `bash bin/build aces-engineering` exit 0 on main 6e54615 (08:29:43Z to 08:37:08Z, signed): selftest `all hard checks passed` (conforming PASS), `Ran 301 tests ... OK (skipped=2)`, `aces golden verify 20261010T083551Z-b3ac99: all checks pass`, venv 1.1G (78 distributions), package 295,086,073 bytes | installed on the rehearsal VM from the signed LAN repo with calculix-ccx-bin 2.23-1, aces-omarchy 0.1.0-6 and aces-os 0.1.0-5 (`sudo pacman -Sy ...` exit 0; `ccx -v`: `This is Version 2.23`; `/usr/lib/calculix-ccx-bin/libgfortran.so.4.0.0` present); the VM rows in the golden path tables below |
| `aces-engineering` | 0.1.0-5 | `golden_cli.devices_used` parses every `rig gpu N (name)` of a compute-used row's device string (a row that ran on several rig GPUs joins them with ", "); a GPU recorded without its name is reported as such, never under an invented name | `bash bin/build aces-engineering` exit 0 on main cb4e7ab (09:16 to 09:21 UTC, signed): selftest `all hard checks passed`, `Ran 302 tests ... OK (skipped=2)`, `aces golden verify 20261010T091959Z-b3ac99: all checks pass`, venv 1.1G (78 distributions), package 295,061,113 bytes | installed on the rehearsal VM (`pacman -Sy aces-engineering`, 0.1.0-4 to 0.1.0-5). Re-rendering the bench rows of the acceptance runs with this code: the --rig-sweep run `20261010T084243Z-b3ac99` now reads `cpu/numpy, NVIDIA RTX PRO 4000 Blackwell SFF Edition (cupy, warp), rig: NVIDIA CMP 170HX x3 (cupy, mg), rig cpu (ccx x16)` (GPUs 0, 2 and 3, as its records say) where 0.1.0-4 had written a mis-joined name; the single-GPU runs render as before |

Both install tests ran `aces fabric refresh`, a step `bin/install-test` has always had, which probes the configured
rig endpoints: from the containers every probe timed out (`unreachable: <urlopen error timed out>`) and the manifest
listed no lanes.

`bin/build` now also drops cached `calculix-ccx-bin-*` files before each build, as it already did for `aces-*` and
`*craft-bin-*` (found while verifying finding 4). pacman does not fetch a cached file of the expected size again, so
after a same-version rebuild of calculix-ccx-bin that compresses to the same size, the next aces-engineering build
would install the stale copy and stop. Reproduced with a fixture, the repo's file with its last byte changed, in
`cache/pacman` (same size, 3,420,468 bytes), through the committed pacman steps: no download, then `File
/var/cache/pacman/pkg/calculix-ccx-bin-2.23-1-x86_64.pkg.tar.zst is corrupted (invalid or corrupted package
(checksum))`, exit 1 (a rebuilt file one byte longer than the cached one had been fetched again and installed). With
the change, over the same kind of fixture: `bash bin/build calculix-ccx-bin` exit 0, then `bash bin/build
aces-engineering` exit 0 (07:41:48Z to 07:51:47Z; pacman fetched calculix-ccx-bin, `Total Download Size: 3.26 MiB`;
`check()` again `92/92 import`, `conforming` PASS, `Ran 300 tests in 344.088s`, `OK (skipped=2)`, `aces golden verify
20261010T074932Z-b3ac99: all checks pass`), and `bin/install-test aces-base aces-engineering aces-voxel
calculix-ccx-bin` exit 0 again (the second integration row of the golden path table).

GitHub Actions on main 954332c (run 38039642430, 2026-10-10 08:56:51 to 09:16:20 UTC, 19 min 29 s): every package
built and signed in the Arch builder container (calculix-ccx-bin fetched its two pinned sources), the default install
test and the engineering install test (CalculiX and the golden path) passed. cudax-sim-runtime CI on main ee67548
(run 38039674084) and 36d0446 (run 38040391660) passed on Python 3.11 and 3.12.

GitHub Actions on main e165872 (run 38045748248, 2026-10-10 10:40:57 to 11:23:08 UTC, 42 min 11 s, the aces-cad merge
61ac8a4 plus the acceptance docs): every package built and signed in the Arch builder container, aces-cad 0.1.0-1 and
aces-os 0.1.0-6 among them; the default install test, the engineering install test (CalculiX and the golden path:
PASS / verified-desktop in 51 s, `aces golden verify` all checks pass) and the new aces-cad install test passed. On
the runner (ubuntu-latest, Docker's default seccomp profile) the aces-cad doctor ran strict and confined with Landlock
ABI 7 (18 rules), so GitHub runners allow the Landlock syscalls and the TCP rules and scoping are in force there too;
the ACES plate exact (11607.300918301276 mm³), its geometry_sha256 `806c9be7…` equal to the Windows, Arch and VM
values, `check_bundle.py` clean, the ledger intact (7 entries), 114 unit tests OK in 592 s (one skip), and aces-os
resolving to 522 packages.

## Changed for the ACES console (2026-10-11, branch `console/d`, not yet integrated)

Built and tested by Builder D in its own worktree on DARKNESS (Windows 11; Python 3.11.9 on the PATH; Docker Desktop
29.6.1 with the `aces-builder` image; no makepkg or namcap outside the container; no signing key in the worktree, so
the container builds are unsigned and the install test ran with `SigLevel = Optional TrustAll`). The branch merges
`console/a` (the backend core) and `console/c` (the shell, the Fabric, Work and Ops plates, the browser harness) and
adds the four records plates, the packaging and the docs. Builder B's backend modules (`sim`, `records`, `ledgers`,
`system`) are not in this tree: `/api/health.modules` reports them `not installed` here, the records plates were
verified against the harness devserver's records routes (`tests/harness/records_routes.py`, the same `safefs`) over
the copied rehearsal-VM work store (`vm-work`: projects cad-vm, cad-gold, golden-demo; ledger 11 entries), and the
install check of spec 7.2 fails honestly at its `/api/ledger` assertion until those modules are merged. The cells hold
only what was run here.

| Package | Version | What changed | Build | Test evidence |
|---|---|---|---|---|
| `aces-console` | 0.1.0-1 | new (the register row above) | `bash bin/build aces-base aces-consoles aces-console`, then after two test fixes (below) `bash bin/build aces-console`, on DARKNESS in the `aces-builder` image, unsigned, 2026-10-11 03:02:41 to 03:03:11 UTC: `Finished making: aces-console 0.1.0-1` (`any`, 1,417,930 bytes); `check()` ran the 91 unit tests as the `builder` user on Python 3.14 against the installed aces-base 0.1.0-4: `Ran 91 tests in 19.265s`, `OK (skipped=3)` (the three records-route tests that need the copied VM store); namcap printed nothing. The package holds `usr/bin/aces-console` (the `python3 -I` shim), `aces_console` with `vocab.json`, the static tree with its vendor files, the tests with fakes, fixtures and `install_check.py`, `aces.desktop`, the README and the example unit, LICENSE and LICENSES.md. The first build of the day failed in `check()` (2 failures, 1 error) on paths the Windows runs had not exercised: the no-VM-store branches of two tests assumed an absent `ACES_WORK` where the tests' scratch store exists but is empty, and A's fabric test needed the shipped `modes.json`, which `tests/__init__.py` copied only from a source tree beside the package; fixed in `tests/__init__.py` (falls back to the installed `/etc/aces`), `tests/test_harness.py` and `tests/test_records_plates.py`, and the same tests pass on DARKNESS with the VM store hidden (`ACES_CONSOLE_VM_WORK` pointing nowhere) | On DARKNESS, the a+c+d tree, Python 3.11.9: `python -m unittest discover -s src/aces-console/tests -t src/aces-console` → `Ran 91 tests in 103.324s`, `OK (skipped=1)` (A's server, SSE, poller, rig-proxy and safefs tests against the fakes; C's harness tests; the 7 records-route tests against the copied VM store: golden runs 4, cad-gold detail 234 files, `/api/cad/runs?project=cad-vm` 16 runs, 4 bundles, 2 drawings, 1 note, file routes with `CSP: sandbox`, ETag and 304, `..` → 400, dotfile/`.env`/`token` → 403, `.3mf`/`.brep` → 415, `check_bundle.py` really run to "all files match" exit 0, golden verify the honest 503 `aces-engineering not installed (exit 127)`, ledger verify 11 chain intact, SIMLAB chain 13; the install check against this tree's own server with a random `ACES_GC_TOKEN`: 3 tests, bind 127.0.0.1, CSP with the importmap hash, nosniff, no CORS, `/api/rig/status` 200 with `data: null` and `unreachable: timed out after 1 s` (the upstreams at closed loopback ports), 3 == 3 ledger entries from `aces ledger verify --json`, the header-less POST 403 and counted, SIGTERM stop with the ledger unchanged since `system` is not installed here, the token in none of 5 reply bodies and not in `server.log`; strict mode fails naming `modules.ledgers`). C's `verify_ui.py --vm-work`: `RESULT: PASS` at 1440×900 and 390×844 (0 console errors, 0 page errors, 0 failed requests, every `[data-vocab]` word known and labelled, 0 colour offenders, no overflow, polling fallback OK). `verify_records.py`: `RESULT: PASS` at both widths: Pipeline 66 experiment rows, 9 cases of `aces-cal-air-03d`, the two scales named ("cases", "queue jobs") side by side, rq and meta separately headed, the MLflow card honest ("not captured: MLflow runs/get for run 54db1a96… was not captured on 2026-10-11"); Solvers 14 registry rows, 14 lanes "not recorded", 9 not-citable notes, 12 `pass` / 2 `fail` chips, 8 GPU rows with the activity labels "outside the queue (operator hand-run)", "rests by mode sim-lab-4", "not in the sim pool", "idle", 12 mode rows; Objects 16 CAD runs (`cad:ok`, `cad:error`), the plate run's volume 11607.3 mm³ and strict sandbox, the `plate-fa5cc45b` SVG rendered, 6 dimensions, hole table "none", 4 bundles, **Check bundle** ran the installed checker to its all-match line (8 lines), **View part.stl** "520 triangles · bbox 0.0,0.0,0.0 to 60.0,40.0,5.0 mm · 26084 bytes", 1 note, Onshape not connected; Evidence cad-gold `20261010T102210Z-bc7391` grade `verified-desktop` followed by "no grade is a sign-off", 9 stages, 15 compute rows, 10 sweep rows, provenance 306, report.html inside the sandboxed iframe (`sandbox=""`, document title "aces golden root-mount 20261010T102210Z-bc7391", 17,944 characters), 5 figures loaded, **Verify** → the honest 503 (no engineering venv on this machine), **Verify ledger** chain intact, 11 ledger rows, 13 SIMLAB chain rows. Container install test: `bash bin/install-test aces-base aces-consoles aces-console` in a clean `archlinux:base` (unsigned repo, `SigLevel = Optional TrustAll`, 2026-10-11 03:06:49 to 03:07:23 UTC): `aces-base aces-consoles` installed first; the block's `pacman -Su --noconfirm` replaced `aces-consoles 0.1.0-2` with `aces-console 0.1.0-1` in one transaction (pacman asks "Replace aces-consoles with aces/aces-console? [Y/n]", answered by `--noconfirm`, with `aces-consoles` still in the repo), no package named `aces-consoles` remains, while `pacman -Q aces-consoles` answers `aces-console 0.1.0-1` through `provides=` (so the spec's "`pacman -Q aces-consoles` fails" cannot hold for a providing package; the block checks `pacman -Qq` by name); `pacman -Qi`: Provides, Conflicts With and Replaces `aces-consoles`; `aces.desktop`, `static/index.html` and `tests/install_check.py` present; `aces-console --version` → `aces-console 0.1.0`, `url` → `http://127.0.0.1:8070`, `status` → `not running (http://127.0.0.1:8070)`; the install check (expected 3 entries, random token) started the installed server (`static /usr/share/aces/console/static [config]`, `state /root/.local/state/aces/console`), passed bind 127.0.0.1:8070, the page's CSP, nosniff, no CORS, `/api/rig/status` 200 with `data: null` and `unreachable: timed out after 4 s`, then **failed as designed**: `FAIL modules.ledgers is 'not installed': GET /api/ledger is not served by this backend (spec 3.11)`, exit 1, because Builder B's `ledgers` and `system` modules are not in this tree; the integration tree must carry them for `bin/install-test` to pass. The same installed package in a separate clean container (a probe, not `bin/install-test`): `install_check.py --expect-ledger 0 --allow-missing-modules` passed every check with the SIGTERM fallback (server exit 0, ledger 0 → 0 "ledger empty", the token in none of 5 reply bodies and not in `server.log`), and the installed tests `python -m unittest discover -s /usr/share/aces/console/tests -t /usr/share/aces/console` as root on Python 3.14.8: `Ran 91 tests in 19.174s`, `OK (skipped=3)` |
| `aces-base` | 0.1.0-4 | a documented `[console]` table in `/etc/aces/aces.toml` and in `DEFAULTS` (`bind 127.0.0.1`, `port 8070`, `static_dir /usr/share/aces/console/static`, `state_dir ""` = `$ACES_STATE/console`); nothing else changes, and the console needs only `>=0.1.0-3` | `bash bin/build aces-base` (the same call, unsigned), 2026-10-11 02:58:49 to 02:59:00 UTC: `Finished making: aces-base 0.1.0-4` (70,362 bytes); `check()`: `aces self-check passed: 3 ledger entries`, `Ran 17 tests in 0.295s`, `OK`, `aces 0.1.0-3 checks passed: 4 ledger entries, golden passthrough 127 and 3`; installed by the console install test as the dependency | `python -m unittest discover -s src/aces-base/tests` → `Ran 17 tests in 0.778s`, `OK`; `aces.config.load()["console"]` with `ACES_ETC=etc` reads `{'bind': '127.0.0.1', 'port': 8070, 'static_dir': '/usr/share/aces/console/static', 'state_dir': ''}` |
| `aces-consoles` | 0.1.0-2 | unchanged; kept this release so the install test exercises `replaces=` | rebuilt unchanged in the same call: `Finished making: aces-consoles 0.1.0-2` (02:59:07 UTC, 4,591 bytes) | installed first by `bin/install-test aces-base aces-consoles aces-console`, then replaced by `pacman -Su` (the aces-console row) |
| `aces-console` | 0.1.0-2 | Agents plate, SIMLAB actions, MLflow browser, auto-switch, red team, model staging; replaced pages never opened; test consoles restore ACES_STATE/ACES_WORK | `bash bin/build aces-console` on DARKNESS, signed, 2026-10-11: `check()` Ran 132 tests OK (skipped=5) | DARKNESS: 147 unit tests OK; `verify_ui.py`/`verify_records.py` against the real server PASS at 1440x900 and 390x844. Rehearsal VM (signed LAN repo, `pacman -Sy aces-base aces-console aces-omarchy aces-os`, the replacement of `aces-consoles` chosen at pacman's prompt): `vm-console-accept.sh` 33 PASS / 0 FAIL; both harnesses PASS against the installed console through an ssh tunnel (live rig, the VM's own store), 0 console errors; ledger 11 entries before and after |
| `aces-console` | 0.1.0-4 | starts golden runs (a project's spec, the demo, or a STEP made into a project) and runs CAD actions (inspect, render, drawing, pull, re-run with parameters, prune) as jobs streamed to the page, each start ledgered; -4 fixes the `lane` option refused by -3 (found on the VM) | `bash bin/build aces-console` on DARKNESS, signed: `check()` Ran 132 tests OK (skipped=6) | DARKNESS: full suite OK including `tests/test_jobs.py` (fakes replaying the real VM records). Rehearsal VM: `vm-jobs-accept.sh` 6 PASS / 0 FAIL: a golden run started through the console (demo root-mount, `--lane none`, nothing sent to the rig) finished as run `20261011T122641Z-b3ac99`, outcome pass, grade verified-desktop, 50.3 s, `aces golden verify` exit 0, listed by the console; `aces-cad` inspect and render on cad-vm's latest run exit 0; ledger 13 -> 17 entries (`console.golden.run`, `golden.run`, `console.cad.inspect`, `console.cad.render`) and verifying. Both UI harnesses PASS against the installed console |
| `aces-os` | 0.1.0-7 | depends `aces-console` instead of `aces-consoles` | `bash bin/build aces-base aces-console aces-omarchy aces-os` (signed, `--nodeps` for aces-os) | installed on the rehearsal VM (`pacman -Qi aces-os` names aces-console); CI install test |
| `aces-omarchy` | 0.1.0-7 | optdepends names `aces-console` (the ACES launcher entry) instead of `aces-consoles` | same call, signed | installed on the rehearsal VM (pacman listed `aces-console [installed]` as its optional dependency) |

`bin/install-test` (2026-10-11): the default set gains `aces-console`; when both `aces-consoles` and `aces-console` are
named, `aces-consoles` is installed first and the aces-console block runs `pacman -Su --noconfirm` (fallback
`--ask=4`) and asserts that no installed package is named `aces-consoles` (`pacman -Qq`; `pacman -Q aces-consoles` keeps
answering through the `provides=`) and that `pacman -Q aces-console` succeeds; the block then lists the
launcher entry, the static tree and the licences, runs `aces-console --version | url | status`, exports a random
`ACES_GC_TOKEN`, runs `python3 -I /usr/share/aces/console/tests/install_check.py --port 8070 --expect-ledger <3 | >=5 |
>=7>` (3 in the default set, `>=5` after the golden block, `>=7` after the CAD block) through `tee
/tmp/console-check.log`, greps the check log and `/root/.local/state/aces/console/*.log` for the token value with an
explicit `if` (the spec's `! grep -q` never stops a `set -e` script), and runs the installed unit tests. The container
script stays single-quoted with double quotes only inside. `.github/workflows/build.yml`: the cad step installs
`aces-base aces-engineering aces-cad aces-console`.

## Golden path (`aces golden`)

One command from a design space to a computed, verified and evidenced organic part (VISION section 11.1: STEP ->
voxel -> sweep -> surrogate -> verify -> evidence -> report). `aces golden run --demo` computes a six-foot root mount
inside a disc design space: every design is SIMP topology optimisation turned into an organic implicit skin and solved
on the real voxel FEA; a GP surrogate fitted to the sweep proposes the winner, which is solved again, checked at a
finer resolution, re-solved on CuPy and Warp when they are usable, and (from aces-engineering 0.1.0-3) solved a
second time on a conforming tet10 mesh with CalculiX. With `--lane sim/pool` the independent check runs on the rig's
GPUs and the conforming check on its CPUs, through simlab-mcp. The loop is described in
`src/aces-engineering/README.md` (section "Golden path") and in the aces-engineering skill.

- Run directory: `$ACES_WORK/<project>/golden/<run-id>/` (a simlab experiment directory plus `run.json`,
  `result.json`, `compute.json`, `compute_used.json`, `validation.json`, `winner/` or `candidate/`, `figures/`,
  `evidence/`, `report.md/html/json`, `log.txt`, `events.jsonl`, `manifest.sha256`).
- Evidence: `evidence/files.sha256` anchors the run's records; its digest is inside a simlab evidence bundle chained
  into the per-user simlab ledger; a `golden.run` entry in the aces ledger names the bundle; the project's `aces.yaml`
  gets the evidence reference and the components used.
- Exit codes: 0 pass, 1 verification failed (or `--strict` without `verified-desktop`), 2 usage (including `--lane`),
  3 thin sweep, 4 under-resolved, 5 infeasible, 6 evidence or ledger failure, 7 internal error, 130 interrupted; 127
  from the `aces golden` passthrough when aces-engineering is not installed. From 0.1.0-3: 2 also for a lane that
  cannot be used (nothing submitted, no run directory) and for `--rig-sweep`, `--rig-budget` or `--conforming rig`
  without a lane; 6 also for a rig integrity failure; 1 also for a PASS below `--require-grade`.
- Verify: `aces golden verify RUN` (files, bundle, both ledgers, sweep table, surrogate models, verdict, report
  provenance, compute-used rows, manifest; from 0.1.0-3 13 checks, adding `rig` and `conforming` before the manifest
  and `resolve` last), `aces-evidence verify`, `aces ledger verify`.
- Grades: desktop solves are `preview-grade`; `verified-desktop` when the finer-resolution check agreed; neither is a
  sign-off. From 0.1.0-3, `rig-verified` when the level-1 check and the conforming check both ran on the rig and
  passed, on images whose `golden-check` and `ccx-check` validations are `pass` in the rig registry, with matching
  code and no integrity error; a constraint violated on the conforming mesh caps any run at `preview-grade`.

| Where | Date | Command | Outcome | Grade | Wall s | Run id | Bundle sha12 | simlab entry | aces entry |
|---|---|---|---|---|---|---|---|---|---|
| Clean container (`bin/install-test aces-base aces-engineering aces-voxel`) | 2026-10-09 | `aces golden run --demo --json --progress lines` (packaged venv, Python 3.14.7, CPU only: no CuPy or Warp in the package) | pass (exit 0) | verified-desktop | 22.3 | `20261009T142823Z-b3ac99` | `54b28de54d31` | 1 | 5 |
| CI (GitHub Actions run 37946810947 on commit 24e86af, ubuntu-latest: AMD EPYC 9V74, 2 logical CPUs, no GPU) | 2026-10-09 | same (`bin/install-test aces-base aces-engineering aces-voxel`, after the build step's makepkg `check()` had already run the 169 golden tests and the demo; build 7.5 min, default install test 50 s, this step 91 s). The run's only failure was the log upload after the tests: the copied run records were root-owned 0600 and the runner could not read them (fixed in `bin/install-test` with `chmod -R a+rX /logs/golden`) | pass (exit 0, 40 s including interpreter start) | verified-desktop | 40 (command time; result.json not kept) | `20261009T145633Z-b3ac99` | `0dcdf22e36c3` | 1 | 5 |
| Rehearsal VM (Omarchy 4.0.4, Python 3.14.7, 4 vCPUs, no GPU) | 2026-10-09 | `aces golden run --demo` from a login shell over ssh, after `pacman -Sy` upgraded aces-base, aces-engineering, aces-voxel and aces-omarchy from the signed LAN repo (packaged venv, CPU only; the 2 mm check ran on the CPU, estimate 2.8 s within the 60 s budget); then `aces golden verify latest` (10 PASS, 1 INFO), `aces-evidence verify` ok, `aces doctor` golden row `ok ... PASS`, the linked skills carry the new text | pass (exit 0, 25 s including interpreter start) | verified-desktop | 23.7 | `20261009T144006Z-b3ac99` | `070c439672c8` | 1 | 2 |
| DARKNESS (workstation) | 2026-10-09 | `python -m aces_engineering.golden_cli run --demo` (workstation dev venv, Python 3.11.9: CPU sweep in 10 workers, check and parity on the RTX PRO 4000 through CuPy and Warp) | pass (exit 0) | verified-desktop | 42.8 | `20261009T143033Z-b3ac99` | `d8977ebb61f8` | 1 | 2 |
| Clean container (`bin/install-test aces-base aces-engineering aces-voxel calculix-ccx-bin`, integration 6db440f) | 2026-10-10 | `aces golden run --demo --json --progress lines` (packaged venv, Python 3.14.7, CPU only; the conforming check on this host with calculix-ccx-bin 2.23-1: 38,618 C3D10, 187,011 DOF, 8 threads, 28.7 s, SPOOLES, equilibrium 3.6e-08); then `aces golden verify latest` (11 PASS, 2 INFO), `aces ledger verify` and `aces-evidence verify` ok | pass (exit 0, 53 s including interpreter start) | verified-desktop | 51.7 | `20261010T072618Z-b3ac99` | `0b0eedd7f5b2` | 1 | 5 |
| Clean container, the same install test after the `bin/build` change (calculix-ccx-bin and aces-engineering built again) | 2026-10-10 | same; the conforming check 38,618 C3D10, 187,011 DOF, 8 threads, 52.2 s (ccx 35.9 s), SPOOLES, equilibrium 3.6e-08, the comparison rows as in the run above; `aces golden verify latest` (11 PASS, 2 INFO) | pass (exit 0, 97 s including interpreter start) | verified-desktop | 94.0 | `20261010T075326Z-b3ac99` | `acdb7c4cc597` | 1 | 5 |
| Rehearsal VM (Omarchy 4.0.4, Python 3.14.7, 4 vCPUs, no GPU; aces-engineering 0.1.0-4 and calculix-ccx-bin 2.23-1 from the signed LAN repo) | 2026-10-10 | `aces golden run --demo --json --progress lines` over ssh (acceptance step 8: the conforming check runs on the VM itself: 38,618 C3D10, 187,011 DOF, ccx 2.23 on 4 threads, 29.5 s; max gap -5.706 % on vm_p99 against the 1 mm voxel solve, constraints met); then `aces golden verify latest` (11 PASS, 2 INFO: rig not run, resolve). Step 9 on the same VM: `aces golden run --demo --lane sim/none --json` exits 2 with exactly one JSON document (`exit_code` 2, `lane 'sim/none' cannot be used: it is not in the fabric manifest (sim lanes: sim/pool); nothing was submitted`) and creates no run directory | pass (exit 0, 56.1 s real) | verified-desktop | 55.0 | `20261010T084129Z-b3ac99` | `8bf243856b58` | 2 | 3 |

### Golden path on the rig

Filled from the acceptance runs on the real rig (build spec section 24), each row from that run's records
(`result.json`, `rig/session.json`, `validation.json`, `compute_used.json`). No row is written before such a run
exists.

| Where | Date | Command | Outcome | Grade | Wall s | Run id | Bundle sha12 | simlab entry | aces entry | Rig experiments | Rig GPUs | Image id12 | Conforming gaps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DARKNESS (workstation dev venv, Python 3.11.9; aces-os main 5c5fdc1 lib; cudax main at rig-golden-1.1) to lane `sim/pool` | 2026-10-10 | `python -m aces_engineering.golden_cli run --demo --lane sim/pool --json --progress lines` after `compute --lane sim/pool` exit 0 (17 tools; the three solvers `validation pass (image d45698807541) · golden code 1f1a56410bf4`); then `verify latest` (12 PASS, 1 INFO resolve), `aces-evidence verify` ok (2 entries), `aces ledger verify` ok (3 entries) | pass (exit 0) | rig-verified | 109.8 | `20261010T083934Z-b3ac99` | `1842f1982879` | 2 | 3 | `golden-20261010T083934Z-b3ac99-check` (x001, x002), `-conf` (m001): 2 experiments, 3 cases, 21 pulled files | x001 and x002 on GPU 0 (NVIDIA CMP 170HX; lease waits 27.4 s and 1.9 s: the rig operator's hand-run jobs held GPUs 1 to 3 outside the queue), m001 on the rig CPU (16 threads, 54.0 s) | `d45698807541` | mass +0.0 %, vm_p99 -4.7 %, umax -4.2 %, compliance -5.0 % (38,618 C3D10, 187,011 DOF): agrees, constraints met |
| DARKNESS, the same environment | 2026-10-10 | `... run --demo --lane sim/pool --rig-sweep --json --progress lines` (acceptance step 6, for information: the 10 LHS designs computed on the rig); `verify latest` 12 PASS, 1 INFO | pass (exit 0) | rig-verified | 340.2 | `20261010T084243Z-b3ac99` | `7f85a28518d4` | 3 | 4 | `-sweep` (d000 to d009: 9 of 10 finished, d006 multi-body as on the desktop), `-check` (x001, x002), `-conf` (m001): 3 experiments, 13 cases, 115 pulled files | sweep cases on GPUs 0 and 3, x001 on GPU 0, x002 on GPU 2 (lease waits 1.9 s); the sweep's 10 cases took 247.3 s of wall for 49.8 s of case time, one case waiting 239 s for a lease while GPUs 1 to 3 were held outside the queue; m001 on the rig CPU | `d45698807541` | the same part and the same gaps: mass +0.0 %, vm_p99 -4.7 %, umax -4.2 %, compliance -5.0 %: agrees |
| Rehearsal VM (aces-engineering 0.1.0-4, packaged venv Python 3.14.7, 4 vCPUs) to lane `sim/pool` | 2026-10-10 | `aces golden run --demo --lane sim/pool --json --progress lines` over ssh after `aces fabric refresh` (acceptance step 7); `aces golden verify latest` 12 PASS, 1 INFO; `aces-evidence verify` ok (3 entries); `aces ledger verify` ok (4 entries); bench row written on the VM by `aces golden show latest --bench-out` | pass (exit 0) | rig-verified | 83.8 | `20261010T084920Z-b3ac99` | `91aaf1ad1877` | 3 | 4 | `golden-20261010T084920Z-b3ac99-check` (x001, x002; rig evidence bundle `ca739f13...`), `-conf` (m001; bundle `99c01690...`): 2 experiments, 3 cases, 21 pulled files | x001 on GPU 0 (50,000 elements, 163,863 DOF, 1.03 s), x002 on GPU 2 (400,000 elements, 1,254,723 DOF, mg, 6.34 s), m001 on the rig CPU (the `-conf` experiment 65.8 s wall) | `d45698807541` | conforming max gap -4.95 % on compliance; check at 2 mm max gap +4.414 % on vm_p99; constraints met |

Before these runs: image `aces-golden:0.1` `sha256:d45698807541...` (built from aces-os 5c5fdc1, golden compute digest `1f1a56410bf4...`) shipped 08:21 UTC with 19 RootFS layers equal on both sides and the rig-side selfcheck ok; `rig-golden-1.1` deployed 08:26 (the rig was on the rig operator's hot patch, classified and upstreamed; see cudax `docs/GOLDEN_RIG.md` 6) and the containers restarted while both queues were empty; the validation `20261010T082807Z` passed all six cases in 54 s (GC-3 rig GPU vs desktop CPU within 1.4e-11, CC-1 exact with equilibrium 5.4e-9, CC-2 0.70 % from Timoshenko; cudax `docs/validation/golden-20261010T082807Z/`); `rig-golden-2` deployed 08:38 with the rows `pass`.

Rig afterwards (step 10, 08:52 UTC): the queue gate ok (the two orphaned deferred `post` jobs of the rig operator reported, not counted); no `simlab-golden-*` container left; `rig_capacity` available `[0]`, GPUs 1 to 3 held by the rig operator's hand-run containers (`plat-v3`, `seg-v3-08c`, `seg-v21-08c`), which take no lease, so the spec's expected `[0, 1, 2, 3]` did not hold and is recorded as it was; MLflow experiment `aces-golden` holds 27 runs (26 FINISHED; the one FAILED is d006, multi-body); the rig ledger holds the `sim_evidence` bundles each `rig/session.json` names (for example the VM run's `-conf` bundle `99c01690...`, chained to the previous entry).

## Not yet written

| Package | What it will be | Why it waits |
|---|---|---|
| `aces-studio` | ACES Video Studio client and LoRA registry client | the server side (ComfyUI + ltx-pipelines workers) is not deployed |

It is listed as an optional dependency of `aces-os` so the meta-package resolves honestly today.

## Live lane tests (DARKNESS to the rig, 2026-10-09, mode `sim-lab-4`)

GRID CONTROL does not publish model endpoints, so they were discovered by probing `/v1/models` on the rig (read-only) and
written into `modes.json`. `aces lane test` then ran one models list, one 32-token completion and one tool-call probe per
lane. Each result is a `lane.test` ledger entry.

| Lane | Endpoint | Model served | Completion | Tool call |
|---|---|---|---|---|
| `model/glm-flash` | `:8016/v1` | `QuantTrio/GLM-4.7-Flash-AWQ` | 32 tokens in 0.36 to 0.56 s (57.6 to 88.4 tokens/s over two runs); reply began with the model's reasoning text, not the single word asked for | rejected, HTTP 400: the server says `"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set` |
| `model/glm-flash-2` | `:8017/v1` | same | 82.1 tokens/s | same rejection |
| `model/gpt-oss` | `:8018/v1` | `openai/gpt-oss-20b` | 31 tokens in 0.23 s (136.4 tokens/s); reply `OK` | accepted in 0.22 s but returned no structured tool call |
| `embed/qwen3` | `:8005/v1` | `Qwen/Qwen3-Embedding-0.6B` (owned_by `embed-cpu`) | not tested (embedding lane; the probe is for chat lanes) | n/a |

What this says for the plan's lane qualification ("ACES 0.1 Bench"): in the current `sim-lab-4` mode no lane returns
structured tool calls. The GLM-Flash servers need `--enable-auto-tool-choice --tool-call-parser` (a server-side change on the
rig, Kimi's to make), and the gpt-oss lane needs its tool parser checked. Until then an agent on this desktop has no
tool-calling lane on the rig; the manifest and the skill say so rather than hiding it. Port `8033` answers `/v1/models` with
HTTP 401 (authenticated service, not identified). GRID CONTROL's status also publishes two consoles, recorded in the
manifest under `server.discovered`: `mc_console` on the rig's loopback (`127.0.0.1:3100`, not reachable from here) and
`gridwatch` at `192.168.1.65:3000`.

## Onshape adapter (`aces-cad-onshape`), 2026-10-09

**What it is.** `aces-cad-onshape` lets the default agent turn a request in words into FeatureScript, run it in Onshape,
read the errors and the geometry back, iterate, and hand the result to the engineering loop as STEP + parameters +
sha256. It is a standard-library REST client with the `aces-onshape` CLI, the `aces-onshape-mcp` stdio MCP server (17
tools) and the `aces-cad-onshape` agent skill, written as the "ACES REST client" of VISION.md's licence row (MIT, no
third-party code) for the `onshape://` pull of VISION.md section 5 step 2 and the `CadSource` pull. Onshape is a cloud
service, so every write, evaluation, export and pull runs the project class gate and refuses `sovereign` and
`controlled` projects before a request is sent.

**Source of truth.** Onshape's public OpenAPI 3.0.1 spec (`info.version` `1.221.89963-13d10d36cb23`, server
`https://cad.onshape.com/api/v17`, saved 2026-10-09) for every path, request body and response field; Onshape's public
FeatureScript documentation (the slot-feature tutorial, imports, the UI spec and the standard library reference) for
the skill; and Onshape's own texts on the official FeatureScript MCP Server: the blog posts of 3, 9 and 30 Sep 2026 and
the Tech Tip "How to Connect the FeatureScript MCP Server and Claude Code" (8 Sep 2026). The package's `README.md`
carries the details.

**Where the OpenAPI differs from or sharpens the brief.**

*Translation states.* The brief had translation jobs going from PENDING to DONE. `BTTranslationRequestState` is
`ACTIVE`, `DONE` or `FAILED`; there is no `PENDING`, and a failure carries `failureReason`. `GET /translations/{tid}`
asks clients not to poll several times a second and to back off exponentially, so the client waits 1 s
(`ACES_ONSHAPE_POLL_S`), then 1.5 times longer before each poll up to 10 s, gives up after 600 s
(`ACES_ONSHAPE_TRANSLATION_TIMEOUT_S`), and records every state change with its time and poll number in
`source.json`. The fake server goes `ACTIVE`, then `DONE` on the second poll.

*The STL redirect.* The brief expected STL bytes straight from `GET .../stl`; the operation "Returns a 307 redirect from
which to download the exported file". urllib's default redirect handler would forward `Authorization` to whatever host
the redirect names, so the client follows redirects itself (at most 5): it forwards `Authorization` only to the base
URL's own origin or between https `*.onshape.com` hosts, sends none to any other host (a pre-signed URL carries its own
authorisation), and shows redirect targets in traces and messages without their query. The `units` value goes out in
lower case (`millimeter`); whether Onshape accepts that case is open.

*The namespace forms (inferred from /fstable, unverified for features).* The brief said a custom feature instance
references its Feature Studio through a version (`d<did>::v<vid>::e<eid>::m<mv>`), so every instance would need
`POST /documents/d/{did}/versions` first. The OpenAPI documents namespace forms only for `/fstable` custom tables
(`tableNamespace`): `e{eid}::m{mid}` when the Feature Studio and the Part Studio are in the same workspace,
`d{did}::v{vid}::e{eid}::m{mid}` when they are in different workspaces, with `{mid}` the Feature Studio's
`microversionId` from getElementsInDocument; `BTMFeature-134.namespace` adds that std features have `""` and custom
features identify their Feature Studio. So `add-custom-feature` uses the namespace the Part Studio's own featurespecs
report for that studio, else the workspace form, and creates no version; `--pin-version` (`pin_version`) creates a
version first and uses the version form, and the skill retries once with it when the workspace form is rejected.
Versions are permanent, and one per agent iteration would litter the document.

*No documented compile errors on write.* The brief expected the studio write to return compile errors. Only
`BTFeatureScriptEvalResponse-1859` documents `notices` (level ERROR, WARNING, INFO, INTERNAL or UNKNOWN; type PARSE,
SEMANTIC, EXECUTION, MODELING, TEST, OTHER or UNKNOWN; a message); the write response, `BTFeatureStudioContents-2239`,
has only `btType`, `contents` and the base fields. So errors are surfaced through the featurespecs call after a write
(`specs_error`, plus a warning when code that defines features yields no specs), the feature status after
instantiation (`OK`, `INFO`, `WARNING`, `ERROR`) with a `getFeatureError`/`getFeatureWarning`/`getFeatureInfo` eval
probe, eval notices, and any `notices` the write response carries anyway, passed through.

**What the unit tests cover.** The tests in `src/aces-cad-onshape/tests` run against a fake Onshape server
(`http.server` on `127.0.0.1` with in-memory documents, versions, Feature Studios, features, translations and external
data, a 307 STL redirect, and knobs for injected statuses, lost responses and delays) and a fake `aces` CLI. There are
69 test methods: the spec's tests 1-56, with 14b, 19b, 47a-c and 55a-c as methods of their own, and the regression
tests 15b, 24b, 34b, 34c, 40b, 44b and 52b added with the review fixes of 2026-10-10. On 2026-10-09 the first 62 passed
on Python 3.11.9 and 3.13.13 on the workstation (1 of them skipped: the file-mode test is POSIX only), 62/62 on 3.14.7
in `aces-builder`, 62/62 in the package's `check()`, and 62/62 installed in a clean container. On 2026-10-10 all 69
passed on 3.11.9 and 3.13.13 on the workstation (1 skipped) and 69/69 on 3.14.7 in the `aces-builder` image, run from
the source tree; the package has not been rebuilt since, so `check()` and the clean-container run are still at 62.

- The client: the Basic header on every request; the 401 and 403 messages (Onshape's text kept, no secret); base URL
  validation; key precedence between the environment and `onshape.env` (a BOM, `export`, quotes, inline comments
  refused, empty values, the 0600 check); 429 with `Retry-After` in seconds and as an HTTP date, and the 60 s cap; 5xx
  retried only on idempotent calls; transport errors classified as not sent or possibly sent (`maybe_applied`); a
  delete that applied while its response was lost, and a 404 taken as "already deleted" only after an attempt that may
  have applied (not after a 429 or a request that was never sent); ids with a trailing newline refused before any
  request; the redirect auth rule, and an STL redirect to another origin that receives no `Authorization`; a download
  retry with a fresh hash; ids in responses validated before use.
- The FeatureScript helpers: header parsing and prepending, value decoding, parameter building (quantities, booleans,
  strings, queries, enums and their namespaces), namespaces, references, imports and declared enums; header and import
  scans that stay linear on comment-heavy and identifier-heavy text (run in a child process with a timeout).
- The operations: the Feature Studio round trip; eval without `queries`; adding, updating and deleting features;
  versions; STEP export polling and its sha256; the custom-feature namespace preference (Part Studio featurespecs,
  computed, version form), `pin_version` creating exactly one version and none for an unknown feature type; the error
  probe; the `specs_error` classification; the pull bundle (layout, hashes, `source.json` never rewritten, the Feature
  Studio capture with workspace, linked and blob imports and the depth limit, `source.json`'s `ledger` note,
  `outcome.json` with the sha256 of `source.json`, no `.partial` left, a lone surrogate from Onshape written as its JSON
  escape); export names; the ledger entry and `add-cad-source` through the fake `aces` in its new and old modes.
- The guards: the class gate for sovereign, controlled, unknown and empty classes with zero requests sent (pull, studio
  write, add feature, eval); the bound project; the `aces` CLI reading projects under the adapter's own `ACES_WORK`
  (an empty one included); the foreign-target and public-document guards; no secret in any file, output or ledger
  payload.
- The CLI's exit codes and redaction, and the MCP server over stdio (initialize, tools/list, tools/call, argument
  errors, session ownership, redaction; a line nested too deep for json, lone surrogates in an id or in Onshape's text,
  ids that are not a string, a number or null, and a reply that cannot be encoded, after each of which the server still
  answers).
- Isolation: every test clears the `ONSHAPE_*`, `ACES_*` and proxy variables, points `HOME`, `USERPROFILE` and the ACES
  directories at a temp directory, and refuses any connection that is not to loopback, so the tests never read the
  operator's keys or reach Onshape.

**What is not verified.** There is no Onshape account on DARKNESS, so nothing has run against Onshape. The open
questions, numbered for the live check:

1. Whether a REST write to a Feature Studio commits it (in Onshape's UI, changes reach other tabs only after Commit).
2. Whether the Part Studio featurespecs report, in `namespace`, the string `POST .../features` needs for a custom
   feature (the adapter prefers it when it names the same studio).
3. Whether `POST .../features` accepts the `/fstable` namespace forms, including a version of the same document, and
   whether an instance follows later studio writes or must be re-pointed (`--replace`).
4. Whether a parameter whose featurespec has no `defaultValue` may be left out of the instance (the adapter omits it).
5. Whether the studio featurespecs' `namespace` and `defaultValue` are usable verbatim.
6. Where the compile errors of a studio write appear.
7. Whether `makeId("<feature id>")` addresses a top-level feature for `getFeatureError`, `getFeatureWarning` and
   `getFeatureInfo` in the eval probe.
8. The shape of error bodies (the adapter shows a JSON `message` when there is one).
9. Whether 429 answers carry `Retry-After`, and which status plan and API-allocation limits answer with.
10. The eval `queries` format and what `console` holds (println output from the lambda is expected there).
11. The target of the `/stl` 307 redirect, whether it needs `Authorization`, and whether lower-case `units` is
    accepted.
12. Translation timing, when `resultDocumentId` is set, and the formats of translation and external-data ids.
13. The official FeatureScript MCP Server's tool list and OAuth scopes.
14. Mass-property units (the schema states none).
15. Free-plan public documents: whether `isPublic: false` holds on a paid plan and is overridden on a free one.
16. The namespace a `BTMParameterEnum-145` needs, for an enum the studio declares and for a std enum it re-exports.
17. Whether an element microversion id is a valid document microversion id (the pull would then read the source a
    stale namespace points at; today it reads the current source and warns).
18. How much API allocation each operation consumes.

**Live check (Aaron).** Record what Onshape actually returned at every step:

1. Create API keys with read and write access in the developer portal (`https://cad.onshape.com/appstore/dev-portal`).
2. Run `install -Dm600 /dev/null ~/.config/aces/onshape.env`, then add `ONSHAPE_ACCESS_KEY=...` and
   `ONSHAPE_SECRET_KEY=...` with an editor (no inline comments).
3. `aces-onshape whoami` must exit 0. Note `plan_group`: on a free plan, documents are public.
4. `aces project new onshape-check --class personal` and `aces project new onshape-sov --class sovereign`.
5. `aces-onshape create-document "ACES onshape check" --project onshape-check --json` gives `did`, `wid` and `public`.
   Then `aces-onshape elements <did> w <wid> --type PARTSTUDIO --json` gives the Part Studio id `<ps>`.
6. `aces-onshape fs write <did> <wid> plate.fs --name "ACES plate" --project onshape-check --json`, where `plate.fs` is
   the skill's example. Record `feature_specs`, `specs_error` and any `notices`.
7. `aces-onshape add-custom-feature <did> <wid> <ps> <studio> acesPlate --param length="60 mm" --param width="40 mm" --param thickness="5 mm" --param holeDiameter="10 mm" --project onshape-check --json`.
   Record `namespace`, `namespace_source`, `featurespec_namespace`, `partstudio_featurespec_namespace`,
   `feature_status` and `messages`.
8. `aces-onshape eval <did> w <wid> <ps> readback.fs --project onshape-check`, where `readback.fs` is the skill's
   readback lambda. Expect 1 part, a bounding box of 60 x 40 x 5 mm, and a volume near 60·40·5 − π·5²·5 ≈ 11 607 mm³.
9. Re-point the instance through a version:
   `aces-onshape add-custom-feature <did> <wid> <ps> <studio> acesPlate ... --pin-version --replace <fid> --confirm-foreign --project onshape-check --json`.
   Record whether the `d::v::e::m` form was accepted. The readback must still show 1 part.
10. Enum check. Add `export enum PlateCorner { SQUARE, ROUND }` to `plate.fs`, and
    `annotation { "Name" : "Corner" } definition.corner is PlateCorner;` to its precondition (the body may ignore it).
    Then:
    - `aces-onshape fs write <did> <wid> plate.fs --eid <studio> --confirm-foreign --project onshape-check --json`;
    - `aces-onshape add-custom-feature ... --param corner=ROUND --replace <fid> --confirm-foreign --project onshape-check --json`.

    Record `parameters_sent` (the namespace sent and `spec_namespace`) and whether the feature regenerated.
11. `aces-onshape pull onshape://<did>/<wid>/<ps> --project onshape-check --json`, then `aces ledger verify`. Check
    `parameters.json` (`feature_studios`, `namespace_current`, `imports`) and `source.json` (`document.public`). With
    aces-base 0.1.0 the `cad.pull` ledger entry is not written (no `ledger record`); `source.json`
    (`ledger.available` false) and `outcome.json` say so, and `outcome.json` holds the sha256 of `source.json`.
12. Sovereign refusals. Each of these must exit 4, and `--verbose` must print no HTTP line:
    - `aces-onshape --verbose pull onshape://<did>/<wid>/<ps> --project onshape-sov`;
    - `aces-onshape --verbose fs write <did> <wid> plate.fs --project onshape-sov`;
    - `aces-onshape --verbose eval <did> w <wid> <ps> readback.fs --project onshape-sov`.
13. `aces-onshape --verbose export stl <did> w <wid> <ps> --project onshape-check --json`. Record the redirect host (the
    trace shows scheme, host and path only) and whether `units=millimeter` was accepted.
14. Optional: connect the official FeatureScript MCP Server per the Tech Tip, and record its tool list, whether its
    sign-in is OAuth (the address of the Authorize page, for example an `/oauth/authorize` path, and what `/mcp` shows),
    and the scopes it asked for.
15. Write the answers to open questions 1-18 into this section.

Not run yet: no Onshape credentials on DARKNESS (2026-10-09).

**The official FeatureScript MCP Server.** Source: Onshape's Tech Tip "How to Connect the FeatureScript MCP Server and
Claude Code" (8 Sep 2026, `https://www.onshape.com/en/resource-center/tech-tips/connect-featurescript-mcp-server-claude-code`)
and the blog posts above.

- It launched through Onshape Labs, an early-access program, and is available through the Onshape App Store
  (`https://cad.onshape.com/appstore/apps/Onshape%20Labs/6a29aea7c03f8bf659841734`); the user subscribes there first.
- Claude Code connects with `claude mcp add --transport http onshape https://fs-mcp.labs.onshape.app/mcp`; the
  `onshape-featurescript` entry in `mcp.json.example` is the same server.
- Sign-in runs in the browser: launching Claude Code asks you to authorize the Onshape subscription on an "Authorize"
  page, and `/mcp` then brings up the authentication screen that connects the Onshape account. It uses neither API keys
  nor `onshape.env`. Onshape's texts do not name the protocol: OAuth is inferred from MCP's authorization model and is
  checked in live-check step 14.
- Its calls draw on the user's API allocation, or the company's when the user asks for that.
- Per Onshape's posts, the assistant can write FeatureScript directly into a document, review errors, iterate on the
  code and inspect the geometry its code produces.
- Not connected from this machine; tool list and OAuth scopes unknown.
- Never for sovereign or controlled projects: the ACES class gate cannot see its calls.

## Local CAD (aces-cad), 2026-10-10

**Why.** Aaron, 2026-10-10: "The cloud-based system from Onshape is useless: the API calls will burn through the annual
allocation straight away, and I don't feel like paying huge amounts if we can build our own integration." `aces-cad` is
that integration. The agent (Claude Code, OpenCode, Codex, Hermes) writes build123d Python; this machine runs it in a
sandboxed child of the aces-engineering venv; the agent inspects, renders, draws and fixes the part, then pulls it into
the ACES project as STEP + parameters + sha256 with a `cad.pull` ledger entry. No API call, no allocation and no
network, so every project class may use it, sovereign and controlled included: the class gate still runs on every tool
and records the class, and an unknown or missing class is refused with the `aces project classify` fix. The package's
`README.md` (`/usr/share/doc/aces-cad/README.md`) has the details.

**Process model.**

- `aces-cad-mcp` (stdio MCP server) and the `aces-cad` CLI run on the system `python3` with the standard library only.
  They never import a network module and never `exec` agent code.
- Every geometry job is a fresh child process: `/opt/aces/engineering/venv/bin/python -I -B -X utf8 -X faulthandler
  /opt/aces/cad/lib/aces_cad/worker/main.py`, with the job JSON on stdin, stdout and stderr in files of the job
  directory, and `result.json` written atomically and validated by the parent. Jobs: `run` (the only one that runs agent
  code) and the data-only `env`, `selftest`, `inspect`, `render`, `drawing` and `export`. Nothing is kept between jobs,
  so an OCCT crash or hang costs one worker, never the server; the price is the imports, about 2 s per job (probe
  below).
- The server reads requests on one thread and runs tool calls on one executor thread (at most 8 waiting).
  `notifications/cancelled` kills the running job, progress notifications go out every 5 s, and the hostile-input
  hardening is aces-onshape's plus a 4 MiB line cap.
- Runs live in `<ACES_WORK>/<project>/cad/local/runs/<run_id>/` (mode 0700). Failed runs are kept, so an agent that lost
  its context gets the script back with `cad_runs`; outputs are promoted from the job directory only after validation.

**Sandbox.** In the order the worker installs it, before any heavy import:

1. its own session, `PR_SET_PDEATHSIG` (the worker dies with its parent) and nice 10;
2. rlimits with soft = hard: address space 4096 MiB, file size 1024 MiB, at most 1024 open files, no core dumps, CPU
   time just past the wall clock; 2 threads for BLAS/OpenMP and the OCCT pool;
3. seccomp-bpf (x86_64, 106 instructions, applied to every thread with TSYNC): `socket` fails with EACCES; `fork`,
   `vfork`, `execve`, `execveat`, `ptrace`, `unshare`, `setns`, `bpf`, `io_uring_*`, `pidfd_*`, `userfaultfd` and the
   other listed calls fail with EPERM; `clone` only makes threads; `kill`, `tgkill`, `prlimit64`, `sched_setaffinity`
   and `setpriority` reach only the worker itself;
4. Landlock: read-only `/usr`, `/etc`, `/opt/aces`, the venv and the run directory; read-write only the job directory,
   where symlinks, FIFOs, sockets, devices and executables cannot be created; `$HOME`, `/tmp`, `/run`, `/dev/shm` and
   other processes' `/proc` are out of reach; ABI 4 and later deny all TCP a second time, ABI 6 and later scope signals
   and abstract sockets;
5. a scrubbed environment (an allowlist: no `ACES_*`, `ONSHAPE_*`, proxy, SSH agent, display or D-Bus variables);
6. op `run` only, after the imports: a 16 MiB memory reserve (so a MemoryError is reported, not a crash) and the Python
   audit hook, which refuses sockets, processes, `ctypes`, tracing and writes outside the job directory with a clear
   message. The hook is not a boundary: OCCT's C++ I/O bypasses it.

The parent adds the wall clock (`cad_run` 120 s by default, at most 600 s), size caps on stdout, stderr and the job
directory, and a process-group kill (`killpg`; `taskkill /F /T` on Windows) so that no grandchild survives.
`ACES_CAD_SANDBOX=strict`, the default, runs no agent code unless seccomp **and** Landlock are both active (otherwise
exit 3, kind `sandbox_unavailable`); `permissive` runs with whatever is available, and every result then says `confined:
false` and names what is missing. Windows and macOS are permissive only (development: the audit hook, the wall clock and
the tree kill).

**Probe (B3's run of the judge's sandbox probe, 2026-10-10).** `archlinux:base` on Docker Desktop, kernel
6.6.114.1-microsoft-standard-WSL2 (no `/sys/kernel/security/lsm` in the container), aces-engineering 0.1.0-2's venv
(Python 3.14.7, build123d 0.11.1, OCP 7.9.3.1, trimesh 5.1.0, ezdxf 1.4.4, numpy 2.4.6), as uid 1000, guards installed
before the imports, RLIMIT_AS 4096 MiB, 2 threads:

- seccomp active (106 BPF instructions, TSYNC); Landlock ABI 3 (no TCP rules and no scoping at this ABI), every rule
  added, no root skipped;
- imports 2.02 s; VmPeak 1256184 kB, VmHWM 566140 kB, 16 threads;
- worked inside the guards: a two-hole plate (11214.60183660255 mm³), BREP, STEP (sha256
  `ef07b1340bc61c27c9cae49dfc2e8b074e523fce5d46c31ace9bf4b14a2565d8`), STL, 3MF, `BRepAlgoAPI_Check`, embree rays
  (`trimesh.ray.ray_pyembree`, thickness 4.9999), a watertight SDF mesh, a matmul, a Python thread, a matplotlib PNG, an
  ezdxf dimension (60.0, audit 0 errors) and an ezdxf PNG preview;
- refused: `socket` (AF_INET and AF_UNIX) errno 13; `fork` and `posix_spawn` errno 1; `kill(parent | 1 | -1, 0)` errno 1
  (its own pid allowed); `prlimit` on the parent errno 1; raising the hard RLIMIT_AS ValueError; `setpriority` on the
  user or the parent errno 1; `sched_setaffinity` on the parent errno 1; `symlink` and `mkfifo` in the run directory
  errno 13; a hard link of a secret into it errno 18; reading the secret, listing `$HOME`, and reading
  `/proc/<parent>/environ` and `/proc/1/environ` errno 13; writing `/tmp` or `/dev/shm` errno 13; OCCT `export_step`
  outside the job directory `RuntimeError: Failed to write STEP file`, and no file appeared;
- allowed: reading `/etc/hostname`, writing `/dev/null`;
- memory: a growing list hit MemoryError at VmHWM 1678912 kB under RLIMIT_AS 2048 MiB and at 3777008 kB under 4096 MiB;
  `np.ones(2**33)` hit MemoryError under both; an OCCT allocation bomb exited 1 under both, which is why the worker
  keeps a memory reserve.

**Tools (8).** `cad_start` (the class gate, the project's `cad/NOTES.md` lessons, the last runs, the conventions, the
limits and the sandbox status), `cad_run` (inline script, a project `.py` or a re-run, with parameter overrides and
input files; per-part volume, area, bbox, centroid, validity and topology), `cad_runs` (list or show runs, with the
script), `cad_inspect` (mass, validity, topology, holes, thickness, draft, radii, interference, section, measure,
design_space), `cad_render` (PNG views in the golden-path look, inline), `cad_drawing` (DXF with dimensions, plus SVG
and PNG rendered from it), `cad_pull` (the CadSource pull) and `cad_note` (one lesson appended verbatim to
`cad/NOTES.md`). The CLI has the same operations plus `doctor`, `selftest`, `params`, `prune` and `notes`.

**Pull bundle.** `cad_pull` re-derives the numbers in an export worker that runs no agent code and writes
`<ACES_WORK>/<project>/cad/local/<UTC stamp>-<name>/`: `part.step` (from the run's BREP, header time
`2000-01-01T00:00:00`, part names as labels), `part.stl`, `part.3mf` (written, never compared: it is not
byte-reproducible), `parameters.json`, `source.py` (its sha256 checked against the run), `inputs/`, `source.json`
(sha256 and size of every file, versions, environment, sandbox) and `outcome.json` (the ledger and cad-source outcomes,
with the sha256 of `source.json`). `geometry_sha256` is the sha256 of `part.step` (of `part.stl` for mesh runs). The
`cad.pull` ledger entry carries the class, the bundle, every file's sha256 and the versions, and `aces project
add-cad-source` records `cad/local/<bundle>/part.step`. An invalid part is refused unless `allow_invalid` is set, and
`valid` is recorded either way. The pull result names the golden hand-off (`aces golden init` or `aces golden run
--step`).

**FreeCAD is not used.** In a clean `archlinux:base` (B3's run, 2026-10-10, resolution only: `pacman -Sp freecad`,
nothing installed), FreeCAD 1.1.4-3 (LGPL-2.0-only in Arch's metadata) brings 141 packages, 403.5 MiB to download and
1905.3 MiB installed, and architect B's probe found no headless page-SVG export in TechDraw. build123d's OCCT
hidden-line removal plus ezdxf makes the drawing (DIMENSION entities in the DXF, SVG and PNG rendered from the DXF)
without it.

**Changed for local CAD.**

| Package | Version | What changed | Test evidence |
|---|---|---|---|
| `aces-cad` | 0.1.0-1 | new (its row is in "Built and tested") | built and install-tested from the merged branch (its row, and "Integration" below) |
| `aces-os` | 0.1.0-6 | `aces-cad` added to `depends` (the sovereign CAD path). Spec section 3: golden-rig/packaging (45f2c16) was not on main when this integration started, so the branch first set 0.1.0-5; main then took it through golden-rig/integration (5c5fdc1), whose aces-os 0.1.0-5 adds `calculix-ccx-bin`, so the branch merged main (954332c) and renumbered (review finding 1): 0.1.0-6, both comment lines kept, `depends` ending `'aces-voxel'`, `'calculix-ccx-bin'`, `'aces-cad'`. calculix-ccx-bin builds on main now (it ships the GCC 7 Fortran runtime it needs), so this aces-os resolves | built (any, `--nodeps`, unsigned, integration worktree) with `bin/build aces-os`, 2026-10-10 09:39 UTC; `.PKGINFO` lists `depend = calculix-ccx-bin` and `depend = aces-cad`; `pacman -Sp aces-os` resolves: 510 packages in the default install test (main's 0.1.0-5 there: 509, so aces-cad adds one), 521 after aces-base, aces-engineering, aces-voxel and calculix-ccx-bin, 522 after aces-base, aces-engineering and aces-cad |

`bin/install-test` gains an aces-cad block, which runs when aces-cad is in the package set (doctor, the ACES plate run,
inspected, rendered, drawn and pulled in a sovereign project, `check_bundle.py`, the ledger, two hostile scripts by exit
code, the installed tests; artefacts in `logs/cad/`), and CI a third install test, `bin/install-test aces-base
aces-engineering aces-cad`. The default install test is unchanged.

**Integration (2026-10-10).** The three builder branches (B1 ef59108, B2 5b5b8c6, B3 2008e7e) merged into
`cad-local/integration` from main 1680edf without a conflict: no file is shared by two builders, and no file on the
spec's never-edit list changed. The branch is named `cad-local/integration` because git cannot hold a branch
`cad-local` next to the builders' `cad-local/b1..b3`. The merged code showed these defects, each fixed in its own
commit:

- aces-os by the spec's merge-order rule (section 3, the table above): 0.1.0-5 while golden-rig/packaging was not on
  main, 0.1.0-6 once main took it (review finding 1, below).
- Children of the parent no longer inherit its stdin. `project.run_aces`, and the Windows `taskkill` of a job, gave
  every child the server's stdin, which under `aces-cad-mcp` is the JSON-RPC pipe. On Windows each aces CLI call then
  waited out its 30 s timeout (B3's probe: 8 s and still waiting with the pipe, 0.08 s with stdin on the null device);
  on Linux a child that read stdin would have consumed MCP input. Children now get the null device.
- The `cad.pull` result keeps `n`, `hash` and `path`. The `--print json` line of `aces ledger record` was cut at 300
  characters before it was parsed; that line is about 245 characters plus the ledger path, so a long path (any Windows
  path, a Linux path over about 55 characters) left only `{"written": true, "output": ...}`. Found by the stdio loop
  below on Windows; a new U03 test fails on the old code and passes on the fix.
- The 100-hole cap is reported only when a part has more holes. `geom.find_holes` cut its own records at 100, so the
  inspect `holes_truncated` flag could never be true for one part and the drawing's note fired at exactly 100 holes;
  both callers now ask for 101.
- Render, drawing and pull remove their job directories under the call's environment (`ACES_CAD_KEEP_JOBS`), as
  `OpContext.finish` does.

**Review fixes (2026-10-10).** The review of the integrated branch found seven defects; each was reproduced on the
branch as it stood (ff3c166) before anything changed, and each fix has a regression test (a red run of the new tests
against the code before the fix is named with each).

- Finding 1, aces-os collided with main. Main took golden-rig/integration (5c5fdc1, carrying golden-rig/packaging
  45f2c16) after this branch's last commit, and both shipped a different aces-os 0.1.0-5 (`git merge-tree` conflicted in
  `pkgbuilds/aces-os/PKGBUILD`, and the merged PACKAGES.md had three `aces-os | 0.1.0-5` rows). The branch merged main
  (954332c) and set aces-os 0.1.0-6 (the table above); every other shared file auto-merged, with only the aces-cad
  additions differing from main.
- Finding 2, a drill point read as a countersink. `geom.find_holes` took any concave cone of a coaxial group for a
  countersink and any larger cylinder for a counterbore, wherever it sat: a blind d6 hole with a 118° point read
  countersink, axis (0, 0, -1), entry at the tip, depth 11.80, csk d0 x 118°, and drew "Ø6 DEEP 11.8, CSK Ø0 X 118°". A
  counterbore or countersink face now counts only between an open end and the drill-radius bore, a blind hole is as deep
  as its full-diameter bore (10, without the point), and a hole countersunk on both faces is measured at its upper end
  (it read d28 for a d12 countersink). The reviewer's narrower rule (faces that touch an open end) was probed and turned
  a chamfered counterbore into a countersink, so it was not used. The hole records of the earlier fixtures are
  byte-identical before and after. New fixture `drill_point_block.py`; W09 and W12 assert it (red on ff3c166).
- Finding 3, the skill's gyroid came out open. Run verbatim it gave `watertight: false` (`open_shell`) and the pull
  exited 1. The cause, measured: its clip box's faces lie on grid planes, so 2162 grid values sat exactly on the level
  and marching cubes left 864 edges shared by more than two triangles. The reviewer's diagnosis (h too coarse for the
  wall) did not hold: with the proposed h = min(cell / 16, wall / 4), 36 mm cubes at a 1 mm wall were still open.
  `mesh_from_sdf` now counts values exactly on the level as outside by 1e-9 h; after that, 17 of 17 probed gyroid runs
  were watertight and valid, including the old h = cell / 16 in 8 parameter sets of which 6 had been open. The example
  keeps the finer h, with ranges that stay under the 2,000,000-triangle mesh cap (a 60 mm cube at a 10 mm period and a 1
  mm wall needs 6,613,936). `tests/scripts/skill_gyroid.py` is the SKILL.md block verbatim (S04c); W13 runs it at its
  defaults (watertight, valid, 6534.400079864546 mm³ on Windows) and pulls it, and W04b runs the old example (red on
  ff3c166).
- Finding 4, the hash depends on the name. The run's name is written into the STEP's `FILE_NAME` and top `PRODUCT`
  records: the ACES plate run as `aces_plate` (a CLI run named after its file) gave `e43f3609...` and run as `plate`
  `806c9be7...`, two files differing only in those records, while the pull's own `name` does not count (pulled as
  `bracket`, `806c9be7...` again). The skill and the README now say equal geometry with equal names gives equal hashes;
  W13 asserts the two-record difference and that the pull name leaves the hash alone.
- Finding 5, the drawing renumbered holes by position, so drawing.json features and hole-table rows did not match
  `cad_inspect` ids (a three-axis block: inspect h2 = Ø10, h3 = Ø6 DEEP 10; drawing h3 = Ø10, h2 = Ø6). The drawing now
  uses the inspect ids (across the parts of an assembly in part order, groups per part, as the inspect check numbers
  them) and only lays out by position; hole-table rows gain `hole`. New fixtures `three_axis_holes.py` and
  `two_plates_holes.py`; W12 compares the two outputs id by id, and the perforated block's 100 table rows (red on
  ff3c166).
- Finding 6, CI lost the engineering step's golden records. The install-test golden block (golden-rig-owned, unchanged)
  starts with `rm -rf /logs/golden` whenever aces-engineering is installed, and the aces-cad step runs after the
  engineering step. The aces-cad step now moves `logs/golden` to `logs/golden-engineering` first. Replayed in order in
  the worktree: the engineering step (`bin/install-test aces-base aces-engineering aces-voxel calculix-ccx-bin`, exit 0,
  golden run 20261010T094054Z-b3ac99 with `conforming.json` and `ccx.log`, conforming status ok), then the two lines of
  the new aces-cad step (exit 0): `logs/golden-engineering` kept that run with both files, and `logs/golden` holds the
  aces-cad container's run 20261010T094259Z-b3ac99, which has neither (conforming status none, ccx not found).
- Finding 7, the hole-cap fix had no test. W09 now runs the perforated block (100 holes: count 100, `holes_truncated`
  false) and `perforated_block_101.py` (count 100, true); W12 checks that no cap warning and no `HOLE DETECTION STOPS`
  text appear at 100 holes and that the warning and the TITLE note appear at 101 (red on a3905e9, the code before
  integration fix c5f93c2).

Results (runs of 2026-10-10, UTC):

- Workstation, Windows 11, Python 3.11.9 parent, the workstation venv as the worker (build123d 0.11.1, OCP 7.9.3.1),
  `ACES_CAD_SANDBOX=permissive`, `ACES_CAD_REQUIRE_WORKER=1`: the merge as it came from the builders (6ad086f) ran 107
  tests OK in 1161.5 s; the final branch (032c1d0) ran 108 OK in 1235.7 s. Both skipped the same 14: the strict-only
  H06, H07, H08 and H12; H03, H14, U06 and the FIFO check (POSIX or Linux only); four U04 checks and W06 (symlinks need
  a privilege there); S02b, because those runs used an archive of `src/aces-cad` alone (the static tests from the full
  tree, 8 of 8, ran S02b and S03 against the real lock). `aces-cad selftest`: 12 passed, 0 failed, 1 skipped (9d needs
  strict) in 79.7 s.
- Linux: the package build and the install tests in the row above, all in strict mode on Python 3.14.7.
- After the review fixes (3651c59): `bin/build aces-cad` (`check()` 114 tests, strict, OK in 444.7 s, one skip),
  `bin/build aces-os` (0.1.0-6), the engineering and aces-cad install tests in CI order (both exit 0) and the default
  install test (exit 0, aces-os 510 packages), all in the row above and the table. Windows (permissive, the workstation
  venv, a `git archive` of `src/aces-cad` with the engineering lock): 114 tests OK in 1311.8 s with 13 skips, the
  integration run's 14 less S02b, which ran against the lock.
- The agent loop through the stdio server, against a scratch project made by the real aces CLI (`aces project new
  cadloop --class sovereign`; aces-base 0.1.0-3, the installed package on Linux and `src/aces-base` on Windows):
  `initialize`, `tools/list` (the 8 tools), `cad_start`, the ACES plate written to `cad/src/plate.py` and run by
  `script_path` with `render` and a progress token, `cad_inspect` (mass in AlSi10Mg, validity, holes, thickness),
  `cad_render` (iso, front and cutaway, inline), `cad_drawing` (inline), `cad_pull`, `cad_note`, `cad_runs`, `cad_start`
  again (the note comes back) and EOF; then `aces ledger verify`, `aces ledger show --kind cad.pull` (class sovereign),
  `aces project show` (the CAD source) and `check_bundle.py`. 17 of 17 steps passed on Linux as uid 1000 in strict mode
  (tool calls of 2.9, 6.9, 3.5, 4.7, 4.2 and 3.6 s from start to pull; no job ran long enough for a progress
  notification) and on Windows in permissive mode (6.7, 14.3, 6.5, 7.9, 6.5 and 7.1 s; progress at 5.3 of 120 s in the
  run job and 10.6 of 125.3 s in its render job). No call waited on a timeout, every reply line was ASCII, and the
  server exited 0 at EOF.
- The images were looked at: the iso, front and cutaway renders in the dark look, captioned "designed in build123d
  (aces-cad)", and the drawing PNG in third-angle layout reading 60, 40, 5, 30, 20 and Ø10 THRU above the title block.
- The ACES plate's `geometry_sha256` was `806c9be7a86c8ae60308ee75d9a910f371d5f5aae6fe6517fa5ce6aa5fd1742f` for a run
  named `plate` in every run on Windows (Python 3.11.9) and on Arch (Python 3.14.7), and again in the install test
  after the review fixes; the run's name is part of the hash (review finding 4). `part.stl` differed
  (`1dfaf2d9aae7…` on Windows, `3d24ea3da0fd…` on Arch), so STL hashes are not compared across operating systems.

**Not verified.**

- The TCP rules (ABI 4) and the signal and abstract-socket scoping (ABI 6) are in force on the VM (Landlock ABI 10,
  acceptance step 2) and were exercised there by the doctor checks only; the Docker Desktop kernel used for the
  container tests, `6.6.114.1-microsoft-standard-WSL2`, offers ABI 3 and does not cover them.
- Which MCP clients show image content to the model: Codex 0.133.0 does not (acceptance step 9: the server sent valid
  `image/png` content with the render and the drawing, the model saw the text and the path); Claude Code is untested
  as a client (its CLI login on DARKNESS had expired on the day).
- Cross-OS equality of `geometry_sha256` beyond the ACES plate: the plate's STEP hash is equal on Windows, Arch (the
  container) and the VM (acceptance step 8); no other part has been compared across platforms.

**Known gaps (aces-cad).**

- Strict mode needs Landlock (and x86_64 for the seccomp filter): a kernel without it refuses every agent run until the
  operator chooses permissive, which runs without the filesystem guard and says so in every result.
- 3MF is not byte-reproducible; its sha256 is recorded and never compared.
- Mesh pulls (SDF and TPMS results) are STL only: no 3MF and no faceted STEP.
- A hole with a counterbore or countersink at both ends reports the upper end's feature only (one `cbore` or `csk`
  per hole record).
- One sourced material (AlSi10Mg, 2670 kg/m³, the golden demo's nominal value); others go in
  `~/.config/aces/cad-materials.json` with a non-empty `source`.
- Drawings have no section hatching, no radius dimensions and no centre marks, and they are not GD&T.
- No `aces cad` passthrough in aces-base 0.1.0: the commands are `aces-cad` and `aces-cad-mcp`.
- The aces-omarchy agent skill does not mention aces-cad yet (golden-rig owns that file now).

**VM acceptance (2026-10-10, 10:15 to 10:35 UTC).** All 13 steps of `rehearsal/README.md`, "aces-cad on the VM", ran
on the Omarchy VM (kernel `7.2.5-3-omarchy`) with the packages `bin/build` made from main 61ac8a4 and served from the
LAN repo (aces-cad 0.1.0-1, aces-os 0.1.0-6, aces-engineering 0.1.0-5, calculix-ccx-bin 2.23-1); the records, renders,
drawing, bundles, the agent loop's event stream and the cancel test are in `rehearsal/reports/2026-10-10-aces-cad-vm/`,
and the README section has every figure. In short: doctor ready, strict, seccomp 106 instructions with TSYNC, Landlock
ABI 10 with the TCP rules and the signal and abstract-socket scoping in force, 14 of 14 checks; selftest 13 PASS in
37.8 s; the ACES plate 11607.300918301276 mm³ exact, 60 × 40 × 5, counts 1/1/7/15/10, valid, 2.4 s; inspect: one
through Ø10 hole entering at (30, 20, 5), thickness min 5.0 mm, mass 0.0310 kg, validity ok; renders 3.5 s and the
drawing 3.1 s reading 60, 5, 40, Ø10 THRU, 30, 20 (checked by eye on the PNG and entity by entity in the DXF); pull with
`check_bundle.py` clean, `aces ledger verify` ok, the `cad.pull` entry with `class: sovereign`, and a second pull with
the same geometry_sha256 `806c9be7…`, equal to the Windows and Arch values. Hostile scripts as the desktop user: socket
exit 5, loop exit 4 with no worker left, memory exit 4, the planted secret exit 5 with the marker in no output, OCCT
export outside fails (exit 1, "Failed to write STEP file") with the file absent, kill parent exit 5; a 60 s loop left
the VM responsive (the worker at nice 10, a foreground Python loop in 0.01 s). The agent loop ran with Codex 0.133.0
(gpt-5.5) as the MCP client over ssh: seven tool calls in order, each right at the first try, 102 s, a lesson in
`cad/NOTES.md`; Codex did not pass the server's `image/png` content to the model (the model saw the text and the
path). Claude Code was not the client: the CLI's stored login on DARKNESS had expired and the user settings route it to
a third-party endpoint; logging in again is the operator's. `notifications/cancelled` (Esc) mid-run: the worker gone in
0.13 s, no reply for the cancelled id, the server still answering; a client dying mid-run: the job runs out the server's
10 s EOF grace and is then killed (10.7 s). Golden hand-off: a disc domain from an aces-cad pull went through `aces
golden run` to PASS / verified-desktop in 50 s with the conforming tet10 check agreeing (`20261010T102210Z-bc7391`,
bench row in BENCHMARKS.md), and `aces golden verify` passes 11 checks with rig and resolve INFO.

## Known gaps

- Rig acceptance 2026-10-10: the sim pool is shared with the rig operator's hand-run containers, which take no lease; `rig_capacity` reports those GPUs as unavailable, so golden cases serialise on the free ones (GPU 0 for most of the day; one sweep case waited 239 s). The golden client behaves as designed; this is the rig's current use.
- The bench row's "Devices used" text mis-joined the rig device names when a run used more than one rig GPU (the
  four rows of 2026-10-10 in BENCHMARKS.md show it; the records themselves are right). Fixed in `golden_cli.devices_used`
  (aces-engineering 0.1.0-5: every `rig gpu N (name)` of a device string is one token; a GPU without a recorded name
  is reported as such); the row as 0.1.0-5 renders it is in BENCHMARKS.md, under the --rig-sweep section of 2026-10-10.
- `scripts/golden_validate.py` (cudax) writes the registry rows' `record` field as the absolute DARKNESS path of its out directory; for `rig-golden-2` the three rows were pointed by hand at `docs/validation/golden-20261010T082807Z/validation-record.json`, committed with the record and the six specs. The script should write a repo-relative path (cudax follow-up).
- The merged `step_structures.py` (cudax rig-golden-1.1, vendored in aces-engineering 0.1.0-4) defaults to `mg`, the rig's behaviour since the rig operator's patch of 2026-10-06, not the build spec's `pcg`; `fea_solver` wins over the rig's `solver` key when both are given. The desktop uses only its mesh stage.
- Two deferred `post` jobs of the rig operator (`mach-dart2-wing-44g`, `-b`, created 2026-10-06, parents failed) sit in the rig's cpu deferred registry; the deploy gate reports them as orphaned and does not wait for them. They are the rig operator's to remove; a note was left in Kimi's workspace and in `/home/pi` on the rig.
- Endpoints are known only for `sim-lab-4` (and the CPU embedding lane); the other modes' lanes stay `null` until probed in
  those modes. Per-instance GPU assignment for the two GLM-Flash servers is not published.
- Corrected 2026-10-09 in `aces-base` (rebuilt): the simlab MCP is on `http://192.168.1.65:8022/mcp` (port 8020 serves the
  Univer workbench) and the SIMLAB console is live on the rig at `http://192.168.1.65:8032`; the earlier defaults (8020 and
  the workstation's 8030) were wrong. `aces doctor` from DARKNESS now reports all five server endpoints reachable.
- `aces-field`'s AUR-only members (`can-utils`, `python-cantools`, `python-udsoncan`, `python-snap7`, `python-pyvisa`,
  `openems`) are optional dependencies until their recipes are forked into `pkgbuilds/`.
- The ArtCraft recipes are byte-for-byte AUR copies; `namcap` warns about a literal `x86_64` in each (upstream style).
- `aces-survey`: CloudCompare, COLMAP and Meshroom are AUR-only, so they stay optional until their recipes are forked;
  the WebODM docker images (`webodm/webodm_webapp`, `webodm_db`, `nodeodx`, `nodeodx:gpu`) are pulled from Docker
  Hub at first start, not shipped; `aces-webodm` has not been run on an Omarchy host yet (the Docker path was
  exercised on DARKNESS under Docker Desktop); the DJI Fly loading step is manual by design (the app has no import).
- Git, CI and signing landed on 2026-10-09 (day-14 gate items in VISION.md §11); the key is a development key. The Omarchy
  rehearsal ran the same afternoon and passed (below); what it did not cover is the desktop session itself (Hyprland and
  the shell on Hyper-V, the bar widget rendering live) and the VHDX cidata path.
- `aces-engineering` vendors `simlab` and `gendesign` under `LicenseRef-ACES-undecided` (CPA code with no licence file
  yet), and the OCP wheel it installs bundles OpenCASCADE (LGPL), libjbig (GPL) and FreeImage without licence files:
  both are distribution-gate items, not trial-mode blockers.
- aces-cad-onshape has not run against a live Onshape account; see its section.
- `calculix-ccx-bin` 2.23-1 brings GCC 7's `libgfortran.so.4` from Ubuntu 20.04's `libgfortran4` 7.5.0-6ubuntu2: the
  official binary needs it and no Arch package provides it. That second download is outside the downloads build spec
  section 3.2 lists; the operator's decisions of 2026-10-10 (item 1) chose the same deb and sha256, with a wrapper
  script at `/usr/bin/ccx` and no `ld.so.conf.d` entry. The fixer committed the reviewer's layout for finding 4
  instead (the library in its own directory behind an `ld.so.conf.d` entry, the binary itself behind `/usr/bin/ccx`),
  because the conforming check hashes `realpath(ccx)` and a wrapper would put the wrapper's sha256 in every desktop
  record. The layout waits for the judge or the operator to confirm it. Building ccx from source against Arch's
  gfortran, ARPACK and a SPOOLES build would change the ccx sha256 every conforming record names. An image that gives
  the binary only `libgfortran.so.5` (Ubuntu 24.04's `libgfortran5`, for example) still cannot run it.
- The conforming check solves with SPOOLES only: a mesh above `SPOOLES_MAX_DOF` (800,000 DOF) is `too-large`, not
  run, on the desktop and on the rig alike, so the `MAX_DOF` limits (1,500,000 here, 3,000,000 on the rig) are not
  reached (review finding 1: ccx's iterative solver left the reactions out of balance by 1.46e-3 to 2.71e-3 relative
  on the test parts, where the tolerance is 1e-6). Raising the limit waits for a measurement of SPOOLES at those sizes
  against the time limits.
- Validation is pending until it runs: the rig registry's `golden-check` and `ccx-check` rows stay `pending` until the
  operator validates them on the rig, so `rig-verified` is unreachable until then, and every image rebuild
  invalidates the validation.
- The rig's patched gendesign evaluator multiplies `self.fea.loads` in place in `evaluate()`, and
  `cases/bracket_param.py` reuses one evaluator, so this matters there (build spec section 1, item 10). Recorded,
  not fixed.
- `CHECK_MAX_CELLS` (2,000,000) on the rig: a check level above it is not run on the rig, so a finer level 2 than that
  stays undone with a lane.

## How to reproduce

```bash
bash bin/build && bash bin/install-test
```

`bin/build` with no arguments follows `pkgbuilds/ORDER` (members before dependants; `nodeps` for the meta-packages) and
signs when `keys/aces-pkgs.sec.asc` is present. The same two commands run in CI on every push.

Logs for every package are in `logs/<pkg>.log` and the install test in `logs/install-test.log`; the repo database is
`repo/x86_64/aces.db.tar.zst`. Sources under `src/<pkg>/` are packed at build time, so a source edit needs a rebuild; a
rebuild without a `pkgrel` bump replaces the package in the local repo in place (fine for development, never for a published
channel).

## Rehearsal VM kit (`rehearsal/`, 2026-10-09)

Scripts that install stock Omarchy 4.0.4 unattended into a Hyper-V VM on DARKNESS (`get-iso.ps1`, `make-cidata.ps1`,
`new-vm.ps1`), serve this repo to it over the LAN (`serve-repo.ps1`) and run `post-install/aces-install.sh` in it
(`validate.ps1`); step order, elevation and reset are in `rehearsal/README.md`.

**Run on 2026-10-09 (afternoon):** `omarchy-4.0.4.iso` downloaded and verified (3.5 min), Omarchy installed itself
from the cidata ISO in under 2 min, and the ACES step passed **13 PASS, 0 FAIL** on its third attempt
(`rehearsal/reports/2026-10-09-aces-rehearsal-report.txt`): `omarchy update -y`, then all 16 packages from the signed
LAN repo under `SigLevel = Required DatabaseOptional` in 75 s, `aces doctor` reaching every rig endpoint from the VM,
`aces fabric refresh` reading the rig's `sim-lab-4` mode and five lanes, the engineering and voxel self-tests passing in
the venv, and `omarchy plugin validate` passing on the real host (the predicted symlink failure is gone: the plugin is
copied). Two checkpoints exist on the VM, `omarchy-clean` and `aces-installed`. The first two attempts found three kit
defects, all fixed: `get-iso.ps1` died on curl's stderr under PowerShell 5.1's error handling; `sudo -v` prompts on a
NOPASSWD host when the stock `%wheel` rule lacks NOPASSWD (`verifypw=all`), stalling an unattended run; and
`omarchy update` aborts with `OMARCHY_PATH` unbound in a plain ssh command, because Arch's bash reads no rc file there
(`validate.ps1` now uses a login shell and `aces-install.sh` sources Omarchy's `env-bootstrap`). Earlier container-stage
findings that still hold:

- `repo-add` in the build container leaves `aces.db` and `aces.files` as WSL symlinks that Windows programs cannot open,
  so `repo/` cannot be served as it is from Windows (`aces.db` answers 404); `serve-repo.ps1` serves real copies. The
  signing key is published to the VM only once every package has a `.sig`: with the key and no signatures,
  `SigLevel = Required` refuses every package.
- The cidata format has no post-install hook (archinstall's `packages` and `custom_commands` are not acted on by the
  Omarchy orchestrator), so the ACES overlay stays a post-install step over SSH or the console.
