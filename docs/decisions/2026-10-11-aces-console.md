# One ACES console: desktop-resident, stdlib-only backend, single Parameter Sky front-end

**Date:** 2026-10-11

**Status:** accepted; the Day 0 reading assumed, Aaron to confirm. The reading: Aaron's words on 2026-10-11 were "one
single unified UI with everything accessible"; the build spec read them as one desktop console at `127.0.0.1:8070`
that replaces the three launcher entries of `aces-consoles` and keeps GRID CONTROL, the SIMLAB console and MLflow where
they run (spec §1). That reading has not been put to him yet. Everything below stands on it.

**Package:** `aces-console` 0.1.0-1 (`pkgbuilds/aces-console`, sources `src/aces-console`); `aces-base` 0.1.0-4
documents the `[console]` table; `aces-os` 0.1.0-7 depends on `aces-console`; `aces-omarchy` 0.1.0-7 names it as an
optional dependency. Branches: `console/a` (backend core), `console/b` (records modules), `console/c` (shell, Fabric,
Projects and Ops plates, harness), `console/d` (Pipeline, Solvers, Objects and Evidence plates, packaging, docs).

## Context

The operator asked for one unified UI instead of three (GRID CONTROL, SIMLAB console, MLflow). Three designs were
judged: A (desktop stdlib backend), B (rig-hosted console extended plus a desktop reporting agent), C (desktop backend
in the engineering venv). All three judges ranked A first. B puts new code on the rig host, which FACT_PACK.md:372 and
VISION.md:129 ("host is never touched") forbid; C ties the console to the engineering venv and its lock (lock.py:32-45)
and would not run on a machine without aces-engineering.

## Decision

Ship `aces-console` 0.1.0-1: a loopback-only Python stdlib server importing `aces` in-process, proxying GRID CONTROL,
rig-state and the SIMLAB console with tokens held server-side; one static front-end built from the SIMLAB console's
Parameter Sky modules; one launcher "ACES". It replaces `aces-consoles`. Reads answer 200 with an envelope that carries
staleness and error text; writes are two-step and ledgered through `aces.ledger.record()`; refusals are ledgered too.

## Grafts

From C: per-case file allow-list, CSP, ledgered refusals, fabric-refresh rate limit, nonce switch, registry citability
note, in-package `[console]` defaults, random token in the install test with a grep. From B: `-1` queue → null with
the reporting sentence, served HTML/SVG under `CSP: sandbox`, one envelope, the v2 path (a rig-side authenticated
control endpoint with a human-knowledge factor) and the rig-operator hand-off requests.

## Consequences

No systemd unit; the server runs on demand from the launcher. Same-user processes on the desktop can reach the
loopback port; v1 accepts this and documents it (spec §1; `src/aces-console/README.md`, "Tokens"), v2 moves authority
to the rig side. aces-base 0.1.0-4 documents `[console]` but the console depends only on `aces-base>=0.1.0-3`. CI grows
by 2-4 minutes (one pure-Python package build, its unit tests, the install check and the replacement step).

## Rejected

B's rig-side changes (host untouched; token exposure to a LAN-served page); C's venv coupling; any CORS or
token-in-URL pattern (GC index.html:686-688); any default status such as `"sim"` for an unrecorded lane (app.js:372).

## Rules this record binds

- The mode switch is a human act: two steps, gated on fresh reads, a 30 s nonce, ledgered; no MCP tool, no CLI flag,
  never pressed by a test or a tool against the real rig (tests use the fake GRID CONTROL of `tests/fakes`).
- Tokens never appear in a page, URL, log, ledger payload, screenshot or test output.
- Loopback bind only (`127.0.0.1` or `::1`; anything else is refused at start).
- The rig host is never touched; the console reaches the rig only through its published ports, never 8020, never redis.
- Nothing fabricated or sampled: fixtures are the captures of 2026-10-11 with their provenance in `MANIFEST.json`;
  absence is drawn with its reason; every status word carries its vocabulary label; no green ink.

## Amendment, 2026-10-11 (later the same day)

Aaron rejected the first cut: "one single unified UI with everything accessible", not GRID CONTROL, the SIMLAB
console and MLflow beside it. The v1 deferrals that kept those pages necessary are reversed: the console carries
Mission Control (an Agents plate), every simlab-mcp tool including submit, sweep and cancel, an MLflow browser, the
auto-switch, the red-team VM and model staging, all as human POSTs that are ledgered, and it no longer opens the three
replaced pages. GRID CONTROL stays the rig's switch engine and Mission Control's server; the SIMLAB console stays the
rig's data plane (its `/gc` read proxy carries GRID CONTROL reads when this desktop holds no token); MLflow stays the
tracking server. Only their pages are retired. Approvals of consequential actions stay with the unbuilt
aces.approvals overlay.

## Open

- Aaron's confirmation of the Day 0 reading (above).
- The v2 control path (rig-side authenticated endpoint with a human-knowledge factor), which removes the GRID CONTROL
  token from desktops; the v1 switch routes are shaped so that endpoint can implement them unchanged.
- The licence of the five files copied from cudax-sim-runtime (`LicenseRef-ACES-undecided` in
  `src/aces-console/LICENSES.md`), to be decided by the cudax owner.
- The deferred v1 items: simlab submit/sweep/cancel, CAD worker actions, starting golden runs, the auto-switch toggle,
  Mission Control planes, approvals, LAN/VPN binding and the phone client, a systemd user unit, per-mode lanes, a light
  theme (`docs/CONSOLE.md`, "Deferred in v1").
