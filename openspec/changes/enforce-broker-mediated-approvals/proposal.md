## Why

The authority boundary (`establish-operator-authority-boundary`) makes the
operator endpoint unreachable to worker processes, and the pause flag
(`add-human-only-pause-flag`) defines who may release a gate — but nothing yet
enforces either at runtime. Today `approve`, `accept`, `reset`, `run`,
`run-one`, and `opsx-run` still mutate JSON execution state directly, so any
process that can run the CLI in a supervised worktree can release a human-only
gate, reset away incidents, or edit the plan/JSON to drop supervised fields and
escape registration. A broker in the trusted authority domain must become the
sole approval authority for registered supervised jobs so human-only approval
is genuinely inaccessible to workers.

## What Changes

- Add a supervision broker in the trusted authority domain, scoped to
  registered supervised worktrees/jobs only. Within a registered job, ordinary
  `approve`, `approve --all`, `approve P<N>`, `accept`, `reset`, `run`,
  `run-one`, and `opsx-run` become broker mediated; unregistered legacy jobs
  keep their existing JSON handling with no backend dependency.
- Make the broker the sole approval authority for registered jobs: approval,
  acceptance, and pause/steer receipts are durable broker database
  transactions with a durable wake-up for the owning job — never dependent on
  the held worktree execution lock — and the JSON execution state becomes a
  projection of broker state rather than a competing phase authority.
- Enforce the `pause_before_human_only` resolution at runtime: human-only
  gates release only through the operator OS-authenticated path (peer
  credentials on the operator endpoint); an explicit delegated (`false`) gate
  may be released by a scoped job service action on the worker-actions
  endpoint.
- Bind every receipt to the exact checkpoint and material revision by hashing
  only the material gate inputs (the gate-relevant manifest fields from the
  protected snapshot plus the explicit policy revision), so unrelated updates
  do not invalidate an approval while stale material inputs never satisfy a
  gate. Plan and policy revisions are explicit operator acts.
- Anchor registration with a protected manifest snapshot plus the external
  registration record, so a worker that edits the JSON state or the repo plan
  to drop supervised fields cannot escape active registration.
- Revalidate the material gate inputs on resume before any dispatch.
- Allow bounded authorized resets (operator via the operator path; the
  supervised service within policy bounds) while refusing blanket
  worker-initiated resets in a registered job.
- Keep read-only diagnostics (`status`, `logs`, `report`, `doctor`) available
  without broker mediation.

## Capabilities

### New Capabilities

(none — this change builds on the proposed `durable-plan-supervision`
capability established by `define-supervision-ledger-contract`)

### Modified Capabilities

- `durable-plan-supervision`: the approval-authority requirement gains runtime
  broker enforcement (operator path vs. scoped job service action), and new
  requirements define broker sole-authority scope, receipt binding to
  checkpoint/material revision, the protected snapshot plus registration
  record, resume revalidation, and bounded resets.
- `plan-operator-cli`: gate and mutating commands in a registered supervised
  job are broker mediated; batch approval/acceptance behavior is scoped so
  registered jobs record receipts through the broker; worker-domain refusals
  use named errors; operator documentation covers the mediated behavior.
- `plan-manifest-lifecycle`: the JSON execution state remains authoritative
  for unregistered legacy jobs, but for a registered supervised job it becomes
  a projection of broker/ledger state rather than a competing phase authority.

## Impact

- **New code**: `lib/supervisor/broker.py` (mediation decisions, receipt
  recording, material-input hashing, resume revalidation); a ledger schema
  bump adding a durable receipts record (forward-only migration);
  `tests/supervisor/test_broker_approvals.py`.
- **Modified code**: `lib/supervisor/endpoints.py` (operator and scoped worker
  verbs perform durable broker transactions instead of validate-only
  handling); `lib/orchestrator/cmd_gates.py` (`approve`/`accept`/`reset`
  route through the broker when the worktree holds a registered job);
  `orchestrator/opsx-plan.py` and `lib/orchestrator/cmd_run_one.py`
  (`run`/`run-one`/`opsx-run` mediation and gate checks against broker
  receipts); gate classification consults broker receipts for registered jobs.
- **Docs**: `core/plan-supervision.md` (broker authority, receipt binding,
  snapshot/registration anchoring); operator workflow documentation for
  mediated approvals and named worker-refusal errors.
- **Compatibility**: unregistered legacy jobs are behavior-identical and need
  no backend; registered jobs change who can approve/reset/run (operator or
  policy-bound service only). No change to the acceptance stage, budgets,
  session bridge, or watchdog (later changes).
