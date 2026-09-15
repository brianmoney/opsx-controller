# Design: add-action-journal-dispatch

## Context

See `proposal.md` — Why for motivation. The current state that shapes this
design:

- `lib/supervisor/ledger.py` (schema head v5) already implements the full
  journal primitive: `begin_action`, `dispatch_action`, `mark_uncertain`,
  `record_evidence` (append-only), `reconcile_action` (explicit),
  `complete_action`, `fail_action`,
  `replay_action`, with `JOURNAL_STATES = (intent, dispatched, uncertain,
  reconciled, completed, failed)` and `JournalStateError` enforcing legal
  transitions. The `dispatches` table has nullable `session_id` and
  `process_id` columns that are never populated.
- The engine half-wires the journal in `orchestrator/opsx-plan.py`:
  `supervised_gate_reserve` calls `ledger.begin_action(...)`, then
  `budget_mod.reserve(...)` (action-linked), then `ledger.dispatch_action(...)`
  as the last step before the caller spawns the worker. Nothing calls
  `complete_action` / `fail_action` / `mark_uncertain` / `record_evidence` /
  `replay_action`, so every supervised action is permanently `dispatched`.
- `lib/supervisor/model_policy.py::check_dispatch` (pure pre-dispatch
  decision) and `validate_dispatch_identity` have no call sites.
- `lib/supervisor/endpoints.py` worker handlers (`record_evidence`,
  `request_action`, `report_status`, `heartbeat`) are echo stubs that return
  the request; they have no ledger access. The service host
  (`lib/orchestrator/supervision.py::open_service_host`) binds the endpoints.
- All stage workers are subprocesses: `run_logged_command` uses
  `subprocess.Popen(..., start_new_session=True)`; `terminate_group` kills the
  process group; `_current_proc` holds the live handle for SIGINT. The create
  stage has a dedicated `dispatch_create_stage`; implement/review/archive go
  through `invoke_direct_stage`. Retries re-enter the same call sites.
- Broker revalidation exists as `broker.assert_resume_clear` (run start) and
  `broker.revalidate_receipts`; the registration record carries the protected
  manifest-snapshot hash and the policy operator revision.
- `lib/supervisor/lock.py` provides process identity helpers
  (`process_start_time`, `boot_identity`) used for fencing.

## Goals / Non-Goals

**Goals:**

- One dispatch boundary — a concern-named `lib/orchestrator/` module — that
  every supervised stage dispatch flows through: gate evaluation, journal
  lifecycle, identity capture, outcome/evidence recording, and replay
  re-observation, per the spec deltas.
- All four inner stages (create/implement/review/archive), retries, and
  `implementer_escalation` journaled end to end with no new DAG.
- Uncertainty as a first-class engine state: unconfirmed outcomes become
  `uncertain`, block silent progress, and reconcile from evidence; replay
  dedups and re-observes.
- Worker endpoint verbs `record_evidence` / `request_action` backed by the
  ledger inside the existing worker-domain authorization boundary.
- Legacy unregistered runs byte-identical; the journaled path activates only
  for registered jobs.

**Non-Goals:**

- No ledger schema migration: the journal tables and `dispatches` identity
  columns already exist. The integration adds the ledger accessors it needs
  and makes `record_evidence` append-only with an explicit `reconcile_action`
  transition (a semantic, not schema, change required by D4); no table or
  column changes.
- The session bridge transport, incident repair policy, acceptance stage,
  watchdog, and report projections (later changes in the plan).
- Changing budget, broker, lock, or model-policy semantics — this change is
  their consumer, not their reviser.
- Native Task interception inside the worker's own process: Task identity is
  reported by the worker through the endpoint, not intercepted by the
  orchestrator.

## Decisions

### D1. One concern-named integration module owns the dispatch boundary

Add `lib/orchestrator/journal_dispatch.py` exposing the dispatch-boundary
operations the entrypoint calls: `gated_dispatch(...)` (gate → intent →
reserve → dispatch record), `resolve_dispatch(...)` (outcome →
evidence → complete/fail/uncertain), `reconcile_pending(job)` (uncertain
inventory + evidence reconciliation), and `replay_uncertain(...)` (dedup →
re-observe → replay). The entrypoint's stage call sites delegate to it when a
registration exists.

*Alternatives considered:* (a) grow `lib/orchestrator/supervision.py` — it
owns registration/broker glue and endpoint hosting, a different concern, and
is already ~1000 lines; (b) keep wiring inline in the entrypoint — the
status quo, which produced the half-wiring and violates the module
discipline this repo enforces per change; (c) put it in `lib/supervisor/` —
rejected because the integration must import `lib.orchestrator` machinery
(pricing, telemetry, stage invocation), which the supervisor package's
documented acyclic discipline forbids.

### D2. Wrap the existing call sites; never fork the run loop

For registered jobs, the boundary wraps the existing dispatch call sites
(`invoke_direct_stage` for implement/review/archive and retries,
`dispatch_create_stage` for create) rather than branching a supervised
variant of the run loop. The supervised gate object the run loop already
threads through (`open_supervised_gate`) carries the registration, ledger,
and policy; the boundary consumes it. Unregistered runs never enter the
module.

*Alternatives considered:* a separate supervised runner — rejected by the
plan ("rather than introducing a new DAG") and because it would double every
future run-loop fix.

### D3. Fixed gate order at the boundary: lock → authority → plan/policy freshness → model policy → reserve → dispatch record

Every supervised dispatch evaluates, in order: (1) the execution lock is held
by this process; (2) broker authority state permits dispatch
(`assert_resume_clear` rechecked per action, not just at run start); (3) the
manifest-snapshot hash and policy operator revision still match the
registration anchors — a mismatch blocks with a named stale-material error;
(4) `model_policy.check_dispatch` passes for the stage's role against the
pinned identity; (5) budget reservation succeeds (existing
`supervised_gate_reserve` mechanics: intent → reserve → dispatch record).
Cheap pure checks run before durable writes; the dispatch record stays the
last step before spawn, preserving the invariant that an undispatched action
never accrues execution time. Each failure names its gate.

*Alternatives considered:* folding plan/policy freshness into the run-start
`assert_resume_clear` only — rejected: a long run must not keep dispatching
against a plan or policy the operator changed mid-run. Hashing only the
material gate inputs (the broker's existing discipline) keeps unrelated file
updates from invalidating.

### D4. Outcome resolution is a structured step keyed on evidence, not exit-code guessing

After the worker returns, is killed, or disappears, the boundary resolves the
action: confirmed success → record result + usage evidence →
`complete_action`; confirmed failure → evidence → `fail_action`; unconfirmed
(spawn lost, timeout after spawn, ambiguous kill, missing result evidence) →
`mark_uncertain` plus whatever evidence exists. The timeout/SIGINT/kill paths
(`terminate_group`, `handle_sigint`) route through the same resolution so an
interrupted dispatch can never linger in `dispatched`. Evidence kinds are a
fixed vocabulary in the module (`stage_result`, `usage`, `spawn_loss`,
`session_binding`, ...). `record_evidence` is append-only; after decisive
evidence is classified, the caller explicitly invokes `reconcile_action`
before applying the terminal transition.

### D5. Replay dedups against recorded identity, then re-observes external state

`replay_uncertain` first deduplicates: if evidence or a delivered result
already resolves the action, reconcile instead of replaying (no double bill,
no double apply). Otherwise it re-observes the external state the stage could
have affected — the JSON execution state, stage artifacts, and telemetry for
the action's dispatch interval — and replays only when observation shows the
work incomplete. Before replaying an action whose worker may still exist, the
boundary fences the recorded process identity (start-time + boot-identity
comparison from `lock.py`, the same discipline as stale-owner fencing) so a
recycled PID cannot cause a double spawn.

Replay is **supersede-and-redispatch**, not a re-dispatch of the same action
row. Once re-observation shows the prior attempt incomplete, the boundary
records an inconclusive `stage_result` evidence entry, explicitly reconciles
the uncertain action, fails it as superseded, and opens a **fresh** journaled
action through `gated_dispatch`. This keeps `completed`/`failed` terminal (a
terminal action is never re-dispatched), re-runs the full gate and reservation
boundary for the replacement attempt, and leaves the superseded action's
retained reservation (retained by `reconcile_pending`) as retained
consumption, so the replacement is reserved under its own action and no
attempt is double-billed. The lower-level `ledger.replay_action` primitive is
deliberately not used here: it re-dispatches the same action row, which would
bypass that fresh gate/reservation pass. The journal never claims exactly-once
effects; re-observation plus deduplication is the mechanism.

### D6. Identity capture per dispatch path

Subprocess path: at dispatch, the boundary records the spawned process
identity (pid + start time + boot identity) into `dispatches.process_id`
(serialized, since the column is text). Native Task path: the worker reports
its Task's session identity through the endpoint; the boundary binds it to
the action's dispatch row (`session_id`) via an additive ledger accessor, and
records a `session_binding` evidence entry so the binding itself is
journaled. Both paths share gates, lifecycle, and journal.

### D7. Endpoint verbs gain ledger context through the service host

`open_service_host` already constructs the endpoint server; it now passes a
ledger/registration context into the worker handlers. `_worker_record_evidence`
validates the caller's worker-domain credentials, resolves the referenced
action, refuses actions not owned by the bound job with a named authorization
error, and persists through `ledger.record_evidence`. `_worker_request_action`
answers from journaled job state (the job's current actionable items:
uncertain actions, delegated-gate releases, steering receipts) rather than
echoing. `report_status` / `heartbeat` stay echo-level; giving them semantics
is the lifecycle change's job.

## Risks / Trade-offs

- [Double spawn when replaying an action whose worker secretly survived] →
  Fence the recorded process identity before replay (D5); a live prior worker
  blocks replay instead of racing it.
- [Evidence-kind vocabulary sprawl making reconciliation ambiguous] → Fixed
  kind vocabulary defined in the integration module (D4); unknown kinds are
  stored but never decisive for reconciliation.
- [Worker-reported Task identity is cooperative, not intercepted] → Accepted:
  the trust model already bounds a worker to its own job; a spoofed or
  escalated worker is surfaced as a blocked policy violation by the
  agent-contracts change, not silently trusted here.
- [Per-action freshness checks add ledger reads to every dispatch] → Cheap
  local SQLite reads; correctness (never dispatch against stale material)
  outweighs the negligible cost.
- [Legacy drift between the wrapped and unwrapped paths] → The legacy path is
  untouched code; wiring tests assert unregistered runs create no journal
  records, and the existing gate-wiring tests keep passing.

## Migration Plan

No schema or data migration: the journal tables and identity columns exist at
schema head v5. Deployment is code-only; the journaled path activates only
for registered supervised jobs on reinstall. Rollback is a revert — a
half-wired journal (intents/dispatches without outcomes) remains valid
ledger data that this change's reconciliation can later resolve, so rolling
forward again is safe. Actions left `dispatched` by the pre-change code are
treated as unconfirmed on first resume after deploy: they surface as
uncertain and reconcile or replay through the new boundary.

## Open Questions

- The full `request_action` payload shape for steering and lifecycle waits is
  defined by `add-supervised-plan-lifecycle`; this change wires the verb to
  journaled state with the minimal actionable-items response and does not
  freeze the richer shape.
