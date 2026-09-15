# Tasks: add-action-journal-dispatch

## 1. Journal dispatch integration module

- [x] 1.1 Create `lib/orchestrator/journal_dispatch.py` (honoring the
  orchestrator module discipline: importable without side effects, no new
  entrypoint surface) with the fixed evidence-kind vocabulary
  (`stage_result`, `usage`, `spawn_loss`, `session_binding`) and the named
  gate-failure error family identifying lock, authority, model-policy,
  budget, and stale-material blocks
- [x] 1.2 Implement `gated_dispatch(...)`: evaluate the gate in the fixed
  order — execution lock held, broker authority revalidated per action
  (`assert_resume_clear`), plan/policy freshness against the registration
  anchors (manifest-snapshot hash and policy operator revision, hashing only
  material gate inputs), `model_policy.check_dispatch` for the stage role —
  then run the existing intent → reserve → dispatch-record mechanics, each
  failure raising its named gate error before any side effect
- [x] 1.3 Implement `resolve_dispatch(...)`: confirmed success → record
  `stage_result` + `usage` evidence → `complete_action`; confirmed failure →
  evidence → `fail_action`; unconfirmed outcome (spawn loss, timeout after
  spawn, ambiguous kill, missing result evidence) → `mark_uncertain` plus
  available evidence; when decisive evidence later lands, classify it and
  explicitly call `reconcile_action` before the terminal transition
- [x] 1.4 Implement `reconcile_pending(job)`: inventory a job's unreconciled
  uncertain actions and expose them as blocking state so a run never silently
  progresses past one
- [x] 1.5 Implement `replay_uncertain(...)`: deduplicate against recorded
  dispatch identity and delivered results (reconcile instead of replaying a
  resolved action), re-observe external state (JSON execution state, stage
  artifacts, telemetry for the dispatch interval), fence the recorded process
  identity against a live prior worker, and only then supersede the prior
  action (reconcile → fail as superseded) and redispatch a fresh journaled
  action through `gated_dispatch`; the lower-level `ledger.replay_action`
  primitive is intentionally not used because it re-dispatches the same action
  row and would bypass the fresh gate/reservation pass
- [x] 1.6 Add the ledger accessors the integration requires (binding a
  dispatch row's `session_id`/`process_id`, listing a job's uncertain
  actions), with no schema migration, and make `record_evidence` append-only
  with an explicit `reconcile_action` transition; serialize process identity
  as pid + start time + boot identity

## 2. Engine wiring

- [x] 2.1 Route the implement/review/archive dispatch path
  (`invoke_direct_stage` and its callers in `_run_direct_change_loop_inner`,
  including retries and `implementer_escalation`) through
  `journal_dispatch.gated_dispatch` / `resolve_dispatch` when a supervised
  registration exists, populating `dispatches.process_id` at spawn
- [x] 2.2 Route `dispatch_create_stage` through the same boundary so the
  create stage is journaled identically
- [x] 2.3 Route the timeout, SIGINT, and `terminate_group` kill paths through
  `resolve_dispatch` so an interrupted dispatch is always completed, failed,
  or marked uncertain — never left `dispatched`
- [x] 2.4 Make run start and resume call `reconcile_pending` and surface
  unreconciled uncertain actions as blocking state instead of dispatching the
  next action
- [x] 2.5 Keep the legacy path byte-identical: unregistered runs never import
  or enter the integration module and produce no journal records

## 3. Worker endpoint backing

- [x] 3.1 Pass a ledger/registration context from
  `lib/orchestrator/supervision.py::open_service_host` into the worker
  endpoint handlers in `lib/supervisor/endpoints.py`
- [x] 3.2 Back `record_evidence` with `ledger.record_evidence`: validate
  worker-domain credentials, resolve the referenced action, refuse actions
  not owned by the bound job with a named authorization error, and persist
  decisive and non-decisive evidence
- [x] 3.3 Back `request_action` with journaled job state: answer with the
  bound job's actionable items (uncertain actions, delegated-gate releases,
  steering receipts) instead of the echo stub, keeping the richer lifecycle
  payload shape deferred to `add-supervised-plan-lifecycle`
- [x] 3.4 Wire the Task-path session binding: a worker-reported native Task
  session identity binds to the action's dispatch row and journals a
  `session_binding` evidence entry

## 4. Tests

- [x] 4.1 Create `tests/supervisor/test_action_journal.py` asserting intent
  durability before a simulated side effect (interrupt after intent, before
  spawn; reopened ledger holds the committed intent)
- [x] 4.2 Assert inner-stage integration: a registered job dispatching
  implement/review/archive/create through the existing run engine (patched
  stage invocation, the `tests/orchestrator/test_supervision_gate_wiring.py`
  harness pattern) produces intent, dispatch, and outcome records per stage,
  including retry and escalation call classes
- [x] 4.3 Assert the four gates block before side effects with their named
  reasons: lock not held, broker authority refusal, model-policy violation
  (missing/unallowlisted/mismatched identity), and budget exhaustion
- [x] 4.4 Assert plan/policy freshness: a changed policy operator revision or
  a changed manifest-snapshot hash blocks the next dispatch with a named
  stale-material error, while an unrelated file update does not
- [x] 4.5 Assert uncertainty handling: a lost worker after spawn marks the
  action uncertain, an unreconciled uncertain action blocks silent progress
  on resume, and recorded evidence reconciles it to the evidenced terminal
  state
- [x] 4.6 Assert replay discipline: a duplicate delivered result reconciles
  without double billing or double apply, and replay occurs only after
  re-observation shows the work incomplete, with a live prior worker fencing
  replay
- [x] 4.7 Assert both dispatch paths: subprocess dispatches record process
  identity and worker-reported Task dispatches record session identity in the
  same journal; endpoint evidence for another job's action is refused
- [x] 4.8 Assert legacy runs create no journal records and need no ledger

## 5. Documentation and validation

- [x] 5.1 Extend `core/plan-supervision.md` with the engine dispatch
  contract: gate order, per-action plan/policy revalidation, uncertainty and
  evidence reconciliation, replay re-observation, and dual subprocess/Task
  identity tracking
- [x] 5.2 Run `python3 -m unittest discover -t . -s tests` and
  `node tests/opencode/test-opsx-usage-emitter.js` from the repository root;
  both pass
- [x] 5.3 Run `openspec validate add-action-journal-dispatch --strict`; it
  passes
