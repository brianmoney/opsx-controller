## 1. Ledger schema version 5

- [x] 1.1 Add the `receipts` table (`id`, `job_id` FK, `change_id`, `kind` in `approval|acceptance|reset|pause|steer`, `checkpoint`, `material_hash`, `authority` in `operator|delegated|service`, `actor_principal`, `detail`, `created_at`, index on `(job_id, change_id, kind)`) and the `manifest_snapshots` table (`job_id`, `snapshot_hash`, `content`, `created_at`, unique `(job_id, snapshot_hash)`) to `lib/supervisor/ledger.py` via `_migrate_4_to_5`; bump `CURRENT_SCHEMA_VERSION` to 5
- [x] 1.2 Add ledger helpers: append-only receipt recording in a single transaction, receipt query by `(job_id, change_id, kind)` and by `id > high_water`, snapshot store/lookup by hash — following the existing transactional-write discipline
- [x] 1.3 Add migration tests: a v4 ledger migrates forward to v5 preserving all existing records, and a v5 ledger opened by older-versioned code fails with `LedgerVersionError`
- [x] 1.4 Capture the protected manifest snapshot content at registration: extend the registration path (`register_job` and its callers) to write the manifest content and its hash to `manifest_snapshots` in the same transaction as the job and policy revision 1, and update the existing registration fixtures to supply content; a registered job without stored snapshot content is invalid

## 2. Broker module

- [x] 2.1 Create `lib/supervisor/broker.py` with named errors `BrokerError`, `BrokerMediationError`, `StaleMaterialError`, `BrokerUnavailableError`, exported per the package's `__all__` discipline, stdlib-only, no import cycles
- [x] 2.2 Implement material-revision hashing: `H(change_id, gate_fields, snapshot_hash, policy_revision)` where `gate_fields` is the gate-relevant subset (phase, `pause_before`, `pause_before_human_only`, `review_created`, dependencies) read from the protected snapshot, never the repo plan
- [x] 2.3 Implement receipt recording for `approve` (single, `--all`, `P<N>` resolved against the protected snapshot), `accept`, operator `reset`, and pause/steer requests — each bound to its checkpoint and current material hash, recorded as one durable transaction, followed by projection regeneration (task 4.3)
- [x] 2.4 Implement the supervised gate resolver: a change is dispatchable when ungated or when a receipt exists whose checkpoint matches and whose `material_hash` equals the current material revision; enforce resolved authority (`human-only` → operator receipts only; `delegated` → scoped service receipts only)
- [x] 2.5 Implement bounded reset: operator resets recorded; service resets require an explicit policy bound; worker-direct reset attempts refuse with `BrokerMediationError` and record nothing
- [x] 2.6 Implement durable wake-up: per-job receipt high-water tracking with scan-on-boot/wake (`id > high_water`); notify triggers an immediate scan but the scan is authoritative; no execution-lock acquisition on any receipt path

## 3. Endpoint integration

- [x] 3.1 Convert the operator `approve` handler in `lib/supervisor/endpoints.py` from validate-only to a broker call that records durable receipts; add the operator `reset_change` verb to `OPERATOR_HANDLERS` only
- [x] 3.2 Add the scoped worker verb `release_delegated_gate` to `WORKER_HANDLERS`, accepted by the broker only when the change's resolved authority is delegated and the requesting identity is the job's registered service identity; keep the dispatch tables disjoint and update `handler_tables_are_disjoint` coverage
- [x] 3.3 Assert no approval-family verb is reachable from the worker endpoint and that `dispatcher_executes_repo_code` still holds for all new handlers

## 4. CLI mediation

- [x] 4.1 Add a registration-detection helper shared by gate and run commands that reuses the `find_job_by_worktree` signal (as `open_supervised_gate` does) and never consults JSON markers or the repo plan for the registration decision
- [x] 4.2 Mediate `cmd_gates.cmd_approve`, `cmd_gates.cmd_accept`, and
  `cmd_gates.cmd_reset` (including `reset --failed`): unregistered jobs take
  the legacy path byte-identically; registered jobs route through the operator
  path as a client against the operator endpoint, with worker-domain or
  unauthenticated attempts failing with `BrokerMediationError` and an
  unreachable broker failing closed with `BrokerUnavailableError`. The
  operator-endpoint listener is provided by the supervised service (later
  change); this change supplies the client and tests the path over the
  authority change's loopback socketpair fixtures
- [x] 4.3 Regenerate the JSON projection (approvals, acceptance flags, change records) from broker and ledger state after each broker transaction, so legacy `status`/`report` read paths are unchanged
- [x] 4.4 Mediate `run`, `run-one`, and `opsx-run`: in a registered job, ordinary CLI dispatch outside the supervised execution refuses with `BrokerMediationError`; supervised dispatch resolves gates through the broker resolver (task 2.4) instead of `state["approvals"]`, leaving legacy `classify()` untouched
- [x] 4.5 Keep `status`, `logs`, `report`, and `doctor` read-only and unmediated in registered jobs

## 5. Resume revalidation

- [x] 5.1 Before the first dispatch after a restart, pause, or human wait, revalidate every relied-upon receipt against the current material revision; re-arm any gate whose receipt is stale and raise `StaleMaterialError` into the job's incident flow instead of dispatching
- [x] 5.2 Cover the three flag-semantics cases end to end under supervision: absent-key gate releases only via operator receipt, explicit `true` only via operator receipt, explicit `false` via the scoped delegated action

## 6. Documentation

- [x] 6.1 Update `core/plan-supervision.md`: broker sole-authority scope, receipt kinds and checkpoint/material-revision binding, the protected snapshot plus external registration anchor, resume revalidation, bounded resets, and the durable wake-up mechanism
- [x] 6.2 Update the operator workflow documentation: mediated commands, the operator OS-authenticated approval path, delegated-gate behavior, the named refusal errors (`BrokerMediationError`, `BrokerUnavailableError`, `StaleMaterialError`), when an explicit plan/policy revision re-arms a gate, and the unchanged legacy behavior — with at least one operator-approval example and one worker-refusal example

## 7. Broker test suite

- [x] 7.1 Create `tests/supervisor/test_broker_approvals.py` (in the existing `tests/supervisor` package): a worker-domain subprocess cannot approve, reset, or run in a registered job (using the authority fixtures' restricted-process path)
- [x] 7.2 Assert a stale material revision does not satisfy a gate while an unrelated update (task progress, telemetry, another change's state) does not invalidate a receipt
- [x] 7.3 Assert `approve --all`, `approve P<N>`, and `accept` are broker mediated in a registered job, printing exactly the affected change IDs
- [x] 7.4 Assert a worker that drops supervised fields from the JSON state and the repo plan remains registered and mediated
- [x] 7.5 Assert receipts wake the owning job without acquiring the held execution lock, and that a restart rescans receipts above the high-water mark
- [x] 7.6 Assert legacy unregistered jobs keep prior JSON handling for every mediated command, with no broker or ledger dependency
- [x] 7.7 Assert the projection follows broker state and direct JSON edits that disagree with it have no authority
- [x] 7.8 Assert pause and steer requests are recorded as durable receipts bound to the change's checkpoint and material revision, wake the owning job through the receipt scan without the execution lock, and are refused for worker-domain callers that bypass their scoped service action

## 8. Validation

- [x] 8.1 `python3 -m unittest discover -t . -s tests` passes from the repository root
- [x] 8.2 `node tests/opencode/test-opsx-usage-emitter.js` passes
- [x] 8.3 `openspec validate enforce-broker-mediated-approvals --strict` passes
