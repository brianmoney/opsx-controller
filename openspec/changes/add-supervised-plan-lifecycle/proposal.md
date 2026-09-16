# Proposal: add-supervised-plan-lifecycle

## Why

Every supervision foundation is in place — the durable ledger and protected
job policy (`define-supervision-ledger-contract`), the human-only pause flag
(`add-human-only-pause-flag`), pinned supervised model roles
(`register-supervised-model-roles`), the operator authority boundary
(`establish-operator-authority-boundary`), broker-mediated approvals
(`enforce-broker-mediated-approvals`), the execution lock
(`add-process-singleton-lock`), budgets (`add-supervision-budgets`), the
action journal (`add-action-journal-dispatch`), the OpenCode session bridge
(`add-opencode-session-bridge`), and the supervised agent contracts
(`enforce-supervised-agent-contracts`) — but there is still no way to put a
plan under supervision: registration exists only as the ledger's internal
`register_job` library path and test fixtures, the `opsx-plan supervise`
namespace stops at `status` / `probe` / `serve`, the operator `cancel` verb is
a stub, `drain` does not exist, the `pause` receipt drives no job-state
transition, and no component decides what "done" means for a supervised job.
This change adds the supervised job registration and lifecycle surface, the
stop boundaries, and evidence-based completion verification — the first live
execution surface, landing only now that action, budget, broker, and
model/agent protection are all in place.

## What Changes

- Add the `opsx-plan supervise` lifecycle commands: `register`, `start`,
  `inspect`, `resume`, `pause`, `drain`, and `cancel`, wired beside the
  existing `status` / `probe` / `serve` subcommands.
- `register` persists the complete job record: the protected manifest
  snapshot and its hash, the repository root and worktree identity, the
  standing permissions (authority configuration), the frozen model allowlist
  and budgets as protected job policy at operator revision 1, and the primary
  session linkage configuration. Registration fails closed on a host without
  a supported isolation backend and refuses a second active job per worktree.
- `start` / `resume` drive the documented job state machine
  (`registered → active → (paused → active)* → completed | failed |
  cancelled`): start boots or attaches the service-owned execution and the
  primary session; resume returns a paused job to active only after the
  broker's resume revalidation passes.
- `pause` and `drain` are explicit, durable stop boundaries: both forbid new
  dispatch; pause interrupts in-flight actions (marked uncertain for evidence
  reconciliation) while drain lets them reach a terminal outcome before the
  job holds paused. Stop requests survive restarts and wake a waiting job.
- `cancel` is a terminal broker/ledger transaction with explicit effects:
  in-flight disposition, terminal ownership (freeing the worktree for a
  future registration), and refusal of all further receipts and requests. It
  replaces the current stub operator verb.
- A human wait is persisted as normal durable job state bound to its gate
  checkpoint — no LLM polling, no stall recovery — with wake-up through the
  existing durable receipt scan.
- Supervised completion is verified from underlying plan, archive, and
  fast-check evidence — never from worker or primary claims. When fast
  checks or a partial archive need revalidation, the lifecycle reruns an
  appropriate fresh review through the existing loop rather than treating a
  prior archive as proof of done. The existing implement/review/archive loop
  and its gates remain the sole authority for progression; no new DAG or
  stage machine is introduced.
- Pending `(manual)` tasks are reported as operator checklists on the
  lifecycle and reporting surfaces without marking the supervised run falsely
  incomplete.

Out of scope (unchanged by this change): the acceptance reviewer stage
(`add-acceptance-review-stage`), incident repair
(`add-bounded-incident-recovery`), watchdog reconstitution
(`add-watchdog-reconstitution`), service packaging and activation, and how
non-supervised runs determine completion.

## Capabilities

### New Capabilities

(None.)

### Modified Capabilities

- `durable-plan-supervision`: defines the supervised job lifecycle — the
  registration record contents, the state-machine transitions driven by
  start/resume/pause/drain/cancel, the pause-versus-drain stop-boundary
  semantics, terminal cancellation effects, human-wait as durable job state,
  and completion verification from plan/archive/fast-check evidence with
  fresh review on revalidation.
- `plan-operator-cli`: adds the `opsx-plan supervise` lifecycle subcommands
  with their named errors and exit behavior, their operator-path mediation,
  and the operator documentation for the supervised lifecycle.
- `plan-manifest-lifecycle`: binds supervised registration to the canonical
  plan manifest through the protected snapshot, and keeps supervised
  completion and plan retirement on the same manifest ground truth as
  unsupervised runs.
- `task-completeness-gates`: supervised completion and reporting present
  pending `(manual)` tasks as an operator checklist and never treat them as
  incompleteness.

## Impact

- **Code:** a new stdlib-only `lib/supervisor/lifecycle.py` (one state
  machine and transition guards shared by the CLI and endpoint verbs);
  `lib/supervisor/ledger.py` (a forward-only migration chained on the schema
  head at merge time, adding the durable human-wait record and the `drain`
  stop-request representation); `lib/supervisor/endpoints.py` (the real
  `cancel` verb plus lifecycle verbs, replacing the stub);
  `lib/orchestrator/cmd_supervise.py` and `orchestrator/opsx-plan.py` (the
  seven new subcommand parsers and handlers);
  `lib/orchestrator/supervision.py` (start/resume driving the service host,
  the supervised run-engine path, and completion verification built on
  `classify`, `verify_direct_archive_done`, and `groundtruth.run_fast_checks`).
- **Docs:** `core/plan-supervision.md` gains the lifecycle contract
  (registration record, state machine, stop boundaries, cancellation,
  human-wait, completion authority); `docs/opsx-plan-operator-workflow.md`
  gains the lifecycle command reference.
- **Tests:** new `tests/supervisor/test_supervision_lifecycle.py` covering
  each lifecycle command, persisted registration fields, pause/drain
  boundaries, cancellation effects, human-wait persistence without polling,
  evidence-only completion with fresh review where required, and `(manual)`
  checklist reporting.
- **Dependencies:** builds on the archived `add-action-journal-dispatch`,
  `enforce-broker-mediated-approvals`, and `enforce-supervised-agent-contracts`
  (and transitively on every earlier supervision change). No new runtime
  dependencies; stdlib only.
- **Compatibility:** no breaking changes. Legacy unregistered runs behave
  exactly as before and never touch the lifecycle surface; non-supervised
  completion semantics are unchanged. The lifecycle commands fail closed with
  named errors on unregistered worktrees, terminal jobs, illegal transitions,
  and unsupported hosts.
