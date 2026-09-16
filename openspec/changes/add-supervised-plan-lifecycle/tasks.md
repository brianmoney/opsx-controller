# Tasks: add-supervised-plan-lifecycle

## 1. Lifecycle core module and ledger migration

- [x] 1.1 Create `lib/supervisor/lifecycle.py` (stdlib-only, import-cycle
  clean): the single job state machine (`registered → active → (paused →
  active)* → completed | failed | cancelled`) with explicit transition
  guards and the named error family `LifecycleError` → `UnknownJobError`,
  `TerminalJobError`, `IllegalTransitionError`; transition functions for
  register, start, resume, pause, drain, and cancel that both the CLI
  handlers and the endpoint verbs call
- [x] 1.2 Add a forward-only ledger migration in `lib/supervisor/ledger.py`,
  chained on the schema head at merge time (currently v5 → v6): a `waits`
  table (job id, checkpoint, material revision, wait kind, started/ended
  timestamps) and `drain` added to `RECEIPT_KINDS` with the receipts `CHECK`
  constraint rebuilt in the same transaction; older ledgers migrate, newer
  fail with the named version error
- [x] 1.3 Implement registration assembly: validate the isolation backend
  fail-closed via the existing authority path, then record in one durable
  transaction the job (repo root, worktree, owner identity), the protected
  job policy at operator revision 1 (authority configuration as standing
  permissions, frozen model selection and allowlist, budgets and deadlines),
  the protected manifest snapshot with its hash captured from the canonical
  plan manifest, and the primary session linkage configuration; no
  registration state is written to the worktree or JSON execution state

## 2. Operator endpoint verbs

- [x] 2.1 Replace the stub `_operator_cancel` in
  `lib/supervisor/endpoints.py` with the real terminal transition through
  the lifecycle module, and add `pause`, `drain`, and `resume` operator
  verbs, keeping the operator/worker handler tables disjoint
- [x] 2.2 Wire the `pause` and `drain` verbs to durable broker stop-request
  receipts with the existing wake-up, and `resume` to the broker's resume
  revalidation, so mediated calls and CLI bootstrap calls share one
  implementation and one transition table

## 3. CLI commands

- [x] 3.1 Add the `register`, `start`, `inspect`, `resume`, `pause`,
  `drain`, and `cancel` subcommand parsers to the `supervise` block of
  `build_parser()` in `orchestrator/opsx-plan.py`, dispatching to new
  `cmd_supervise_*` handlers in `lib/orchestrator/cmd_supervise.py`
- [ ] 3.2 Implement the handlers: `register` through the trust-root
  registration path; `start` / `resume` bringing up
  `lib/orchestrator/supervision.py`'s service host and primary session and
  driving the existing supervised run-engine path (no new DAG); `pause` /
  `drain` / `cancel` mediated through `call_operator` for a live job and
  failing closed with the named broker-unavailable error when the authority
  path is unreachable; named unknown-job, illegal-transition, terminal-job,
  and unsupported-host errors with non-zero exits
- [x] 3.3 Implement `inspect` as a read-only projection of the job — state,
  recorded waits, policy revision, budget posture, recent actions and
  incidents — requiring neither the execution lock nor a live service, and
  failing with the named unknown-job error when no registered job exists

## 4. Human-wait and stop-boundary integration

- [x] 4.1 Record a durable human wait (awaiting checkpoint, material
  revision, wait start) when the supervised run path reaches a human-only
  gate; the job dispatches no model action and runs no stall recovery while
  the wait lasts, retains ownership without the execution lock, and wakes
  only through the durable receipt scan, resuming with receipt revalidation
- [x] 4.2 Implement stop-request observance at the journal dispatch
  boundary: `pause` interrupts in-flight actions (marked `uncertain` for
  evidence reconciliation) and enters `paused`; `drain` forbids new dispatch
  while in-flight actions reach terminal outcomes, then enters `paused`; a
  restarted execution observes a durable stop request before dispatching any
  new action

## 5. Completion verification and manual checklist

- [x] 5.1 Implement supervised completion: the job reaches `completed` only
  when every enabled change classifies done from `verify_direct_archive_done`
  archive evidence with `groundtruth.run_fast_checks` and post-archive
  cleanliness passing — never from a worker or primary claim
- [ ] 5.2 Implement fresh-review revalidation: a failed post-archive fast
  check or an unverified/partial archive reruns an appropriate fresh review
  for that change through the existing implement/review/archive loop,
  bounded by the change's existing round budget, rather than treating a
  prior archive as proof of done
- [x] 5.3 Attach `state.pending_manual_tasks` to the completion record and
  the `inspect`/report surfaces as the operator checklist; pending `(manual)`
  tasks never mark the change, job, or run incomplete or failed, and the
  implement/review/archive task-completeness gates are unchanged

## 6. Documentation

- [x] 6.1 Extend `core/plan-supervision.md` with the lifecycle contract: the
  registration record, the state machine and transition guards, the
  pause-versus-drain stop boundaries, cancellation effects, the durable
  human wait with no polling, and evidence-based completion with fresh
  review on revalidation
- [x] 6.2 Extend `docs/opsx-plan-operator-workflow.md`'s `opsx-plan
  supervise` section with the lifecycle command reference: each command,
  its named errors, the stop boundaries, cancellation, human waits,
  completion evidence semantics, and the `(manual)` checklist

## 7. Tests

- [ ] 7.1 Create `tests/supervisor/test_supervision_lifecycle.py` asserting:
  each lifecycle command end to end (register, start, inspect, resume,
  pause, drain, cancel); the persisted registration fields (snapshot and
  hash, repo/worktree, standing permissions, frozen allowlist and budgets at
  revision 1, linkage configuration); pause and drain stop-boundary
  dispositions, including stop-request survival across a restart and no
  execution-lock requirement; cancellation effects (terminal state,
  in-flight disposition, refused later requests, legal re-registration);
  human-wait persistence across a service restart with no model dispatch and
  no stall recovery during the wait; completion only from
  plan/archive/fast-check evidence (a worker claim does not complete) with
  fresh review where revalidation is required; and `(manual)` tasks reported
  as the operator checklist without false incompleteness
- [x] 7.2 Add migration tests: a ledger at the prior schema head migrates
  forward with jobs, receipts, and policies preserved, the `drain` receipt
  kind is accepted post-migration, and a newer-than-code ledger is refused
  with the named version error
- [x] 7.3 Verify `lib/supervisor/` imports stay acyclic: `python3 -m
  unittest tests.supervisor.test_module_layout`

## 8. Validation

- [x] 8.1 Run `python3 -m unittest discover -t . -s tests` from the
  repository root — all tests pass
- [x] 8.2 Run `node tests/opencode/test-opsx-usage-emitter.js` — passes
- [x] 8.3 Run `openspec validate add-supervised-plan-lifecycle --strict` —
  passes
