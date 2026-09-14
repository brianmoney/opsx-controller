# durable-plan-supervision Specification

## Purpose

Durable, versioned supervision storage for plan execution: a schema-versioned
SQLite ledger that records supervised jobs, actions, incidents, and protected
job policy in trusted storage outside any writable worktree, so intent and
evidence survive restarts and every later supervision change builds on one
accepted contract.

## Requirements

### Requirement: The supervisor ledger stores jobs, actions, and incidents under a versioned schema

The system SHALL provide a durable supervisor ledger implemented with the
Python standard library only, using SQLite as the storage engine. The ledger
SHALL record supervised jobs, actions, and incidents, and SHALL carry an
explicit schema version stored in the ledger itself.

Opening a ledger at a lower schema version than the current code SHALL migrate
it forward. Schema migrations SHALL be forward-only: opening a ledger whose
recorded schema version is newer than the code supports SHALL fail with a
named error rather than silently downgrading, rewriting, or dropping data.

#### Scenario: A ledger is created and reopened

- **WHEN** a new ledger is created at a trusted location and then closed and
  reopened
- **THEN** the previously recorded job, action, and incident records are
  intact and the recorded schema version matches the code's current version

#### Scenario: An older ledger migrates forward

- **WHEN** a ledger recorded at an older supported schema version is opened by
  newer code
- **THEN** the ledger is migrated to the current schema version, existing
  records are preserved, and the recorded schema version advances

#### Scenario: A newer ledger is rejected

- **WHEN** a ledger whose recorded schema version is newer than the code
  supports is opened
- **THEN** opening fails with a named error and the ledger file is not
  modified

### Requirement: The protected job policy is versioned and revised only explicitly

The ledger SHALL hold a protected job-policy record per supervised job. The
policy record SHALL carry the authority configuration, the model selection and
the inexpensive-model allowlist selection, the hash of the manifest snapshot
the job registered with, the job's budgets and deadlines, and an explicit,
monotonically increasing operator revision.

The policy record SHALL be versioned by its own policy-schema version so later
changes can evolve it under the same forward-only discipline as the ledger
schema. The policy SHALL change only through an explicit operator revision:
an in-place mutation that does not create a new revision SHALL be rejected,
and there SHALL be no silent fallback, inheritance, or defaulting of policy
fields.

#### Scenario: Policy is recorded with its registration fields

- **WHEN** a supervised job is registered
- **THEN** its policy record persists the authority configuration, model and
  allowlist selection, manifest-snapshot hash, budgets, deadlines, the
  policy-schema version, and operator revision 1

#### Scenario: A policy change requires an explicit revision

- **WHEN** a write attempts to change a persisted policy field without
  increasing the operator revision
- **THEN** the write is rejected with a named error and the stored policy is
  unchanged

#### Scenario: An explicit revision supersedes the prior policy

- **WHEN** an operator records a new policy revision
- **THEN** the ledger stores the new revision with an increased revision
  number, and reading the current policy returns the newest revision

### Requirement: Supervision records have durable identities linked to existing runs

Every job, action, and incident SHALL have a unique, durable identity assigned
at creation: a job id, an action id, and an incident id respectively. Action
and incident records SHALL reference their owning job id. Job and action
records SHALL carry the existing `run_id` of the plan run they belong to, so
supervision records link to the existing run schema without redefining it.

#### Scenario: Identities are unique and stable

- **WHEN** jobs, actions, and incidents are created and the ledger is
  reopened
- **THEN** each record retains its original id, no two records of the same
  kind share an id, and each action and incident references its owning job id

#### Scenario: Records link to the existing run

- **WHEN** a job and its actions are recorded for a plan run
- **THEN** they carry that run's `run_id`, and querying by `run_id` returns
  the job and its actions

### Requirement: The ledger lives in trusted storage outside any writable worktree

The ledger SHALL reside in external, service-owned storage outside the
repository and any writable worktree. Configuring a ledger location that
resolves inside the repository or a writable worktree SHALL be rejected with a
named trusted-location error. Path comparisons SHALL be made on canonicalized
paths so a symlink or relative-path spelling cannot smuggle a repo-internal
location past the check.

References from ledger records back into the repository (for example the
manifest snapshot path) SHALL be stored as repository-relative data, with the
repository root recorded separately, so records remain valid if the checkout
moves.

#### Scenario: A repo-internal location is rejected

- **WHEN** a ledger is configured at a path inside the repository or a
  writable worktree, including through a symlinked or relative spelling
- **THEN** creation is refused with a named trusted-location error and no
  ledger file is created at that path

#### Scenario: Repository references are stored as relative data

- **WHEN** a job records a reference to a file in its repository
- **THEN** the ledger stores the repository root and the repository-relative
  path as data rather than an absolute path embedded in the record

### Requirement: The journal records intent before side effects and reconciles evidence

The ledger SHALL journal every supervised action's intent in its own committed
transaction before any side effect of that action is attempted. A dispatch
record, carrying the action id and its owning job, SHALL be recorded
transactionally after the intent. An action whose outcome cannot be confirmed
SHALL be marked explicitly uncertain, and an uncertain action SHALL be
reconciled against recorded evidence before it is completed, failed, or
replayed. `completed` and `failed` SHALL be terminal: a terminal action SHALL
NOT be dispatched or replayed. The journal SHALL NOT claim exactly-once
external effects: replay SHALL deduplicate and re-observe rather than assume a
prior attempt had no effect.

#### Scenario: Intent is durable before the side effect

- **WHEN** a supervised action is begun and the process is interrupted after
  the intent is recorded but before any side effect completes
- **THEN** the reopened ledger contains the committed intent record for that
  action

#### Scenario: An uncertain action requires reconciliation

- **WHEN** an action's outcome cannot be confirmed
- **THEN** the action is marked uncertain, and it cannot be marked complete or
  failed, replayed, or dispatched until evidence reconciling it has been
  recorded

#### Scenario: A failed action is terminal

- **WHEN** an action has failed
- **THEN** dispatch and replay refuse it with a named error and record no new
  dispatch, and only a new action can retry the work

### Requirement: Ledger writes are transactional and survive interruption

Every ledger write that spans multiple records SHALL execute in a single
transaction. An interrupted multi-record write SHALL leave no partial state:
reopening the ledger SHALL expose either the complete write or none of it.

#### Scenario: An interrupted write leaves no partial record

- **WHEN** a simulated interruption kills a multi-record write mid-transaction
  and the ledger is reopened
- **THEN** none of that transaction's records are present, and the ledger is
  otherwise consistent and usable

### Requirement: At most one supervised job owns a worktree

The ledger SHALL enforce a single active supervised job per worktree.
Registering a second supervised job for a worktree that already has an active
job SHALL be refused with a named error.

#### Scenario: A second job for the same worktree is refused

- **WHEN** an active supervised job exists for a worktree and a second
  registration for the same worktree is attempted
- **THEN** the registration fails with a named error and the existing job is
  unchanged

### Requirement: The supervisor ledger does not change legacy execution state

The supervisor ledger SHALL be storage separate from the authoritative JSON
execution state, and introducing it SHALL NOT change the JSON state's
location, schema, or semantics. Operators who never register a supervised job
SHALL observe no behavioral difference and SHALL NOT need the ledger or any
supervision backend for legacy runs.

#### Scenario: A legacy run needs no ledger

- **WHEN** an ordinary, unsupervised `opsx-plan` command runs in a repository
  with no registered supervised job
- **THEN** it completes without opening, creating, or requiring a supervisor
  ledger, and the JSON execution state is handled exactly as before

### Requirement: The supervision contract is documented as a client-neutral reference

The supervision contract SHALL be documented in `core/plan-supervision.md` as
a client-neutral reference covering: the job, action, and incident lifecycles;
journal-before-side-effects ordering; evidence reconciliation; the ownership
fields on each record; the single-supervised-job-per-worktree invariant; the
trusted-location path semantics consistent with the isolation boundary; the
distinction between permanent job ownership and the worktree execution lock;
the fencing-record identity semantics (process start time and boot identity,
not a bare PID); quiesced-verified takeover; and the exclusion of approval
and acceptance receipts from the execution lock.

#### Scenario: The reference covers the contract elements

- **WHEN** `core/plan-supervision.md` is reviewed against this capability
- **THEN** every element listed above is defined in it, and the document
  states them without reference to any specific client adapter

### Requirement: A gated change's approval authority is human-only unless explicitly delegated

Every change gated with `pause_before = true` SHALL have a defined approval
authority: human-only, meaning only a human operator can release the gate, or
delegated, meaning the supervised job's policy-bound authority may release
it.

The manifest key `pause_before_human_only` SHALL select the approval
authority for a gated change. When the key is absent on a gated change, the
gate SHALL resolve to human-only, so legacy manifests keep their existing
human-approved semantics. An explicit `pause_before_human_only = false`
SHALL delegate approval to the supervised job's policy-bound authority.

`pause_before_human_only = true` SHALL be invalid on a change that is not
gated with `pause_before = true`: a human-only approval requirement is
meaningless without a gate, so the combination SHALL be rejected rather than
silently accepted.

The resolution SHALL be preserved as manifest data so a supervision broker
can enforce it in a later change; the resolution itself SHALL NOT change how
unsupervised runs handle gates.

#### Scenario: Legacy gate resolves to human-only

- **WHEN** a change sets `pause_before = true` and omits
  `pause_before_human_only`
- **THEN** its approval authority resolves to human-only

#### Scenario: Explicit opt-out delegates approval

- **WHEN** a change sets `pause_before = true` and
  `pause_before_human_only = false`
- **THEN** its approval authority resolves to the supervised job's
  policy-bound delegated authority

#### Scenario: Human-only without a gate is invalid

- **WHEN** a change sets `pause_before_human_only = true` without
  `pause_before = true`
- **THEN** the manifest is rejected as invalid rather than silently accepted

#### Scenario: Ungated changes resolve no approval authority

- **WHEN** a change sets neither `pause_before = true` nor
  `pause_before_human_only`
- **THEN** no approval authority applies and the change loads unchanged

### Requirement: The job policy carries versioned model-selection and allowlist payloads

The protected job policy's `model_selection` and `inexpensive_allowlist`
fields SHALL each be a JSON-compatible object carrying a `version` key. Both
payloads SHALL share one model-policy schema version, `MODEL_POLICY_VERSION =
1`, independent of the outer ledger `policy_version` column. The two values
currently both start at 1 but SHALL be validated independently. A nested
payload version newer than the code supports SHALL be rejected with a named
error rather than silently interpreted (forward-only).

`model_selection` SHALL be a JSON-compatible object of the shape
`{"version": <int>, "roles": {<role>: <exact model identifier>}, "stages":
{<stage>: <role>}}`:

- `roles` pins an exact model identifier per supervised role the job selects;
  there SHALL be no wildcard, pattern, prefix, or default entry.
- `stages` is the explicit supervised stage-to-role mapping. Every role named
  by a stage SHALL have a corresponding pin in `roles`. The standard mapping
  is `create` to `supervised_author`, `implement` to `implementer`, `review`
  to `reviewer`, `archive` to `archiver`, `acceptance` to
  `acceptance_reviewer`, `fix` to `fixer`, `verify` to `verifier`, and
  `escalate` to `implementer_escalation`; a job records only stages it uses.
  The legacy `controller` compile role remains distinct.

`inexpensive_allowlist` SHALL be a JSON-compatible object of the shape
`{"version": <int>, "models": [<exact model identifier>, ...], "source":
<resolved source description>}`, freezing the allowlist selection the job was
registered with so a later configuration edit cannot silently change a running
job's policy.

Model selection SHALL be supervision policy data. It SHALL NOT be expressed as
new plan manifest keys: the manifest stays the plan's change graph, and the
supervised model policy lives in the protected job policy, which changes only
through an explicit operator revision.

The ledger SHALL validate both payloads on write and decode them through the
same model-policy functions on read. Except for the explicitly recognized
pre-policy case below, a payload that is not a JSON object, or whose shape or
version is unsupported, SHALL be rejected with a named model-policy error. A
payload stored before this schema existed (a JSON value with no `version` key)
SHALL remain readable as `legacy_unversioned`: no schema migration is
performed, it SHALL NOT be reinterpreted as a current payload, and a consumer
SHALL treat it as carrying no pins and fail closed rather than defaulting. The
ledger SHALL preserve the raw field value and expose its decoder state in a
`model_policy_state` mapping with
`model_selection` and `inexpensive_allowlist` keys whose values are
`versioned` or `legacy_unversioned`. Replacing a legacy payload requires an
explicit operator revision recording a versioned payload.

#### Scenario: Registration persists the pinned model policy

- **WHEN** a supervised job is registered
- **THEN** its policy record persists the per-role pinned model identifiers,
   the explicit stage-to-role mapping, the frozen allowlist selection and source,
  and `MODEL_POLICY_VERSION`, at operator revision 1

#### Scenario: Changing the model selection requires an explicit revision

- **WHEN** a write attempts to change a persisted model-selection or
  allowlist field without increasing the operator revision
- **THEN** the write is rejected and the stored model policy is unchanged

#### Scenario: A newer model-policy version is rejected

- **WHEN** a job policy records a model-policy version newer than
  `MODEL_POLICY_VERSION`
- **THEN** reading the model policy fails with a named model-policy version
  error rather than silently interpreting the unknown fields

#### Scenario: A malformed payload is rejected on write

- **WHEN** a write supplies a model-selection or allowlist value that is not a
  JSON object of the defined shape
- **THEN** the write is rejected with a named model-policy error and the
  stored policy is unchanged

#### Scenario: A pre-policy payload reads as legacy-unversioned

- **WHEN** a policy row written before this schema (a JSON value with no
  `version` key) is read
- **THEN** it is classified `legacy_unversioned` and returned unmodified, and
  a consumer treats it as carrying no pins and fails closed rather than
  defaulting

### Requirement: The model policy separates the allowlist-exempt supervisor from allowlisted supervised dispatch roles

Under a supervised job's policy, `supervisor` SHALL be a pinned operator-
selected model classified as exempt from the inexpensive allowlist and as
budget-counted. "Frontier" is descriptive only and is not an automatically
verifiable model property.
The supervised dispatch roles SHALL be the existing dispatch roles
`implementer`, `reviewer`, and `archiver`, plus `supervised_author`,
`acceptance_reviewer`, `fixer`, `verifier`, and `implementer_escalation`.
Each supervised dispatch role SHALL be subject to the allowlist check. A
supervised job's `stages.create` mapping SHALL name `supervised_author`; the
legacy `controller` compile role SHALL govern only non-supervised compilation
and SHALL NOT be a supervised dispatch role. Live routing through this mapping
belongs to the later dispatch and lifecycle changes.

The policy SHALL classify `supervisor` usage as budget-counted as policy data
and as a pure decision. This change does not perform the counting or enforce a
budget; `add-supervision-budgets` applies that classification.

There SHALL be no silent fallback, inheritance, or defaulting between roles: a
supervised dispatch role that is unresolved, fails identifier-syntax
validation, or resolves to an identifier different from its exact
`model_selection.roles` pin SHALL be reported by the policy check as blocking
rather than substituting another role's model.

A model is "unavailable" for this policy only when its resolved identifier
fails the target adapter's existing identifier-syntax validation. Live model
availability probing and pricing are out of scope.

#### Scenario: The frontier supervisor is allowlist-exempt and budget-counted

- **WHEN** the policy check evaluates the `supervisor` role
- **THEN** it reports no allowlist-membership requirement for that role and
  classifies its usage as budget-counted

#### Scenario: A supervised dispatch role must be allowlisted

- **WHEN** the policy check evaluates an `implementer`, `reviewer`,
  `archiver`, `supervised_author`, `acceptance_reviewer`, `fixer`, `verifier`,
  or `implementer_escalation` role whose resolved model is not on the job's
  allowlist
- **THEN** it reports the role as blocking with a named reason, and no
  fallback or inherited model is substituted

#### Scenario: An identifier-syntax-invalid model is unavailable

- **WHEN** the policy check evaluates a supervised dispatch role whose
  resolved identifier fails the adapter's existing identifier-syntax
  validation
- **THEN** it reports the role as blocking with a named reason, without making
  any live availability claim

#### Scenario: The supervised create stage maps to the supervised author

- **WHEN** a supervised job's policy is decoded
- **THEN** its `stages` mapping resolves the create stage to
  `supervised_author`, while the legacy `controller` compile role is unchanged

### Requirement: Dispatch model identity is action evidence with pure mismatch and retention decisions

The model policy SHALL define a dispatch identity record as action/evidence
data, separate from the insert-only policy payload. The record SHALL carry:
`action_id` (integer), `role` (the `supervisor` role or a supervised dispatch
role), `requested_model` (exact identifier), `observed_model` (exact identifier
or null), `observation_state`, and `reservation_state`.

`observation_state` SHALL be one of `requested`, `observed`, `unknown`, or
`interrupted`. `reservation_state` SHALL be one of `reserved`, `retained`, or
`reconciled`.

The policy SHALL provide a pure mismatch predicate that reports a mismatch
only when `observed_model` is non-null and differs from `requested_model`, and
reports no mismatch when `observed_model` is null.

At dispatch intent, `requested_model` SHALL equal the exact policy pin for the
record's role. A requested identity that does not equal that pin is a named
policy block; it SHALL not be repaired by selecting another role or model.

The policy SHALL provide a pure retention decision: when `observation_state`
is `unknown` or `interrupted`, the reservation SHALL be classified `retained`
rather than released, so unresolved consumption is never treated as free.

These are pure decisions over the record. Recording the identities during
dispatch and applying the decisions to a live journal belongs to
`add-action-journal-dispatch`, and reservation enforcement belongs to
`add-supervision-budgets`; this change defines and validates the shapes and
decisions only.

#### Scenario: A requested/observed mismatch is reported

- **WHEN** the mismatch predicate evaluates a dispatch identity record whose
  non-null `observed_model` differs from its `requested_model`
- **THEN** it reports a mismatch, and both identities remain separately
  recorded

#### Scenario: A missing observation is not a mismatch

- **WHEN** the mismatch predicate evaluates a dispatch identity record whose
  `observed_model` is null
- **THEN** it reports no mismatch

#### Scenario: An unknown or interrupted reservation is retained

- **WHEN** the retention decision evaluates a dispatch identity record whose
  `observation_state` is `unknown` or `interrupted`
- **THEN** it classifies the reservation as `retained` rather than released

#### Scenario: The identity record is action data, not policy data

- **WHEN** a dispatch identity record is written for an action
- **THEN** it is stored as action/evidence data and does not mutate the
  insert-only job-policy payload

### Requirement: The trust root is the local OS owner and every model session runs in the worker domain

The supervision trust model SHALL treat the local OS owner as the trust root
and SHALL use isolated OS principals as the baseline isolation backend, with
Linux as the first supported platform. The supervision service SHALL run under
a trusted OS identity distinct from any identity a model session runs under.

Every model session — including the frontier primary session — SHALL run in
the constrained worker domain. The primary SHALL NOT run as, or hold the
privileges of, the trusted service identity: no model session is ever a
privileged daemon, so a compromised or manipulated model session cannot reach
operator authority by construction.

#### Scenario: The service and the workers are distinct principals

- **WHEN** the supervision service and a model session are both running on a
  supported host
- **THEN** the service runs under the trusted OS identity and the model
  session runs under a separate, constrained worker identity that cannot
  impersonate the service identity

#### Scenario: The frontier primary is confined to the worker domain

- **WHEN** the frontier primary session executes under supervision
- **THEN** it runs in the worker domain with no service-identity privileges,
  exactly like any other model session, and cannot perform a privileged
  service operation

### Requirement: Operator authority and worker actions use separate endpoints

The service SHALL expose a separate operator endpoint and a restricted
worker-actions endpoint. The operator endpoint SHALL be authenticated by OS
peer credentials, so only a process running under the operator's OS identity
can invoke it. The worker-actions endpoint SHALL carry no operator authority:
it SHALL expose only the scoped job-service actions a worker may request.

Operator credentials SHALL NOT be exposed to a model session: no token,
credential, or capability that can invoke the operator endpoint SHALL be
present in a worker's environment, filesystem domain, or transport. An
authority scheme that a worker process can reach — a flag, a TTY check, a
token in the worker environment, or a same-UID permission bit — SHALL NOT
satisfy this requirement.

#### Scenario: A worker principal cannot invoke the operator endpoint

- **WHEN** a process running under the worker identity attempts to connect to
  the operator endpoint
- **THEN** the OS peer-credential check rejects the connection before any
  operation is evaluated

#### Scenario: The worker endpoint carries no operator authority

- **WHEN** a worker process invokes the worker-actions endpoint
- **THEN** only the scoped job-service actions are available, and no
  operator-only operation is reachable through it

#### Scenario: No operator credential exists in the worker domain

- **WHEN** a worker session's environment, filesystem domain, and transport
  are inspected
- **THEN** no credential capable of invoking the operator endpoint is present

### Requirement: Service assets live outside the worktree and the repo copy is untrusted

Service code, service configuration, the supervisor ledger, the protected job
policy, and the manifest snapshot SHALL reside outside the worktree under the
trusted-location semantics already defined for the ledger, writable only by
the trusted service identity. The editable repository copy of service code
and configuration SHALL be treated as untrusted input: the privileged service
SHALL NOT execute or load its privileged assets from the writable checkout.

Repository hooks, tests, and repo commands SHALL execute in the worker
domain, never in the privileged service, so a repo-controlled script cannot
smuggle worker-domain code into the trusted identity.

#### Scenario: A worker-domain write to the authority store is denied

- **WHEN** a process running under the worker identity attempts to write the
  supervisor ledger, the protected job policy, the manifest snapshot, or the
  service configuration
- **THEN** the write is denied by the OS-level isolation, and the attempt is
  observable as a denial rather than silently succeeding

#### Scenario: A repo hook never executes as the service

- **WHEN** a repository hook, test, or repo command runs while supervision is
  active
- **THEN** it executes under the worker identity and cannot act as the
  trusted service identity

### Requirement: Backend capability detection fails closed

The system SHALL detect whether the host provides a supported isolation
backend. Enabling supervision on a host without a supported backend SHALL be
refused, fail closed, with a named unsupported-host error. There SHALL be no
silent downgrade: supervision SHALL NOT fall back to a weaker isolation
posture (for example same-UID conventions) when the baseline backend is
unavailable.

Provisioning of the accounts and service the backend requires SHALL be
manual: detection and enablement SHALL NOT create accounts, install service
units, or otherwise provision the host automatically.

#### Scenario: An unsupported host is refused

- **WHEN** supervision enablement is attempted on a host where the isolation
  backend is unavailable
- **THEN** the attempt fails with a named unsupported-host error, nothing is
  enabled, and no weaker posture is substituted

#### Scenario: Detection never provisions

- **WHEN** backend capability detection runs on any host
- **THEN** it creates no accounts, installs no service units, and changes no
  host configuration

### Requirement: An activation probe is mandatory before supervision is enabled

Before supervision is enabled for the first time on a host, an activation
probe SHALL verify that the boundary actually holds: a probe process running
in the worker domain SHALL attempt to write the authority store, and the
attempt SHALL be denied. Supervision SHALL NOT be enabled when the probe
fails or cannot run, and the failure SHALL be reported with a named error.

The probe SHALL exercise the real platform backend when one is available;
fixture-only simulation SHALL NOT be sufficient evidence that a host
satisfies the boundary.

#### Scenario: A passing probe enables supervision

- **WHEN** the activation probe runs on a host with a supported backend and
  the worker-domain write attempt is denied
- **THEN** the probe reports success and supervision may be enabled

#### Scenario: A failed probe blocks enablement

- **WHEN** the activation probe's worker-domain write attempt succeeds, or
  the probe cannot run
- **THEN** the probe reports failure with a named error and supervision is
  not enabled

### Requirement: The contract documents the selected backend and the rejected alternatives

`core/plan-supervision.md` SHALL record the selected isolation backend, the
trust-root model, the operator/worker endpoint split, the worker-domain
confinement of every model session including the primary, the untrusted-repo
rule, the mandatory activation probe, and the fail-closed unsupported-host
behavior. It SHALL also record the rejected alternatives — a `--human` flag,
a TTY check, a token in the worker environment, and a same-UID `chmod`
scheme — with the reason each is insufficient.

#### Scenario: The reference records the boundary decision

- **WHEN** `core/plan-supervision.md` is reviewed against this capability
- **THEN** it names the selected backend, covers every element listed above,
  and states why each rejected alternative is insufficient

### Requirement: The authority store is an explicit, validated service-owned file

The authority store SHALL be an explicit regular file, never its parent
directory, owned by the service principal and writable only by it. Detection
SHALL canonicalize the target (resolving symlinks and `..`) before validating
it, and SHALL reject a target whose parent chain is writable by the worker
principal, so a worker cannot replace the protected store — or a parent symlink
— after the activation probe.

The write-denial decision SHALL account for POSIX ACL grants, not only mode
bits, because mode bits do not express a *named* grant. An ACL that is present
but unreadable, truncated, or structurally malformed SHALL fail closed rather
than fall back to the safe-looking mode bits.

#### Scenario: A worker-reachable parent chain is refused

- **WHEN** the store file itself denies the worker a write but an ancestor
  directory is writable by the worker principal
- **THEN** the store is reported unprovisioned with the failing ancestor named,
  because the worker could replace the protected file

#### Scenario: A named ACL grant to the worker is refused

- **WHEN** the store file's mode bits deny the worker a write but a POSIX ACL
  entry grants the worker principal write access
- **THEN** the write decision denies the worker and the store is reported
  unprovisioned

#### Scenario: An unreadable or malformed ACL fails closed

- **WHEN** an ACL is present on the store file or an ancestor but cannot be read
  or does not decode to a valid structure
- **THEN** detection reports the target unprovisioned rather than accepting the
  mode bits

### Requirement: The restricted-process launcher is authenticated before use

The switch mechanism the activation probe uses to drop to the worker principal
SHALL be part of the trusted base. It SHALL be resolved only from a fixed list
of trusted system directories — never the ambient `PATH` — and SHALL be a
regular executable owned by the trust root or the service principal in a
directory chain the worker cannot write (ACL-aware, like the store parents).

The launcher path SHALL be canonicalized before it is both validated and
executed, so a worker cannot repoint a pre-canonical spelling at a wrapper
between validation and execution. A bare name, a worker-owned helper, or an
untrusted directory SHALL be refused without executing anything.

#### Scenario: A PATH-shadowed launcher is not used

- **WHEN** an attacker-controlled directory earlier in `PATH` contains a
  same-named switch executable
- **THEN** the launcher is resolved from the trusted directories only and the
  attacker-controlled file is never executed

#### Scenario: A symlink repoint cannot interpose a wrapper

- **WHEN** the launcher's pre-canonical spelling is a symlink that is repointed
  after validation
- **THEN** the probe executes the canonical verified file resolved before
  validation, not the repointed target

### Requirement: The activation probe requires verifiable execution evidence

The probe's acceptance SHALL rest on execution evidence the worker-domain child
reports — its effective uid and the real `open` result — bound to a fresh
per-invocation nonce, not on the child's exit status alone. The child SHALL run
in Python isolated mode with a scrubbed, non-inherited environment so that
`PYTHONPATH`, `sitecustomize`/`usercustomize`, and shell startup hooks cannot
forge the evidence.

A bare exit status, a wrong identity, replayed evidence, or an indeterminate
result SHALL be a named failure and supervision SHALL NOT be enabled.

#### Scenario: Forged exit status alone is rejected

- **WHEN** the child exits with the denial status but reports no matching
  execution evidence
- **THEN** the probe fails with a named error and supervision is not enabled

#### Scenario: Replayed evidence is rejected

- **WHEN** evidence from a previous probe invocation is replayed without the
  current invocation's nonce
- **THEN** the probe fails with a named error

#### Scenario: Injected startup hooks cannot forge evidence

- **WHEN** `PYTHONPATH` or a `sitecustomize` hook is set in the ambient
  environment
- **THEN** the scrubbed, isolated child does not load it and cannot be made to
  emit false evidence

### Requirement: Repository-controlled code never executes as the service identity

No operator or worker endpoint verb SHALL execute a repository-controlled path:
the privileged service executes no repository hooks, tests, or commands and
treats repository paths purely as data. Repository hooks, tests, and repo
commands SHALL execute in the worker domain, never in the privileged service,
so a repo-controlled script cannot act as the trusted identity.

#### Scenario: The dispatched surface has no execution primitive

- **WHEN** the operator and worker dispatch tables are inspected
- **THEN** no verb and no handler reaches a process-execution primitive, so no
  repository-controlled code can be run through either endpoint

#### Scenario: A repository path is never executed by the service

- **WHEN** a repository hook, test, or repo command runs while supervision is
  active
- **THEN** it executes under the worker identity and cannot act as the trusted
  service identity

### Requirement: Permanent job ownership is distinct from the worktree execution lock

Permanent ownership of a supervised job SHALL be durable ledger state,
independent of any lock acquisition: registering, owning, pausing, or
completing a job changes ledger records, not a lock file. The worktree
execution lock SHALL be a separate, ephemeral arbitration mechanism held
only while a mutating command executes.

Releasing the execution lock SHALL NOT release or alter job ownership, and
holding job ownership SHALL NOT by itself hold the execution lock. A
supervised job waiting on a human approval SHALL retain its permanent
ownership without holding the execution lock, so the wait cannot block an
approval or another job's diagnostics.

#### Scenario: Ownership survives lock release

- **WHEN** a supervised execution acquires the worktree execution lock,
  finishes its mutating work, and releases the lock
- **THEN** the job's permanent ownership record in the ledger is unchanged
  and the job remains the worktree's owner

#### Scenario: A human wait holds ownership without the lock

- **WHEN** a supervised job stops at a human-only gate and records the wait
- **THEN** the job retains permanent ownership of the worktree while holding
  no execution lock, so an approval receipt can be recorded without waiting
  for a lock

### Requirement: The worktree execution lock enforces mutual exclusion between mutating processes

At most one mutating process per worktree SHALL hold the execution lock at
any time. A mutating process that cannot acquire the lock because another
process holds it SHALL be refused with a named lock-contention error rather
than waiting, proceeding unlocked, or corrupting the holder's state. This
SHALL hold regardless of the command name a mutating command is invoked
under: a command exposed under multiple names (for example `opsx-run` and
`opsx-plan run-one`) SHALL acquire the same lock.

The lock SHALL refuse a second supervised execution for the same worktree
independently of job registration, so the single-supervised-job-per-worktree
invariant holds at the execution layer even if registration state is
bypassed.

#### Scenario: A second mutating process is refused

- **WHEN** one process holds the worktree execution lock and a second
  mutating process attempts to acquire it for the same worktree
- **THEN** the second process fails with a named lock-contention error and
  performs no mutating work

#### Scenario: A second supervised execution for the worktree is refused

- **WHEN** a supervised execution holds the lock for a worktree and another
  supervised execution for the same worktree attempts to acquire it
- **THEN** the acquisition is refused with a named error, regardless of how
  the second execution was started

### Requirement: Lock ownership is fenced by process identity, not a bare PID

The execution lock SHALL record its owner as a fencing record carrying the
owning process id, the process start time, and the boot identity of the
host, so a stale owner remains distinguishable from a live one across PID
reuse and reboots. A bare PID SHALL NOT be treated as proof of ownership or
liveness.

A holder whose recorded boot identity differs from the current boot SHALL be
treated as stale. On the current boot, a holder whose recorded identity matches
a live process SHALL be treated as live, whether or not its kernel-held file
lock is still held; the kernel-held lock remains the arbitration for
contention, and its release alone SHALL NOT be accepted as proof of quiescence
while a matching live process remains. On a platform that does not expose a
process start time or boot identity, liveness SHALL be established by the
kernel-held lock alone; a bare PID SHALL still never be treated as proof of
ownership or liveness.

#### Scenario: PID reuse does not impersonate the owner

- **WHEN** a fencing record names a process id that has since been reused by
  an unrelated process with a different start time
- **THEN** the recorded owner is not treated as live on the strength of the
  reused PID alone

#### Scenario: A record from a previous boot is stale

- **WHEN** a fencing record carries a boot identity different from the
  current boot identity
- **THEN** the recorded owner is treated as stale regardless of whether a
  process with the recorded PID exists

#### Scenario: A released lock with a live recorded identity is not quiescence

- **WHEN** a held fencing record names a process identity that is still live
  on the current boot but the kernel-held flock has been released
- **THEN** the recorded owner is still treated as live and takeover is
  refused with a named error rather than fenced

### Requirement: Stale owners are fenced only after verified quiescence

Takeover of a stale execution lock SHALL happen only after the acquiring
process verifies the previous worker is quiesced: the kernel-held file lock
is no longer held, and no live process matching the recorded fencing
identity remains. Takeover while the previous owner is live SHALL be
refused with a named error, including when the kernel-held lock has been
released but the recorded process identity is still live. The refusal SHALL
apply for every owner kind.

Every takeover SHALL be recorded, so the fencing history shows which owner
was fenced and which owner replaced it. A takeover SHALL be classified as a
fencing only when the prior fencing record was not cleanly released: a clean
release SHALL rewrite the record to a released state before the kernel lock
is released, and a subsequent acquisition of a cleanly released or absent
record SHALL record an ordinary acquisition rather than a fencing.

#### Scenario: Verified-quiesced takeover succeeds

- **WHEN** the previous holder exited without cleanly releasing (its record
  is still held), its kernel-held lock is released, no process matches its
  recorded identity, and a new mutating process acquires the lock
- **THEN** the acquisition succeeds and records that the prior owner was
  fenced

#### Scenario: Takeover of a live owner is refused

- **WHEN** the previous holder is still alive and holding the kernel-held
  lock
- **THEN** the acquiring process is refused with a named error and the live
  owner's work is not interrupted

#### Scenario: A free flock with a live identity is not taken over

- **WHEN** the previous holder's flock has been released but its recorded
  process identity still matches a live process on the current boot
- **THEN** the acquisition is refused with a named error and no `fenced`
  event is recorded, for both ordinary and supervised owners

#### Scenario: A clean handoff is not recorded as fencing

- **WHEN** a holder releases the execution lock cleanly and a new mutating
  process subsequently acquires it
- **THEN** the handoff is recorded as a release followed by a normal
  acquisition, and the prior owner is not reported as fenced

### Requirement: Supervised executions persist fencing records in the supervisor ledger

For a registered supervised job, every execution-lock acquisition, release,
and takeover SHALL be persisted in the supervisor ledger as a fencing record
against that job, carrying the process identity and boot identity, so a
later reconstitution can reconstruct who last held the worktree. The ledger
schema SHALL evolve forward-only to hold these records, and opening an older
ledger SHALL migrate it.

An ordinary, unsupervised run SHALL NOT create, open, or require these
ledger records.

#### Scenario: A supervised acquisition is journaled

- **WHEN** a supervised execution acquires the worktree execution lock
- **THEN** the ledger records a fencing record against the job with the
  acquiring process identity and boot identity, and the record survives a
  ledger reopen

#### Scenario: An older ledger migrates to hold fencing records

- **WHEN** a ledger written before fencing records existed is opened
- **THEN** it migrates forward in one transaction, existing job, action,
  incident, and policy records are preserved, and fencing records can be
  written

### Requirement: Lock release is durable or it is reported as failed

Releasing the execution lock SHALL rewrite the fencing record to its
released state and persist the supervised `released` event when a ledger is
supplied, and SHALL NOT treat either step as best-effort. The kernel-held
lock and descriptor SHALL always be released so exclusion is never leaked,
but if the released record cannot be written or the ledger event cannot be
persisted, the release SHALL surface a named failure after cleanup rather
than returning as if the release succeeded.

#### Scenario: A failed release record write is surfaced

- **WHEN** the holder releases the lock and rewriting the fencing record to
  its released state fails
- **THEN** the kernel-held lock is still released, and a named release
  failure is raised instead of a silent success

#### Scenario: A failed release journal write is surfaced

- **WHEN** a supervised holder releases the lock and persisting the
  `released` fencing event fails
- **THEN** the kernel-held lock is still released and the on-disk record is
  still rewritten to released, and a named release failure is raised

### Requirement: Approval and acceptance receipts do not require the execution lock

Recording an approval, an acceptance, or a pause/steer request SHALL NOT
require acquiring or waiting for the worktree execution lock. For
supervised jobs these receipts SHALL be durable broker database
transactions with a durable wake-up for the owning job, so a human wait
that retains permanent ownership never blocks an approval, and a receipt
recorded while another process holds the lock is not lost.

#### Scenario: A receipt is recorded during a held lock

- **WHEN** the worktree execution lock is held by a running execution and an
  approval or acceptance receipt is recorded for that worktree's job
- **THEN** the receipt is durably recorded without acquiring the lock and
  the owning job is woken to observe it

#### Scenario: A human wait cannot block approval

- **WHEN** a supervised job is waiting on a human-only approval and holds
  no execution lock
- **THEN** recording the operator's approval requires no lock acquisition
  and cannot deadlock against the waiting job

### Requirement: Ordinary runs use the lock without the supervision backend

An ordinary, unsupervised mutating run SHALL acquire the worktree execution
lock without opening or requiring the supervisor ledger and without running
under a separate principal. Aside from the new contention and supervised-race
refusals, ordinary runs SHALL behave exactly as before.

An ordinary mutating run that would race a supervised execution — one whose
worktree lock is held by a supervised owner — SHALL be refused with a
documented named error identifying the supervised ownership, rather than
proceeding or waiting.

#### Scenario: A legacy run works with no ledger present

- **WHEN** an ordinary `opsx-plan run` executes in a repository with no
  registered supervised job and no reachable supervisor ledger
- **THEN** the run acquires the lock, completes as before, and never opens
  or requires the ledger

#### Scenario: An ordinary run racing a supervised execution is refused

- **WHEN** a supervised execution holds the worktree lock and an operator
  runs an ordinary mutating command in the same worktree
- **THEN** the command fails with a documented named error stating the
  worktree is owned by a supervised execution

### Requirement: The job policy carries a versioned budgets and deadlines payload

The protected job policy's `budgets` and `deadlines` fields SHALL each be a
JSON-compatible object carrying a `version` key. Both payloads SHALL share
one budget schema version, `BUDGET_SCHEMA_VERSION = 1`, independent of the
outer ledger `policy_version` column and of `MODEL_POLICY_VERSION`. A nested
payload version newer than the code supports SHALL be rejected with a named
error rather than silently interpreted (forward-only).

`budgets` SHALL be a JSON-compatible object of the shape `{"version": <int>,
"total_cost_usd": <number|null>, "per_action_cost_usd": <number|null>,
"total_elapsed_minutes": <number|null>, "per_action_elapsed_minutes":
<number|null>, "max_incident_attempts": <integer|null>}`. A null value
disables that limit; at least one limit SHALL be non-null for a supervised
job. `max_incident_attempts` bounds how many times one incident attempt
signature may recur before further identical attempts are refused.

`deadlines` SHALL be a JSON-compatible object of the shape `{"version":
<int>, "execution_deadline_minutes": <number|null>}`, carrying only
execution-time limits; human-wait duration is governed by the separate
deadline-separation requirement below and SHALL NOT appear here as a wait
budget.

Budget and deadline values SHALL be supervision policy data. They SHALL NOT
be expressed as new plan manifest keys: the manifest stays the plan's change
graph, and budget policy lives in the protected job policy, which changes
only through an explicit operator revision.

The ledger SHALL validate both payloads on write and decode them through the
same budget functions on read. A payload that is not a JSON object of the
defined shape SHALL be rejected with a named budget error. A payload stored
before this schema existed (a JSON value with no `version` key) SHALL remain
readable as `legacy_unversioned`: no migration is performed, it SHALL NOT be
reinterpreted as a current payload, and a consumer SHALL treat it as carrying
no enforceable limits and fail closed for supervised dispatch rather than
defaulting to unlimited. Replacing a legacy payload requires an explicit
operator revision recording a versioned payload.

#### Scenario: Registration persists the budget policy

- **WHEN** a supervised job is registered with total and per-action cost and
  elapsed limits and an execution deadline
- **THEN** its policy record persists those values as versioned `budgets`
  and `deadlines` payloads at `BUDGET_SCHEMA_VERSION`, at operator revision 1

#### Scenario: A policy with no limits is rejected

- **WHEN** a write supplies a `budgets` payload in which every limit is null
- **THEN** the write is rejected with a named budget error and the stored
  policy is unchanged

#### Scenario: A newer budget schema version is rejected

- **WHEN** a job policy records a budget payload version newer than
  `BUDGET_SCHEMA_VERSION`
- **THEN** reading the budget policy fails with a named budget version error
  rather than silently interpreting the unknown fields

#### Scenario: A pre-schema payload fails closed for supervised dispatch

- **WHEN** a policy row written before this schema (a JSON value with no
  `version` key) is read
- **THEN** it is classified `legacy_unversioned` and returned unmodified, and
  supervised dispatch is blocked until an explicit operator revision records
  a versioned payload, rather than defaulting to unlimited spend

### Requirement: Every dispatched supervised model call is budget-counted against per-action and total limits

Under a supervised job's policy, every model call the plan orchestrator
dispatches SHALL be accounted against the job's budgets: `implementer`,
`reviewer`, and `archiver` workers, the `supervised_author` create stage,
`acceptance_reviewer`, `fixer`, and `verifier` auxiliary calls, every retry,
and `implementer_escalation` dispatches.

Each dispatch SHALL be checked against both the per-action limits and the
job-total limits before it proceeds. A dispatch that would exceed a
per-action limit, or that arrives when a total limit is already exhausted,
SHALL be blocked with a named budget-exhaustion state rather than dispatched.

Budget enforcement SHALL apply only to registered supervised jobs. A legacy
unregistered run SHALL keep its existing `--budget-minutes` / `--budget-usd`
behavior with no durable budget layer.

Scope boundary: the frontier `supervisor` primary session is not a stage the
plan orchestrator dispatches, so this change wires no production
supervisor-primary call site. The supervisor role's pricing and reservation
behavior is defined and tested here as a boundary primitive; when
`add-opencode-session-bridge` invokes the supervisor primary, it routes that
usage through this change's reserve/reconcile boundary rather than introducing
a separate accounting path.

#### Scenario: Create, retry, and escalation calls are budget-counted

- **WHEN** a supervised job performs a `supervised_author` create dispatch,
  re-dispatches a stage as a retry, or dispatches through
  `implementer_escalation`
- **THEN** each call is reserved and reconciled against the job's budgets as
  its own action, with no call class running unaccounted

#### Scenario: A per-action limit blocks an oversized dispatch

- **WHEN** a dispatch's reserved estimate exceeds `per_action_cost_usd` or
  `per_action_elapsed_minutes`
- **THEN** the dispatch is blocked with a named budget-exhaustion state and
  no side effect is dispatched

#### Scenario: An exhausted total limit blocks further dispatch

- **WHEN** the job's reconciled plus retained consumption has reached a total
  limit
- **THEN** any further dispatch under that job is blocked with a named
  budget-exhaustion state

#### Scenario: A legacy unregistered run is not budget-gated

- **WHEN** a plan runs without a registered supervised job
- **THEN** the durable budget layer performs no reservation, blocking, or
  accounting for that run, and the legacy budget flags behave as before

### Requirement: Budget is reserved before dispatch and reconciled afterward without double billing

A supervised dispatch SHALL reserve budget before any side effect: the
reservation record SHALL be durably written in the supervisor ledger with the
action identity, role, requested model, and the estimated cost and elapsed
amounts before the dispatch proceeds. A reservation that cannot be durably
recorded SHALL block the dispatch rather than run unaccounted.

After the dispatch completes, observed usage SHALL be reconciled against the
same reservation record, replacing the estimate with the observed amounts.
Reconciliation SHALL NOT create a second charge: the reservation and its
reconciliation are one accounting entry per action.

Duplicate results delivered for the same action — a repeated completion, a
re-observed usage record, or a retried delivery — SHALL be deduplicated
against the action's existing reservation and SHALL NOT be billed twice.

The reservation estimate SHALL be computed from the pricing catalog using the
role's pinned model identity and SHALL include the provider token caps and a
stated headroom margin, so the reserve conservatively bounds the expected
charge.

#### Scenario: Reservation is durable before dispatch

- **WHEN** a supervised dispatch is prepared
- **THEN** its reservation record exists in the ledger with the action
  identity, role, requested model, and estimated amounts before the dispatch
  side effect begins, and a dispatch whose reservation write fails is blocked

#### Scenario: Reconciliation replaces the estimate without a second charge

- **WHEN** observed usage arrives for a dispatched action
- **THEN** it is recorded against that action's reservation, the job's
  consumption reflects the observed amounts exactly once, and no separate
  additional charge is created

#### Scenario: A duplicate result is not billed twice

- **WHEN** a second completion or usage record arrives for an action that
  already has a reconciled reservation
- **THEN** it is deduplicated against the existing reservation and the job's
  accounted consumption is unchanged

### Requirement: Unknown or interrupted usage is retained and unpriceable dispatch is blocked

A reservation whose observed usage is unknown or whose dispatch was
interrupted SHALL be classified `retained` rather than released, using the
dispatch identity record's `observation_state` and `reservation_state`
vocabulary: unresolved consumption SHALL continue to count against the job's
budgets at its reserved estimate and SHALL NOT be treated as free.

A dispatch whose pinned model cannot be priced — the pricing catalog resolves
it to an unresolved result, or the model identity is unavailable — SHALL be
blocked before any side effect with a named unknown-pricing error. An
unpriceable dispatch SHALL NOT proceed on a zero or assumed cost.

#### Scenario: An unknown-outcome reservation is retained

- **WHEN** a dispatched action's outcome cannot be observed and its
  observation state is `unknown`
- **THEN** its reservation is classified `retained`, its reserved estimate
  continues to count against the job's budgets, and the amount is not
  released back to the job

#### Scenario: An interrupted reservation is retained

- **WHEN** a dispatched action is interrupted before reconciliation and its
  observation state is `interrupted`
- **THEN** its reservation is classified `retained` rather than released

#### Scenario: Unknown pricing blocks dispatch with a named error

- **WHEN** a supervised dispatch is prepared for a role whose pinned model
  the pricing catalog cannot resolve to a price
- **THEN** the dispatch is blocked with a named unknown-pricing error before
  any side effect, and no reservation at zero or assumed cost is created

### Requirement: Budget and incident-attempt accounting survives plan reset

All budget state — reservations, reconciled consumption, and exhaustion
state — SHALL live in the durable supervisor ledger in trusted external
storage, never in the worktree JSON execution state. An `opsx-plan reset`
SHALL NOT erase, reduce, or refresh a supervised job's accounted consumption.

Each incident under a supervised job SHALL be recorded with a durable attempt
signature identifying its failure class and material identity, together with
an attempt count. Attempt signatures and counts SHALL persist across
`opsx-plan reset`. When an incident's attempt count reaches the job's bounded
limit, further identical attempts SHALL be refused with a named
bounded-attempts state rather than looping, and the refusal SHALL survive
subsequent resets.

#### Scenario: Consumption survives reset

- **WHEN** `opsx-plan reset` runs on a plan whose supervised job has
  reconciled and retained consumption
- **THEN** the job's accounted consumption after the reset is exactly what it
  was before, and subsequent dispatches remain bound by the same totals

#### Scenario: Identical incidents cannot loop without bound across resets

- **WHEN** the same incident signature recurs and each recurrence is followed
  by an `opsx-plan reset`
- **THEN** the attempt count accumulates across the resets, and once the
  bounded limit is reached any further identical attempt is refused with a
  named bounded-attempts state

### Requirement: Execution deadlines exclude expected human waits

Deadline accounting SHALL track execution elapsed time separately from
human-wait duration. Only time in which the job is actively executing —
actions dispatched or in progress — SHALL consume `execution_deadline_minutes`
and the elapsed budgets. An expected human wait, such as a held human-only
approval gate, SHALL be persisted as normal durable state and SHALL consume
no execution deadline or elapsed budget while it lasts.

When execution resumes after a human wait, deadline and elapsed accounting
SHALL continue from the pre-wait accumulated values, excluding the wait
duration.

#### Scenario: A human wait consumes no execution deadline

- **WHEN** a supervised job waits on a human-only approval for a period
  longer than the remaining execution deadline
- **THEN** the job is not failed on the execution deadline for that period,
  and the wait is recorded as durable human-wait state

#### Scenario: Elapsed accounting resumes excluding the wait

- **WHEN** execution resumes after a recorded human wait
- **THEN** accumulated execution elapsed time continues from its pre-wait
  value with the wait duration excluded

### Requirement: Budget increases are explicit operator policy revisions

Budget and deadline values SHALL change only through an explicit operator
revision of the protected job policy, under the ledger's existing
revision-increment rule. No worker, agent, or supervised dispatch path SHALL
increase a budget, and no automated process — including `opsx-plan reset`,
incident recovery, or a budget-exhaustion response — SHALL raise, clear, or
re-baseline a limit.

A budget increase SHALL take effect only for subsequent reservations; it
SHALL NOT rewrite previously reconciled or retained consumption.

#### Scenario: An operator revision raises a limit

- **WHEN** an operator records a policy revision with a higher
  `total_cost_usd` at the next revision number
- **THEN** subsequent reservations are evaluated against the new limit while
  previously accounted consumption is unchanged

#### Scenario: A worker-path increase is refused

- **WHEN** any non-operator path attempts to raise, clear, or re-baseline a
  budget — including as part of a reset or an exhaustion response
- **THEN** the attempt is refused and the stored budget policy is unchanged

### Requirement: Hard cost limits are enforced with stated headroom rather than false precision

Hard USD limits SHALL be enforced through the reserve-before-dispatch model:
because observed usage is known only after dispatch, enforcement SHALL
incorporate the provider token caps and a stated headroom margin into each
reservation so that the expected overshoot beyond a hard limit is bounded by
at most one in-flight action's headroom.

The supervision contract documentation SHALL state this semantics explicitly:
a hard limit bounds total overshoot and SHALL NOT be described as an exact,
immediately enforced ceiling.

#### Scenario: Reservations include headroom

- **WHEN** a reservation estimate is computed for a dispatch
- **THEN** the estimate is derived from the pricing catalog with the provider
  token caps and the stated headroom margin applied, and the recorded
  reservation reflects that bounded worst case

#### Scenario: Enforcement is documented as bounded, not exact

- **WHEN** the supervision contract describes hard cost limits
- **THEN** it states that enforcement bounds overshoot to at most one
  in-flight action's headroom and makes no claim of an exact real-time
  ceiling

### Requirement: Transient budget failures use bounded backoff and human blockers are actionable state

A transient failure in budget-gated dispatch — such as a temporarily
unwritable ledger or a retryable pricing-catalog load failure — SHALL be
retried with bounded backoff: a limited attempt count and capped delay,
recorded in the ledger. When the bound is reached, the failure SHALL surface
as a blocked state with its named reason rather than retrying indefinitely.

A genuine human blocker — an exhausted budget awaiting an operator increase,
or a bounded-attempts refusal — SHALL be surfaced as actionable state
identifying the required operator action, and SHALL NOT be retried or
recovered automatically.

#### Scenario: Transient failures back off within a bound

- **WHEN** a budget-gated dispatch fails transiently
- **THEN** it is retried with bounded backoff, each retry is recorded, and
  exceeding the bound surfaces a blocked state with the named reason instead
  of further retries

#### Scenario: An exhausted budget surfaces as an actionable human blocker

- **WHEN** a job's budget is exhausted and further dispatch is blocked
- **THEN** the job surfaces an actionable blocked state identifying the
  required operator budget increase, and no automatic retry, recovery, or
  self-increase is attempted
