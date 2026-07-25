---
description: Start or resume the OpenSpec controller for one change
agent: opsx-controller
subtask: false
---

**Deprecated.** This nested-controller path is superseded by direct dispatch.
Use `opsx-run <change-id>` (equivalently `opsx-plan run-one <change-id>`)
instead — it drives the same implement/review/archive loop with the same
gates and requires no manifest. This command remains functional during the
deprecation period but will be removed in a later change.

Start or resume the OpenSpec controller for exactly one change.

Resolved controller inputs:
- Requested change id: `$1`
- Unexpected second positional argument: `$2`
- State file: `.opencode/opsx-controller/$1.json`

Controller entry rules:
- If `$1` is empty, stop and tell the operator to run `/opsx-drive <change-id>`.
- If `$2` is non-empty, fail closed and explain that the controller supports
  only one change per run.
- Use `$1` as the only change identifier.
- Start or resume `.opencode/opsx-controller/$1.json`.
- Use the `opsx-controller` agent workflow and do not fall back to
  `openspec-loop.sh` for the normal apply-review-archive loop.
- `opsx-plan` no longer invokes this command internally for OpenCode-backed
  plan runs; this command remains the manual single-change controller surface.
- The controller result is final for this command run.
- Do not run follow-up recovery, reconciliation, retries, repo edits, or extra
  tool calls in the command layer after the controller responds.
