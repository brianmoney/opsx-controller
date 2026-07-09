## Why

Operators currently have to pass the plan TOML path to every `opsx-plan` command. That makes normal plan supervision noisy and error-prone, especially across repeated `run`, `status`, `approve`, `accept`, `reset`, `report`, and `dashboard` invocations. A repo-local active-plan pointer lets operators select a plan once while keeping explicit path usage unchanged.

This change introduces the first requirement set for the proposed `plan-operator-cli` capability: deterministic plan identity resolution for operator-facing commands.

## What Changes

- Add an `opsx-plan use <plan.toml>` command that validates and records the active plan as a repo-relative TOML path under `.opsx-plan/`.
- Make the plan positional optional for `run`, `status`, `approve`, `accept`, `reset`, `report`, and `dashboard`.
- Resolve omitted plan arguments in this order: explicit CLI argument, `OPSX_PLAN` environment variable, active-plan pointer file, then a clear error naming `opsx-plan use <plan.toml>`.
- Auto-activate the output plan after successful `opsx-plan compile -o <plan.toml>` and after any `opsx-plan run <plan.toml>` invocation with an explicit plan path.
- Show the active plan in `status` output.
- Fail closed when the pointer file references a missing plan, preserving the recorded path in the error instead of guessing or auto-discovering another plan.
- Add unit tests for resolution precedence, activation paths, status rendering, stale pointer handling, and unchanged explicit-path behavior.

## Capabilities

### New Capabilities

- `plan-operator-cli`: Defines operator-facing `opsx-plan` ergonomics for selecting and reusing an active plan across supervision commands.

### Modified Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-operator-cli/spec.md` (new capability).
- Modified files: `orchestrator/opsx-plan.py` CLI parsing and plan-loading call sites for the listed subcommands.
- Runtime state: new active-plan pointer file under `.opsx-plan/` containing a repo-relative TOML path.
- Test coverage: orchestrator unit tests for plan resolution, activation, stale pointer errors, and status output.
- No changes to plan execution semantics, change scheduling, worker dispatch, archive verification, telemetry aggregation, or report/dashboard data generation.
