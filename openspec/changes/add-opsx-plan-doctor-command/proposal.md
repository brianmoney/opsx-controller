## Why

Operators currently learn about several known `opsx-plan` environment problems only after a run is already underway. Stale installed orchestrator copies, missing required model environment variables, missing CLI dependencies, tracked bytecode artifacts, and dirty tracked files are all predictable failure modes that should be surfaced before work is dispatched.

Phase 1 of the operator workflow upgrades plan calls for a `doctor` preflight that uses the active-plan resolution flow introduced by `add-active-plan-resolution` and reuses the same checks as non-fatal warnings when `opsx-plan run` starts.

## What Changes

- Add an `opsx-plan doctor [plan]` command to the `plan-operator-cli` capability.
- Run deterministic preflight checks for: installed orchestrator freshness versus the repo copy, required `OPSX_*_MODEL` environment variables, `openspec` availability, configured adapter client availability, tracked `__pycache__` or `.pyc` entries, and tracked-tree cleanliness.
- When a plan is resolved from an explicit argument or the active-plan flow, validate that the plan loads and check plan-conditional requirements such as `gh` availability and a git remote when pull-request delivery is enabled.
- Emit one pass/fail line per check with a remediation hint and exit non-zero when any `doctor` check fails.
- Reuse the same checks at `opsx-plan run` start as warnings only, without changing run control-flow outcomes.
- Add unit tests with fabricated environments for each failure class and for run-start warning behavior.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `plan-operator-cli`: Adds an operator-facing `doctor` preflight command and run-start warning checks for known environment failure modes.

## Impact

- Affected specs: `openspec/specs/plan-operator-cli/spec.md`.
- Modified files later: `orchestrator/opsx-plan.py` command parsing, preflight check helpers, run-start warning output, and plan-conditional dependency checks.
- Runtime behavior later: `opsx-plan doctor` can fail fast before any stage dispatch, while `opsx-plan run` surfaces the same issues as warnings only.
- Test coverage later: orchestrator unit tests for stale install detection, missing env vars, missing CLIs, tracked bytecode findings, dirty tracked tree findings, plan-load failures, plan-conditional git delivery requirements, and warning-only reuse during `run`.
- No auto-remediation, no installer execution, no network reachability probes, and no validation of model identifiers against providers.
