# ACES Console Redesign — Conversation-Spine Workbench + Multi-Physics Golden Loop

Date: 2026-10-12
Status: approved design (sections §1–§3 approved in conversation)
Target repo: `aces-os` (`src/aces-console`, `src/aces-engineering`)

## 1. Problem

The current aces-console (8 plates: Fabric, Projects, Pipeline, Solvers, Objects, Evidence,
Agents, Ops) presents state as a document: every field annotated with its provenance, absence
explained in prose, controls buried. The owner cannot operate it: "it reads like a document",
"too confusing, I don't understand". The console must become the front-end of the ACES
generative engineering loop, not a status ledger.

## 2. Goals

1. Describe a part/machine in natural language → a visible plan → validated golden runs →
   results in the thread: 3D geometry, verdict, constraint scorecard, STL/STEP + report.
2. Conversational iteration: follow-up messages ("what if shock doubles?") spawn comparable
   sibling candidates in the same thread.
3. Plan-first: every substantive request generates a plan card (decomposition, per-piece
   specs, GPU cost) before any compute is spent. Nothing runs without the plan being visible;
   pieces can be struck or reordered.
4. Live specification sheet: every generated thing owns a spec sheet docked beside its 3D
   view. Bidirectional: edit a value in the sheet → regenerate + re-score; click a member in
   3D → its row highlights. Deltas shown (`mass 41.2 → 38.7 kg`). Provenance becomes a
   hover, never the headline.
5. Multi-physics capacity: the golden loop grows from single-part structural to as many
   fields as practicable (§5).
6. Complete reskin: new visual language and plain-words copy. The audit discipline of the
   backend is unchanged; the presentation stops being the audit.

## 3. Non-goals (v1)

- No LAN/VPN binding: the console stays loopback-only.
- No ground-up React rewrite (that was Approach C, deferred; A + reskin first).
- No Tier 3 physics: crash, multibody dynamics, aeroelasticity. The plan card names them
  "not yet wired — tracked" when a request needs them; nothing silently drops.
- No changes to the golden stage order, exit codes, or record formats.

## 4. Architecture

Approach A: conversation as the spine; the existing plates become panels the thread opens.

### 4.1 Frontend (static/, vanilla JS, stdlib server — unchanged stack)

- **Thread view** replaces the plate atlas as the home screen. Message types:
  - user text
  - assistant text
  - **plan card** — decomposition line items (name, mini-spec summary, estimated GPU cost,
    physics involved), each strikeable/reorderable; "run" / "just this piece" actions
  - **spec draft card** — the agent-filled `aces-golden-spec/1` as an editable table;
    confirm edits or "go"
  - **stage chips** — live per-run: `meshed → swept N designs → optimising round k/m →
    verified`; clickable for detail, quiet otherwise; driven by the existing SSE bus
  - **result card** — 3D winner (existing three.js viewer), plain-English verdict line,
    constraint scorecard (pass/fail + margin per constraint), collapsed "why" panel, STL/STEP
    + report.md/html download buttons
  - **compare toggle** — sibling candidates in a thread become comparable rows
- **Live spec sheet dock** — right-hand dock beside the 3D view for the selected candidate:
  geometry pane + sheet pane, bidirectional editing as in goal 4. Rows grouped by physics.
- **Plates as panels** — the 8 plates remain, opened as overlays/side panels when the thread
  references them ("see the fleet" → Fabric panel). The plate rail is no longer the home
  screen.
- **Reskin** — new stylesheet (visual language: plain-words status, cards over ledger rows,
  provenance demoted to hover/inspector). Copy rewrite: every block title and note reviewed
  for operator comprehension. The inspector column and `/vocab.json` discipline remain
  underneath.

### 4.2 Backend (aces_console server, Python stdlib)

- **Agent orchestration layer** (new module, e.g. `aces_console/loop.py`): interprets the
  user message, decomposes into part-level work, fills `aces-golden-spec/1` drafts, and —
  after plan/spec confirmation — triggers the fixed pipeline. The agent may only *trigger*
  runs with a validated spec; the run itself is the deterministic golden pipeline
  (hybrid model, approved). Every agent action lands in the existing operator ledger.
  The agent's model backend (local rig-hosted model vs an external API) is deliberately
  left to the implementation plan; the spec only requires that no credential or ledger
  discipline changes.
- **New API routes** (all loopback, token discipline unchanged):
  - `POST /api/loop/message` — submit a thread message; returns plan/spec-draft/running state
  - `GET /api/loop/thread/{id}` + SSE stamp `loop.thread` — thread state and deltas
  - `POST /api/loop/plan/{id}/approve` (with struck/reordered items)
  - `POST /api/loop/spec/{id}/confirm` (with edits)
  - `GET /api/loop/candidate/{project}/{run}` — result-card payload (wraps existing
    `/api/golden/run/...` + file routes)
  - `POST /api/loop/candidate/{id}/edit` — spec-sheet edit → regenerate + re-score job
- Existing `/api/golden/*`, jobs, poller, SSE, ledgers, secrets handling reused as-is.
  Evidence plate's start-run machinery becomes the run launcher the loop calls.

### 4.3 Data flow

user message → loop.py → plan card → (approve) → spec draft(s) → (confirm) →
`aces golden run` (or rig lane) as a tracked job → SSE stage stamps → thread stage chips →
result.json/report.json + exports → result card → (follow-up message) → sibling candidate
linked to parent, compare enabled.

### 4.4 Error handling

- Agent cannot parse a request → thread reply naming the missing constraint(s), no run.
- Plan item whose physics is not wired → item rendered "not yet wired — tracked"; the rest
  of the plan proceeds.
- Golden exits map to thread-visible plain-language outcomes (exit 5 → "no feasible design
  inside the envelope — here's the closest candidate and which constraint binds").
- Token/secrets behaviour unchanged: no route, log, URL, or ledger payload carries a token.

## 5. Multi-physics expansion (approved tiers)

The registry pattern (`solvers.json` + `solvers.d/*.json` drop-ins + adapter modules) makes
this additive; golden's stages dispatch through the registry. We extend what stages can
dispatch; we never rewrite the loop.

**Tier 1 — wire existing validated capability into the loop:**
- **CalculiX (ccx)** — adapter `ccx-check` exists (validation pending). Adds modal/vibration,
  buckling, steady-state thermal on the same part geometry. Unlocks "military spec"
  shock/vibe checks and cooling questions.
- **PyFR CFD on parts** — PyFR 3.1 images (`simlab-pyfr:0.3-avfix`) validated and active on
  the rig; `aces-tpms-periodic` (`aces-cfd:0.1`) covers lattice/tessellated flow. Tier 1 adds
  case templating from a spec (mesh + `.ini` generation from the exported STL + flow
  conditions) and a `physics: cfd` stage dispatch.
- **EM tier** — the Palace/gprMax pipeline in the `aces` repo (branch `em-gprmax-tier`,
  PR #4, jobs 28–33 shipped) becomes solver drop-ins, scoring RF performance (gain, S11)
  inside the loop for antenna/radar parts (AERIS).

**Tier 2 — extend existing kernels:**
- Steady-state thermal conduction on the voxel FEA kernel (same stiffness-matrix machinery)
  for lattice heat-path scoring.
- Fatigue life as a post-processor on verified stress fields (S-N from the material record;
  a new score, not a new solver).

**Tier 3 — not yet:** crash, multibody dynamics, aeroelasticity (see non-goals).

**Spec-sheet consequence:** one row-group per physics — mass/stiffness (structures),
natural frequencies (modal), max temp / thermal margin (thermal), drag / max velocity (CFD),
gain / S11 (EM), fatigue life. Each with margin and provenance hover.

## 6. STEP export (approved default)

Ship **STL + faceted STEP** labelled honestly "mesh-derived, not B-rep" (suitable for Fusion
import/iteration). True B-rep reconstruction via the formgen→Fusion bridge is a later
milestone. Golden's current STL-only export rationale (organic marching-cubes mesh) is
preserved in the label; no fake precision.

## 7. Testing

- Console: existing fixture-based tests extended with golden-run thread fixtures (the
  console already ships golden fixtures); new tests for plan-card state machine, spec
  confirm/edit round-trip, candidate compare, and every new route's refusal paths.
- Loop orchestration: unit tests with a fake agent + recorded golden runs; the pipeline
  itself is tested by golden's existing suite (300 passing at audit time).
- Solver drop-ins: each Tier 1 adapter lands with a validation case in `solvers.json`
  (status pass) before the loop will dispatch it — matching registry discipline.
- Acceptance: the "15 kg bracket, 8 g shock" and "tessellated chassis mass estimate"
  conversations run end-to-end on the rehearsal VM with ledgers verified.

## 8. Reuse map (verified 2026-10-12)

| Capability | Where | State |
|---|---|---|
| golden pipeline (10 stages, spec `aces-golden-spec/1`) | aces-engineering `aces_engineering/golden` (14.5k lines, v0.2.0) | verified: `--demo` PASS, deterministic, ledgers verify |
| console server/SSE/jobs/ledger/secrets | aces-console `aces_console` | in production use |
| golden console integration (start/list/verify runs) | Evidence plate + `/api/golden/*` | exists |
| Structural SIMP/FEA | `gendesign-topopt`, `gendesign-beam-check`, `step-structures` | validated pass |
| CFD | `pyfr` (PyFR 3.1), `aces-tpms-periodic` | validated pass, actively running on rig |
| ccx adapter | `ccx-check` drop-in | exists, validation pending → Tier 1 task |
| EM pipeline | aces repo `em-gprmax-tier` | shipped, live rig run verified → Tier 1 task |
| Surrogates | `surrogate-bracket-gp-lhs`, `-fno` | pass (two older ones fail — noted) |
