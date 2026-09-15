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

## Single supervised job per worktree

At most one active supervised job owns a worktree. Registering a second
supervised job for a worktree that already has an active job is refused with a
named `DuplicateJobError`, and the existing job is left unchanged. A new
registration becomes legal only once the prior job reaches a terminal state.

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
