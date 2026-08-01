## 1. Capture the behavior baseline

- [ ] 1.1 Record the current full-suite result (`python3 -m unittest discover
  -t . -s tests` from the repository root) as the count that must not regress,
  and confirm `node tests/opencode/test-opsx-usage-emitter.js` passes
- [ ] 1.2 Build a fixed fixture under the scratchpad holding a plan state file
  and a telemetry directory with records spanning at least two changes, two
  stages, and one unresolved-cost record
- [ ] 1.3 Capture `opsx-plan report --json` and `opsx-plan dashboard` output
  against that fixture from the pre-extraction entrypoint, and store both as
  the golden comparison for task 6.1

## 2. Create the package and the base module

- [ ] 2.1 Create `lib/orchestrator/__init__.py` with no import side effects, so
  importing a submodule neither parses arguments nor touches `.opsx-plan/`
- [ ] 2.2 Move `log`, `utcnow`, `PlanError`, and the `DONE`/`PENDING`/
  `RUNNING`/`FAILED`/`SKIPPED` status constants from
  `orchestrator/opsx-plan.py` into `lib/orchestrator/base.py`, leaving that
  module with no dependency on any other orchestrator module
- [ ] 2.3 Import the module (`from lib.orchestrator import base`) in the
  entrypoint and rewrite all call sites to module-qualified form
  (`base.log(...)`, `base.PlanError`), per design decision D2 — do not import
  the names directly
- [ ] 2.4 Retarget the tests that patch `log` (5 sites) to
  `lib.orchestrator.base.log`, and assert in each that the mock was actually
  invoked so a mis-targeted patch fails loudly
- [ ] 2.5 Run the full suite and confirm the count from 1.1 with no failures

## 3. Extract the plan-resolution closure

- [ ] 3.1 Move the 13-definition closure into `lib/orchestrator/planref.py`:
  `load_plan`, `resolve_plan`, `_resolve_plan_path`,
  `single_change_manifest_path`, `read_active_plan`, `active_plan_pointer_path`,
  `topo_sort`, `is_direct_mode`, `_parse_escalation_threshold`,
  `_parse_finding_recurrence_limit`, and `_parse_git_delivery_config`
- [ ] 3.2 Point `planref` at `base` for `log` and `PlanError`, and confirm it
  imports nothing else from the orchestrator package
- [ ] 3.3 Rewrite the entrypoint's call sites to module-qualified form
  (`planref.load_plan(...)`) and retarget the tests that patch these names
- [ ] 3.4 Run the full suite and confirm the count from 1.1 with no failures

## 4. Extract cost, report, and dashboard

- [ ] 4.1 Move the cost estimation section (lines 2218–2447: `_get_catalog`,
  `_build_price_snapshot`, `_compute_per_token_cost`,
  `_compute_subscription_cost`, `estimate_stage_cost`, the `_cost_catalog`
  module global, and the `PERMISSION_REJECTION_MARKERS`-adjacent constants that
  belong to it) into `lib/orchestrator/cost.py`
- [ ] 4.2 Keep the lazy `lib.pricing` initialization working from the new
  module, including the `_cost_catalog` failure sentinel and the cold-start
  behavior the existing pricing tests rely on
- [ ] 4.3 Update the single telemetry call site to `cost.estimate_stage_cost`
  and run the full suite
- [ ] 4.4 Move the report section (lines 6187–6680) into
  `lib/orchestrator/report.py`, including `_resolve_for_change_plan`, the
  `_fmt_*` and `_col_widths` helpers, the `_print_*` renderers, and `cmd_report`
- [ ] 4.5 Move the dashboard section (lines 6681–7640) into
  `lib/orchestrator/dashboard.py`, including `_DASHBOARD_CSS`, `_html_escape`,
  the `_render_*` helpers, and `cmd_dashboard`; have it reach `report` for
  `_resolve_for_change_plan`
- [ ] 4.6 Wire `cmd_report` and `cmd_dashboard` into the entrypoint's argparse
  dispatch through module-qualified references, leaving subcommand names,
  flags, defaults, and exit codes unchanged
- [ ] 4.7 Verify no module imports a module that transitively imports it, and
  that the direction is `dashboard → report → planref → base` and
  `cost → base`

## 5. Update installation and staleness detection

- [ ] 5.1 Extend `scripts/install-orchestrator.sh` to deploy `lib/orchestrator`
  to `~/.local/lib/opsx-controller/lib` alongside `metrics`, `pricing`, and
  `models`, replacing the managed copy on repeated installs so a module deleted
  from the repository does not persist in the installed tree
- [ ] 5.2 Extend `_check_stale_install` to compare the installed
  `lib/orchestrator` tree against the repository copy, reporting stale when a
  module differs, is missing, or exists only in the installed copy
- [ ] 5.3 Make the entrypoint's orchestrator import fail with a `sys.exit`
  message naming the missing package and directing the operator to rerun a
  global installer, matching how `lib.models.resolver` is already guarded —
  no unhandled `ModuleNotFoundError`
- [ ] 5.4 Add installer tests covering the deployed package, the stale-module
  case, and the missing-package case
- [ ] 5.5 Verify a global install from a clean `HOME` produces a runtime that
  runs `report` and `dashboard` without importing from the repository checkout

## 6. Verify and split the tests

- [ ] 6.1 Re-run the task 1.3 capture against the extracted build and confirm
  the report JSON and dashboard HTML are byte-identical to the golden output
- [ ] 6.2 Move the cost, report, and dashboard test classes out of
  `tests/orchestrator/test_opsx_plan.py` into `test_cost.py`,
  `test_report.py`, and `test_dashboard.py`, changing only import targets and
  patch targets while keeping every assertion
- [ ] 6.3 Confirm the new test modules are discovered from the repository root
  and that the total count is at least the task 1.1 baseline
- [ ] 6.4 Run the full suite plus `node tests/opencode/test-opsx-usage-emitter.js`
  and confirm both are green
- [ ] 6.5 Record the resulting `orchestrator/opsx-plan.py` line count and
  confirm the extraction removed roughly 1,900 lines

## 7. Documentation

- [ ] 7.1 Update `orchestrator/README.md` and `AGENTS.md` to describe the
  `lib/orchestrator/` layout and state that the entrypoint is no longer
  self-contained
- [ ] 7.2 Note in `AGENTS.md` that the post-merge installer rerun is now
  required rather than merely recommended, since a stale runtime is missing
  modules and not just out of date
- [ ] 7.3 Run `openspec validate extract-orchestrator-reporting-modules
  --strict`
