# Proposal: add-action-journal-dispatch

## Why

The archived supervision changes built the journal primitive and its policies
but left the run engine only half wired: `supervised_gate_reserve` records
action intent (`begin_action`) and a bare dispatch row, yet nothing in the
engine ever completes, fails, marks uncertain, or records evidence for an
action; `model_policy.check_dispatch` has no call site; dispatch rows carry no
session or process identity; and the worker endpoint verbs (`record_evidence`,
`request_action`) are echo stubs. A supervised run that is interrupted
mid-stage today leaves a permanently `dispatched` action that nothing
reconciles — exactly the unbounded-blind-replay failure mode the journal
exists to prevent. This change integrates the existing `opsx-plan run` engine
and its inner create/implement/review/archive dispatch with the journal, the
model policy, budget reservation, and the worker sandbox as one gated
dispatch path, so every supervised action is intent-first, policy-gated,
identity-carrying, and explicitly reconciled before any replay.

## What Changes

- Wire the existing run engine's inner stage dispatch
  (create/implement/review/archive, including retries and
  `implementer_escalation`) through the full journal lifecycle for registered
  supervised jobs: intent committed before any side effect, a transactional
  dispatch record carrying action id, owning job, session identity, and
  process identity, then a terminal outcome (`completed` / `failed`) or an
  explicit `uncertain` mark with evidence. No new DAG or stage machine is
  introduced; the journal wraps the existing dispatch call sites.
- Gate every supervised dispatch, before side effects, on the model policy
  (`model_policy.check_dispatch` against the job policy's pinned role
  identity), the budget reservation, the broker's authority state, and the
  held execution lock — all four, in that order, at one dispatch boundary.
- Revalidate the immutable job plan and policy before each action: the
  manifest-snapshot hash and the policy operator revision recorded at
  registration are rechecked per action, and a changed plan or policy blocks
  dispatch with a named error rather than running against stale material.
- Mark an action explicitly `uncertain` whenever its outcome cannot be
  confirmed (worker loss, timeout after spawn, ambiguous kill, missing result
  evidence), record outcome and usage evidence against the action, and
  reconcile uncertain actions against evidence before they are completed,
  failed, or replayed.
- Implement engine-level replay that deduplicates and re-observes before
  re-dispatching: a prior uncertain attempt is never assumed to have had no
  effect, and no exactly-once external effect is claimed through a lease.
- Track both worker dispatch paths in the same journal: the subprocess path
  (`run_logged_command` process identity) and the native Task path inside the
  OpenCode worker (session identity reported through the worker endpoint), so
  `dispatches.session_id` / `process_id` are populated rather than NULL.
- Wire the worker actions endpoint verbs `record_evidence` and
  `request_action` to the ledger journal (replacing the echo stubs), keeping
  them inside the existing worker-domain authorization boundary.
- Preserve legacy behavior exactly: unregistered runs keep their current
  dispatch path with no journal, policy, or ledger dependency.

Out of scope (unchanged by this change): the session bridge transport
(`add-opencode-session-bridge`), incident repair policy
(`add-bounded-incident-recovery`), report/observability projections, the
acceptance stage, watchdog behavior, and any change to the journal ledger
schema or budget/broker/lock semantics established by the dependency
changes.

## Capabilities

### New Capabilities

(None.)

### Modified Capabilities

- `durable-plan-supervision`: adds the engine-integrated dispatch contract —
  full journal lifecycle wiring for every supervised stage dispatch, the
  four-part pre-dispatch gate (lock, broker authority, model policy, budget
  reservation), per-action revalidation of the immutable job plan and policy
  revision, session/process identity on dispatch records for both the
  subprocess and native Task paths, uncertain-action marking and evidence
  reconciliation in the engine, deduplicating re-observant replay, and the
  worker endpoint evidence verbs backed by the journal.
- `orchestrator-module-layout`: places the journal dispatch integration in a
  concern-named orchestrator runtime module that the entrypoint calls,
  following the established extraction discipline rather than growing inline
  entrypoint logic.

`plan-manifest-lifecycle` is intentionally not modified: this change consumes
the protected manifest snapshot and the existing phase-authority semantics
exactly as the broker change defined them; no manifest, derived-manifest, or
JSON-state requirement changes.

## Impact

- **Code:** a new concern-named `lib/orchestrator/` module for the journal
  dispatch integration (the gated dispatch boundary: policy check, budget
  reservation, journal lifecycle calls, identity capture, outcome and
  evidence recording, replay re-observation);
  `orchestrator/opsx-plan.py` (inner stage dispatch call sites —
  `_run_direct_change_loop_inner`, `dispatch_create_stage`, retry and
  escalation paths — routed through the integration module for registered
  jobs); `lib/supervisor/endpoints.py` (`record_evidence` / `request_action`
  handlers backed by the ledger); `lib/supervisor/ledger.py` (only additive
  accessors if the integration needs them — no schema change expected).
- **Docs:** `core/plan-supervision.md` gains the engine dispatch contract:
  the pre-dispatch gate order, per-action plan/policy revalidation,
  uncertainty and evidence reconciliation, replay re-observation, and the
  dual subprocess/Task identity tracking.
- **Tests:** new `tests/supervisor/test_action_journal.py` covering intent
  durability before a simulated side effect, inner-stage integration of the
  existing run engine, policy/model/budget gating before dispatch,
  uncertain-action reconciliation, duplicate deduplication, replay only after
  re-observation, plan/policy change blocking dispatch, and both dispatch
  paths journaled; existing supervision and orchestrator suites keep passing.
- **Dependencies:** builds on the archived `define-supervision-ledger-contract`,
  `register-supervised-model-roles`, `add-process-singleton-lock`,
  `add-supervision-budgets`, and `enforce-broker-mediated-approvals`. No new
  runtime dependencies; stdlib only.
- **Compatibility:** no breaking changes. Legacy unregistered runs dispatch
  exactly as before; the journaled path activates only for registered
  supervised jobs.
