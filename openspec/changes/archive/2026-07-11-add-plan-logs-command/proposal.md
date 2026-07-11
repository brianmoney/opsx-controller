## Why

Operators currently have to inspect `.opsx-plan/logs/` by hand to answer basic supervision questions such as which stage is running, which log is most recent, or where a specific change's review log lives. That makes active-plan supervision slower and more error-prone than it needs to be.

Phase 3 of the operator workflow upgrades plan calls for an `opsx-plan logs` command that resolves the active or explicit plan, surfaces the most relevant stage log by default, supports deterministic filtering, and can follow an in-progress run without requiring manual globbing.

## What Changes

- Add an `opsx-plan logs [plan]` command to the `plan-operator-cli` capability.
- Make the default command print the most recent stage log path for the resolved plan and a tail of that log.
- Support deterministic log selection by change id and stage.
- Support a listing mode that enumerates available logs for the resolved plan.
- Support a follow mode for an in-progress run.
- Resolve the target log from recorded state metadata first and fall back to `.opsx-plan/logs/` ordering when state does not identify a usable log.
- Emit a clear message when no matching log exists instead of printing an empty tail.
- Add unit tests for latest-log selection, change/stage filtering, listing, follow-mode selection, and missing-log handling.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `plan-operator-cli`: Adds operator-facing log discovery and tailing for plan-backed stage logs.

## Impact

- Affected specs: `openspec/specs/plan-operator-cli/spec.md`.
- Modified files later: `orchestrator/opsx-plan.py` command parsing, state-backed log selection helpers, log-directory fallback logic, and tail/follow output.
- Runtime behavior later: operators can inspect the latest or filtered stage log for a resolved plan with one command instead of browsing `.opsx-plan/logs/` manually.
- Test coverage later: orchestrator unit tests for latest-log resolution from state and fallback ordering, deterministic filters, list output, follow-mode target selection, and clear missing-log errors.
- No log rotation, no structured parsing, no colorized renderer, and no long-running monitoring UI.
