## Context

See proposal.md — Why. The relevant current state:

- `lib/supervisor/endpoints.py` already provides the trust split: an operator
  endpoint (`ENDPOINT_OPERATOR`, `SO_PEERCRED`-authenticated against the
  operator principal) and a worker-actions endpoint (`ENDPOINT_WORKER`) with
  disjoint dispatch tables (`OPERATOR_HANDLERS`, `WORKER_HANDLERS`). The
  handlers are validate-only today; they record nothing durable.
- `lib/supervisor/ledger.py` (schema version 4, after
  `add-supervision-budgets`) persists jobs, actions, incidents, evidence,
  protected `job_policies` (insert-only, explicit `revision`), fencing records,
  budget reservations, and reservation outcome timestamps
  (`reconciled_at`/`retained_at`). Registration already stores
  `manifest_snapshot_hash` in the policy but no snapshot content.
- `orchestrator/opsx-plan.py::open_supervised_gate(repo)` already detects a
  registered job via `find_job_by_worktree`, so the CLI has a proven,
  service-owned registration signal that does not depend on repo-writable
  files.
- Gate commands are unmediated JSON mutations today: `cmd_gates.cmd_approve`,
  `cmd_gates.cmd_accept`, `cmd_gates.cmd_reset`; `classify()` computes
  `awaiting_approval` from `state["approvals"]` and `c["pause_before"]`.
- `lib/supervisor/lock.py` already refuses ordinary mutating runs that race a
  supervised execution (`SupervisedOwnershipError`) and exempts receipts from
  the execution lock.

## Goals / Non-Goals

**Goals:**

- A broker module in the trusted authority domain that is the sole writer of
  approval/acceptance/reset records for registered supervised jobs.
- Durable, revision-bound receipts with a durable wake-up, no dependency on
  the held execution lock.
- Runtime enforcement of the three `pause_before_human_only` resolution cases.
- Fail-closed behavior: no broker, no mutating command in a registered job.
- Byte-identical behavior for unregistered legacy jobs.

**Non-Goals:**

- The acceptance review stage, budgets enforcement, the session bridge, the
  watchdog, and the lifecycle command surface (`supervise register/start/...`)
  — later changes. Registration itself continues to come from the ledger's
  existing `register_job` path and test fixtures.
- Incident-recovery-driven resets; this change only provides the bounded
  reset mechanism they will consume.
- Changing any legacy unregistered-job behavior, including `classify()` on
  the legacy path.

**Review scope:** the long-running supervised service that hosts the operator
endpoint listener, the acceptance review stage, the watchdog, and lifecycle
commands are explicitly out of scope. Requiring a running daemon or a
production supervisor call site contradicts the plan document's out-of-scope
statement for this change; the handler side and the CLI client are what this
change builds and tests.

## Decisions

### 1. New `lib/supervisor/broker.py` owns mediation logic; `endpoints.py` stays transport

The broker is a pure, stdlib-only module that takes a ledger connection plus
an authenticated-principal descriptor and returns recorded receipts or named
refusals. The endpoint handlers become thin adapters that call the broker;
the CLI mediation shim calls the same broker client surface. This keeps the
socket/peer-credential machinery unchanged and lets the entire mediation
matrix be unit-tested without sockets. Alternative considered: putting
durable logic directly in the endpoint handlers — rejected, because the CLI
and the future service need the same decisions without a socket dependency,
and it would blur the module-layout discipline (concern-named modules).

Named errors: `BrokerError` base, `BrokerMediationError` (worker-domain or
unauthenticated mutation attempt in a registered job), `StaleMaterialError`
(receipt/gate mismatch on resume or dispatch), `BrokerUnavailableError`
(registered job but the broker path cannot be reached — fail closed).

### 2. Receipts are a new ledger table; schema bumps to version 5

New `receipts` table via the existing forward-only `MIGRATIONS` mechanism
(`_migrate_4_to_5`, `CURRENT_SCHEMA_VERSION = 5`), on top of the
`add-supervision-budgets` v4 schema:

- `id` (generated, never reused), `job_id` (FK), `change_id`, `kind`
  (`approval` | `acceptance` | `reset` | `pause` | `steer`),
  `checkpoint` (gate kind + change id), `material_hash`, `authority`
  (`operator` | `delegated` | `service`), `actor_principal`, `detail`,
  `created_at`; index on `(job_id, change_id, kind)`.

Alternatives considered: reusing the `actions` table — rejected, receipts
are authority records, not dispatched work, and the action journal change
owns that table's semantics; storing receipts in JSON state — rejected
outright, JSON is worker-writable.

**Durable wake-up:** the receipts append is the durable record. The
supervised execution tracks a high-water receipt id per job and, on boot or
wake, scans `id > high_water`. An in-process/same-host notify (the existing
endpoint connection) triggers an immediate scan for liveness, but the scan is
the authority, so a restart never loses a receipt. No execution-lock
acquisition at any point, per the existing receipt-exclusion requirement.

### 3. Material revision hashes only gate-relevant fields from protected records

`material_hash = H(change_id, gate_fields, snapshot_hash, policy_revision)`
where `gate_fields` is the minimal gate-relevant subset of the change's
manifest entry (phase, `pause_before`, `pause_before_human_only`,
`review_created`, and the change's declared dependencies) taken from the
**protected snapshot**, `snapshot_hash` is the policy's
`manifest_snapshot_hash`, and `policy_revision` is the current insert-only
policy revision. Alternatives considered: hashing the whole manifest —
rejected, unrelated edits would invalidate approvals; hashing the live repo
plan — rejected, it is worker-writable. Consequence: an operator who wants a
plan change to take effect registers a new protected snapshot explicitly;
worker edits never move the hash.

### 4. The protected manifest snapshot content lives in the ledger

New `manifest_snapshots` table (same v5 migration): `job_id`, `snapshot_hash`
(matching the policy field), `content`, `created_at`, unique on
`(job_id, snapshot_hash)`. Storing content in the ledger keeps one durable,
transactional store instead of adding a second service-owned file with its
own fsync discipline. Gate evaluation and `P<N>` phase resolution for
registered jobs read the snapshot, never the repo plan; when snapshot and
repo plan diverge, the snapshot is authoritative by construction. The
registration path writes the snapshot content and its hash in the same
transaction as the job and its policy revision 1, so a registered job always
has protected content to evaluate gates against.

### 5. The CLI mediates through a registration check, then the operator path

`cmd_approve`/`cmd_accept`/`cmd_reset` and the `run`/`run-one`/`opsx-run`
entrypoints first detect registration exactly as `open_supervised_gate` does
(ledger lookup by worktree — service-owned, not JSON markers). Unregistered:
legacy code path, byte-identical. Registered: route to the broker through the
operator endpoint as a client; the socket's filesystem permissions plus
`SO_PEERCRED` do the authentication. A worker principal cannot connect
(permissions) or authenticate (peer uid mismatch), and the worker endpoint
has no approval verbs, so its attempt ends in `BrokerMediationError`. If the
job is registered but the broker is unreachable, the command fails closed
with `BrokerUnavailableError`; read-only diagnostics never take this path.
Alternative considered: a CLI flag/env token to identify the operator —
already rejected by the authority boundary; peer credentials only.

New operator verbs are added to `OPERATOR_HANDLERS` only where the endpoint
split requires them (e.g. `reset_change`); no approval-family verb is ever
added to `WORKER_HANDLERS`. Delegated (`pause_before_human_only = false`)
gates are released through a new scoped worker verb `release_delegated_gate`
that the broker accepts only when (a) the change's resolved authority is
delegated and (b) the requesting identity is the job's registered service
  identity — keeping human-only gates unreachable from the worker endpoint by
  construction.

**Transport at this stage:** the operator-endpoint listener is hosted by the
supervised service, which does not exist yet (`add-supervised-plan-lifecycle`
and the service-packaging change own it). This change therefore implements the
broker handler side and the CLI client transport against the endpoint, and the
tests exercise both over the authority change's real loopback socketpair
fixtures. A real registered-job CLI approval with no reachable service fails
closed with `BrokerUnavailableError`; that is the correct behavior until the
service is present, not a missing production call site for this change.

### 6. Gate resolution for registered jobs consults receipts, not `state["approvals"]`

A supervised gate resolver (broker-side) answers "is change X dispatchable"
as: ungated, or a receipt exists whose `checkpoint` matches and whose
`material_hash` equals the current material revision. The run engine uses
this resolver only when a registered job is present; legacy `classify()` is
untouched. Resume revalidation is the same resolver applied before the first
dispatch after a restart/pause/human wait: any relied-upon receipt whose
`material_hash` no longer matches re-arms its gate and raises
`StaleMaterialError` into the job's incident flow rather than dispatching.

### 7. The JSON projection is regenerated from broker state after each transaction

After every recorded broker transaction, the service-side writer regenerates
the JSON projection (approvals, acceptance flags, change records) so legacy
read paths (`status`, `report`, dashboards) keep working unchanged. The
projection is write-only from the broker side; direct worker edits to it are
never read back as authority (gate decisions come from receipts), which is
what makes JSON "a projection, not a competing phase authority".

### 8. Resets are recorded broker transactions with two authorized paths

Operator reset: new operator-endpoint verb, recorded as a `reset` receipt,
change returns to its pre-dispatch gate state (a human-only gate re-arms).
Service reset: permitted only through the same broker function with an
explicit policy bound (the policy's budget/attempt records cap it); the
recovery change consumes this later. Worker-direct `opsx-plan reset`,
`opsx-run`-driven reset, or JSON rewrite in a registered job:
`BrokerMediationError`, no state change.

## Risks / Trade-offs

- [CLI must read the ledger to detect registration, but the ledger is
  service-owned] → Detection uses a read-only open consistent with the
  existing `open_supervised_gate` behavior; all writes go through the broker.
  Filesystem permissions on the service state directory remain the boundary
  for writes.
- [A registered job with an unreachable broker bricks mutating commands] →
  Fail closed with `BrokerUnavailableError` naming the remediation; this is
  deliberate — silently falling back to JSON authority would reintroduce the
  forgeable path. Diagnostics stay available.
- [Hashing too much invalidates approvals on every policy touch] → The
  material set is the minimal gate-relevant subset; tests assert an unrelated
  update (task progress, telemetry, other changes) preserves receipt validity.
- [Phase (`P<N>`) resolution against a stale snapshot surprises the operator]
  → Snapshot is authoritative by design and documented; registering a new
  snapshot is an explicit operator act covered by the observability change.
- [Schema v5 migration on existing ledgers] → Forward-only migration via the
  existing mechanism; an older runtime opening a v5 ledger fails with the
  existing named `LedgerVersionError`, which is the established contract.

## Migration Plan

1. Ship the v5 migration with the broker module; existing ledgers migrate
   forward automatically on first open by the new runtime.
2. Reinstall the runtime (`bash install.sh --global --verify` per the
   maintainer notes) so the installed `lib/supervisor/` gains `broker.py`.
3. No operator action is required for unregistered jobs; registered jobs
   (fixtures/tests only at this stage) pick up mediation on next command.
4. Rollback: reinstall the prior runtime; a v5 ledger is rejected by it with
   `LedgerVersionError` rather than silently mishandled, and unregistered
   jobs are unaffected either way.
