# The ACES console (`aces-console`)

One desktop console for the rig, the simulation work, the projects, the golden and CAD records and the ledger. It is a
per-user, loopback-only web server written with the Python standard library (`aces_console`, importing the `aces`
package of aces-base in-process) and one static Parameter Sky front-end served from the same origin, opened from one
launcher entry, **ACES**. It replaces the three `aces-consoles` entries (GRID CONTROL, SIMLAB, MLflow) on the desktop.
Package `aces-console` 0.1.0-1; decision record `docs/decisions/2026-10-11-aces-console.md`; package notes
`src/aces-console/README.md`; register rows in `docs/PACKAGES.md`.

Every value on the page traces to a file on this desktop or to an endpoint the rig publishes. Nothing is deployed to
the rig and the rig host is never touched: GRID CONTROL at `:3200` stays the rig operator's shell and the mode-switch
engine the console calls; the SIMLAB console container at `:8032` stays the rig's read-only data plane and is proxied;
MLflow's REST API is read and its UI stays a link; rig-state at `:8010` is read through its MCP tool. Port 8020 and
redis are never contacted.

## For the operator

### Install and replacement

`pacman -S aces-console` (or `pacman -Su` on a desktop that has `aces-consoles`: the package carries
`provides`/`conflicts`/`replaces` for it, so the upgrade asks "Replace aces-consoles with aces/aces-console?" and
removes `aces-consoles` in the same transaction, whether or not `aces-consoles` is still in the repository; seen in
a clean container on 2026-10-11). Afterwards `pacman -Q aces-consoles` still answers, with `aces-console 0.1.0-2`,
because it resolves the `provides=`; `pacman -Qq | grep -x aces-consoles` is the by-name check. `aces-os` 0.1.0-7
depends on `aces-console`. Files: `/usr/bin/aces-console`, the front-end under
`/usr/share/aces/console/static`, the tests and the install check under `/usr/share/aces/console/tests`,
`/usr/share/applications/aces.desktop`, `/usr/share/doc/aces-console/` (README, an example systemd user unit that is
documentation only) and `/usr/share/licenses/aces-console/` (LICENSE, LICENSES.md).

### Launch, stop, status

| Command | What it does |
|---|---|
| **ACES** in the app menu, or `aces-console` / `aces-console open` | starts the server when `/api/health` does not answer, waits for it (up to 90 s), then opens the page in an Omarchy web-app window (`omarchy-launch-webapp`), else `xdg-open`, else Python's `webbrowser` |
| `aces-console start` | starts the server detached (`python -I -m aces_console serve`), its output appended to `server.log` |
| `aces-console serve [--bind H] [--port N] [--static DIR]` | runs the server in the foreground |
| `aces-console stop` | SIGTERM to the recorded pid, 5 s, SIGKILL, removes the pid file |
| `aces-console status` | pid, URL and the health summary, or `not running` |
| `aces-console url` | prints the URL (`http://127.0.0.1:8070` by default) |
| `aces-console <server key>` | the old `aces-consoles` behaviour: opens `server.<key>` from the aces configuration (`grid_control`, `simlab_console`, `mlflow`, `webodm`, `video_studio`) in a web-app window |

The server runs on demand; no systemd unit is installed. The page's Ops plate offers **Stop console** (type `stop`),
which is ledgered as `console.server.stop`. Exit codes: 0, 1 failure, 2 usage.

### Configuration

Read through `aces.config.load()`: built-in defaults, `/etc/aces/aces.toml`, then `~/.config/aces/aces.toml`.
aces-base 0.1.0-4 documents the table; the console carries the same defaults in-package and needs only
`aces-base>=0.1.0-3`.

```toml
[console]
bind = "127.0.0.1"                                # 127.0.0.1 or ::1 only; anything else is refused at start
port = 8070
static_dir = "/usr/share/aces/console/static"     # ACES_CONSOLE_STATIC overrides it (development)
state_dir = ""                                    # empty = $ACES_STATE/console (default ~/.local/state/aces/console)
```

The rig endpoints come from the `[server]` table (`grid_control`, `rig_state`, `simlab_mcp`, `simlab_console`,
`mlflow`, `timeout_s`). LAN or VPN binding is not offered in v1.

### Tokens

The GRID CONTROL token (header `x-gc-token`) is read server-side from `$ACES_GC_TOKEN`, `$SIMLAB_GC_TOKEN`, then
`KEY=VALUE` lines of `~/.config/aces/gridcontrol.env` (mode `0600`; a wider mode produces one log warning naming the
file, never the value). The simlab bearer token follows the same order with `$ACES_SIMLAB_TOKEN`, `$SIMLAB_MCP_TOKEN`
and `~/.config/aces/simlab.env`. The operator writes these files by hand; nothing pre-populates them. `/api/config`
reports only whether each token is configured and where it came from. No route, log line, URL, page, ledger payload or
screenshot carries a token value, and the JSON encoder masks any credential-named key as a last line of defence.
`~/.config/aces/onshape.env` is never opened.

**Known limit, stated plainly.** Agents run as the same Unix user as the shell, so a token in `gridcontrol.env` or the
environment is readable by them and gives them a direct, unledgered path to GRID CONTROL's `POST /api/switch`. The v1
mitigations are the loopback bind, no MCP tool and no CLI flag for the switch, the gates, the two-step nonce and the
ledger trail. The v2 path is a rig-side authenticated control endpoint with a human-knowledge factor.

### State and logs

`$ACES_STATE/console/server.pid` and `server.log` (mode 0600; one line per request: method, path without the query,
status, duration; never headers or bodies). Everything else lives in memory. Secrets are written nowhere.

## The page

Seven plates, one rail (at the bottom below 768 px wide), deep links `?plate=<id>&...`, an inspector panel for the
selected row, a strip that shows the SSE state (`live`, `polling`, `reconnecting`) and the fixture stamp when the
server replays captures.

| Plate (`id`) | What it shows | Sources |
|---|---|---|
| Fabric (`fabric`) | GRID CONTROL mode and queue (`-1` reported as "GRID CONTROL reported -1", never as a count), GPUs, telemetry ring, the rig-state snapshot with every service and endpoint classified against the mode's stop/keep lists, the fabric manifest and its age, modes | `rig.status`, `rig.telemetry`, `rig.snapshot`, `rig.modes`, `rig.decisions`, `fabric.manifest`, `sim.capacity` |
| Work (`projects`) | the projects under `$ACES_WORK` with class, counts (golden runs, CAD runs, ledger entries), evidence, CAD sources, the Onshape gate as the class allows | `projects`, `/api/projects/{name}` |
| Pipeline (`pipeline`) | **SIMLAB actions**: every simlab-mcp tool (validate, put spec, put file, submit, sweep, cancel, status, results, reports, evidence) with a form built from the tool's own input schema, the write tools confirmed by typing their name; the **MLflow browser**: experiments, their runs with the latest metrics, a run's params, metrics with their history, tags and artifacts (listed and downloaded); the SIMLAB console's summary with its two scales side by side ("cases" on the console case scale, "queue jobs" on the rq job scale; they differ and are never merged), the queue by rq status and by meta state, the experiments, an experiment's stages, reports and cases, a case's params, results, files, jobs and MLflow run, the MLflow experiments, `whoami` | `sim.summary`, `sim.experiments`, `sim.capacity`, `sim.whoami`, `mlflow.experiments`, `/api/sim/experiments/{eid}`, `/api/sim/case/...`, `/api/mlflow/run/...` |
| Solvers (`solvers`) | the solver registry with validation status on the registry scale, image ids ("not recorded" when absent), the citability note, lanes ("not recorded" unless the rig records one), GPU capacity with each GPU's activity, the sim modes beside GRID CONTROL's modes | `sim.registry`, `sim.capacity`, `rig.modes` |
| Objects (`objects`) | a project's CAD runs (status on the CAD scale, unreadable runs with the reason), a run's record and files, drawings (SVG, dimensions, hole table), bundles with **Check bundle** (the installed `check_bundle.py`, its own lines) and **View part.stl** (the Three.js viewer), notes, the Onshape card (never connected from here) | `/api/cad/*`, `/api/projects` |
| Evidence (`evidence`) | golden runs per project; a run's grade followed by "no grade is a sign-off", verdict, stages, validation, compute used, sweep, winner, figures, the report in a sandboxed iframe, **Verify** (`aces golden verify`, or the honest 503 when aces-engineering is absent), files, events, read errors; the desktop operator ledger with **Verify ledger**; the rig-side SIMLAB chain | `golden.runs`, `ledger.aces`, `ledger.simlab`, `sim.evidence`, `sim.evidence.verify`, `console.health` |
| Agents (`agents`) | Mission Control as GRID CONTROL served it: the agent runs (live and recent) with their step timelines and artifacts, the chat sessions with a composer and attachments, the missions with their steps, outputs, approvals and actions (launch, retry failed, compile, mark done), a new-mission form, the agents. Agent-reported, never compute truth | `mc.runs`, `mc.missions`, `mc.sessions`, `mc.agents`, `/api/mc/*` |
| Ops (`ops`) | the switch (prepare, confirm), the auto-switch (typed words), the red-team mission VM (status; start and end with typed words), model staging, **Refresh fabric**, **Run doctor**, packages, desktop entries, links with their reach (GRID CONTROL, the SIMLAB console and MLflow are listed but never opened: their content is on these plates), **Stop console** | `rig.switch`, `rig.modes`, `rig.redteam`, `rig.downloads`, `doctor`, `system.packages`, `system.entries`, `/api/system/links` |

### Reading rules

- Every status word carries the name of its vocabulary (`/vocab.json`): the console's `PENDING / RUNNING / FINISHED /
  STALE / FAILED`, rq's `queued / started / finished / failed / deferred`, the job meta's `waiting_for_gpu / running`,
  golden's `running / done / interrupted / error`, a case's `FINISHED / NOT_RUN / FAILED`, CAD's `ok / running /
  error / unreadable`, the registry's `pass / not recorded / fail`, an endpoint's `by-design / transition / unreachable`.
  These are different scales and are never merged or summed.
- Four inks: chalk for the normal, cyan for the live, amber for the stale or the in-between, the fault ink for the
  broken. No green: nothing on this page is a sign-off.
- Absence is drawn with its reason ("no manifest yet; run aces fabric refresh", "GRID CONTROL reported -1",
  "rig-state unreachable: ...", "not captured", "not recorded"), never as an empty cell or a zero.
- A golden grade (`verified-desktop`, `rig-verified`, ...) is always followed by "no grade is a sign-off"; desktop
  grades are not rig results.
- A stale read keeps its last good value under the amber `STALE` word with its age and the next retry; after 600 s it
  becomes null with the error text.

### The mode switch

Switching the rig's mode is a human act. The console never switches on its own, exposes no MCP tool and no CLI flag
for it, and never calls GRID CONTROL's `POST /api/auto`. **Switch mode…** on the Ops plate asks for a target; the
server checks on fresh reads that a token is configured, GRID CONTROL answers, the mode exists and is not the current
one, it is marked switchable, no switch is in progress, `detected_mode` is not `transition`, auto mode is off and the
switch engine is idle or done, and hands back a nonce that lives 30 s. The person types the target mode name and
confirms; the server checks everything again, writes `console.switch.request`, posts the switch, writes
`console.switch.result`, and writes `console.switch.outcome` once when the engine reports done (or `ok: null` with the
reason when it never does). No test and no tool presses this against the real rig: the tests use the fake GRID CONTROL
of `tests/fakes`.

### What the console writes to the operator ledger

All writes go through `aces.ledger.record()` and verify with `aces ledger verify`. Reads are never ledgered.

| Kind | When |
|---|---|
| `console.switch.request` / `console.switch.result` / `console.switch.outcome` | the two-step switch, its upstream result, and the engine's final outcome |
| `console.switch.refused` | a switch request that reached the gates and was refused (one per target and reason per minute) |
| `console.fabric.refresh` | **Refresh fabric** (`aces fabric refresh`, once per 60 s) |
| `console.system.open` | **Open** of an allow-listed link or desktop entry (`{id, target_kind}`, never the URL) |
| `console.server.stop` | **Stop console** |
| `console.sim.tool` | a simlab-mcp write tool run from SIMLAB actions (`{tool, arg_keys, args_sha256, ok, error, experiment}`, never the arguments) |
| `console.mc.session` / `console.mc.message` / `console.mc.mission` / `console.mc.approve` / `console.mc.upload` | Mission Control writes (counts and sha256 of the text, never the text) |
| `console.redteam.vm` / `console.auto` | the red-team VM start or end, the auto-switch on or off |

A write refused before it reaches the rig (no GRID CONTROL token on this desktop) is not an action and is not ledgered.

### Everything GRID CONTROL, the SIMLAB console and MLflow offered (2026-10-11, later the same day)

Aaron: one UI with everything accessible, and not GRID CONTROL, the SIMLAB console and MLflow. So the console now
carries their actions too: Mission Control (Agents plate), the simlab-mcp tools including submit, sweep and cancel and
the MLflow browser (Pipeline plate), the auto-switch, the red-team VM and model staging (Ops plate). Their pages are
listed as endpoints but never opened from here, and `aces-console grid_control|simlab_console|mlflow` opens this
console at the plate that holds their content. Not copied: GRID CONTROL's "Connectors" page, a hard-coded list in its
page source rather than a probed status.

Reads of GRID CONTROL's token-protected routes work without a token on this desktop: they go through the SIMLAB
console's GET-only `/gc` read proxy on the rig (which forwards with the rig's own token) and every reply says so.
Writes (chats, missions, approvals, uploads, the red-team VM, the auto-switch, the mode switch) need the token on this
desktop: `~/.config/aces/gridcontrol.env` with `ACES_GC_TOKEN=...`, mode 0600.

### Golden runs and CAD actions (0.1.0-3)

The Evidence plate starts golden runs (a project's `golden/spec.json`, the built-in demo, or a STEP file made into a
new project with `aces golden init`), with every `aces golden run` option, confirmed by typing the project name;
the run's progress streams into the plate and the finished run opens. The Objects plate runs `aces-cad` inspect,
render, drawing, pull and re-run with changed parameters (the run's own script in the sandbox; new code stays the
agents' text-to-CAD path) on a selected run, and prune on the project (typed `prune <project>`). Each start is
ledgered (`console.golden.run`, `console.golden.init`, `console.cad.<verb>`, `console.job.cancel`); routes:
`GET /api/jobs`, `GET /api/job/{id}?since=`, `POST /api/job/{id}/cancel`, `POST /api/golden/run`,
`POST /api/golden/init`, `POST /api/cad/action`.

### Still deferred

Approvals of consequential actions
(a UI button is not an approval: the aces.approvals overlay with a physical-presence factor), LAN/VPN binding and the
phone client, a systemd user unit, per-mode lanes from GRID CONTROL, a light theme, ACES Studio (the video lane: no
video service is deployed on the rig).

## API

The contract frozen on Day 0 (spec 3.1-3.13). `/api/health.data.modules` names which backend modules the running
server installed (`rig`, `sim`, `records`, `ledgers`, `system`); a route of a module that is not installed answers 404
and the server runs without it.

### Shapes and rules

Every JSON GET answers 200 with the envelope

```json
{"ok": true, "data": {}, "meta": {"source": "<url|path>", "fetched_utc": "...", "age_s": 0.4, "ttl_s": 3, "stale": false,
                                   "ok": true, "error": null, "next_try_s": null, "fixture": null}}
```

When an upstream is down, `data` is the last good payload with `meta.stale: true` for at most 600 s, then `null`;
`meta.ok` is false, `meta.error` is one line (`"unreachable: <reason>"`, `"HTTP 404"`, `"not configured: ..."`) and
`meta.next_try_s` the backoff. Other statuses: 400 bad parameters, 403 refused, 404 unknown record or route, 405
method, 409 gate or busy, 410 nonce spent or expired, 413 too large, 415 type not served, 429 rate limit, 500 console
bug, 502 an upstream POST failed, 503 not configured or not installed, with the error shape
`{"ok": false, "error": "<one line>", "code": "<slug>", "source": null, "status": null, "next_try_s": null}`.

Headers: every reply carries `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`; `/api/*` adds
`Cache-Control: no-store`; the page carries a Content-Security-Policy with the sha256 of its inline importmap,
`frame-ancestors 'none'`, `connect-src 'self'`; no CORS header is ever sent.

POST guards: a POST is accepted only from a loopback peer with `Origin` equal to the console's own origin,
`X-ACES-Intent: human`, `Content-Type: application/json` and a JSON object body of at most 64 KiB; anything else is
403 `{code: "refused"}`, counted in `/api/health.counters.refused_posts`.

Files (`/api/golden/file/...`, `/api/cad/file/...`, `/api/cad/bundle/...`, `/api/sim/file`): opened through `safefs`
(component-wise, `O_NOFOLLOW`, regular files only, under `$ACES_WORK`; no `.`/`..`, no dotfiles, nothing named `.env`,
`.key`, `.pem` or containing `token`; size caps by kind; a content-type allow-list, 415 otherwise) and served with
`Content-Security-Policy: sandbox`, `Content-Disposition: inline`, `Cache-Control: private, max-age=60`, a quoted
sha256 `ETag` and 304 on `If-None-Match`.

### Routes

| Route | Module | Data | Status codes beyond 200 |
|---|---|---|---|
| `GET /` | core | the shell (`index.html`) | |
| `GET /vocab.json` | core | the status vocabularies and inks | |
| `GET /api/health` | core | version, pid, started_utc, bind, port, origin, clients, poller, sources, counters, switch_outcome_pending, static_manifest_sha256, fixture, modules | never errors |
| `GET /api/config` | core | the `[server]` endpoints, the console table, token sources (never values), paths, platform | |
| `GET /api/events?plate=<id>` | core | the SSE stream: `event: stamp` per polled source, `: ping` every 15 s, `Last-Event-ID` replay, at most 32 streams | |
| `GET /api/rig/status` | core | GRID CONTROL status; `queue.queued/running` null when reported below zero with `queue_note` and `raw_queue` | |
| `GET /api/rig/modes` | core | `{gridcontrol_modes{label, switchable, switchable_reported, pending}, sim_modes, current}` | |
| `GET /api/rig/telemetry` | core | `{telemetry, history (≤ 240 samples), sources_used}` | |
| `GET /api/rig/snapshot` | core | rig-state `rig_snapshot` plus `classification[{name, kind, verdict, matched, rule}]` | |
| `GET /api/rig/decisions` | core | GRID CONTROL decisions, `schema: "not interpreted"` | 200 with `data: null` and the reason in `meta.error` without a token |
| `GET /api/rig/switch` | core | the switch engine's status with `log_tail` | |
| `POST /api/rig/switch/prepare` `{mode}` | core | the gates on fresh reads, a single-use 30 s nonce | 403, 409 gate, 503 no token |
| `POST /api/rig/switch/confirm` `{mode, nonce, confirm}` | core | the switch (ledgered) | 403, 409, 410 nonce, 502 upstream |
| `GET /api/fabric` | core | `{manifest, path, age_s, max_age_s, stale, modes_table}`; absent → `manifest: null`, `reason` | |
| `POST /api/fabric/refresh` | core | 202 `{started: true}` (ledgered) | 403, 409 running, 429 inside the minute |
| `GET /api/sim/summary` | sim | the SIMLAB console summary | |
| `GET /api/sim/capacity` | sim | capacity plus per-GPU `activity` (`job`, `lease`, `outside the queue (operator hand-run)`, `rests by mode <mode>`, `not in the sim pool`, `derated`, `idle`) | |
| `GET /api/sim/experiments` | sim | experiments and queue plus `queue_by_rq_status{}` and `queue_by_meta_state{}` | |
| `GET /api/sim/experiments/{eid}?root=` | sim | the full experiment record | 404 passthrough |
| `GET /api/sim/case/{eid}/{cid}?root=` | sim | the case plus `mlflow{run, ui_url, error}` | 404 |
| `GET /api/sim/file?eid=&root=&cid=&name=` / `&report=` | sim | a case or report file named in the cached record, under `CSP: sandbox` | 404 `not a file of this case` |
| `GET /api/sim/registry` | sim | rows plus `citable`, `citable_note`, `lane` (`"not recorded"` when the rig records no queue) | |
| `GET /api/sim/evidence` · `/api/sim/evidence/verify` · `/api/sim/audit` | sim | the rig-side evidence ledger, its verify result, the MCP audit | |
| `GET /api/sim/status/{eid}` · `/api/sim/whoami` | sim | simlab-mcp `sim_status` / `sim_whoami` plus `principal` / `token_source` | |
| `GET /api/mlflow/experiments` | sim | MLflow `experiments/search` passthrough | |
| `GET /api/mlflow/run/{run_id}` | sim | `runs/get` with `ui_url`; `data: null` with the reason until the shape is captured | |
| `GET /api/projects` · `/api/projects/{name}` | records | the projects with class, counts and the Onshape gate; a project with its ledger entries, golden runs and CAD runs | 404 |
| `GET /api/golden/runs?project=` | records | `{runs[{run_id, project, status, outcome, verdict, grade, ...}], root, reason}` | |
| `GET /api/golden/run/{project}/{run_id}` | records | run, result (or `result_absent_reason`), validation, compute_used, report, sweep, winner, candidate, figures, evidence_ledgers, files, events_tail, read_errors, notes | 404 |
| `GET /api/golden/file/{project}/{run_id}/<rel>` | records | the file under `CSP: sandbox` | 400, 403, 404, 413, 415 |
| `GET /api/golden/events/{project}/{run_id}?after=` | records | `{lines, eof, status}` while running | 404 `events.jsonl absent` |
| `POST /api/golden/verify/{project}/{run_id}` | records | `aces golden verify <run_dir> --json --no-model` → `{run_dir, ok, checks[{name, ok, detail}], exit, ran_at}`; a read, not ledgered | 403, 404, 409 running, 503 `aces-engineering not installed (exit 127)`, 500 |
| `GET /api/cad/runs?project=` | records | `{project, runs[{run_id, status, name, started, finished, sandbox, unreadable}], counts}` | |
| `GET /api/cad/run/{project}/{run_id}` | records | `{run_id, record, files[{rel, size, sha256, kind}]}` | 404, 413 |
| `GET /api/cad/file/{project}/{run_id}/<rel>` · `/api/cad/bundle/{project}/{name}/<rel>` | records | the file under `CSP: sandbox` | 400, 403, 404, 413, 415 |
| `GET /api/cad/bundles?project=` | records | `{bundles[{name, has{}, outcome_summary{ledger_written, ledger_n, ledger_kind, cad_source_recorded, cad_source_ref}}]}` | |
| `POST /api/cad/bundle-check/{project}/{name}` | records | `check_bundle.py <bundle>` → `{name, exit, lines, all_match}` (true only when the checker printed its all-match line); not ledgered | 403, 409, 503 `aces-cad not installed` |
| `GET /api/cad/notes/{project}` | records | `{entries[{ts, tags, run_id, body}], truncated}` from `cad/NOTES.md` | |
| `GET /api/cad/drawings/{project}` | records | `{drawings[{id, schema, views, n_dimensions, hole_table, files{dxf, svg, png, json}}]}` | |
| `GET /api/cad/onshape/{project}` | records | `{connected: false, note, project_class, gate_note}`; `onshape.env` is never opened | |
| `GET /api/ledger?kind=&n=` | ledgers | `{path, n_total, entries[{n, ts, kind, payload, hash, prev}]}`, newest first, masked payload keys | a corrupt file → `data: null`, `meta.error "ledger unreadable: ..."` |
| `GET /api/ledger/verify` | ledgers | `{ok, n, first_bad, message}` | |
| `GET /api/ledger/simlab` | ledgers | the rig-side chain `{entries, verify, note}` | |
| `GET /api/policy` | system | `{policy, override{present, path}}` | |
| `GET /api/doctor` · `POST /api/doctor/run` | system | the last `aces doctor` text and exit, or the note that it has not run; run it (90 s cap) | 403, 409 running |
| `GET /api/system/packages` · `/api/system/entries` · `/api/system/links` | system | `pacman -Q` filtered to `aces-*`/`omarchy*`; the `.desktop` entries owned by `aces-*`; the allow-listed links with `reach` and `openable` | |
| `POST /api/system/open` `{id}` | system | opens an allow-listed link or entry (ledgered `console.system.open`) | 403, 404 |
| `POST /api/system/stop` `{confirm: "stop"}` | system | ledgers `console.server.stop`, says bye to the SSE clients, exits 0 | 400 without the exact string, 403 |
| `GET /api/sim/tools` | sim | simlab-mcp `tools/list`: `{tools: [{name, description, input_schema, write}], write_tools}` | |
| `POST /api/sim/tool/{name}` `{arguments, confirm?}` | sim | one `tools/call`; write tools need `confirm` equal to the tool name and are ledgered | 400, 404 unknown tool, 502 tool error |
| `GET /api/mlflow/metric-history?run_id=&key=` | sim | `metrics/get-history` (at most 25000 points) | 400 |
| `GET /api/mlflow/artifacts?run_id=&path=` | sim | `artifacts/list` | 400 |
| `GET /api/mlflow/artifact?run_id=&path=` | sim | the artifact's bytes (`get-artifact`), sandboxed, 64 MiB cap | 400, 404, 413 |
| `GET /api/mc/agents`, `/api/mc/runs?limit=`, `/api/mc/run/{id}`, `/api/mc/sessions`, `/api/mc/session/{id}`, `/api/mc/missions`, `/api/mc/mission/{id}` | mc | Mission Control reads (tRPC `agents.list`, `runs.list`, `runs.get`, `chat.listSessions`, `chat.getSession`, `missions.list`, `missions.get`); `via: token` or `proxy` | |
| `POST /api/mc/session` `{title}`, `/api/mc/session/{id}/message` `{content, uploads}` | mc | `chat.createSession`, `chat.sendMessage`; ledgered | 503 without a token |
| `POST /api/mc/mission` `{title, objective, files, launch}`, `/api/mc/mission/{id}/{launch,retryFailed,compile,complete}`, `/api/mc/item/{id}/approve` | mc | missions; ledgered | 400, 503 without a token |
| `POST /api/mc/upload` `{filename, mime, data_b64}`; `GET /api/mc/upload/{id}`, `/api/mc/artifact/{id}` | mc | uploads (16 MiB) and their bytes, sandboxed | 400, 413, 503 |
| `GET /api/rig/downloads`, `/api/rig/redteam`; `POST /api/rig/redteam/vm` `{action, confirm}`, `/api/rig/auto` `{enabled, confirm}` | mc | model staging, red-team status, the VM up/down (`start mission vm` / `END MISSION`), the auto-switch (`enable auto-switch` / `disable auto-switch`); ledgered | 400 wrong words, 503 without a token |

### SSE topics

`event: stamp` records carry `{topic, fetched_utc, ok, error, next_try_s, stale, fixture, payload?}`; inline topics
carry their payload, the others are notices and the page refetches the route. The polling fallback uses the same
table. Topics and the plates that subscribe: `rig.status`, `rig.switch`, `fabric.manifest`, `console.health` (every
plate; inline), `rig.modes` (fabric, ops, solvers), `rig.telemetry` (fabric), `rig.snapshot`, `rig.decisions` (fabric,
ops), `sim.capacity` (fabric, pipeline, solvers), `sim.summary`, `sim.experiments`, `sim.whoami`, `mlflow.experiments`
(pipeline), `sim.registry` (solvers), `sim.evidence`, `sim.evidence.verify`, `sim.audit`, `golden.runs`, `ledger.aces`,
`ledger.simlab` (evidence), `projects` (projects), `doctor`, `system.packages`, `system.entries`, `rig.redteam`,
`rig.downloads` (ops), `mc.runs`, `mc.missions`, `mc.sessions`, `mc.agents` (agents).

## Development and verification

- Run from a source tree: `PYTHONPATH=src/aces-base:src/aces-console python -I -m aces_console serve --port 8070`
  (`;` joins the path on Windows). `ACES_WORK`, `ACES_USER_CONFIG`, `ACES_STATE` and `XDG_DATA_HOME` point the console
  at a work store.
- Unit tests (standard library `unittest`, no network beyond the fakes on 127.0.0.1; the tests' socket guard refuses
  any other host): `python -m unittest discover -s src/aces-console/tests -t src/aces-console`. They are installed under
  `/usr/share/aces/console/tests` and run again by the install test.
- Fixtures mode: `aces-console serve --fixtures tests/fixtures` with `ACES_CONSOLE_ALLOW_FIXTURES=1` replays the
  captures of 2026-10-11 (`MANIFEST.json` carries each file's origin host, route, capture time and sha256), stamps
  `FIXTURE 2026-10-11` on every reply and refuses the switch and the refresh.
- Browser harness (`tests/harness/`, Playwright when present, else SKIP): `devserver.py` serves the static tree with the
  fixtures and, with `--vm-work <copied work store>`, the real records; `verify_ui.py` and `verify_records.py` drive
  every plate at 1440 and 390 px, check every `[data-vocab]` word against its vocabulary, the inks against the `:root`
  tokens, overflow, console errors and failed requests, press **Check bundle**, **View part.stl**, **Verify** and
  **Verify ledger**, and write their findings as JSON. Nothing in the harness presses the switch.
- Install check (`tests/install_check.py`, spec 7.2): `bin/install-test` installs the package in a clean Arch
  container, exercises the replacement of `aces-consoles` with `pacman -Su`, sets a random `ACES_GC_TOKEN`, runs
  `python3 -I /usr/share/aces/console/tests/install_check.py --port 8070 --expect-ledger <3 | >=5 | >=7>` (loopback bind,
  CSP, `/api/rig/status` with `data: null` and the reason, the ledger count against `aces ledger verify --json`, the
  POST guard, `POST /api/system/stop` writing exactly one `console.server.stop`), greps the check log and the server log
  for the token value, and runs the installed unit tests.

What was run where, with its real output, is recorded in `docs/PACKAGES.md` (the aces-console rows).
