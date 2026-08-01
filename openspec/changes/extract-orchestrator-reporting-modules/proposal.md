## Why

`orchestrator/opsx-plan.py` has grown from 822 lines (2026-06-12) to 8,218
lines (2026-08-01) across 238 top-level definitions, with 44 of its 47 lifetime
commits landing in the last 30 days. The file now exceeds the default read
window of the very workers that maintain it — `opsx-implementer` and
`opsx-reviewer` cannot read it in one pass, so every round is a partial read or
a blind grep. Because the plan orchestrator runs changes from a DAG in
parallel, concurrent changes also collide on this single file.

Measurement shows the file is already structured for extraction: 23
banner-delimited sections with only 76 cross-section reference pairs, and just
two bidirectional pairs — both artifacts of banner placement rather than real
cycles. This change takes the first slice: the three sections with the lowest
coupling and no test-monkeypatch exposure.

## What Changes

- Introduce an importable `lib/orchestrator/` runtime package alongside the
  existing `lib/metrics`, `lib/pricing`, and `lib/models` packages.
- Move three sections out of `orchestrator/opsx-plan.py` into that package,
  preserving behavior exactly:
  - **Dashboard command** (960 lines) — HTML rendering helpers and
    `cmd_dashboard`
  - **Report command** (494 lines) — table/JSON formatting helpers and
    `cmd_report`
  - **Cost estimation** (230 lines) — `estimate_stage_cost` and its
    price-snapshot and per-token helpers
- Keep `orchestrator/opsx-plan.py` as the CLI entrypoint. It continues to own
  argument parsing and dispatch, importing the extracted modules.
- Extend `scripts/install-orchestrator.sh` to deploy the new package to
  `~/.local/lib/opsx-controller/lib`, matching how the existing runtime
  packages are shipped.
- Split the corresponding test coverage out of
  `tests/orchestrator/test_opsx_plan.py` into test modules matching the new
  source modules.
- No user-visible behavior changes. No CLI flags, output formats, telemetry
  shapes, or file locations change. This is not a redesign — the extracted code
  moves as-is.

## Capabilities

### New Capabilities
- `orchestrator-module-layout`: How the orchestrator's Python source is
  organized into importable runtime modules, how the CLI entrypoint resolves
  them, and the requirement that extraction preserves observable behavior.

### Modified Capabilities
- `shared-orchestrator-installation`: The requirement enumerating the runtime
  packages every global installer deploys currently names `metrics`, `pricing`,
  and `models`. It must also cover the orchestrator package, and the stale-copy
  detection in `opsx-plan doctor` must account for an entrypoint whose logic
  now lives partly in installed modules.

## Impact

- **Code**: `orchestrator/opsx-plan.py` (three sections removed, imports
  added); new `lib/orchestrator/` package; `scripts/install-orchestrator.sh`.
- **Tests**: `tests/orchestrator/test_opsx_plan.py` (17,401 lines) loses the
  report, dashboard, and cost test classes to new sibling modules;
  `tests/installer/test_installers.py` gains coverage for the newly deployed
  package. The suite is currently 738 tests, green in ~81s, and must stay green
  at the same count.
- **Deployment**: `~/.local/lib/opsx-controller/lib` gains a directory. Any
  operator running an installed `opsx-plan` must rerun a global installer after
  this lands, since the entrypoint alone is no longer sufficient — `opsx-plan
  doctor` must report the mismatch rather than failing at import time.
- **Not affected**: telemetry schema, pricing catalog, state files, plan
  manifests, adapter definitions, and every requirement in
  `plan-run-observability`.
