---
description: Show the status of the active opsx-plan and the next operator actions
agent: plan
---

Report the current status of the repository's active `opsx-plan` plan.

This command takes no plan-specific argument. Do not hard-code, infer, or
substitute a plan name or path. Run exactly:

```bash
opsx-plan status
```

The command is read-only. Do not run, approve, accept, reset, or otherwise
modify the plan, its changes, gates, state, or repository files. Do not follow
any suggested command from the status output.

Report concisely:

- The active plan identified by the command.
- Every change's phase and current state, one line per change.
- Any blocked, failed, interrupted, or gated change and its reason.
- The exact next operator command shown by the status output for each change
  requiring action, such as `opsx-plan approve <change-id>` or
  `opsx-plan accept <change-id>`.

If no active plan is configured, say so plainly and report the command the
operator can use to select one: `opsx-plan use <plan.toml>`.
