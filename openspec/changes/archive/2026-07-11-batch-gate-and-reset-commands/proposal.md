## Why

Supervising a multi-change plan currently requires one `approve`, `accept`, or `reset` command per blocked change. That adds avoidable operator friction at exactly the moments where several changes may be waiting on the same human action.

Phase 3 of the operator workflow upgrades plan calls for batch gate-clearing commands and clearer `status` guidance so operators can unblock an entire gate class with one command and see the exact next step for each blocked change.

## What Changes

- Add `opsx-plan approve --all` to approve every change currently awaiting approval.
- Add `opsx-plan accept --all` to accept every change currently awaiting acceptance.
- Add `opsx-plan reset --failed` to reset every failed change back to pending.
- Require each batch command to print exactly which change IDs it affected, including empty-set cases where nothing matched.
- Extend `opsx-plan status` so every blocked change prints the exact next command needed to unblock it, using the active-plan short form when the command can omit an explicit plan path.
- Preserve existing single-change command forms and existing gate semantics.
- Add unit tests for empty, partial, and full batch target sets plus status guidance output.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `plan-operator-cli`: Adds batch approval, acceptance, and failed-reset commands plus actionable next-command status output for blocked changes.

## Impact

- Affected specs: `openspec/specs/plan-operator-cli/spec.md`.
- Modified files later: `orchestrator/opsx-plan.py` command parsing, batch state-transition helpers, and status rendering.
- Runtime behavior later: operators can clear each gate class with one command and can copy the next-step command directly from `status` output.
- Test coverage later: orchestrator unit tests for batch commands on empty, partial, and full matching sets and for blocked-change status guidance.
- No automatic gate clearing, no change to single-change commands, and no change to approval or acceptance semantics.
