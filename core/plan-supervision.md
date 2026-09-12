# Plan Supervision Contract

Client-neutral contract for durable plan supervision: the storage model, the
record lifecycles, and the ordering rules every supervision change builds on.
It is written against the shared runtime surface (the `lib/supervisor/`
package and its SQLite ledger) and describes no specific adapter.

The supervision ledger is **additive**. The authoritative JSON execution state
under `.opsx-plan/` remains the source of truth for plan execution, and an
ordinary, unsupervised run neither requires nor opens the ledger. See
[Separation from execution state](#separation-from-execution-state).

## Storage

The supervisor ledger is a single SQLite database implemented with the Python
standard library only (the `sqlite3` module). One file holds every record kind
described below.

- The database opens in WAL journal mode (so a future watchdog can read while
  a writer holds a short transaction) with foreign keys enabled.
- The schema version is stored in the ledger itself in `PRAGMA user_version`,
  so the recorded version can never drift from the data.
- Writes that span multiple records execute in a single transaction. An
  interrupted multi-record write leaves no partial state: after a reopen the
  ledger shows either the complete write or none of it.
- Repository references (worktree paths, manifest snapshot paths) are stored
  as repository-relative data with the repository root recorded once per job,
  so moving a checkout does not orphan ledger rows.

### Schema versioning and forward-only migration

The ledger carries an explicit schema version. Opening logic is:

| Recorded version | Behavior |
| --- | --- |
| older than current | apply ordered `migrate_N_to_N+1` steps in one transaction |
| equal to current | open as-is |
| newer than current | fail with a named `LedgerVersionError`, without modifying the file |

Migrations are forward-only and additive (new tables or columns), so a
mid-plan reinstall never strands an existing ledger, and a host that downgrades
its runtime fails loudly instead of silently corrupting newer data. The same
forward-only discipline applies to the job-policy payload, which carries its
own policy-schema version.

## Record model

### Jobs

A supervised job is one unit of supervised work for one worktree of one
repository. A job row carries:

- an integer `id`, assigned at creation and never reused;
- the existing plan-run `run_id` it belongs to (a link, not a foreign key);
- the repository root and the repository-relative worktree path;
- a lifecycle `state`;
- ownership identity fields (owner label and, where available, principal,
  host, and boot identity) that fence who currently owns the job;
- a high-water incident marker.

Job lifecycle: `registered` → `active` → (`paused` → `active`)* →
`completed` | `failed` | `cancelled`. The terminal states are `completed`,
`failed`, and `cancelled`.

### Actions

An action is one supervised side-effecting step of a job. An action row
carries:

- an integer `id`, assigned at creation and never reused;
- its owning `job_id` (foreign key into `jobs`);
- the `run_id` it belongs to, as data distinct from the record identifiers;
- the action kind and a journal `state`;
- the intent timestamp, dispatch timestamp, and update timestamp.

Action journal states are `intent`, `dispatched`, `uncertain`, `reconciled`,
`completed`, and `failed`. The journal is the durable record of what the
supervisor intended to do and what it observed; it is described under
[Journal semantics](#journal-semantics).

### Incidents

An incident is a bounded, durable record of a supervision interruption or
failure associated with a job. An incident row carries an integer `id`, its
owning `job_id` (foreign key), an optional `run_id`, a kind, a state, a
summary, and creation/update timestamps. Incident lifecycle progresses from an
open state toward a resolved or escalated state; incident repair policy and
backoff belong to later changes and are not fixed here.

### Protected job policy

Each job has a protected job-policy record. The policy payload carries:

- the authority configuration;
- the model selection and the inexpensive-model allowlist selection;
- the hash of the manifest snapshot the job registered with;
- the job's budgets;
- the job's deadlines.

Policy rows are **insert-only** and explicitly revised:

- The first revision is recorded as revision 1 when the job is registered,
  together with the policy-schema version.
- A change writes a new row with `revision = previous + 1` and supersedes the
  prior row's current marker. Any other revision number is rejected with a
  named error and changes nothing.
- Prior revisions remain retrievable, so a later approval can bind to the
  exact revision it approved.
- There is no silent in-place mutation, fallback, inheritance, or defaulting
  of policy fields. A database-level guard rejects in-place updates of
  persisted policy rows.

The manifest-snapshot hash makes a silently edited manifest detectable at
revalidation time.

### Model policy

The protected job policy's `model_selection` and `inexpensive_allowlist`
fields carry a versioned content schema owned by
`lib/supervisor/model_policy.py`. Both payloads share one nested
`MODEL_POLICY_VERSION = 1`, which is **independent** of the outer ledger
`policy_version` column (both currently equal 1 by coincidence and are
validated separately). Reads are forward-only: a recorded nested version newer
than the code supports raises a named `ModelPolicyVersionError` rather than
silently interpreting unknown fields.

`model_selection` has the shape
`{"version": 1, "roles": {<role>: <exact model identifier>}, "stages":
{<stage>: <role>}}`. `roles` pins an exact identifier per role the job selects
(no wildcard, pattern, prefix, or default entry); `stages` is the explicit
stage-to-role mapping, and every role a stage names has a corresponding pin.
The standard mapping is `create → supervised_author`, `implement →
implementer`, `review → reviewer`, `archive → archiver`, `acceptance →
acceptance_reviewer`, `fix → fixer`, `verify → verifier`, and `escalate →
implementer_escalation`; a job records only the stages it uses. The
supervised create stage maps to `supervised_author`; the legacy `controller`
compile role is distinct and governs only non-supervised compilation.

`inexpensive_allowlist` has the shape `{"version": 1, "models": [<exact model
identifier>, ...], "source": <resolved source description>}`, freezing the
allowlist selection the job was registered with so a later configuration edit
cannot silently change a running job's policy.

**Fail-closed classification.** `supervisor` is a pinned operator-selected
model classified as exempt from the inexpensive allowlist and as
budget-counted. "Frontier" is descriptive only, not an automatically
verifiable property. The supervised dispatch roles are the existing dispatch
roles `implementer`, `reviewer`, and `archiver`, plus `supervised_author`,
`acceptance_reviewer`, `fixer`, `verifier`, and `implementer_escalation`; each
must resolve to an allowlisted inexpensive model. The legacy `controller` role
is not a supervised dispatch role. There is no silent fallback, inheritance,
or defaulting between roles: an unresolved, identifier-syntax-invalid, or
off-pin role is reported as blocking. A model is "unavailable" for this policy
only when its resolved identifier fails the adapter's existing
identifier-syntax validation; live availability probing and pricing are out of
scope.

**Boundary validation and compatibility.** The ledger validates both payloads
through the model-policy module on write and decodes them through it on read.
New writes are strict and versioned. A stored value with no `version` key —
including the pre-schema list-shaped allowlist — is classified
`legacy_unversioned`, returned unmodified, and never reinterpreted as a
current payload; a consumer treats it as carrying no pins and fails closed. No
schema migration is performed; replacing a legacy payload requires an explicit
operator revision recording a versioned payload.

**Dispatch identity record.** Dispatch model identity is action/evidence data,
separate from the insert-only policy payload, with the shape `{"action_id":
<int>, "role": <policy role>, "requested_model": <exact identifier>,
"observed_model": <exact identifier or null>, "observation_state":
<requested|observed|unknown|interrupted>, "reservation_state":
<reserved|retained|reconciled>}`. At dispatch intent `requested_model` equals
the exact policy pin for the role. The policy provides a pure `mismatch`
predicate (true only when `observed_model` is non-null and differs from
`requested_model`) and a pure retention decision (`unknown` or `interrupted`
classifies the reservation as `retained`, so unresolved consumption is never
treated as free). These are pure decisions; recording identities during
dispatch and enforcing reservations belong to later changes.

### Identity and `run_id` linkage

Job, action, and incident identifiers are generated at insert time and are
stable across reopen. No two records of the same kind share an identifier.
Records that belong to an existing plan run carry that run's `run_id`, so
supervision records link to the existing run schema without redefining it.
`run_id` is stored as plain column data — a link, not a foreign key — because
the JSON run state owns run identity and the ledger must not couple its
lifecycle to JSON files.

## Trusted location

The ledger lives in external, service-owned storage outside the repository and
any writable worktree. The trusted-location rule is evaluated on canonicalized
paths:

1. Expand `~`, absolutize, and resolve symlinks and `..` spellings.
2. Walk the candidate path and every ancestor. If any ancestor is a
   `.opsx-plan/` directory, refuse.
3. Walk the candidate path and every ancestor. If any ancestor contains a
   `.git` entry (a worktree or repository root), refuse.

Refusal raises a named `TrustedLocationError` before anything is created: no
ledger file, WAL file, or directory appears at the rejected path.

**Consistent with the isolation boundary.** Canonical path exclusion catches
symlink and relative-path spellings, which are the realistic accident cases.
A determined process can still alias the repository through a mount namespace
or bind mount. The principal-level boundary — running supervision components
under an isolated OS principal that cannot write the worktree — is owned by
the later authority-boundary change; this contract fixes the path semantics
that boundary enforces on top of, including the default location being
outside any worktree.

## Journal semantics

The journal records intent before side effects and reconciles evidence rather
than claiming exactly-once external effects.

1. **Intent before side effects.** Beginning an action commits an `intent` row
   in its own transaction *before* any side effect is attempted. If the
   process is interrupted after the intent and before any effect, the reopened
   ledger still contains the committed intent.
2. **Dispatch.** After the intent, a dispatch record is written
   transactionally, carrying the action id and its owning job (and, in later
   changes, session and process identity). The action moves to `dispatched`.
3. **Uncertainty.** When an outcome cannot be confirmed, the action is marked
   explicitly `uncertain`. Uncertainty is a first-class state, not a silence.
   Uncertainty is resolved only by evidence: an `uncertain` action cannot be
   marked complete or failed, replayed, or dispatched until a reconciling
   evidence row has been recorded. Declaring an unconfirmed action `failed`
   is not a substitute for reconciliation.
4. **Evidence reconciliation.** Evidence rows are recorded against an action.
   Recording evidence for an `uncertain` action reconciles it in the same
   transaction, moving it to `reconciled`. Only then is the observed outcome
   applied: the action may be completed or failed according to what the
   evidence shows.
5. **Terminal states.** `completed` and `failed` are terminal. A terminal
   action is never dispatched or replayed, and it cannot transition again.
   Retries happen through a fresh action, not by resurrecting a terminal one.
6. **No exactly-once claim.** The journal never asserts that an external
   effect happened exactly once. Replaying a reconciled action is a fresh,
   observable dispatch; its effects must be deduplicated and re-observed
   rather than assumed absent. A lease-style claim was rejected because a
   lease cannot prove an external effect did not happen.

## Single supervised job per worktree

At most one active supervised job owns a worktree. Registering a second
supervised job for a worktree that already has an active job is refused with a
named `DuplicateJobError`, and the existing job is left unchanged. A new
registration becomes legal only once the prior job reaches a terminal state.

## Separation from execution state

The supervisor ledger is storage separate from the authoritative JSON
execution state under `.opsx-plan/`:

- The ledger is never stored under `.opsx-plan/` or anywhere else inside a
  writable worktree.
- Introducing the ledger does not change the JSON state's location, format,
  or read/write semantics.
- An ordinary, unsupervised run completes without opening, creating, or
  requiring the ledger.
- The JSON state remains the authority for phase progression; the ledger is
  additive supervision data.

## Ownership fields

Ownership is recorded on the job as the owner label plus, where the platform
provides them, the owner principal, host, and boot identity. These fields
fence which process currently owns the job and survive restarts. The broker
and process-singleton changes build on these fields; arbitration between
mutating processes (the worktree execution lock) is a later change, and the
ledger itself only provides transactional writes.
