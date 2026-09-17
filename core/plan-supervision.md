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
under an isolated OS principal that cannot write the worktree — is enforced on
top of these path semantics by the
[operator authority boundary](#operator-authority-boundary) below, including
the default location being outside any worktree.

## Operator authority boundary

The path rule above fixes *where* trusted assets may live; this section fixes
*who* can reach them. It is the boundary the trusted-location section defers
to, and it is enforced by the kernel, not by application-level conventions.

### Selected backend and trust root

The selected backend is **Linux isolated principals**. The local OS owner is
the trust root; isolation is ordinary Linux user isolation (filesystem
ownership and mode bits, peer credentials), with the Python standard library
only and no new runtime dependency. There is exactly one supported backend
today. A host that cannot provide it is reported unsupported and supervision
enablement fails closed; there is no lesser backend.

### Three principals, and the primary is a worker

Three OS identities participate:

- **Operator** — the human's own login uid. Only it may invoke the operator
  endpoint.
- **Service** — a dedicated, manually provisioned OS user (for example
  `opsx-supervisor`) that owns the installed service code, configuration, the
  supervisor ledger, the protected job policy, and the manifest snapshot, all
  with owner-only permissions.
- **Worker** — a dedicated, manually provisioned unprivileged OS user (for
  example `opsx-worker`) under which **every** model session runs, including
  the frontier primary. No model session is ever a privileged daemon.

The three identities must be pairwise distinct. A collapsed configuration (for
example a single-user host where all three would share a uid) is rejected by
detection: it cannot enforce the boundary, so it is treated as unsupported and
the gate refuses.

### Endpoint split and kernel authentication

The service exposes two Unix-domain sockets with **disjoint dispatch tables**:

- The **operator endpoint** accepts only peers whose kernel-reported uid is the
  configured operator principal; a mismatched peer is closed before any request
  is read. It is authenticated with `SO_PEERCRED`, not a token.
- The **worker-actions endpoint** serves only the scoped job-service verbs a
  worker may request. No operator-only verb is reachable through it, by
  construction rather than by a flag on a shared handler.

There is no bearer token, capability file, or environment variable that could
leak into a worker domain. Authentication is the kernel-reported peer identity;
nothing in the worker's environment, filesystem domain, or transport can invoke
the operator endpoint.

### Trusted assets and the untrusted repo

Service code, configuration, the ledger, the policy, and the manifest snapshot
live outside the worktree under the trusted-location semantics above, writable
only by the service principal. The editable repository copy is **untrusted
input**: the privileged service never loads or executes its privileged assets
from the writable checkout, and treats repository paths purely as data to hash
and snapshot. Repository hooks, tests, and repo commands execute in the worker
domain, never in the privileged service, so a repo-controlled script cannot
smuggle itself into the trusted identity.

### Capability detection, the probe, and fail-closed enablement

Detection (`lib/supervisor/authority.py`) is pure, unprivileged, and
side-effect free. It reports one of:

- `available` — Linux, peer credentials present, principals exist and are
  distinct, the store path resolves under the trusted-location rule, and the
  explicit **service-owned store-file contract** holds;
- `unprovisioned` — backend supported but prerequisites are missing, with the
  manual provisioning pointer;
- `unsupported` — the platform cannot provide the backend.

The authority store is an explicit **regular file**, never its parent
directory. The default is derived from the **service** principal's home
(`<service-home>/.local/share/opsx-controller/supervisor/supervisor.sqlite3`)
or, before that principal is provisioned, from the root-owned system directory
`/var/lib/opsx-controller/supervisor/supervisor.sqlite3`. It never follows the
invoking user's home, which a caller could make writable. An explicit
`OPSX_SUPERVISOR_STATE_FILE` overrides the default.

The target is **canonicalized** (expand `~`, absolutize, resolve symlinks and
`..`) *before* validation, so a symlink or relative spelling cannot conceal a
worktree or a worker-owned location from the trusted-location rule or the
ownership checks. Detection then requires the target to exist as a regular
file, be owned by the service principal, and carry a mode that denies the
worker principal write access. It additionally validates the whole mutable
**parent chain** up to the filesystem root: every ancestor directory must be
owned by the root trust root or the service principal and must deny the worker
a write, so a worker-writable directory cannot replace the protected store (or
a parent symlink) after the probe. Because ownership and mode bits do not
express a *named* POSIX ACL grant, the denial decision also inspects the
`system.posix_acl_access` xattr, decoding the real Linux wire layout (a single
little-endian `u32` version header followed by 8-byte `(tag, perm, id)`
records with the count implied by the payload length) and applying kernel
precedence with the mask filtering named-user/group and owning-group entries: a
named ACL entry that grants the worker
write is a failure, and an ACL that is present but unreadable or malformed
(bad version or a payload length that is not `4 + 8n`) fails closed rather than
falling back to the safe-looking mode bits. A missing
file, a directory target, wrong ownership, a worker-writable mode, an
ACL-granted worker write, or a worker-writable/non-service ancestor is
reported `unprovisioned` with the failing condition named.

Detection creates no accounts and writes nothing, so `opsx-plan supervise
status` works for any user on any host. Before supervision is enabled for the
first time, a **mandatory activation probe** spawns a real subprocess under the
worker principal and requires it to prove its execution before its write
attempt against the store file is accepted as denied with `EACCES`. The probe
reports the child's effective uid and the real `open` result as evidence; a
bare exit status is never proof. A successful write, a wrong or unproven
identity, an inability to spawn the restricted process, or any indeterminate
result is a named failure and supervision is not enabled.

The switch mechanism is part of the trusted base and is **authenticated**:
launchers are resolved only from a fixed list of trusted system directories
(never the ambient `PATH`), and the executable must be a regular executable
file owned by the root trust root or the service principal in a
non-worker-writable directory chain (ACL-aware, exactly like the store
parents). Allowlisted `setpriv`/`runuser` are recognised by basename; any
other basename is accepted only when it passes the same trusted-ownership
checks, which is how an explicitly provisioned absolute helper qualifies. A
bare name, a worker-owned helper, or an untrusted directory is refused. The
launcher path is **canonicalized before it is both checked and executed**: a
symlink is resolved once and only the canonical verified file is spawned, so a
worker that repoints the pre-canonical spelling after validation cannot
interpose a wrapper before the identity switch. The probe child runs with a
**scrubbed, non-inherited environment** (only a small allowlist such as
`PATH`/`LANG` survives, so `PYTHONPATH`, `LD_PRELOAD`, and shell startup hooks
are dropped) in Python **isolated mode** (`-I -S`, no `site`), so
`sitecustomize`/`usercustomize` hooks cannot inject code. Each invocation
carries a fresh **nonce** that the child must echo: pre-baked or replayed
evidence is rejected. There is no no-probe enablement route: fixture
simulation never substitutes for the real probe on an enablement path.

One function, `require_authority_backend(...)`, composes detection and the
probe into the single fail-closed gate. On an unavailable backend it raises the
named `UnsupportedHostError`; a failing probe raises the named
`ActivationProbeError`. It never provisions and never substitutes a weaker
posture. `opsx-plan supervise probe` exposes this gate, and later enablement
paths (job registration, service activation) call the same function.

Provisioning of the accounts and service is manual, an explicit operator step
the error output points to. The independent diagnostics (`doctor`, `status`,
`logs`, `report`) remain available on a host without the boundary, so legacy
unsupervised operation keeps its observability.

### Rejected alternatives

The plan's carried-forward semantics reject every application-level scheme
below as insufficient, because each is reachable by the very model processes it
is meant to exclude:

- **`--human` flag** — a flag is parsed by the same process that decides
  whether to honor it; a model session can pass it.
- **TTY check** — a model session can allocate or attach a pseudo-terminal, so
  a TTY is not an identity.
- **Token in the worker environment** — anything in the worker's environment is
  readable by the worker, so it authenticates the worker to itself.
- **Same-UID `chmod`** — permission bits are meaningless when the writer and
  the protected asset share a uid; the writer can simply change the bits.

A scheme a worker process can reach does not satisfy the boundary. Only a
distinct principal enforced by the kernel does.


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
   evidence row has been recorded and explicitly reconciled. Declaring an
   unconfirmed action `failed` is not a substitute for reconciliation.
4. **Evidence reconciliation.** Evidence rows are appended against an action
   without changing its state. After classifying decisive evidence, the caller
   explicitly reconciles an `uncertain` action, moving it to `reconciled`.
   Only then is the observed outcome applied: the action may be completed or
   failed according to what the evidence shows.
5. **Terminal states.** `completed` and `failed` are terminal. A terminal
   action is never dispatched or replayed, and it cannot transition again.
   Retries happen through a fresh action, not by resurrecting a terminal one.
6. **No exactly-once claim.** The journal never asserts that an external
   effect happened exactly once. Replaying a reconciled action is a fresh,
   observable dispatch; its effects must be deduplicated and re-observed
   rather than assumed absent. A lease-style claim was rejected because a
   lease cannot prove an external effect did not happen.

## Engine dispatch contract

The run engine's inner stage dispatch — create, implement, review, and archive,
including stage retries and `implementer_escalation` — is wrapped by one
concern-named dispatch boundary in `lib/orchestrator/journal_dispatch.py`. The
boundary reuses the existing run engine and stage invocation; it introduces no
new DAG or stage machine. An unregistered legacy run never enters the module.

### Pre-dispatch gate order

Every supervised action evaluates one boundary before any side effect, in a
fixed order. Each refusal raises a named gate error identifying the failing
gate:

1. **Execution lock.** The worktree execution lock is held by the dispatching
   process. The durable proof is the service-owned supervised-execution fence
   in the ledger (matching boot identity and process start time, so PID reuse
   cannot impersonate it) naming this process or an ancestor; a repo-writable
   lock record is consulted only to fail closed on a foreign live holder.
2. **Broker authority.** The broker's authority state permits the dispatch:
   relied-upon receipts are revalidated against the current material revision
   per action, not only at run start.
3. **Plan/policy freshness.** The manifest-snapshot hash and policy operator
   revision recorded when the gate opened are rechecked before each action. A
   changed policy revision or re-registered snapshot blocks with a named
   stale-material error; the job is never silently re-bound to new material.
   Only the material gate inputs are compared, so unrelated repository edits do
   not invalidate.
4. **Model policy.** `lib/supervisor/model_policy.py::check_dispatch` must
   allow the action's role against the job policy's pinned identity. A
   missing, unallowlisted, or identity-mismatched role blocks with its named
   reason; there is no cross-role fallback or inheritance.
5. **Budget reservation.** The action intent is committed, the reservation is
   written durably, and the dispatch record is inserted as the last step before
   the worker is spawned, so an undispatched action never accrues
   execution-elapsed time.

### Journal lifecycle and evidence

For each dispatch the boundary commits the action intent before any side
effect, writes a transactional dispatch record carrying the action id, owning
job, and worker identity, and then resolves the action to a terminal or
explicitly uncertain state. The fixed evidence-kind vocabulary is
`stage_result`, `usage`, `spawn_loss`, and `session_binding`; unknown kinds are
stored but never decisive.

A confirmed outcome records outcome and usage evidence and completes or fails
the action. An outcome that cannot be confirmed — a worker lost after spawn, a
timeout after dispatch, an ambiguous kill, or missing result evidence — marks
the action `uncertain` with whatever evidence exists. After decisive evidence
is recorded and classified, the caller explicitly reconciles the uncertain
action to `reconciled`; only then may it transition to a terminal state. A run
that resumes with an unreconciled
uncertain action surfaces it as blocking state instead of dispatching the next
action as though the uncertain one had succeeded or never happened. A stale
`dispatched` row found on resume is first classified as `uncertain` and enters
the same reconciliation path.

### Deduplicating, re-observant replay

Replay never assumes a prior attempt had no effect and never claims exactly-once
external effects. Before replaying an uncertain action the engine deduplicates:
a delivered result already recorded against the action reconciles it instead of
replaying. Otherwise it re-observes the external state the prior attempt may
have affected and replays only when that observation shows the work incomplete.
The recorded worker process identity is fenced before replay, so a recycled PID
or a still-live prior worker blocks a double spawn.

### Dual worker identity tracking

Both worker dispatch paths write to the same journal with the same lifecycle
and gating. The subprocess path records the spawned worker's process identity
(pid, process start time, and boot identity) on the dispatch record at spawn.
The native Task path records the session identity the worker reports through
the worker-actions endpoint, and journals a `session_binding` evidence entry.
A supervised dispatch therefore never has a null identity regardless of which
path carried it. The worker endpoint's `record_evidence` and `request_action`
verbs are backed by the ledger, remain inside the worker-domain authorization
boundary, and accept evidence only for actions of the job the worker is bound
to.

## Acceptance stage

A registered supervised job runs one additional stage between an implementation
review `pass` and archive. It is a stage in the existing run loop, dispatched
through the same journaled, gated boundary (`gated_dispatch`) under the job
policy's pinned `acceptance_reviewer` role and its installed
`opsx-acceptance-reviewer` agent. No new DAG, stage machine, or scheduler is
introduced: the implement/review/archive loop and its gates remain the
progression authority, and acceptance only gates advancement to archive. A
legacy unregistered run never enters the stage; a registered run whose adapter
ships no `acceptance_invoke` fails closed with a named error rather than
skipping it.

### Artifact review set and revision

The acceptance reviewer judges the change's **real artifacts**, never a worker
summary or transcript:

- the protected canonical plan manifest snapshot hash and its dependency edges;
- the change's authored artifacts — proposal, design, tasks;
- its spec deltas with their delta identity (delta operation plus requirement
  name, so a renamed requirement is a distinct identity from the one it
  replaced);
- the referenced canonical specs under `openspec/specs/`; and
- the tracked change diff.

The engine computes an **acceptance artifact revision** as a content hash over
that canonical, order-independent review set. The revision is deliberately
separate from the broker's `material_hash`: the material hash invalidates
approval receipts on gate-field changes, while the artifact revision
fingerprints the artifacts a verdict reviewed. An acceptance revision is never
the material gate hash, and a valid approval receipt never satisfies the
acceptance stage.

A verdict is recorded against the exact revision it reviewed. Before the service
records an `accept`, the engine recomputes the revision: a mismatch marks the
verdict stale, the stale verdict does not satisfy the stage, and a fresh
acceptance runs over the new revision (bounded by the change's round budget).

The engine also derives an **authoritative artifact-identity list** from the
captured review set — the protected manifest snapshot hash, every dependency
edge, and every file artifact — and hands it to the reviewer together with the
manifest/dependency ground truth (`ACCEPTANCE_ARTIFACTS`,
`ACCEPTANCE_MANIFEST_SNAPSHOT_HASH`, `ACCEPTANCE_DEPENDS_ON`). An `accept` is
valid only when its `artifacts_reviewed` names exactly that authoritative set:
a partial, arbitrary, or manifest/dependency-omitting accept is a contract
violation, fails the change with a named `acceptance_invalid` error, and never
reaches the ledger or archive. The recorded verdict row therefore reflects an
acknowledgment of the complete canonical review set, not an unverifiable worker
claim.

### Created-change check at the reviewed revision

At the start of each acceptance attempt, before the reviewer is dispatched, the
change's configured created-change check (`groundtruth.verify_change_created`,
`openspec validate <change> --strict` by default) runs and the artifact
revision is captured immediately after it, so the verdict binds to content that
passed validation at the moment of review. A failing or timed-out check blocks
the stage with the recorded reason and no `accept` is recorded.

### Outcomes

The reviewer returns exactly one of three outcomes:

- **`accept`** — the change satisfies its accepted intent, with the reviewed
  artifact set named as exactly the authoritative artifact-identity list the
  engine derived from the captured review set (manifest snapshot hash,
  dependency edges, and every file artifact). The loop advances to archive only
  on a non-stale accept that acknowledges that complete set.
- **`fix`** — a mechanical defect, named precisely enough for the job's pinned
  cheap `fixer` to repair, with a `fix_prompt` carrying the defect and the check
  that must pass. The engine dispatches the `fixer` role, then the independent
  `verifier` role; the repair is consumed only when the verifier's verdict
  passes on the actual diff (`repair_verified` and `diff_reviewed` true) in a
  session distinct from the fixer's, recorded under the existing
  repair-consumability contract. A verified repair recomputes the artifact
  revision and runs a fresh acceptance. The route is bounded by the change's
  existing round budget, and exhaustion fails the change with a reason naming
  the unrepaired defect.
- **`escalate`** — a hard judgment returned to the primary session rather than
  decided by a subagent. The escalation is recorded as unresolved blocking
  state; the engine never defaults it to `accept` or `fix`, and the change does
  not advance to archive while it is unresolved. Only the primary resolves it
  (`resolve_acceptance_escalation`), after which a fresh acceptance runs.

### Acceptance is a review outcome, not an approval authority

An acceptance verdict only gates advancement from review to archive. It never
releases a `pause_before` / `pause_before_human_only` gate, never satisfies the
operator `acceptance` receipt for an orchestrator-created change, never replaces
the implementation review verdict or its findings, and never marks a task
complete or waives the implement/review/archive task-completeness gates. An
unchecked automatable task remains blocking regardless of any acceptance
verdict, and a repair consumed by the acceptance fix route is validated by the
independent verifier rather than accepted on the fixer's own account.

Verdicts live in the append-only `acceptance_reviews` ledger table (job, change,
outcome, artifact revision, reviewed artifact set, reason, fix prompt, dispatch
action id, session id, created-check evidence, created at), added by the
forward-only v6 → v7 migration. The per-change JSON `acceptance` posture is a
projection of that ledger state for the operator, never an authority, and it
surfaces a stale or unresolved verdict rather than hiding it.

The durable writes fail closed. A verdict is authoritative only once its
`acceptance_reviews` row exists: if that write fails, the verdict does not
satisfy the stage, no `fix`/`escalate` transition is driven, the change is
failed with a named persistence error, and archive is never reached. Likewise, a
verified repair is authoritative only once its independent-verifier evidence row
exists: if that write fails, the repair is not consumed and no fresh acceptance
runs. Both failures are recorded as a named `persistence_error` in the
`acceptance` projection instead of being logged and advanced past.

## Single supervised job per worktree

At most one active supervised job owns a worktree. Registering a second
supervised job for a worktree that already has an active job is refused with a
named `DuplicateJobError`, and the existing job is left unchanged. A new
registration becomes legal only once the prior job reaches a terminal state.

## Supervised job lifecycle

One stdlib-only module (`lib/supervisor/lifecycle.py`) owns the state machine,
its transition guards, and the named failure family; the operator endpoint
verbs and the `opsx-plan supervise` CLI handlers are thin adapters over it, so
the mediated path and the trust-root bootstrap path can never diverge.

### Registration record

Registration (`opsx-plan supervise register`) runs as the trust root, because
the endpoint host is job-scoped and no endpoint can exist before a job does.
It validates the isolation backend through the fail-closed authority gate
(`require_authority_backend`, which includes the mandatory activation probe)
before writing anything, and then records, in one durable transaction in the
service-owned ledger:

- the job row: repository root, repository-relative worktree, owner identity;
- the protected job policy at operator revision 1: the standing permissions
  (authority configuration), the frozen model selection and inexpensive
  allowlist, and the budgets and deadlines;
- the protected manifest snapshot captured from the plan's canonical manifest
  content, with its hash derived by the ledger from that content; and
- the primary-session linkage configuration.

Registration fails closed with the named unsupported-host error and records
nothing on a host without a supported backend. Nothing is written to the
worktree or to JSON execution state. A second active job for the same worktree
is refused with `DuplicateJobError`.

### State machine and transition guards

```
registered -> active -> (paused -> active)* -> completed | failed | cancelled
```

`completed`, `failed`, and `cancelled` are terminal. Every transition is a
durable ledger transaction; a refused transition changes nothing.

| Verb | Legal source | Effect |
| --- | --- | --- |
| `register` | — | records the job in `registered` |
| `start` | `registered` | `active`; brings up the service-owned execution and primary session and drives the existing run engine |
| `resume` | `paused` | `active` only after resume revalidation confirms every relied-upon receipt still matches the current material revision |
| `pause` | `active` | durable stop request; in-flight actions are marked `uncertain`; `paused` |
| `drain` | `active` | durable stop request; no new dispatch; in-flight actions reach a terminal outcome, then `paused` |
| `cancel` | any non-terminal | `cancelled`; terminal effects below |
| `inspect` | any | read-only projection; no transition |

A target outside the legal set raises the named illegal-transition error; a
mutating verb against a terminal job raises the named terminal-job error.
Neither refusal alters the job record, and a terminal job refuses every later
receipt, stop request, and lifecycle verb.

### Pause and drain: stop boundaries

`pause` and `drain` each record a **durable stop request**: a job-scoped
receipt (kind `pause` or `drain`) that participates in the existing
receipt-driven wake-up, plus an open `stop` wait row. Both are written without
acquiring the worktree execution lock, so a request is legal while the job
waits on a human-only gate. Because the request is ledger state, a job that
restarts between the request and its observance still honors it at the
dispatch boundary before any new action is dispatched.

The boundaries differ only in in-flight disposition. `pause` interrupts
in-flight actions and marks them `uncertain` for evidence reconciliation, then
enters `paused`. `drain` forbids new dispatch but lets in-flight actions reach
a terminal outcome, entering `paused` only afterward. A job that is still
`active` under a drain hold reports `draining` and refuses dispatch; once the
in-flight set is terminal, the boundary records `paused`.

### Cancellation

`cancel` records the `cancelled` terminal state in one durable transaction. An
action that holds only an intent (no side effect) is failed with a cancellation
reason; a dispatched action whose outcome cannot be confirmed is marked
`uncertain` for reconciliation; an already-uncertain action is left for
evidence. Cancellation ends open waits and terminates ownership, so the partial
unique index frees the worktree for a new registration. No transition out of
`cancelled` exists.

### Human wait

When the run engine reaches an unsatisfied human-only gate, the wait is
recorded as normal durable job state: an open `human` wait carrying the
awaiting checkpoint, the material revision, and the start. The job keeps
permanent ownership without holding the execution lock. There is no LLM polling
and no stall recovery for a human wait: the job dispatches no model action
while the wait lasts, and wake-up is through the durable receipt scan. A gate
that becomes dispatchable (an approval receipt arrives and still matches the
material revision) ends the wait; resume after the wait revalidates receipts
before dispatch. A delegated gate is not a human wait: its scoped service
action resolves it.

### Completion from evidence

A supervised job reaches `completed` only when every enabled change classifies
done from the existing ground truth — archive evidence per change
(`verify_direct_archive_done`), the post-archive fast checks
(`groundtruth.run_fast_checks`), and post-archive cleanliness
(`delivery.verify_post_archive_clean`). A worker or primary session's claim of
done never completes the job; without that evidence the change continues
through the existing implement/review/archive loop, and a repeated failure is
bounded by the change's existing round budget. Because a valid archive
removes the active change directory, a supervised requeue first reactivates
the archived change at its active `openspec/changes/<id>` location — the
prior archive stays auditable through any `archive(<id>):` commit and a
`reactivated` history entry carrying the source archive path and failure
reason — and a requeue whose archived artifacts are unrecoverable fails
closed instead of requeuing a change the loop cannot resolve. Reactivation
never trusts the recorded archive path to select the move source: only the
canonical dated archive directory for the change — derived independently via
`groundtruth.find_archive_dir` and confined beneath
`openspec/changes/archive` — may be moved back, and a recorded path that is
absolute, traversing, mismatched, or symlink-escaping is rejected before any
filesystem mutation. Non-supervised
runs determine completion exactly as before.

Pending `(manual)` tasks are collected via `state.pending_manual_tasks`,
attached to the completion record, and shown on the `inspect` surface as the
operator checklist. They never mark the change, job, or run incomplete or
failed, and the implement/review/archive task-completeness gates are
unchanged.

## Worktree execution lock

Permanent job ownership (the ledger's ownership fields and single-job
invariant above) is distinct from the **worktree execution lock**. Ownership
is durable ledger state: registering, owning, pausing, or completing a job
changes ledger records, never a lock file, and releasing the execution lock
never releases or alters ownership. The execution lock is ephemeral
arbitration held only while a mutating command executes. A supervised job
waiting on a human approval retains its permanent ownership while holding no
execution lock, so the wait cannot block an approval.

The lock is two files under `.opsx-plan/` in the worktree:

- `.opsx-plan/execution.lock` — a dedicated, never-renamed inode the holder
  `flock(LOCK_EX)`s for the command's lifetime. The kernel releases it when
  the holder dies, so a free flock is the necessary arbitration for a new
  acquirer; but a released flock is not by itself proof of quiescence, because
  the prior owner may have released or inherited the descriptor away while its
  process is still alive. The recorded identity must also be quiesced before
  takeover.
- `.opsx-plan/execution-lock.json` — the fencing record, written with atomic
  temp-file-then-rename. It carries the owner label, owner kind
  (`ordinary`/`supervised`), optional job id, process identity, host,
  acquisition timestamp, and a `state` that is `held` while the owner holds
  the lock and rewritten to `released` (identity preserved) *before* the
  kernel lock is released.

Splitting the two files matters: renaming a record over a flock'ed path would
hand later openers a different inode and break mutual exclusion. With a
stable lock inode, `flock` arbitrates contention while the fencing record
carries the identity used to decide whether a prior holder is quiesced.

### Mutual exclusion and acquisition set

At most one mutating process per worktree holds the execution lock at any
time. A contended acquirer is refused fail-fast with a named
`LockContentionError` (or `SupervisedOwnershipError` when the recorded holder
is supervised) rather than waiting or proceeding unlocked. The acquisition
set is `run`, `reset`, and the single-change handler shared by `opsx-run` and
its alias `opsx-plan run-one`; a command exposed under multiple names acquires
the same lock. Future supervised mutating paths (such as supervised recovery)
acquire the same lock when introduced. Approval/acceptance receipts and the
read-only diagnostics (`doctor`, `status`, `logs`, `report`, `dashboard`)
never acquire it.

### Fencing identity, not a bare PID

The record carries the owning process id, its process start time
(`/proc/<pid>/stat` field 22), and the host boot identity
(`/proc/sys/kernel/random/boot_id`). A bare PID is **never** proof of
ownership or liveness. A record from a different boot is always stale. On the
current boot, a holder is live while **both** identity discriminators match a
live process: the recorded boot identity equals the current boot identity and
the recorded start time equals the observed start time for that PID. That
liveness is independent of whether the kernel-held lock is still held. A reused
PID with a different start time never matches. When the platform exposes no
boot identity or no process start time, identity liveness cannot be
established and the kernel-held flock is the only arbitration; a bare PID is
still never proof of ownership or liveness.

### Quiesced-verified takeover

Takeover of a stale lock happens only after the acquirer verifies the
previous worker is quiesced: the kernel-held flock is no longer held **and** no
live process matches the recorded identity. Takeover of a live owner is
refused with a named error and the owner's work is not interrupted, including
when the flock has already been released but the recorded identity is still
live. Every takeover is recorded so the fencing history shows which owner was
fenced and which replaced it. A takeover is classified as a fencing **only**
when the prior record was not cleanly released (`state == "held"`): a clean
release rewrites the record to `released` before the kernel lock is released,
and a subsequent acquisition of a cleanly released or absent record records an
ordinary acquisition rather than a fencing.

### Fencing persistence and receipt exclusion

For a registered supervised job, every acquisition, release, and takeover is
persisted in the supervisor ledger as an insert-only fencing record against
that job, carrying the process identity and boot identity, so a later
reconstitution can reconstruct who last held the worktree. Fencing records
describe *executions* and never reassign ownership, which stays on the job
row. An ordinary, unsupervised run creates, opens, and requires none of these
records: its lock acquisition is operable without the ledger and carries no
backend dependency.

Approval, acceptance, and pause/steer receipts never require acquiring or
waiting for the execution lock. For supervised jobs these receipts are (or, in
the broker change, become) durable broker database transactions with a
durable wake-up for the owning job, so a human wait that retains permanent
ownership never blocks an approval, and a receipt recorded while another
process holds the lock is not lost.

## Broker: sole approval authority

For a registered supervised job, the **broker** (`lib/supervisor/broker.py` in
the trusted authority domain) is the sole authority that releases approval and
acceptance gates. Direct mutation of the JSON execution state, the plan
manifest, or any repo-writable file releases no gate, satisfies no checkpoint,
and alters no supervised identity. Within a registered job, `approve`,
`approve --all`, `approve P<N>`, `accept`, `reset`, `run`, `run-one`, and
`opsx-run` are broker mediated: each is recorded as a durable broker
transaction or refused, and execution decisions consult broker state rather
than unmediated JSON writes. For registered jobs the JSON execution state is a
**projection** of broker and ledger state, not a competing phase authority.

Unregistered legacy jobs keep their existing JSON handling with no dependency
on the broker, the supervisor ledger, or any supervision backend.

### Receipts bind to a checkpoint and material revision

A receipt is an append-only ledger row (`receipts`) recording who released (or
requested) a checkpoint against which material revision. Receipt kinds are
`approval`, `acceptance`, `reset`, `pause`, and `steer`; the recording
authority is `operator`, `delegated`, or `service`.

Every approval or acceptance receipt binds to:

- the exact **checkpoint** — the specific gated change and gate kind; and
- the **material revision** `H(change_id, gate_fields, snapshot_hash,
  policy_revision)`, where `gate_fields` is the minimal gate-relevant subset of
  the change's manifest entry (phase, `pause_before`,
  `pause_before_human_only`, `review_created`, and the declared dependencies)
  read from the **protected manifest snapshot**, `snapshot_hash` is the
  policy's recorded snapshot identity, and `policy_revision` is the current
  insert-only policy revision.

Unrelated updates — task progress, telemetry, other changes' state, or
non-gate manifest fields — do not invalidate a receipt. A receipt recorded
against a different material revision does not satisfy the gate. Plan and
policy revisions are explicit: the material revision changes only when an
operator registers a new protected snapshot or records a new explicit policy
revision, never as a side effect of worker writes to the repository.

### Authority resolution

`pause_before_human_only` resolves at runtime as the manifest loader defines
it: an absent key on a gated change is human-only, an explicit `false`
delegates to the supervised job's policy-bound authority, and an explicit
`true` is human-only.

- A **human-only** gate is released only by an operator approval receipt
  recorded through the OS-authenticated operator path (kernel
  `SO_PEERCRED` peer identity). The worker-actions endpoint exposes no
  approval-family verb, so a worker cannot reach one by construction.
- A **delegated** gate is released only by the scoped job service action
  (`release_delegated_gate`), accepted by the broker only when the change's
  resolved authority is delegated and the requesting identity is the job's
  registered service identity.

An operator approval of a delegated gate and a scoped service release of a
human-only gate are both refused with the named `BrokerMediationError`.

### Protected snapshot and external registration anchor

A registered supervised job is anchored by two service-owned records held
outside the worktree: the external registration record (the ledger job and its
protected insert-only job policy) and a **protected manifest snapshot**
(`manifest_snapshots`) capturing the manifest content the job was registered
with. Registration writes the snapshot content and its content-addressed
`snapshot_hash` in the same transaction as the job and policy revision 1, so a
registered job always has protected content to evaluate gates against and a
registered job without stored snapshot content is invalid. The policy's
`manifest_snapshot_hash` is always the content's hash, so the two can never
disagree.

Gate and mediation decisions are evaluated from these protected records, never
from repo-writable copies. Registration detection reuses the service-owned
ledger lookup by worktree (the same signal the supervised budget gate uses),
never JSON markers or the repo plan. A worker that edits the JSON execution
state or the repo plan to drop supervised fields does not escape active
registration: the job remains registered and broker mediated until the
registration record itself reaches a terminal state through an authorized
path.

**Store substitution fails closed.** Registration detection does not trust a
worker-selectable path. The authority-validated service-owned store — derived
from the service principal's home (or the root-owned `/var/lib` directory),
never the invoking user's home — is always consulted, so repointing
`OPSX_SUPERVISOR_STATE_FILE` at an empty or missing location cannot hide the
real registered job. An explicitly configured store that does not exist raises
the named `BrokerUnavailableError` rather than returning "unregistered"; a
store at an untrusted location, or one that exists but is unreadable, likewise
fails closed. The legacy unmediated path is reached only when supervision is
genuinely not provisioned (no candidate store exists at all).

**Batch and phase selection come from the snapshot.** For a registered job,
`approve --all`, `approve P<N>`, `accept --all`, `accept <id>`, and
`reset --failed` resolve change membership, order, phase values, and the
`review_created` flag from the protected snapshot, never the repo-writable
plan. A worker that rewrites the plan's phases or order cannot redirect a
`P<N>` batch or hide a gated change from `--all`.

**Dispatch authorization is unforgeable.** A registered job dispatches only
inside the supervised execution the trusted service actually started. The
authorization is not an environment variable (a worker controls its
environment and can set `OPSX_SUPERVISED_EXECUTION=1`), not a repo-writable
lock record, and not an in-process marker: the control plane exposes no
process-local flag, context manager, or importable helper that grants
dispatch. It requires the job's **service-owned ledger fencing record** (the
`fencing_records` table, not the repo-writable `.opsx-plan` fencing file) to
name a currently-live supervised execution — matching boot identity and
process start time, so PID reuse cannot impersonate it, and not since
released or fenced — and requires the calling process to be that execution or
a descendant of it. A worker process therefore cannot self-authorize by
exporting a marker, by forging a repo-writable lock record, or by assigning
the control plane's own module attributes; an unprovable or stale fence fails
closed with the named `BrokerMediationError`, and an unreadable service store
fails closed with `BrokerUnavailableError`.

### Resume revalidation

Before a supervised job resumes dispatch after a restart, a pause, or a human
wait, the broker revalidates the material gate inputs for every gate the job
believes is satisfied: each relied-upon receipt is matched against the current
material revision, and any gate whose receipt no longer matches returns to
awaiting its defined authority. The stale gate raises the named
`StaleMaterialError` into the job's incident flow (recorded as a durable
`stale_material` incident) instead of dispatching; a resume with every
relied-upon receipt still matching proceeds using those receipts.

### Bounded resets

Within a registered supervised job a reset occurs only through an authorized
path: an operator reset recorded through the operator OS-authenticated path, or
a bounded service reset within the job's policy limits (an explicit
`max_service_resets` policy bound). Authorized resets are recorded as durable
`reset` receipts so the reset history is auditable. A reset attempted directly
by a worker-domain process — through `opsx-plan reset`, `opsx-run`, or JSON
mutation — is refused with the named `BrokerMediationError` and alters no
broker or ledger state. A broker reset never acquires the worktree execution
lock.

### Durable wake-up

The receipt append is the durable record. The supervised execution tracks a
per-job receipt high-water id and, on boot or wake, scans `id > high_water`
(`ledger.receipts_after`). An in-process/same-host notify may trigger an
immediate scan for liveness, but the scan is the authority, so a restart never
loses a receipt. No receipt path acquires or waits for the execution lock.

### Service projection writer is installed before requests are served

Every broker receipt transaction must regenerate the JSON projection from
broker and ledger state, so the trusted service installs exactly one
projection writer
(`lib.orchestrator.supervision.install_projection_writer`) at boot, before it
accepts any operator or worker endpoint request. The production call site is
the service-side endpoint host (`opsx-plan supervise serve`), which boots the
broker session — opening the service-owned ledger, identifying the worktree's
active nonterminal registration, and installing and retaining the writer — and
only then binds the endpoint sockets. Every session is bound to that
registration: an explicit `--job-id` is accepted only when it *is* the
worktree's active job, so a foreign (another worktree's) or terminal job id is
rejected as a registration mismatch before the writer is installed. There is
deliberately no per-request or per-call callback: the broker consults the
installed writer itself, and a missing writer fails closed with
`BrokerUnavailableError` recording nothing, so a recorded receipt can never
silently leave the projection stale. A provisioning failure (missing store,
unregistered worktree, unresolvable principal, foreign or terminal explicit job
id, or an unbindable socket) fails the service closed before any socket is bound
rather than serving authority records whose projection cannot be regenerated;
socket provisioning failures are translated to the named `BrokerUnavailableError`
and tear the partially booted session down instead of escaping as a raw socket
error.


## Separation from execution state

The supervisor ledger is storage separate from the authoritative JSON
execution state under `.opsx-plan/`:

- The ledger is never stored under `.opsx-plan/` or anywhere else inside a
  writable worktree.
- Introducing the ledger does not change the JSON state's location, format,
  or read/write semantics for unregistered jobs.
- An ordinary, unsupervised run completes without opening, creating, or
  requiring the ledger.
- For unregistered jobs the JSON state remains the authority for phase
  progression; the ledger is additive supervision data. For a registered
  supervised job the broker and ledger are the phase authority, and the JSON
  state is a projection of broker and ledger state: a direct JSON write
  releases no gate, satisfies no checkpoint, and alters no supervised
  identity, and any direct JSON edit that disagrees with the broker records
  has no authority.

## Ownership fields

Ownership is recorded on the job as the owner label plus, where the platform
provides them, the owner principal, host, and boot identity. These fields
fence which process currently owns the job and survive restarts. Permanent job
ownership is distinct from the ephemeral
[worktree execution lock](#worktree-execution-lock), which is held only while
a mutating command executes and is recorded separately (in the lock's fencing
record and, for supervised jobs, as ledger fencing records). The ledger itself
only provides transactional writes.

## Budget contract

The protected job policy's `budgets` and `deadlines` fields carry the
versioned supervision-budget schema owned by `lib/supervisor/budgets.py`. Both
payloads share one nested `BUDGET_SCHEMA_VERSION = 1`, which is
**independent** of the outer ledger `policy_version` column and of
`MODEL_POLICY_VERSION`. Reads are forward-only: a recorded nested version newer
than the code supports raises a named `BudgetVersionError` rather than silently
interpreting unknown fields.

`budgets` has the shape `{"version": 1, "total_cost_usd": <number|null>,
"per_action_cost_usd": <number|null>, "total_elapsed_minutes": <number|null>,
"per_action_elapsed_minutes": <number|null>, "max_incident_attempts":
<integer|null>}`. A null value disables that limit; at least one limit must be
non-null for a supervised job. `deadlines` has the shape `{"version": 1,
"execution_deadline_minutes": <number|null>}` and carries execution-time limits
only: human-wait duration is governed by the deadline-separation rule below and
never appears here as a wait budget.

**Fail-closed classification.** A stored value with no `version` key is
classified `legacy_unversioned`, returned unmodified, and never reinterpreted
as a current payload. A consumer treats it as carrying no enforceable limits
and fails closed for supervised dispatch rather than defaulting to unlimited
spend; replacing it requires an explicit operator revision recording a
versioned payload. The ledger validates both payloads through the budget module
on write and decodes them through it on read, exposing the classification as
`budget_policy_state`.

### Reserve-before-dispatch and reconciliation

Every supervised model call — the `supervisor` primary, `implementer` /
`reviewer` / `archiver` workers, the `supervised_author` create stage,
`acceptance_reviewer` / `fixer` / `verifier` auxiliaries, every retry, and
`implementer_escalation` — is accounted against the job's budgets. The
`supervisor` role is exempt from the inexpensive allowlist but is **not**
exempt from budget counting.

Each dispatch reserves budget before any side effect: the reservation row is
committed in the supervisor ledger with the action identity, role, requested
model, and estimated amounts before the dispatch proceeds. A reservation that
cannot be durably recorded blocks the dispatch rather than running
unaccounted. After the dispatch, observed usage is reconciled against the same
reservation, replacing the estimate with the observed amounts; the reservation
and its reconciliation are one accounting entry per action, so a duplicate
completion or usage record is deduplicated and never billed twice. A
reservation whose outcome is `unknown` or `interrupted` is classified
`retained` at its reserved estimate rather than released, so unresolved
consumption is never treated as free. The `reservations` and
`incident_attempts` tables are added by a strictly additive, head-chained
forward-only migration.

### Headroom honesty for hard limits

The reservation estimate is `rate(pinned model) x token-cap envelope x headroom
margin`, computed at the orchestrator boundary from the pricing catalog using
the exact model identifier pinned for the dispatch's role (no cross-role or
ambient fallback), and recorded per-reservation with the catalog version.
Because observed usage is known only after dispatch, a hard USD limit is
enforced with this stated headroom: **overshoot beyond a hard limit is bounded
by at most one in-flight action's headroom**. The contract makes no claim of an
exact, immediately enforced real-time ceiling.

### Execution deadline versus human wait

Deadline accounting tracks execution-elapsed time separately from human-wait
duration. Execution elapsed is accumulated from ledger dispatch intervals
(`dispatches.dispatched_at` to completion); a human wait is durable job state
and is **not** a dispatch interval, so it is excluded by construction rather
than subtracted after the fact. Only active execution consumes
`execution_deadline_minutes` and the elapsed budgets. On resume after a wait,
accounting continues from the pre-wait accumulated value.

### Incident attempt signatures and bounded attempts

Each incident is recorded against a stable signature
`sha256(kind | change_id | stage | failure discriminator)`, which identifies
"the same incident" without embedding volatile detail. `record_incident_attempt`
increments the durable `(job_id, signature)` count and returns it; the gate
refuses a further identical attempt with `BoundedAttemptsExceededError` once
the policy's `max_incident_attempts` is reached. No incident-repair policy is
decided here.

### Reset survival and operator-only increases

All budget state — reservations, reconciled consumption, retained estimates,
and attempt counts — lives in the durable supervisor ledger in trusted external
storage, never in the worktree JSON execution state. An `opsx-plan reset` does
not erase, reduce, or refresh a supervised job's accounted consumption, and
does not clear an attempt bound.

Budget and deadline values change only through an explicit operator policy
revision under the ledger's existing revision-increment rule. No worker,
agent, reset, incident-recovery, or exhaustion path may raise, clear, or
re-baseline a limit, and an increase applies only to subsequent reservations
because previously reconciled or retained consumption is never rewritten.

### Bounded backoff and terminal human blockers

Transient budget-gate failures — such as a temporarily unwritable ledger or a
retryable pricing-catalog load failure — are retried with bounded exponential
backoff (base 0.5 s, cap 8 s, max 3 attempts), each retry recorded. Exhausting
the bound surfaces a blocked state with its named reason rather than retrying
indefinitely. A genuine human blocker — an exhausted budget awaiting an
operator increase, a bounded-attempts refusal, or a legacy-unversioned policy —
is a terminal, actionable state naming the required operator action, and is
never retried or self-repaired.

### Supervised gates and legacy runs

Budget enforcement applies only to worktrees with a registered supervised job.
For such a job, the run path reserves before each stage dispatch and reconciles
after it; the legacy `--budget-minutes` / `--budget-usd` gates keep their exact
behavior for unregistered runs, which create, open, and require no durable
budget layer. When a supervision backend is present but its ledger or policy
cannot be read, the run fails closed: it blocks before dispatch rather than
falling back to the unbudgeted legacy path, so a registered job is never
dispatched without a reservation.

## OpenCode session bridge

The supervised primary session is driven through a documented, versioned
session bridge implemented in `lib/supervisor/session_bridge.py`. The bridge
belongs to the `lib/supervisor/` runtime package, so it is standard-library
only, imports no other runtime package, and is importable without side effects.
The transport is stdlib `http.client` over **loopback only**: a non-loopback
server address is refused rather than dialed.

### Documented API subset and result-schema

The bridge depends on exactly five operations against a headless
`opencode serve` instance and on no other server surface:

| Operation | Server surface |
| --- | --- |
| version capability check | `GET /global/health` |
| create | `POST /session` |
| prompt | `POST /session/{session_id}/prompt_async` |
| lookup | `GET /session/{session_id}`, `GET /session/{session_id}/message` |
| abort | `POST /session/{session_id}/abort` |

The event channel (`GET /event`, `text/event-stream`) is consumed as a hint
source only. The typed **result-schema** a prompt returns is
`opencode-session-result` version `1`, derived from polled authoritative
message state — never from the streamed event channel. Its fields are:
`version`, `schema`, `session_id`, `marker`, `marker_found` (whether the
marker's user message was actually discovered — a requested marker is never
echoed back as if it were observed), `status`
(`pending`/`completed`/`error`/`aborted`), `terminal`, `message_id`,
`provider_id`, `model_id`, `text`, `usage` (`usage_available`, `input_tokens`,
`output_tokens`, `cached_input_tokens`, `reasoning_tokens`), `cost`
(`status`, `estimated_cost`), `duration_ms`, and `source` (`poll`). The
`usage`/`cost` shapes are the same ones the dispatch boundary reconciles, so
supervisor-primary usage crosses the existing budget boundary unchanged.

### Version capability check

Before any session operation the bridge queries `GET /global/health` and
compares the reported version against the pinned supported range
**`>=1.18.0 <1.19.0`**. Any mismatch — an older or newer version, an
unparseable or missing version, an unhealthy report, or an unreachable health
endpoint — fails closed with the named `UnsupportedVersionError` /
`UnreachableServerError` and no create, prompt, lookup, or abort request is
issued. A session operation requested before a successful check raises the
named `CapabilityNotCheckedError`. The range is pinned at implementation time
against the operator's installed server; extending it is an explicit,
verify-then-extend action, never a silent hope that the server matches.

### Service-owned session lifetime

For a registered supervised job the service owns the primary session's
lifetime. The headless server is launched in the **worker domain** through the
authenticated restricted-process launcher from the authority boundary, not
through a new endpoint verb, so the endpoint tables' no-execution contract is
intact and no model session — including the frontier primary — runs with
service-identity privileges. The server binds a job-specific loopback address,
and its process identity (pid, process start time, boot identity) is journaled
alongside the session identity so the existing fencing machinery can tell a
live server from a recycled PID.

On service restart the host first performs an **adopt-by-lookup**: it resolves
the job's recorded session identity through lookup against the recorded
server, and adopts that live session rather than spawning a replacement. A
replacement server and session are launched only when lookup shows the session
is gone. Either way the **primary session linkage** (server address + session
id + server process identity) is journaled at registration/first use, and an
operator's interactive chat starts or attaches to the same service-managed
session through that linkage instead of a divergent private one. The linkage is
recorded through the existing action-detail and `session_binding` evidence
surfaces: it is additive and requires no ledger schema migration.

### Identity-before-prompt journaling

Every bridge operation that can cause a model side effect is a journaled
supervised action, and it reuses the existing lifecycle rather than a parallel
one:

1. the action intent is committed in its own transaction **before any server
   request**, carrying a generated **request identity** in its detail;
2. a budget reservation is committed for the `supervisor` role;
3. a dispatch record is written carrying the session identity and the headless
   server's process identity;
4. the prompt request is issued, with the same request identity embedded in the
   prompt payload as a single machine-readable marker line
   (`OPSX-REQUEST-MARKER:<id>`), so the identity stays discoverable through
   lookup afterward;
5. a terminal outcome is recorded, or the action is marked explicitly
   `uncertain` with evidence.

At most one prompt per session is in flight; a second prompt is refused with
the named `PromptInFlightError` rather than queued or issued.

A **lost launch acknowledgement** is resolved by lookup, never by
re-prompting: the bridge polls the authoritative message list, matches the
recorded request marker, and reconciles the existing action — completing it,
failing it, or re-observing it as still in flight. Terminal reconciliation
targets that recorded marker: when the marker is absent from authoritative
state the lookup reports `pending` and selects no assistant reply, so an older,
unrelated completed turn is never reported as this request's outcome. Only
authoritative session disappearance is terminal without a matching marker. A
still-pending poll is an observation, not a resolution: while the prompt's
outcome is unresolved the action stays in the session's in-flight guard and no
second prompt may be issued, whether the marker was discovered
(marker-confirmed in-flight) or not observed at all. An unobservable prompt retains its reservation as unknown
consumption; a marker-confirmed in-flight prompt keeps its reservation for its
eventual observed usage. Only a positive terminal observation releases the
guard. A mutating request is issued
at most once: the transport never blind-retries a prompt, because a duplicate
prompt is exactly the failure the request identity exists to prevent.

### Agent contracts and the pre-prompt transport gate

Two fail-closed contracts run inside the intent window — after the action
intent is committed and **before** the dispatch record and any server request —
in `lib/supervisor/agent_contracts.py` (stdlib only; it imports only
`ledger`/`clock`-level modules, never `session_bridge` or `endpoints`, so the
package's acyclic graph holds).

**The session contract.** Each supervised policy role maps to exactly one
concrete installed agent, and each agent carries a least-privilege capability
allowlist:

| Role | Concrete agent | Capabilities |
| --- | --- | --- |
| `supervisor` | `opsx-supervisor` | `read`, `glob`, `grep`, plus the tracked service tool |
| `implementer` / `supervised_author` / escalation | `opsx-implementer` | `read`, `glob`, `grep`, `edit`, `bash` |
| `reviewer` | `opsx-reviewer` | `read`, `glob`, `grep`, `bash` |
| `archiver` | `opsx-archiver` | `read`, `glob`, `grep`, `edit`, `bash` |
| `acceptance_reviewer` | `opsx-acceptance-reviewer` | `read`, `glob`, `grep`, `bash` |
| `fixer` | `opsx-fixer` | `read`, `glob`, `grep`, `edit`, `bash` |
| `verifier` | `opsx-verifier` | `read`, `glob`, `grep`, `bash` |

`check_session_contract` is the pure predicate: an unregistered role, a
session whose observed agent is not the role's concrete agent (an unbound
agent counts as not matching), a role with no pin in the job policy, a
`requested_model` that is missing, malformed, or not the role's **exact** pin,
or any requested capability outside the role's allowlist is a violation —
never defaulted. `enforce_session_contract` adds the durable consequence: a
`policy_violation` incident is recorded against the job and the dispatch is
blocked before any side effect.

The contract is **non-optional** on the worker path. Every worker-domain request
must carry its role, its observed concrete agent, and the job's registered
service identity; `check_worker_identity` refuses a request that omits any of
them, and there is no unauthenticated legacy worker path on the endpoint. The
mandatory fields are wired at four points: the journaled bridge's session
create and prompt (the agent it binds must be the role's registered agent, and
the model must normalize to the role's exact pin — a missing or malformed model
is refused, never defaulted, while the service resolves its own pinned identity
from the policy for a primary session it creates), the worker-actions
endpoint request path (identity plus role contract), `build_request` in the
tracked service tool (an unidentified request is never framed), and the
per-role agent permission blocks.

Delegation is non-recursive by construction: `task` is denied on every
supervised agent. The primary's `bash` allows only the `opsx-supervise *`
pattern; each worker role's `bash` allows only the tracked shell wrapper
`opsx-worker-exec *`. The executable layer (`lib/supervisor/worker_exec.py`)
is a **fail-closed allowlist**, not a denylist. A denylist of dangerous
executables is unbounded — any program with a programmable or
configuration-mediated execution feature (`git -c alias.x='!cmd'`, a repo
hook or content filter, `make`, `awk system()`, `sed`'s `e` flag,
`find -exec`, `tar --to-command`, `ssh`, an execution-prefix wrapper such as
`nice`/`nohup`/`setsid`/`env -i`) can reach a shell or a model client without
naming it — so the single rule is the reverse: **a command executes only when
its leading executable is an explicitly enumerated safe executable AND its
argv satisfies that executable's form constraints; everything else is refused
before execution** and reported to the worker endpoint through the
`report_violation` verb as a durable `policy_violation` incident. The safe
surface is three shapes:

- **Single-purpose inspection tools** (`ls`, `cat`, `grep`, `head`, `diff`,
  `wc`, ...) whose argv is data, never a command — plus constrained variants
  whose exec-capable flags are refused (`find` without `-exec`/`-execdir`,
  `sort` without `--compress-program`, `rg` without `--pre`/`--hostname-bin`,
  with `RIPGREP_CONFIG_PATH` scrubbed so its config cannot re-open them).
- **Named non-interpreter check tools** the roles need (`openspec validate`
  and its siblings).
- **git, special-cased** because it is programmable through configuration:
  only built-in read-only subcommands (`status`, `diff`, `log`, `show`,
  `rev-parse`, `rev-list`, `ls-files`, `grep`, `shortlog`, `describe`,
  `show-ref`, `cat-file`) are allowlisted, so an alias or external `git-*`
  command can never be the subcommand (an alias cannot shadow a builtin) and
  hooks never run for the allowlisted builtins; worker-supplied global config
  options (`-c`, `--config-env`, `--exec-path`, `--git-dir`, `-C`, ...) are
  refused; flags that would re-enable an execution surface are refused
  (`cat-file --filters`, `--ext-diff`, `--textconv`); the wrapper injects
  config pins making the pager, external diff, textconv, the fsmonitor hook,
  credential helpers, SSH, and every signing helper inert; and a preflight
  refuses the command when the repository's effective config defines any
  external clean/smudge/process filter the pins do not already neutralize —
  that is worktree-controlled command execution, and it fails closed, as does
  a preflight that cannot complete.

Every allowed command runs with a scrubbed environment: inherited `GIT_*`
variables and interpreter/loader hooks (`NODE_OPTIONS`, `PYTHONPATH`,
`LD_PRELOAD`, ...) are stripped, and the wrapper pins its own inert values,
so environment-mediated execution cannot smuggle code into an allowed tool
either. The residual trust assumptions are stated in the module docstring:
the allowlist trusts the binaries it names (`openspec` is the project's own
check tool), and the wrapper's own environment is service-provisioned because
the permission pattern admits only the literal `opsx-worker-exec *` command
line.

**Worker-initiated delegation is journaled on both paths.** A native Task
session identity reported through `record_evidence` is bound to the owning
action's dispatch row as `session_binding` evidence; a worker-reported
subprocess process identity is bound the same way against the same action in
the same journal the orchestrator uses. A binding failure is surfaced rather
than swallowed, and delegation that cannot be journaled does not execute.

**The pre-prompt transport gate.** `evaluate_transport` is the pure decision:
model traffic is *enforced* when a trusted model gateway endpoint is configured
(`OPSX_MODEL_GATEWAY_ENDPOINT`), or when an equivalently enforced isolated
transport holds — the transport target is the **launched service-owned session
server**, or the dispatch is a service-owned spawn inside the isolated worker
domain **and** the worker environment is free of any reusable provider
credential. `check_server_identity` is what binds the isolated path to the
launched server, and its authority is a `LaunchedServerBinding` the service
captured at launch (the launched process's fenceable pid/start-time/boot
identity plus the address of the transport the service created). A bare loopback
hostname, a caller-supplied address, or a caller-supplied live process identity
is never accepted — any process on the host could bind a loopback port, and a
live foreign pid proves nothing. The binding's identity must name a live process
on the current boot, and any presented transport target must equal the binding's
reported address. `JournaledSessionBridge.enforce_prompt_contract` derives this
binding itself and refuses a caller-supplied `target`, `server_address`,
`server_identity`, or `server_binding` override rather than trusting it. A
provider credential in the worker environment is never enforced,
even with a gateway configured, because the credential is itself the bypass. `assert_pre_prompt_transport` raises the
named `EgressEnforcementError` before any prompt or spawn side effect when the
decision is unenforced, and records the decision as
`transport_decision` action evidence **before** the dispatch record when it is
enforced — so the journal shows enforcement before the side effect rather than a
usage observation afterward. `transport_decision` is outside the decisive
evidence vocabulary: recording it can never complete, fail, or reconcile an
action on its own.

Call sites are the two supervised choke points: `JournaledSessionBridge.prompt`
(before the dispatch record and the server request) and
`lib/orchestrator/journal_dispatch.gated_dispatch` for supervised stage-worker
spawns (the named egress gate, evaluated after the model-policy gate and before
the reserve/spawn, with the same fail-closed semantics). On failure the action
is failed with the gate reason and nothing needs unwinding, because no prompt or
subprocess exists yet.

**Verifier independence.** A `fixer` report never self-certifies completion:
`repair_consumable` requires an independent `verifier` verdict (`pass`) from a
*different* session that reviewed the actual diff (`diff_reviewed` and
`repair_verified` both true). A missing, contradicting, or same-session verdict
blocks the repair regardless of the fixer's report, and no repair verdict ever
checks a task or waives the implement/review/archive task-completeness gates.

`repair_consumable` is consumed in production, not merely exported:
`assert_repair_consumable` gates every repair-consuming transition — `resume`
and `dispatch` in `journal_dispatch` (`assert_repair_gate`, inside the authority
and full gate order), the operator `reset_change` endpoint, and the worker
`release_delegated_gate` endpoint. The gate reads the change's latest recorded
fixer report and verifier verdict from the journal when the caller does not
present them, so a caller cannot escape it by omission. The recorded
`repair_verdict` evidence is non-decisive: it never completes, fails, or
reconciles an action on its own, and a refused consumption records a durable
`policy_violation` before the transition's side effect.

### The tracked service tool

The supervised primary's only side-effecting capability is the tracked service
tool `opsx-supervise` (`lib/supervisor/service_tool.py`, deployed by the
OpenCode adapter as an executable alongside its other support files). It maps a
**closed** set of worker-actions verbs — `report_status`, `request_action`,
`record_evidence`, `heartbeat`, `release_delegated_gate`, `report_violation` —
one-to-one onto the worker endpoint through `lib/supervisor/broker_client.py`.
It resolves only the worker endpoint: an operator verb is unreachable through it
by construction, and journaling is structural rather than voluntary, because
every verb maps to an endpoint handler that writes the ledger before its effect.
Every request it frames carries the supervised role, the observed concrete
agent, and the registered service identity; a payload cannot override those
fields, and an unidentified request is refused before it is framed. The
`opsx-supervisor` agent's `bash` permission allows only the `opsx-supervise *`
invocation pattern, and each worker role's allows only `opsx-worker-exec *`
(`lib/supervisor/worker_exec.py`), so arbitrary Bash and Task dispatch are
denied while the single journaled path and the tracked shell wrapper remain
available.

### Events-as-hints semantics

The streamed session event channel is **hints only**. No event is ever
recorded as an outcome; a hint at most schedules or accelerates an
authoritative poll and may be journaled as `session_hint` evidence against the
action it reconciles. `session_hint` is outside the decisive evidence
vocabulary, so it is stored but never decisive: it can neither complete, fail,
nor reconcile an action. An event matching no journaled action is an orphan:
at most an observation, never an outcome.

The authoritative **poll loop** — bounded-backoff polling of session and
message state, reusing the budget module's capped exponential backoff and
bounded attempts — is the sole path to terminal results, usage figures, and the
typed result-schema. Lost, duplicate, out-of-order, and no-replay event cases
all converge on that same poll:

- **duplicates and out-of-order arrivals** are recognized by message/part
  identity before any evidence or usage is recorded, so nothing is
  double-applied or double-billed;
- **stream loss and reconnect gaps** converge by polling: the bridge never
  requests event replay and never assumes the contents of missed events.

### Briefing composition and bounding rule

A reconnect or replacement briefing is composed from durable state only: the
journaled job record and protected plan snapshot reference, active and
unreconciled uncertain actions, budget and reservation state, incident history
including previous failed remedies, and — where a single change is registered —
the broker's gate resolution. A transcript replay is never the briefing; the
adopted session's server-side transcript is not trusted as the sole context
source.

The briefing is explicitly **bounded**. Blocking items — unreconciled uncertain
actions and pending gates — are always retained **in full** and are rendered as
blocking state, never summarized away. Overflow is shed from the **oldest
non-blocking detail first**. An adopted session receives a bounded **re-brief**
(2000 chars); a replacement session receives the full bounded briefing (6000
chars).

The composed text is delivered to the managed session through the same
journaled bridge as any other primary prompt: it is reserved, dispatched with
the session and server process identity, and reconciled from polled usage, so
the briefing appears in the action journal as a `supervisor_prompt` action
tagged with `stage=briefing` rather than bypassing the journal as an
out-of-band write. A replacement session that cannot be briefed is torn down
instead of being handed back unbriefed.
