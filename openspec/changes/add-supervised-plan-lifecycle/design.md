# Design: add-supervised-plan-lifecycle

## Context

See proposal.md — Why for motivation. The load-bearing current state:

- `lib/supervisor/ledger.py` (schema v5) defines `JOB_STATES = ("registered",
  "active", "paused", "completed", "failed", "cancelled")`,
  `TERMINAL_JOB_STATES`, `register_job` / `set_job_state` /
  `find_job_by_worktree`, the receipts table with `RECEIPT_KINDS =
  ("approval", "acceptance", "reset", "pause", "steer")` behind a `CHECK`
  constraint, and the single-active-job-per-worktree partial unique index.
- `lib/supervisor/broker.py` owns gate authority: `record_pause_or_steer`
  writes durable `pause` / `steer` receipts with a wake-up, and
  `assert_resume_clear` / `revalidate_receipts` implement resume
  revalidation. No receipt drives a job-state transition today.
- `lib/supervisor/endpoints.py` exposes `OPERATOR_HANDLERS = {approve,
  reset_change, revise_policy, enable, cancel}`; `_operator_enable` and
  `_operator_cancel` are stubs returning the verb and uid.
- `orchestrator/opsx-plan.py` wires `supervise status | probe | serve` to
  `lib/orchestrator/cmd_supervise.py`; `cmd_supervise_serve` boots
  `lib/orchestrator/supervision.py`'s `open_service_host` /
  `ServiceEndpointHost` (`bind`, `serve_forever`, `start_primary_session`).
  CLI commands reach the broker through `supervision.call_operator`, which
  fails closed (`BrokerUnavailableError`) when the endpoint is unreachable.
- The run engine (`_cmd_run_body_inner`, `run_direct_change`,
  `_run_direct_change_loop_inner`) already routes every inner stage dispatch
  through `lib/orchestrator/journal_dispatch.py`'s gated boundary when a
  registered job exists. Completion ground truth is `classify(...)` per
  change plus `verify_direct_archive_done`, `groundtruth.run_fast_checks`,
  and `delivery.verify_post_archive_clean` in `apply_archive_result`.
  `state.pending_manual_tasks` produces the operator manual checklist.
- `core/plan-supervision.md` documents the job state machine and defers the
  lifecycle command surface to this change.

## Goals / Non-Goals

**Goals:**

- One lifecycle module owns the state machine and transition guards, consumed
  identically by the CLI handlers and the operator endpoint verbs.
- The seven lifecycle commands with durable, fail-closed semantics:
  registration record, start/resume, pause/drain stop boundaries, terminal
  cancel, read-only inspect.
- Human wait as first-class durable job state with receipt-driven wake-up.
- Completion decided only from plan/archive/fast-check evidence; fresh
  review on revalidation; `(manual)` tasks as operator checklists.

**Non-Goals:**

- The acceptance stage, incident repair policy, watchdog reconstitution, and
  service packaging/activation (later changes).
- Changing legacy unregistered-run behavior or non-supervised completion.
- A new DAG, stage machine, or dispatcher: the existing run engine loop and
  its gates remain the progression authority.
- New model roles, vendor model IDs, or pricing changes.

## Decisions

### D1. One `lib/supervisor/lifecycle.py` owns the state machine

All job transitions — register, start, resume, pause, drain, cancel — live in
a single stdlib-only module with explicit transition guards (legal source
states, required evidence, named errors). The CLI handlers in
`cmd_supervise.py` and the operator endpoint verbs in `endpoints.py` are thin
adapters over it, so the bootstrap path and the mediated path can never
diverge. Named errors ride the existing families: `LifecycleError` with
`UnknownJobError`, `TerminalJobError`, `IllegalTransitionError`, and reuse of
`UnsupportedHostError`, `BrokerUnavailableError`, `DuplicateJobError`.

*Alternatives considered:* putting transition logic in `cmd_supervise.py`
(the endpoint verbs could not share it, and endpoint/CLI semantics would
drift) or in `endpoints.py` (the CLI bootstrap paths — register, start —
cannot require a per-job endpoint that does not exist yet). A single library
module under the existing module-import discipline is the only option that
keeps one implementation.

### D2. Registration is a trust-root library path, not an endpoint verb

The endpoint host is job-scoped: `open_service_session` requires a registered
job, so no endpoint can exist before registration. `supervise register`
therefore runs as the operator (the trust root in the boundary model) and
records the job, the protected policy at revision 1 (frozen model selection,
allowlist, budgets, deadlines, authority configuration = standing
permissions), and the manifest snapshot with its hash directly through the
ledger, after validating the isolation backend the same fail-closed way
`supervise probe` does. The primary session linkage configuration is
persisted at registration; the live linkage is recorded later through the
existing journaled linkage action when the session starts.

*Alternatives considered:* an "enablement service" endpoint that can register
jobs (rejected: it invents a second service surface with its own lifecycle
for exactly one verb, and the OS owner is already the trust root with
OS-level access to the store); writing registration through repo-writable
files (rejected: violates the untrusted-repo rule).

### D3. Start/resume drive the existing run engine supervised path

`start` transitions `registered → active`, boots or attaches the
`ServiceEndpointHost`, records the fencing identity for the supervised
execution, starts or adopts the primary session through the bridge (the
recorded linkage), and drives the existing run-engine loop — whose inner
dispatches already flow through the journal's gated boundary. `resume`
transitions `paused → active` only after `broker.assert_resume_clear` passes;
a stale receipt re-arms its gate per the existing broker contract. Neither
command introduces a scheduler: progression, gates, and rounds stay with the
run engine and the broker.

*Alternatives considered:* a supervisor-side DAG runner that re-plans the
manifest (rejected: duplicates the run engine and invites two authorities);
driving stages by polling the primary session (rejected: the engine drives
stages; the primary directs through the tracked service tool).

### D4. Pause and drain are stop requests with distinct in-flight disposition, converging on `paused`

A stop request is a durable broker receipt (kind `pause`, or kind `drain` —
added by the migration in D8) bound to the job, observed at the dispatch
boundary before any new side effect, and waking a waiting job through the
existing receipt scan. On `pause`, in-flight actions are interrupted and
marked `uncertain` under the journal's existing semantics, and the job enters
`paused`. On `drain`, no new dispatch begins but in-flight actions run to a
terminal outcome, and only then does the job enter `paused`. Stop requests
are durable, so a restart between request and observance still honors them.

*Alternatives considered:* adding `draining` to `JOB_STATES` (rejected: the
contract fixes six states and every consumer would grow a transitional state
that carries no extra authority — the stop request plus `paused` expresses
the same behavior with evidence); a process signal or flag file (rejected:
not durable, not broker-mediated, and invisible to the receipt scan).

### D5. Cancel is a terminal transaction with explicit effects

`cancel` records the `cancelled` terminal state in one ledger transaction:
in-flight actions are failed with a cancellation reason (or marked
`uncertain` where the outcome cannot be confirmed, per journal rules);
ownership becomes terminal, so the partial unique index frees the worktree
for a future registration; and all later receipts, stop requests, and
lifecycle verbs for the job are refused with `TerminalJobError`. Cancel never
resumes. This replaces the stub `_operator_cancel` and is reachable both
through the operator endpoint (live service) and the CLI trust-root path
(non-live job), per D1.

*Alternatives considered:* cancel-as-pause with a flag (rejected: terminal
semantics must be unforgeable and obvious — a paused job is resumable, a
cancelled one is not); killing processes first and recording later (rejected:
violates journal-before-side-effects; the record leads, process teardown
follows and its outcome reconciles through evidence).

### D6. Human wait is first-class durable job state

When the run engine reaches a human-only gate, the job records a durable
human wait — the awaiting checkpoint, the material revision, and the wait
start — through the migration in D8, and keeps permanent ownership without
holding the execution lock. There is no LLM polling and no stall recovery
for a human wait; the job wakes only through the durable receipt scan, and
execution-deadline accounting excludes the wait under the existing budgets
requirement (this change records the state; it does not re-implement deadline
math). Resume after the wait revalidates receipts per D3.

*Alternatives considered:* encoding the wait as `paused` with a detail
string (rejected: a human wait is semantically distinct — no stop was
requested, and watchdog/classification logic landing later must distinguish
"expected wait" from "operator-stopped"); polling the operator endpoint from
the primary (rejected: the plan forbids LLM polling for human waits).

### D7. Completion is the existing ground truth, with fresh review on revalidation

The lifecycle declares a supervised job `completed` only when every enabled
change in the plan classifies `DONE` from the existing evidence:
`verify_direct_archive_done` over archive records, `groundtruth.run_fast_checks`,
and post-archive cleanliness — never from a worker or primary claim of done.
When completion evidence needs revalidation (a failed post-archive fast check
or an unverified/partial archive), the lifecycle reruns an appropriate fresh
review round through the existing implement/review/archive loop; a prior
archive is not treated as proof of done. Bounded by the existing round
budgets, so revalidation cannot loop forever; repair policy itself belongs to
`add-bounded-incident-recovery`. Pending `(manual)` tasks are collected via
`state.pending_manual_tasks` and attached to the completion record and the
inspect/report surfaces as an operator checklist; they never block completion
or mark the job incomplete. Non-supervised completion is untouched.

*Alternatives considered:* trusting the primary's completion report with a
post-hoc audit (rejected: audit-after-the-fact is exactly the worker-claim
model the plan forbids); a separate supervised completion checklist
(rejected: a second authority would diverge from the run engine's).

### D8. One forward-only migration adds waits and the `drain` receipt kind

A single migration chained on the schema head at merge time (currently v5 →
v6) adds (a) a `waits` table — job id, checkpoint, material revision, kind
(human wait vs stop-boundary hold), started/ended timestamps — and (b)
`drain` in the receipts `CHECK` constraint, rebuilt in the same transaction.
Forward-only discipline is unchanged: older ledgers migrate, newer fail with
a named error.

*Alternatives considered:* encoding waits as receipt payloads (rejected:
receipts are point-in-time records; a wait is an interval with a start and an
end, and querying open intervals from receipts is fragile); adding job
columns instead of a table (rejected: a job can accumulate many waits over
its lifetime, and per-wait material revisions must stay auditable).

## Risks / Trade-offs

- CLI bootstrap path and endpoint path diverge over time → one lifecycle
  module with shared guards; endpoint verbs and CLI handlers are thin and
  tested against the same transition table.
- Pause interrupting in-flight work creates `uncertain` actions that block
  resume → the journal's reconciliation already exists; `inspect` and the
  resume briefing surface unreconciled uncertain actions as blocking state
  rather than hiding them.
- Fresh-review revalidation loops on a persistently failing fast check →
  bounded by the existing per-change round budgets; exhaustion surfaces as a
  failed change with its named reason, and repair is the incident-recovery
  change's job.
- Cancel raced with a live execution → the ledger transaction records
  terminal state first; the execution observes it at its dispatch boundary
  and tears down, with outcomes reconciled through evidence, so a kill mid
  action never leaves a silent side effect.
- Registration persisting secrets-adjacent configuration (allowlist, budgets)
  → all of it lives in the protected policy record in service-owned storage,
  never in the worktree.

## Migration Plan

1. Land the lifecycle module, the ledger migration, the endpoint verbs, and
   the CLI wiring with their tests.
2. Maintainer reinstalls the runtime: `bash install.sh --global --verify`
   (required — the installed `lib/supervisor` and `lib/orchestrator` packages
   are what actually run).
3. Existing ledgers at v5 (or older) migrate forward on first open; no data
   is rewritten beyond the migration transaction.
4. Rollback: legacy runs never touch the lifecycle surface. Supervised jobs
   registered under this change keep their ledger; reverting the runtime
   leaves the ledger at the newer schema version, which older code refuses
   with a named error — the documented forward-only posture, not silent
   corruption.

## Open Questions

None that change the specs. The exact `inspect` output field layout is a
presentation detail settled in implementation against the test assertions;
the observability change later owns report/dashboard projections.
