## Context

`orchestrator/opsx-plan.py` is 8,218 lines across 238 top-level definitions,
organized into 23 banner-delimited sections. Static analysis of the call graph
shows 76 cross-section reference pairs and only two bidirectional pairs, both
caused by banner placement rather than genuine cycles. The file is already
close to a directed acyclic module graph; nothing needs to be redesigned to
extract from it.

This change takes the first slice. Measured dependencies of the three target
sections:

| Section | Lines | Outbound dependencies |
|---|---|---|
| Cost estimation (2218–2447) | 230 | `_ensure_runtime_modules` only |
| Report command (6187–6680) | 494 | `PlanError`, `load_plan`, `resolve_plan`, `_resolve_plan_path`, `single_change_manifest_path` |
| Dashboard command (6681–7640) | 960 | the same three plan-resolution helpers, plus `_resolve_for_change_plan` from report |

Inbound references from the rest of the file are only three: `cmd_report`,
`cmd_dashboard`, and `estimate_stage_cost`. The first two are argparse dispatch
targets; the third has a single call site in telemetry recording.

The shared plan-resolution helpers are not themselves in the slice. Their
transitive closure is 13 definitions totalling roughly 250 lines
(`load_plan`, `resolve_plan`, `_resolve_plan_path`, `single_change_manifest_path`,
`read_active_plan`, `active_plan_pointer_path`, `topo_sort`, `is_direct_mode`,
the three `_parse_*` config helpers, `PlanError`, and `log`), and that closure
is self-contained — every one of its own dependencies is inside it.

Constraints: stdlib only, Python 3.11+, no behavior change, and the suite
(738 tests, ~81s) must stay green at no lower a count.

## Goals / Non-Goals

**Goals:**
- Establish `lib/orchestrator/` as the package where orchestrator
  implementation modules live, deployed by the existing installer mechanism.
- Move the cost, report, and dashboard sections into it without changing
  observable behavior.
- Fix the monkeypatch-target problem structurally, so later and larger slices
  do not each have to relitigate it.
- Leave `orchestrator/opsx-plan.py` around 6,300 lines, below the point where a
  worker must read it in fragments to see one concern.

**Non-Goals:**
- Redesigning the report/dashboard/cost logic. Definitions move as-is, keeping
  their names and signatures.
- Extracting the remaining 19 sections. `Commands` (fan-out 14) and the direct
  execution loop hold most of the monkeypatch exposure and are deliberately
  deferred.
- Splitting `tests/orchestrator/test_opsx_plan.py` beyond the classes covering
  the moved code.
- Any change to telemetry schema, pricing catalog, state files, or plan
  manifests.

## Decisions

### D1: The package lives at `lib/orchestrator/`, not `orchestrator/`

The entrypoint's `_ensure_runtime_modules()` already places a root containing
`lib/` on `sys.path`, and `scripts/install-orchestrator.sh` already copies
`lib/metrics`, `lib/pricing`, and `lib/models` to
`~/.local/lib/opsx-controller/lib`. Putting the new package there means the
resolution mechanism, the installed layout, and the import spelling
(`lib.orchestrator.<module>`) all follow existing precedent.

*Alternative considered:* making `orchestrator/` itself a package. Rejected —
`orchestrator/` holds the executable and its samples, is installed to a
different destination (`~/.local/bin`), and would need a second, parallel
resolution path.

### D2: Call across modules through the module object, never through imported names

Modules import each other as `from lib.orchestrator import base` and call
`base.log(...)` — not `from lib.orchestrator.base import log` and `log(...)`.

This is the load-bearing decision. Today every test patches
`self.opsx_plan.<name>` and it works because there is exactly one module, so
there is exactly one binding to rebind. Under name-imports, each importing
module gets its own binding and patching any single one misses the others,
which fails *silently* — the mock is installed, the assertion setup looks
right, and the real function runs. Under module-qualified calls the attribute
is resolved on the module object at call time, so patching
`lib.orchestrator.base.log` is seen by every caller. This preserves current
patching ergonomics exactly rather than degrading them.

It also means the `log` and `git` helpers, which the whole file leans on
(`log` is referenced from 9 sections), can be extracted safely in this slice
instead of being a hazard in every later one.

*Alternative considered:* re-exporting names from the entrypoint for backward
compatibility. Rejected — reads would work, writes would not, so the failure
mode is precisely the silent one described above.

### D3: The slice includes two foundation modules, not just the three targets

Report and dashboard cannot move without the plan-resolution closure. Rather
than deferring them or injecting resolved paths through changed signatures, the
slice extracts:

- `lib/orchestrator/base.py` — `log`, `utcnow`, `PlanError`, and the status
  constants. Zero dependencies; everything else may import it.
- `lib/orchestrator/planref.py` — the ~250-line plan location and loading
  closure identified above.
- `lib/orchestrator/cost.py`, `report.py`, `dashboard.py` — the three targets.

Dependency direction is strictly `dashboard → report → planref → base` and
`cost → base`, with the entrypoint importing all of them. No cycles.

Total extracted is roughly 1,930 lines (~23%).

*Alternative considered:* extracting only cost, report, and dashboard and
having them import the entrypoint for plan resolution. Rejected — the
entrypoint imports them, so that is a direct cycle.

### D4: Doctor hashes the package, not just the entrypoint

`_check_stale_install` currently compares the installed executable against
`orchestrator/opsx-plan.py`. Once logic lives in installed modules, a matching
entrypoint no longer implies a current installation. The check compares the
installed `lib/orchestrator/` tree against the repository copy, and reports
stale when a module differs, is missing, or exists only in the installed copy.

### D5: A missing package produces a diagnostic, not a traceback

An operator whose installed runtime predates this change has `metrics`,
`pricing`, and `models` but no `orchestrator` package. The entrypoint's
existing sentinel check (`(runtime_root / "lib" / "metrics").is_dir()`) would
succeed and then fail at import with a bare `ModuleNotFoundError`. The
entrypoint wraps the orchestrator import the way it already wraps
`lib.models.resolver` — `sys.exit` with a message naming the package and
telling the operator to rerun a global installer.

## Risks / Trade-offs

- **A moved test patches the wrong module and silently passes** → D2 makes
  module-qualified calls the rule, so there is one canonical patch target per
  definition. Verify per moved test by asserting the mock was actually called,
  not merely that the surrounding assertion held.
- **`log` and `PlanError` move out from under 24 and 25 call sites
  respectively** → these are mechanical rewrites to `base.log` / `base.PlanError`,
  and any missed site is a `NameError` at import or first call, not a silent
  wrong result. The test suite covers all three commands.
- **Operators running an installed `opsx-plan` break until they reinstall** →
  D5 turns this into an actionable message and D4 makes `doctor` report it.
  Per AGENTS.md the maintainer reruns a global installer after merge anyway.
- **The refactor collides with concurrent work on `opsx-plan.py`** → the file
  took 44 commits in 30 days. Land this slice as its own change with nothing
  else in flight, and rerun the full suite immediately before merge.
- **Extraction quietly changes report or dashboard output** → capture
  `report --json` and `dashboard` output against fixed telemetry before the
  move and diff after; this is cheaper than trusting the assertions alone,
  since formatting helpers are where an accidental edit would hide.
- **The package makes `git grep` for a definition return two hits** during any
  window where a definition exists in both places → do each extraction as a
  move in one commit, never a copy-then-delete across commits.

## Migration Plan

1. Create `base.py` and `planref.py`, rewrite call sites in the entrypoint to
   module-qualified form, run the suite. This step alone touches the most call
   sites and should land verified before anything else moves.
2. Move `cost.py` (one dependency, one inbound call site).
3. Move `report.py`, then `dashboard.py` (dashboard depends on report).
4. Update `scripts/install-orchestrator.sh` and `_check_stale_install`.
5. Split the corresponding test classes into sibling test modules.

Rollback is a single revert; the installed runtime is restored by rerunning a
global installer, which is already the documented post-merge step.

## Open Questions

- Should `base.py` also absorb `git()` now? It has fan-in from 4 sections and
  is patched in 6 tests, but it is not needed by this slice. Deferring keeps
  the slice honest; taking it now would pay the retargeting cost once. Leaning
  defer, and revisit when the ground-truth verification section moves.
- Whether the moved test modules should share a fixture helper for
  `load_opsx_plan()`, or import the extracted modules directly. Direct import
  is faster and simpler for `cost`, `report`, and `dashboard`; tests that
  exercise CLI dispatch still need the entrypoint.
