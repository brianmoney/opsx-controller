---
description: Start or resume the OpenSpec controller for one change
agent: opsx-controller
subtask: false
---

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
- The controller result is final for this command run.
- Do not run follow-up recovery, reconciliation, retries, repo edits, or extra
  tool calls in the command layer after the controller responds.
