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

For a registered supervised job, the broker SHALL enforce the resolved
authority at runtime: a human-only gate SHALL be released only by an approval
receipt recorded through the operator OS-authenticated path, and a delegated
gate SHALL be released only by a receipt recorded through the scoped job
service action. Unsupervised, unregistered runs SHALL handle gates exactly as
before.

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

#### Scenario: A human-only gate refuses the worker path

- **WHEN** a gated change in a registered supervised job resolves to
  human-only and a release is attempted through the worker-actions endpoint
  or by mutating execution state directly
- **THEN** the gate is not released and the attempt is refused or ignored as
  a non-authoritative write

#### Scenario: A delegated gate releases through the scoped service action

- **WHEN** a gated change in a registered supervised job resolves to
  delegated and the policy-bound job service records the approval through its
  scoped action
- **THEN** the broker records the receipt and the gate is released for the
  bound checkpoint and material revision

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

### Requirement: The broker is the sole approval authority for registered supervised jobs

For a registered supervised job, the broker in the trusted authority domain
SHALL be the sole authority that releases approval and acceptance gates.
Direct mutation of the JSON execution state, the plan manifest, or any
repo-writable file SHALL NOT release a gate, satisfy a checkpoint, or alter a
job's supervised identity.

Within a registered job, `approve`, `approve --all`, `approve P<N>`,
`accept`, `reset`, `run`, `run-one`, and `opsx-run` SHALL be broker mediated:
each is recorded as a durable broker transaction or refused, and execution
decisions consult broker state rather than unmediated JSON writes. For
registered jobs the JSON execution state SHALL be a projection of broker and
ledger state, not a competing phase authority.

Unregistered legacy jobs SHALL keep their existing JSON handling with no
dependency on the broker, the supervisor ledger, or any supervision backend.

#### Scenario: A worker write cannot release a gate

- **WHEN** a worker-domain process edits the JSON execution state to add an
  approval for a gated change in a registered supervised job
- **THEN** the gate remains unreleased, because only a broker-recorded
  receipt satisfies it

#### Scenario: An unregistered job needs no broker

- **WHEN** an ordinary `opsx-plan approve` runs in a repository with no
  registered supervised job
- **THEN** it mutates the JSON execution state exactly as before, without
  opening or requiring any broker connection or supervisor ledger

#### Scenario: A stale projection does not mislead the operator

- **WHEN** the JSON projection for a registered job is regenerated from
  broker and ledger state after a receipt is recorded
- **THEN** the projected gate state matches the broker's recorded receipts

#### Scenario: The service installs the projection writer before serving

- **WHEN** the trusted service boots the broker session before accepting any
  operator or worker endpoint request
- **THEN** it installs and retains the projection writer, so every receipt it
  records regenerates the JSON projection from broker and ledger state
- **AND** a missing writer fails closed recording nothing, and a provisioning
  failure (missing store, unregistered worktree, unresolvable principal,
  foreign or terminal explicit job id, or unbindable endpoint socket) refuses
  to serve rather than recording a receipt whose projection cannot be
  regenerated

#### Scenario: A service session serves only the current worktree's job

- **WHEN** the trusted service opens a session for a worktree whose active
  nonterminal registration is job A, with or without an explicit job id
- **THEN** the session serves job A only, and an explicit job id naming any
  other job (another worktree's job, or a terminal one) is rejected as a
  registration mismatch before the projection writer is installed
- **AND** an unbindable operator or worker endpoint socket fails closed with
  the named broker-unavailable error and tears the session down rather than
  escaping as a raw socket error, so no request is served and no receipt or
  projection change occurs

### Requirement: Approval and acceptance receipts bind to the exact checkpoint and material revision

Each approval or acceptance receipt SHALL bind to the exact checkpoint (the
specific gated change and gate kind) and to a material revision computed by
hashing only the material gate inputs: the gate-relevant manifest fields for
that change taken from the protected manifest snapshot, the protected
snapshot's identity hash, and the current explicit policy revision.

Unrelated updates — task progress, telemetry, other changes' state, or
non-gate manifest fields — SHALL NOT invalidate a receipt. A receipt recorded
against a different material revision SHALL NOT satisfy the gate.

Plan and policy revisions SHALL be explicit: the material revision changes
only when an operator registers a new protected snapshot or records a new
explicit policy revision, never as a side effect of worker writes to the
repository.

#### Scenario: A stale material revision does not satisfy the gate

- **WHEN** an approval receipt exists for a gated change but the operator has
  since registered a new policy revision or protected snapshot that changes
  the material gate inputs
- **THEN** the prior receipt does not satisfy the gate and the change awaits
  approval again

#### Scenario: An unrelated update does not invalidate a receipt

- **WHEN** an approval receipt exists for a gated change and task progress or
  telemetry for an unrelated change is recorded without any explicit plan or
  policy revision
- **THEN** the receipt continues to satisfy the gate

#### Scenario: Worker edits cannot shift the material revision

- **WHEN** a worker-domain process edits the repo plan file or JSON state in
  a way that would alter gate-relevant fields
- **THEN** the material revision computed from the protected snapshot is
  unchanged and existing receipts keep their defined validity

### Requirement: A protected manifest snapshot and external registration record anchor supervised identity

A registered supervised job SHALL be anchored by two service-owned records
held outside the worktree: the external registration record (the ledger job
and its protected job policy) and a protected manifest snapshot capturing the
manifest content the job was registered with.

Gate and mediation decisions SHALL be evaluated from these protected records,
not from repo-writable copies. A worker that edits the JSON execution state
or the repo plan to drop supervised fields SHALL NOT escape active
registration: the job remains registered and broker mediated until the
registration record itself reaches a terminal state through an authorized
path.

#### Scenario: Dropping supervised fields does not escape registration

- **WHEN** a worker-domain process removes supervised markers from the JSON
  execution state or the repo plan for a registered job
- **THEN** the job is still registered, mutating commands are still broker
  mediated, and the tampered files have no effect on gate authority

#### Scenario: The protected snapshot lives outside the worktree

- **WHEN** a job is registered
- **THEN** the manifest snapshot is stored in service-owned storage outside
  the worktree and is not writable by the worker domain

### Requirement: Resume revalidates material gate inputs before dispatch

Before a supervised job resumes dispatch after a restart, a pause, or a
human wait, the broker SHALL revalidate the material gate inputs for every
gate the job believes is satisfied: each relied-upon receipt SHALL be matched
against the current material revision, and any gate whose receipt no longer
matches SHALL return to awaiting its defined authority.

#### Scenario: Resume with valid receipts dispatches

- **WHEN** a supervised job resumes and every relied-upon receipt matches the
  current material revision
- **THEN** dispatch proceeds using those receipts

#### Scenario: Resume with a stale receipt re-arms the gate

- **WHEN** a supervised job resumes after an explicit policy revision
  invalidated a relied-upon receipt
- **THEN** the affected change returns to awaiting its approval authority and
  is not dispatched

### Requirement: Supervised resets are bounded and worker-initiated resets are refused

Within a registered supervised job, a reset SHALL occur only through an
authorized path: an operator reset recorded through the operator
OS-authenticated path, or a bounded service reset within the job's policy
limits. A reset attempted directly by a worker-domain process — through
`opsx-plan reset`, `opsx-run`, or JSON mutation — SHALL be refused with a
named error and SHALL NOT alter broker or ledger state.

Authorized resets SHALL be recorded as broker transactions so the reset
history is durable and auditable.

#### Scenario: A worker reset is refused

- **WHEN** a worker-domain process runs `opsx-plan reset` for a change in a
  registered supervised job
- **THEN** the command fails with a named error identifying broker mediation
  and no reset occurs

#### Scenario: An operator reset is recorded

- **WHEN** the operator resets a change through the operator path for a
  registered job
- **THEN** the broker records the reset as a durable transaction and the
  change returns to its pre-dispatch gate state

### Requirement: Pause and steer requests are durable broker receipts

Within a registered supervised job, a pause or steer request SHALL be recorded
as a durable broker transaction of kind `pause` or `steer`, bound to the
requesting change's checkpoint and material revision, and SHALL wake the
owning job through the same durable wake-up mechanism as approval receipts. A
pause or steer request SHALL NOT require the held worktree execution lock, and
a worker-domain process SHALL NOT record one except through its scoped service
action.

#### Scenario: A pause request is a durable receipt

- **WHEN** the policy-bound service records a pause request for a change in a
  registered supervised job
- **THEN** the broker records a durable `pause` receipt bound to the change's
  checkpoint and material revision, and the owning job is woken by the receipt
  scan without the execution lock

#### Scenario: A worker cannot record a pause or steer request

- **WHEN** a worker-domain process attempts to record a pause or steer request
  except through its scoped service action
- **THEN** the attempt is refused and no receipt is recorded

### Requirement: Every supervised stage dispatch flows through the journal lifecycle

For a registered supervised job, the run engine SHALL drive every inner stage
dispatch — create, implement, review, and archive, including stage retries and
`implementer_escalation` dispatches — through the full journal lifecycle:
action intent committed in its own transaction before any side effect, a
transactional dispatch record after the intent, and then a terminal outcome
(`completed` or `failed`) or an explicit `uncertain` mark with recorded
evidence. The journal SHALL wrap the existing run engine dispatch path; no new
DAG, stage machine, or replacement dispatcher SHALL be introduced.

A supervised action SHALL NOT be left without an outcome: when the engine
finishes, interrupts, or abandons a dispatch, it SHALL record the outcome or
the uncertainty in the journal before the run continues, pauses, or exits.

For an unregistered legacy run, dispatch SHALL behave exactly as before: no
journal records are created and no journal, policy, or ledger dependency is
introduced.

#### Scenario: An inner stage dispatch is journaled end to end

- **WHEN** a registered supervised job dispatches an implement, review,
  archive, or create stage through the existing run engine
- **THEN** the ledger holds the intent record committed before the stage's
  side effects, a dispatch record bound to that action, and a terminal or
  explicitly uncertain outcome with evidence once the stage resolves

#### Scenario: Every call class is journaled

- **WHEN** a supervised job performs a create-stage dispatch, re-dispatches a
  stage as a retry, or dispatches through `implementer_escalation`
- **THEN** each dispatch is its own journaled action with its own intent,
  dispatch record, and outcome, and no call class runs outside the journal

#### Scenario: A legacy run creates no journal records

- **WHEN** an ordinary, unregistered plan run executes in a repository with
  the supervisor package present
- **THEN** its dispatch path is unchanged and the supervisor ledger contains
  no actions for that run

### Requirement: Supervised dispatch is gated on lock, authority, model policy, and budget before side effects

For a registered supervised job, every action SHALL pass one dispatch boundary
that evaluates, before any side effect of the action: the worktree execution
lock is held by the dispatching process; the broker's authority state permits
the dispatch; the model policy check passes for the action's role against the
job policy's pinned model identity; and the budget reservation for the action
succeeds. A failure at any of the four SHALL block the dispatch with a named
error identifying the failing gate, and no side effect of the action SHALL
occur. A blocked action SHALL be recorded as failed with the gate reason, or
shall leave the job in its prior state with the block surfaced, rather than
running ungated.

The model policy gate SHALL enforce the archived decision vocabulary: a
missing, unallowlisted, or identity-mismatched role blocks with its named
reason, and no silent fallback or inheritance to another model identity is
permitted.

#### Scenario: A model-policy violation blocks dispatch before side effects

- **WHEN** a supervised action's resolved model identity is missing,
  unallowlisted, or mismatched against the pinned job policy
- **THEN** the dispatch is blocked with the named policy reason, the action
  records no dispatch side effects, and no worker process or task is spawned

#### Scenario: Budget exhaustion blocks dispatch at the same boundary

- **WHEN** a supervised action's reservation would exceed a per-action limit
  or arrives with a total limit exhausted
- **THEN** the dispatch is blocked with the named budget-exhaustion state at
  the same dispatch boundary, after the model policy check and before any
  side effect

#### Scenario: The failing gate is identified

- **WHEN** a dispatch is blocked at the boundary
- **THEN** the error names which gate failed — lock, authority, model policy,
  or budget — so the operator can act on the specific blocker

### Requirement: Each action revalidates the immutable job plan and policy before dispatch

Before each supervised action is dispatched, the engine SHALL revalidate that
the manifest snapshot hash recorded at registration still matches the plan on
disk and that the job policy's operator revision is unchanged since the action
was planned. A plan or policy that changed since registration or since the
prior action SHALL block dispatch with a named stale-material error; the
engine SHALL NOT dispatch against stale material and SHALL NOT silently
re-bind the job to the new plan or policy. Adoption of a changed plan or
policy SHALL require an explicit operator revision, after which dispatch
resumes against the new material.

#### Scenario: A policy change between actions blocks the next dispatch

- **WHEN** the job policy's operator revision changes after one action
  completes and before the next is dispatched
- **THEN** the next dispatch is blocked with a named stale-material error
  until the operator explicitly acknowledges the new revision for the job

#### Scenario: A changed plan snapshot blocks dispatch

- **WHEN** the manifest snapshot hash recorded at registration no longer
  matches the plan on disk at dispatch time
- **THEN** the dispatch is blocked with a named stale-material error and the
  action is not begun

### Requirement: Dispatch records carry session and process identity for both worker dispatch paths

Every supervised dispatch record SHALL carry the identity needed to fence and
reconcile the actual worker: the subprocess dispatch path SHALL record the
worker process identity, and the native Task dispatch path inside a worker
session SHALL record the session identity reported through the worker
endpoint. Both paths SHALL write to the same journal with the same lifecycle
and gating, so a supervised dispatch never has a null identity regardless of
which path carried it.

#### Scenario: A subprocess worker dispatch records process identity

- **WHEN** a supervised stage is dispatched as a subprocess
- **THEN** its dispatch record carries the spawned process identity, and a
  later reconciliation can distinguish that exact process from a recycled PID

#### Scenario: A native Task dispatch records session identity

- **WHEN** a worker dispatches a native Task subagent and reports it through
  the worker endpoint
- **THEN** the journal records the Task's session identity against the action
  in the same journal as subprocess dispatches

### Requirement: Unconfirmed outcomes are marked uncertain and reconciled from evidence in the engine

When a supervised action's outcome cannot be confirmed — the worker process is
lost after spawn, a timeout fires after dispatch, a kill's effect is
ambiguous, or result evidence is missing — the engine SHALL mark the action
explicitly `uncertain` rather than guessing success or failure. The engine
SHALL record outcome and usage evidence against the action as it becomes
available, and an uncertain action SHALL be reconciled against recorded
evidence before it is completed, failed, or replayed. A run with an
unreconciled uncertain action SHALL NOT silently progress to subsequent
actions as if the uncertain one had succeeded or never happened.

#### Scenario: A lost worker marks its action uncertain

- **WHEN** a supervised stage's worker process disappears after the dispatch
  record was written and no outcome was recorded
- **THEN** the action is marked uncertain, and the run does not treat the
  stage as complete or failed until evidence reconciles it

#### Scenario: Evidence reconciles an uncertain action

- **WHEN** evidence resolving an uncertain action's outcome is recorded
- **THEN** the action is reconciled and may then transition to a terminal
  state consistent with that evidence

#### Scenario: An unreconciled uncertain action blocks silent progress

- **WHEN** a supervised run resumes while an action remains uncertain and
  unreconciled
- **THEN** the run surfaces the uncertain action as blocking state instead of
  dispatching the next action as though nothing was pending

### Requirement: Replay deduplicates and re-observes without claiming exactly-once effects

Before replaying an uncertain action, the engine SHALL deduplicate against the
prior dispatch — matching the action's recorded identity and any delivered
results — and SHALL re-observe the external state the prior attempt may have
affected. Replay SHALL NOT assume a prior attempt had no effect, SHALL NOT
double-bill or double-apply a duplicate result, and SHALL NOT claim
exactly-once external effects through a lease or any other mechanism.

#### Scenario: A duplicate result is not applied twice

- **WHEN** a prior attempt's result is delivered again before replay
- **THEN** the duplicate is recognized against the recorded dispatch identity
  and reconciles the existing action rather than being billed or applied as a
  new dispatch

#### Scenario: Replay happens only after re-observation

- **WHEN** an uncertain action has no recorded evidence resolving it
- **THEN** the engine re-observes the external state first and replays only
  when that observation shows the prior attempt did not complete the work

### Requirement: Worker evidence and action requests are journaled through the worker endpoint

The worker actions endpoint SHALL back its `record_evidence` and
`request_action` verbs with the supervisor ledger: `record_evidence` SHALL
persist evidence against the referenced action so uncertain actions can
reconcile, and `request_action` SHALL be answered from journaled job state
rather than synthesized ad hoc. Both verbs SHALL remain inside the existing
worker-domain authorization boundary: a caller outside the worker domain SHALL
be denied, and evidence SHALL be accepted only for actions of the job the
worker is bound to.

#### Scenario: Worker evidence reconciles through the endpoint

- **WHEN** a bound worker reports outcome evidence through `record_evidence`
  for one of its job's uncertain actions
- **THEN** the evidence is persisted in the ledger and the action reconciles
  as it would for engine-recorded evidence

#### Scenario: Evidence for another job is refused

- **WHEN** a worker presents evidence referencing an action owned by a
  different job
- **THEN** the write is refused with a named authorization error and the
  target action is unchanged

### Requirement: The session bridge exposes a documented, version-checked session API

The supervision service SHALL drive the supervised primary session through a
documented, versioned session bridge API with five operations — create,
prompt, result-schema, lookup, and abort — backed by a headless session
server. Before any session operation, the bridge SHALL perform a version
capability check against the server's reported version and SHALL fail closed
with a named unsupported-version error when the server does not satisfy the
documented supported range. No session operation SHALL be attempted against an
unchecked or unsupported server, and the bridge SHALL NOT depend on any server
surface outside the documented subset.

The typed result returned by a prompt SHALL be defined by the documented
result-schema and SHALL be derived from authoritative session state, never
from the streamed event channel.

#### Scenario: The version capability check gates first use

- **WHEN** the bridge connects to a session server whose reported version is
  outside the documented supported range
- **THEN** the bridge refuses with a named unsupported-version error and
  issues no create, prompt, lookup, or abort request

#### Scenario: An unchecked server is never used

- **WHEN** any bridge operation is requested before the version capability
  check has succeeded
- **THEN** the operation is refused with a named error and no server request
  is issued

#### Scenario: Prompt results follow the documented result-schema

- **WHEN** a prompted session reaches a terminal state
- **THEN** the bridge returns the typed result defined by the documented
  result-schema, derived from polled authoritative session state

### Requirement: The service owns supervised primary session lifetime

For a registered supervised job, the service SHALL own the primary session's
lifetime: it SHALL launch the headless session in the worker domain, record
the session identity against the job, and on restart SHALL attempt to adopt
the existing session through lookup before creating a new one. An operator's
interactive chat SHALL start or attach to the same service-managed session via
the primary session linkage recorded at registration, so the human and the
supervised job share one authoritative conversation.

The headless session process SHALL run in the worker domain: the service owns,
observes, and re-briefs the session, but no model session — including the
frontier primary — runs with service-identity privileges.

#### Scenario: A restart adopts the live session by lookup

- **WHEN** the service restarts while a job's headless session is still live
- **THEN** it resolves the job's recorded session identity through lookup and
  adopts that session rather than spawning a replacement

#### Scenario: An interactive chat attaches to the managed session

- **WHEN** an operator opens an interactive chat for a supervised job
- **THEN** the chat starts or attaches to the service-managed primary session
  via the recorded primary session linkage rather than a divergent private
  session

#### Scenario: The headless session is confined to the worker domain

- **WHEN** the headless session process executes under supervision
- **THEN** it runs under the worker identity with no service-identity
  privileges, exactly like any other model session

### Requirement: The session bridge operates over the action journal

Every bridge operation that can cause a model side effect SHALL be a journaled
supervised action: action intent committed in its own transaction before any
server request, a dispatch record carrying the session identity and the
headless server's process identity, and a terminal outcome or an explicit
uncertain mark with recorded evidence. Session events SHALL be reconciled
against the journal: an event is never recorded as an outcome without a
journaled action it reconciles.

For an unregistered legacy run, the bridge SHALL create no journal records and
introduce no ledger dependency.

#### Scenario: A prompt is journaled end to end

- **WHEN** the bridge prompts the supervised primary session
- **THEN** the ledger holds the intent committed before the server request, a
  dispatch record bound to the session identity and the server process
  identity, and a terminal or explicitly uncertain outcome with evidence once
  the prompt resolves

#### Scenario: An orphan event is never an outcome

- **WHEN** a session event arrives that matches no journaled action
- **THEN** it is treated as a hint and recorded at most as evidence, never as
  an action outcome

#### Scenario: A legacy run creates no bridge records

- **WHEN** an ordinary, unregistered plan run executes with the bridge
  installed
- **THEN** its execution is unchanged and the supervisor ledger contains no
  bridge actions for that run

### Requirement: Request and action identities are journaled before prompt side effects

Before issuing a prompt, the bridge SHALL record the action identity and a
discoverable request identity in the journal, and SHALL carry the request
identity into the session so it remains discoverable through the lookup
operation afterward. A lost launch acknowledgement SHALL be resolved by
lookup against the recorded request identity — recovering the real prompt's
state — rather than by blindly re-prompting, and a recovered prompt SHALL
reconcile the existing action instead of creating a duplicate dispatch.

#### Scenario: Identities are durable before the side effect

- **WHEN** the process is interrupted after the identities are recorded but
  before the prompt request completes
- **THEN** the reopened ledger contains the action identity and the request
  identity for that prompt

#### Scenario: A lost acknowledgement is recovered by lookup

- **WHEN** a prompt's acknowledgement is lost after the server accepted the
  prompt
- **THEN** the bridge discovers the in-flight prompt through lookup using the
  recorded request identity and reconciles the existing action rather than
  issuing a second prompt

### Requirement: Streamed session events are hints reconciled against authoritative session state

The bridge SHALL treat the streamed session event channel as hints only: no
event SHALL be trusted as an outcome, and every hint SHALL be confirmed by
polling authoritative session state. The bridge SHALL tolerate lost,
duplicate, and out-of-order events and SHALL NOT depend on event replay: when
the stream is disconnected, delivers duplicates, or cannot supply events
missed during a gap, the bridge SHALL converge on the same authoritative state
by polling.

#### Scenario: A duplicate event is applied once

- **WHEN** the same session event is delivered more than once
- **THEN** the duplicate is recognized and no duplicate outcome, evidence, or
  billing is recorded

#### Scenario: A lost stream falls back to polling

- **WHEN** the event stream disconnects while a prompt is in flight
- **THEN** the bridge determines the prompt's outcome by polling authoritative
  session state

#### Scenario: Missed events are never replayed or assumed

- **WHEN** a reconnected stream cannot supply the events missed during the
  disconnection
- **THEN** the bridge polls authoritative session state rather than requesting
  replay or assuming the missed events' contents

### Requirement: Reconnect briefing is composed from the authority store and the ledger

When the service reconnects to an adopted session or starts a new bounded
briefing, the briefing content SHALL be derived from the authority store, the
ledger, active and uncertain actions, budget state, and previous failed
remedies — not from transcript-only replay. The briefing SHALL be explicitly
bounded, and an unreconciled uncertain action SHALL be presented as blocking
state rather than summarized away.

#### Scenario: A restarted session is briefed from durable state

- **WHEN** a primary session is briefed after a service restart
- **THEN** the briefing content is composed from journaled job, action,
  incident, budget, and authority records rather than a transcript replay

#### Scenario: The briefing stays within its bound

- **WHEN** the derived briefing context exceeds the documented bound
- **THEN** the briefing is reduced according to the documented bounding rule
  rather than growing without limit

#### Scenario: An uncertain action blocks in the briefing

- **WHEN** an unreconciled uncertain action exists at briefing time
- **THEN** the briefing presents it as blocking state

### Requirement: Supervisor primary usage flows through the budget reserve and reconcile boundary

Every supervisor-primary prompt SHALL reserve budget through the existing
reservation boundary before the prompt side effect and SHALL reconcile
observed usage afterward through the same boundary as any other supervised
model call. An interrupted or unknown-usage prompt SHALL retain its
reservation rather than releasing it. The bridge SHALL introduce no separate
accounting path for the primary session.

#### Scenario: A reservation exists before the prompt side effect

- **WHEN** a supervisor-primary prompt is dispatched
- **THEN** a budget reservation for the supervisor role is committed before
  the server request is issued

#### Scenario: An interrupted prompt retains its reservation

- **WHEN** a prompt's usage cannot be observed after an interruption
- **THEN** the reservation is retained as unknown or interrupted usage per the
  budget contract rather than released

### Requirement: Concrete supervised agents and the supervision skill are installed and verified

The supervised roles `supervisor`, `acceptance_reviewer`, `fixer`, and
`verifier` SHALL each have a concrete, installed agent definition, and the
primary session SHALL have an installed supervision skill that briefs it on
its bounded loop. Each agent definition SHALL pin its model through the
registered role resolution so the installed agent cannot drift from the job
policy's pinned identity, and SHALL carry a least-privilege permission
contract for that role. Installation SHALL be verified: a missing or
differing installed agent or skill SHALL be reported by the installer's
verification step rather than silently serving a stale contract. The legacy
`opsx-implementer`, `opsx-reviewer`, and `opsx-archiver` agents SHALL NOT be
modified by this contract.

#### Scenario: A supervised role dispatches under its concrete agent

- **WHEN** a supervised job dispatches the `supervisor`,
  `acceptance_reviewer`, `fixer`, or `verifier` role
- **THEN** the session runs under that role's installed concrete agent
  definition, with the model resolved from the registered role pin

#### Scenario: A stale or missing installed agent is reported

- **WHEN** installer verification runs and an installed supervised agent or
  the supervision skill is missing or differs from the repository source
- **THEN** verification reports the differing file by name instead of
  treating the installation as current

#### Scenario: Legacy agents are untouched

- **WHEN** the supervised agents and skill are installed
- **THEN** the installed `opsx-implementer`, `opsx-reviewer`, and
  `opsx-archiver` agent definitions are byte-identical to their prior
  installed form

### Requirement: Every supervised role runs under a constrained tool and subagent allowlist

Each supervised role SHALL run under an explicit per-role contract naming
the tools and subagents it may invoke. A supervised session SHALL NOT
dispatch an agent outside its contract's allowlist, and no supervised role
SHALL perform recursive arbitrary dispatch: an allowlisted delegation target
SHALL NOT itself gain unconstrained delegation. A worker SHALL NOT bypass
its contract through a shell — for example by launching a model client or
another agent runner as a subprocess — and such an attempt SHALL be treated
as a policy violation rather than executed.

#### Scenario: A non-allowlisted subagent dispatch is blocked

- **WHEN** a supervised worker attempts to dispatch a Task agent that its
  role contract does not allowlist
- **THEN** the dispatch is denied by the worker's permission contract and no
  subagent session starts

#### Scenario: A shell bypass attempt is a policy violation

- **WHEN** a supervised worker attempts to invoke a model client or agent
  runner through its shell tool instead of through an allowlisted, journaled
  path
- **THEN** the attempt is blocked or surfaced as a blocked policy violation
  incident, and no ungated model side effect occurs

#### Scenario: A shell bypass is refused at the executable layer

- **WHEN** a supervised worker attempts to reach a model client, an agent
  runner, a nested shell, or a shell indirection through its shell tool
- **THEN** the tracked shell wrapper refuses the command without executing it,
  reports the attempt to the worker-actions endpoint, and a
  `policy_violation` incident is recorded against the job

#### Scenario: A supervised worker request is fully identified

- **WHEN** a supervised worker request reaches the worker-actions endpoint
  without its role, its observed concrete agent, or the job's registered
  service identity
- **THEN** the request is refused with a recorded `policy_violation`, never
  treated as an unauthenticated legacy request

#### Scenario: Delegation is not recursive

- **WHEN** an allowlisted delegation target begins its session
- **THEN** its own permission contract denies arbitrary subagent dispatch,
  so delegation depth is bounded by the contracts rather than by worker
  choice

### Requirement: The primary session reads evidence and invokes only the tracked service tool

The frontier `supervisor` primary session's capability SHALL be bounded to
reading journaled evidence and invoking the tracked service tool — the
scoped, journaled job-service actions exposed through the worker-actions
endpoint. The primary SHALL NOT run arbitrary Bash and SHALL NOT dispatch
arbitrary Task agents. Every service-tool invocation SHALL be journaled as a
supervised action before its side effect, so the primary's decisions are
reconstructible from the ledger rather than from its transcript.

#### Scenario: A service-tool invocation is journaled before execution

- **WHEN** the primary invokes a tracked service-tool action
- **THEN** the action intent is committed to the journal before the action's
  side effect, with the primary's session identity bound to the dispatch

#### Scenario: An arbitrary shell or subagent request is denied

- **WHEN** the primary session attempts to run an arbitrary shell command or
  dispatch an arbitrary Task agent
- **THEN** its permission contract denies the attempt, and the denial does
  not interrupt the supervised job

#### Scenario: The primary holds no operator authority

- **WHEN** the primary session inspects its reachable endpoints
- **THEN** only the restricted worker-actions endpoint is reachable, and no
  operator-endpoint credential or capability is present in its environment

### Requirement: Model credentials and network egress are enforced before a prompt executes

Every supervised prompt SHALL pass a named, fail-closed enforcement step
before the prompt executes, verifying that model traffic flows only through
the trusted model gateway or an equivalently enforced isolated transport.
When neither enforcement path is in place, the step SHALL block the prompt
with a named error before any model side effect, rather than detecting
unenforced usage afterward. No reusable provider credential SHALL be present
in a worker session's environment or filesystem domain; credentials SHALL
remain behind the gateway or enforced transport. The enforcement decision
and its outcome SHALL be recorded as action evidence.

#### Scenario: A prompt is blocked before execution when enforcement is absent

- **WHEN** a supervised prompt is about to execute and neither the trusted
  model gateway nor an equivalently enforced isolated transport is in place
- **THEN** the named enforcement step blocks the prompt with a named error,
  the block is recorded, and no model request is issued

#### Scenario: An arbitrary loopback target is not isolated transport

- **WHEN** a supervised prompt presents a loopback transport target without
  the launched service-owned session server's live fenceable identity and its
  reported address
- **THEN** the isolated-transport path is denied, the enforcement step blocks,
  and no model request is issued

#### Scenario: A model override is refused at dispatch

- **WHEN** a supervised session create or prompt carries a requested model
  that is not the role's exact policy pin
- **THEN** the request is refused with a recorded `policy_violation` before
  any session or prompt side effect, and the pin is never substituted

#### Scenario: Enforcement runs before, not after, the side effect

- **WHEN** a supervised prompt executes under an enforced transport
- **THEN** the journal shows the enforcement step's decision recorded before
  the prompt dispatch record, not a usage observation reconciled afterward

#### Scenario: A worker environment carries no provider credential

- **WHEN** a supervised worker session's environment is inspected at
  dispatch
- **THEN** no reusable provider credential is present, and model access is
  possible only through the enforced path

### Requirement: Worker-initiated delegation is journaled on both dispatch paths

Delegation initiated inside a supervised worker session SHALL be journaled
regardless of the path that carries it: native Task dispatch SHALL report
its session identity through the worker-actions endpoint, and subprocess
dispatch SHALL record its process identity, each bound to its owning action
in the same journal. Delegation that cannot be journaled SHALL NOT be
executed. This extends the dispatch-identity contract: both paths were
recorded at orchestrator dispatch, and worker-initiated delegation inside
the session is recorded with the same lifecycle.

#### Scenario: Native Task delegation is journaled

- **WHEN** an allowlisted Task subagent starts inside a supervised worker
  session
- **THEN** its session identity is reported through the worker-actions
  endpoint and bound to the owning action as journaled evidence

#### Scenario: Subprocess delegation is journaled

- **WHEN** a supervised worker spawns an allowlisted subprocess
- **THEN** the process identity is recorded against the owning action in the
  same journal used for orchestrator dispatch

#### Scenario: Unjournalable delegation does not execute

- **WHEN** a worker attempts delegation for which no identity can be
  recorded through the worker-actions endpoint
- **THEN** the delegation is denied rather than running unjournaled

### Requirement: A spoofed or escalated worker is surfaced as a blocked policy violation

A supervised session whose observed identity, agent, or requested
permissions do not match its registered role contract SHALL be surfaced as a
blocked policy violation incident, and the violating dispatch SHALL NOT
execute. A request to escalate beyond the role's contract — a broader tool
set, a different agent, or an unpinned model — SHALL be refused with the
violation recorded, never silently granted or defaulted.

#### Scenario: A spoofed worker is blocked and recorded

- **WHEN** a session presents an identity or agent that does not match the
  registered role contract for the action it attempts
- **THEN** the dispatch is blocked, a policy-violation incident is recorded
  against the job, and no side effect of the dispatch occurs

#### Scenario: An escalation request is refused, never defaulted

- **WHEN** a supervised worker requests a capability beyond its role
  contract
- **THEN** the request is refused with the violation recorded, and no
  broader permission is substituted

### Requirement: Mechanical work routes to inexpensive agents and hard judgments return to the primary

Mechanical investigation and repair SHALL be routed to the inexpensive
`fixer` agent, and independent validation to the `verifier` agent. Hard
judgments — remedy selection and any accept, fix, or escalate decision —
SHALL return to the primary session and SHALL NOT be delegated to an
expensive subagent. The `verifier` SHALL be independent of the `fixer`: a
separate role with a separate session, so a repair is never validated by the
session that produced it.

#### Scenario: Mechanical repair is routed to the fixer

- **WHEN** the primary selects a mechanical repair from journaled evidence
- **THEN** the repair is dispatched to the `fixer` role under its pinned
  inexpensive model and constrained contract

#### Scenario: A hard judgment returns to the primary

- **WHEN** a supervised decision requires remedy selection or an accept,
  fix, or escalate judgment
- **THEN** the decision is made by the primary session from journaled
  evidence rather than delegated to a subagent

#### Scenario: The verifier is independent of the fixer

- **WHEN** a repair produced by the `fixer` role is validated
- **THEN** the validation runs in a separate `verifier` session whose
  verdict is derived from the actual diff and evidence, not from the fixer
  session's report

### Requirement: Supervised job registration persists the complete job record

The system SHALL provide a supervised job registration that persists, in one
durable transaction: the protected manifest snapshot and its hash, the
repository root and worktree identity, the standing permissions as the
authority configuration, the frozen model selection and inexpensive
allowlist together with the budgets and deadlines as the protected job
policy at operator revision 1, and the primary session linkage
configuration. The registration SHALL be durable before the job can be
started: a lifecycle command targeting a job that was never registered SHALL
be refused with a named unknown-job error.

Registration SHALL fail closed: on a host without a supported isolation
backend it SHALL be refused with the named unsupported-host error and SHALL
record nothing. Registration SHALL record nothing outside the trusted
service-owned storage: no supervised registration state SHALL be written to
the worktree or the JSON execution state.

#### Scenario: Registration persists the full record

- **WHEN** an operator registers a plan as a supervised job on a supported
  host and the ledger is reopened
- **THEN** the job record carries the repository root and worktree identity,
  the protected manifest snapshot and its hash, and the primary session
  linkage configuration, and the protected job policy at operator revision 1
  carries the authority configuration, the frozen model selection and
  allowlist, and the budgets and deadlines

#### Scenario: Registration fails closed on an unsupported host

- **WHEN** registration is attempted on a host without a supported isolation
  backend
- **THEN** it is refused with the named unsupported-host error, no job
  record is created, and no registration state appears in the worktree

#### Scenario: A lifecycle command on an unregistered worktree is refused

- **WHEN** `start`, `resume`, `pause`, `drain`, or `cancel` targets a
  worktree with no registered supervised job
- **THEN** the command fails with a named unknown-job error and no job
  record is created

### Requirement: The lifecycle commands drive the supervised job state machine

A supervised job SHALL follow the state machine `registered → active →
(paused → active)* → completed | failed | cancelled`, with `completed`,
`failed`, and `cancelled` terminal. Every transition SHALL be a durable
ledger transaction.

`start` SHALL transition a `registered` job to `active`, bringing up the
service-owned execution and the primary session. `resume` SHALL transition a
`paused` job back to `active` only after resume revalidation confirms every
relied-upon receipt still matches the current material revision; a gate
whose receipt no longer matches SHALL return to awaiting its defined
authority instead of dispatching.

A transition outside the legal set SHALL be refused with a named
illegal-transition error, and a mutating lifecycle verb targeting a terminal
job SHALL be refused with a named terminal-job error. Neither refusal SHALL
alter the job record.

#### Scenario: Start activates a registered job

- **WHEN** `start` runs for a `registered` job
- **THEN** the job transitions to `active`, the service-owned execution and
  the primary session come up, and the transition is durable across a ledger
  reopen

#### Scenario: Resume revalidates before activating

- **WHEN** `resume` runs for a `paused` job whose relied-upon receipts all
  match the current material revision
- **THEN** the job transitions to `active` and dispatch proceeds using those
  receipts

#### Scenario: Resume with a stale receipt re-arms the gate

- **WHEN** `resume` runs for a `paused` job and an explicit policy revision
  has invalidated a relied-upon receipt
- **THEN** the affected change returns to awaiting its approval authority
  and is not dispatched

#### Scenario: An illegal transition is refused

- **WHEN** a lifecycle verb requests a transition outside the legal set —
  for example `resume` on an `active` job or `start` on a `paused` job
- **THEN** the command fails with a named illegal-transition error and the
  job state is unchanged

#### Scenario: A terminal job refuses mutation

- **WHEN** a mutating lifecycle verb targets a `completed`, `failed`, or
  `cancelled` job
- **THEN** it is refused with a named terminal-job error and the job record
  is unchanged

### Requirement: Pause and drain are explicit stop boundaries with defined in-flight disposition

`pause` and `drain` SHALL each record a durable stop request for the job,
observed at the dispatch boundary before any new side effect. From the
boundary, no new action SHALL be dispatched. The stop request SHALL survive
restarts: a job that restarts between the request and its observance SHALL
still honor it. Recording a stop request SHALL NOT require acquiring the
worktree execution lock, and the request SHALL wake a waiting job through
the same durable receipt mechanism as approval receipts.

The two boundaries differ in in-flight disposition. Under `pause`, in-flight
actions SHALL be interrupted and marked `uncertain` for evidence
reconciliation, and the job SHALL enter `paused`. Under `drain`, in-flight
actions SHALL run to a terminal outcome while no new dispatch begins, and
only then SHALL the job enter `paused`.

#### Scenario: Pause interrupts in-flight work

- **WHEN** `pause` is recorded while an action is in flight
- **THEN** no new action is dispatched, the in-flight action is interrupted
  and marked uncertain for later reconciliation, and the job enters `paused`

#### Scenario: Drain lets in-flight work finish

- **WHEN** `drain` is recorded while an action is in flight
- **THEN** no new action is dispatched, the in-flight action runs to a
  terminal outcome, and the job enters `paused` only afterward

#### Scenario: A stop request survives a restart

- **WHEN** a stop request is recorded and the job's execution restarts
  before observing it
- **THEN** the restarted execution observes the durable request at its
  dispatch boundary and honors it before dispatching any new action

#### Scenario: A stop request needs no execution lock

- **WHEN** a pause or drain request is recorded while the job waits on a
  human-only approval and holds no execution lock
- **THEN** the request is durably recorded and the job is woken to observe
  it, without any lock acquisition

### Requirement: Cancellation is a terminal transaction with explicit effects

`cancel` SHALL record the `cancelled` terminal state in one durable
transaction. In-flight actions SHALL be failed with a cancellation reason,
or marked `uncertain` where the outcome cannot be confirmed, under the
journal's existing semantics. Cancellation SHALL terminate the job's
ownership: a new registration for the same worktree SHALL become legal once
the prior job is `cancelled`, under the single-supervised-job-per-worktree
invariant.

A cancelled job SHALL NOT be resumable: every later receipt, stop request,
or lifecycle verb for the job SHALL be refused with a named terminal-job
error, and no transition out of `cancelled` SHALL exist.

#### Scenario: Cancel records the terminal state with in-flight disposition

- **WHEN** `cancel` runs for a non-terminal job with an action in flight
- **THEN** the job records `cancelled` durably, and the in-flight action is
  failed with a cancellation reason or marked uncertain for reconciliation

#### Scenario: A new registration is legal after cancellation

- **WHEN** a job for a worktree has reached `cancelled` and the operator
  registers a new supervised job for the same worktree
- **THEN** the new registration succeeds and the cancelled job's record is
  unchanged

#### Scenario: A cancelled job refuses all further requests

- **WHEN** an approval receipt, a stop request, or a lifecycle verb targets
  a `cancelled` job
- **THEN** it is refused with a named terminal-job error and nothing is
  recorded

### Requirement: A human wait is durable job state with receipt-driven wake-up

When a supervised job reaches a human-only gate, the wait SHALL be persisted
as normal durable job state carrying the awaiting checkpoint, the material
revision, and the wait start, and SHALL survive restarts. During a human
wait the job SHALL retain permanent ownership without holding the worktree
execution lock.

There SHALL be no LLM polling and no stall recovery for a human wait: the
job SHALL dispatch no model action while the wait lasts, and no automated
recovery SHALL fire on its account. The wait SHALL consume no execution
deadline or elapsed budget, under the existing deadline-separation
requirement. The job SHALL wake through the durable receipt scan, and
resuming after the wait SHALL revalidate receipts before dispatch.

#### Scenario: The wait is durable across restarts

- **WHEN** a supervised job records a human wait and the service restarts
  before any approval arrives
- **THEN** the reopened job still carries the wait with its checkpoint,
  material revision, and start, and dispatches no model action for it

#### Scenario: No polling during a human wait

- **WHEN** a supervised job is waiting on a human-only approval
- **THEN** it issues no model dispatch and runs no stall recovery while the
  wait lasts, however long it lasts

#### Scenario: An approval receipt wakes the job

- **WHEN** the operator's approval receipt is recorded while the job waits
- **THEN** the job is woken through the durable receipt scan without the
  execution lock, revalidates the receipt against the current material
  revision, and resumes dispatch

### Requirement: Supervised completion is verified from plan, archive, and check evidence

A supervised job SHALL reach `completed` only when every enabled change in
the plan is verified done from the existing ground truth: archive evidence
per change, the post-archive fast checks, and post-archive cleanliness —
never from a worker or primary session's claim of done. A completion claim
without that evidence SHALL NOT complete the job.

When completion evidence needs revalidation — a failed post-archive fast
check or an unverified or partial archive — the lifecycle SHALL rerun an
appropriate fresh review through the existing implement/review/archive loop
rather than treating a prior archive as proof of done. Revalidation SHALL be
bounded by the existing round budgets, so it cannot loop without limit.

Completion of non-supervised runs SHALL be determined exactly as before.

#### Scenario: Completion only from evidence

- **WHEN** every enabled change in a supervised job's plan is verified done
  from archive evidence with the post-archive fast checks and cleanliness
  passing
- **THEN** the job reaches `completed`

#### Scenario: A worker claim does not complete the job

- **WHEN** a worker or the primary session reports the plan done but archive
  evidence or fast checks do not verify
- **THEN** the job does not reach `completed`, and the unverified change
  continues through the existing loop

#### Scenario: A failed fast check triggers fresh review

- **WHEN** a post-archive fast check fails or an archive cannot be verified
  for a change in a supervised job
- **THEN** the lifecycle reruns a fresh review for that change through the
  existing loop instead of treating the prior archive as proof of done, and
  repeated failure is bounded by the change's round budget

#### Scenario: A legacy run's completion is unchanged

- **WHEN** an ordinary, unregistered plan run finishes
- **THEN** its completion is determined exactly as before this change, with
  no supervised completion logic involved

### Requirement: The implement/review/archive loop remains the progression authority

The lifecycle surface SHALL drive progression only through the existing
implement/review/archive loop and its gates: `pause_before` approval gates,
`review_created` acceptance, and the task-completeness gates SHALL apply
exactly as they do for the run engine today. The lifecycle SHALL introduce
no new DAG, stage machine, or dispatcher, and no lifecycle command SHALL
mark a change done, release a gate, or satisfy a checkpoint outside the
loop's existing authorities.

#### Scenario: A gated change still awaits its approval authority

- **WHEN** a supervised job reaches a change gated with `pause_before =
  true`
- **THEN** the gate is released only through the approval authority the
  broker enforces for it, exactly as for the unsupervised engine

#### Scenario: A lifecycle command cannot progress a change

- **WHEN** any lifecycle command other than the loop's own dispatch runs for
  a supervised job
- **THEN** no change is marked done, no gate is released, and no checkpoint
  is satisfied by that command

### Requirement: The supervised acceptance stage reviews real artifacts before archive

The run engine SHALL provide a supervised `acceptance` stage distinct from the
implementation `review` stage. When a registered supervised job's change
passes implementation review, the engine SHALL dispatch the acceptance stage
before archive through the existing journaled, gated dispatch boundary, using
the job policy's pinned `acceptance_reviewer` role and its installed
`opsx-acceptance-reviewer` agent. The acceptance reviewer SHALL inspect the
change's real artifacts — the accepted plan and its dependency edges, the
change's proposal, design, tasks, and spec deltas with their delta identity,
and the referenced canonical specs — and SHALL return exactly one of `accept`,
`fix`, or `escalate`. A worker or primary claim of completion, or a summary
that is not the artifact, SHALL NOT be accepted as the review input.

The stage SHALL introduce no new DAG or stage machine: the existing
implement/review/archive loop and its gates remain the progression authority,
and acceptance only gates advancement to archive.

#### Scenario: Acceptance runs between review and archive

- **WHEN** a change in a registered supervised job returns an implementation
  review `pass`
- **THEN** the engine dispatches the `acceptance` stage under the pinned
  `acceptance_reviewer` role before any archive dispatch

#### Scenario: Acceptance is not the implementation review

- **WHEN** an acceptance verdict is recorded for a change
- **THEN** the implementation review's stored verdict and findings are
  unchanged, and the acceptance outcome is recorded as a separate stage result

#### Scenario: A claim is not the artifact

- **WHEN** the acceptance reviewer is asked to judge a change using only a
  worker summary or transcript rather than the change's real artifacts
- **THEN** that input does not satisfy the stage

### Requirement: Acceptance verdicts bind to an exact artifact revision

The engine SHALL compute an acceptance artifact revision as a content hash
over the protected canonical plan manifest snapshot and its dependency edges,
the change's authored artifacts (proposal, design, tasks), its spec deltas with
their delta identity (delta operation plus requirement name), the referenced
canonical specs, and the tracked change diff. The revision SHALL be computed
on canonicalized inputs so an unrelated path or ordering difference does not
change it.

The acceptance verdict SHALL be recorded against the exact artifact revision
it reviewed. Before the service records an `accept`, the engine SHALL recompute
the revision and reject a verdict whose revision no longer matches the
artifacts under review as stale; a stale verdict SHALL NOT satisfy the stage,
and the stage SHALL be re-run over the new revision. Acceptance revision
binding SHALL be separate from approval checkpoint binding: an acceptance
revision SHALL NOT be the broker's material gate hash, and an approval receipt
SHALL NOT satisfy the acceptance stage.

The engine SHALL derive an authoritative artifact-identity list from the
captured review set — the protected manifest snapshot hash, every dependency
edge, and every file artifact — and SHALL present it to the acceptance
reviewer with the manifest/dependency ground truth. An `accept` verdict SHALL
acknowledge exactly that authoritative set: a partial, arbitrary, or
manifest/dependency-omitting `accept` is a contract violation and SHALL NOT
satisfy the stage or advance the change to archive.

#### Scenario: The revision is captured before review

- **WHEN** the acceptance stage begins for a change
- **THEN** the artifact revision is computed and recorded before the
  acceptance reviewer is dispatched against it

#### Scenario: A changed artifact makes a verdict stale

- **WHEN** a change's plan, dependency edges, or reviewed artifacts change
  after an acceptance verdict was recorded
- **THEN** the recorded verdict's revision no longer matches the current
  artifacts and the verdict does not satisfy the stage

#### Scenario: A stale verdict is rejected

- **WHEN** an `accept` verdict for an earlier artifact revision is presented
  for a change whose artifacts have since changed
- **THEN** the stage refuses to accept it as satisfied and requires a fresh
  acceptance

#### Scenario: Approval binding does not satisfy acceptance

- **WHEN** a change holds a valid approval receipt bound to its material gate
  revision but no matching acceptance verdict
- **THEN** the acceptance stage is not satisfied by the approval receipt

#### Scenario: An accept must acknowledge the complete authoritative set

- **WHEN** an `accept` verdict names only a subset of the authoritative
  artifact identities, names arbitrary paths, or omits the manifest/dependency
  ground truth
- **THEN** the verdict is rejected as a contract violation and the change does
  not advance to archive

#### Scenario: A complete acknowledgment advances

- **WHEN** an `accept` verdict names exactly the authoritative artifact
  identities for the reviewed revision and is not stale
- **THEN** the verdict satisfies the stage and the change advances to archive

### Requirement: Acceptance re-validates creation evidence at the reviewed revision

At the start of an acceptance attempt, before dispatching the acceptance
reviewer, the engine SHALL run the change's configured created-change check
(the `created_check` command, `openspec validate <change> --strict` by default)
and capture the artifact revision immediately after it, so a verdict binds to
validated content. A failing or timed-out created-change check SHALL block the
stage with the recorded reason, and the service SHALL NOT record an `accept`
for a revision whose creation evidence failed.

#### Scenario: The created-change check and revision capture precede the verdict

- **WHEN** the acceptance stage begins
- **THEN** the configured created-change check runs and the artifact revision
  is captured immediately after it, before the service records any `accept`

#### Scenario: A failing created-change check blocks acceptance

- **WHEN** the created-change check exits non-zero or times out
- **THEN** the stage is blocked with the recorded check reason and no `accept`
  is recorded

### Requirement: Acceptance fix routes to the cheap fixer and a fresh acceptance

On a `fix` outcome, the verdict SHALL name the mechanical defect precisely
enough for the job's pinned `fixer` role to repair. The repair SHALL be
consumed only after the independent `verifier` validates the actual diff and
records its verdict under the existing repair-consumability contract; a
fixer's own account SHALL NOT be sufficient. After the verified repair, the
engine SHALL run a fresh acceptance over the new artifact revision. The route
SHALL be bounded by the change's existing round budget, and exhaustion SHALL
fail the change with a reason naming the unrepaired defect.

#### Scenario: A fix is routed to the fixer

- **WHEN** the acceptance reviewer returns `fix`
- **THEN** the named mechanical defect is dispatched to the pinned `fixer`
  role and not to an expensive subagent

#### Scenario: A fix is verified before it is consumed

- **WHEN** a fixer reports the acceptance defect repaired
- **THEN** the repair is not consumed until the independent `verifier`
  validates the real diff and records its verdict

#### Scenario: A fresh acceptance follows a verified fix

- **WHEN** a verified repair changes the change's artifacts
- **THEN** the engine recomputes the artifact revision and runs a fresh
  acceptance over the new revision

#### Scenario: The fix route is bounded

- **WHEN** the acceptance fix route reaches the change's round budget without
  an `accept`
- **THEN** the change fails with a reason naming the unrepaired defect

### Requirement: Acceptance escalation returns the hard judgment to the primary

On an `escalate` outcome, the engine SHALL return the judgment to the primary
session rather than deciding it with a subagent, and SHALL NOT silently
default to `accept` or `fix`. The escalation SHALL be recorded as blocking
state, and the change SHALL NOT advance to archive while it is unresolved.

#### Scenario: Escalation goes to the primary

- **WHEN** the acceptance reviewer returns `escalate`
- **THEN** the judgment is returned to the primary session and is not decided
  by the acceptance reviewer or any other subagent

#### Scenario: Escalation is never silently defaulted

- **WHEN** an `escalate` outcome is received
- **THEN** the stage does not treat it as `accept` or `fix`, and records the
  escalation as unresolved blocking state

#### Scenario: An unresolved escalation blocks archive

- **WHEN** a change has an unresolved acceptance escalation
- **THEN** the change does not advance to archive

### Requirement: The acceptance stage never releases a human gate or replaces the review gate

The acceptance verdict SHALL be a review outcome, not an approval authority.
It SHALL NOT release a `pause_before` or `pause_before_human_only` gate,
satisfy the operator `acceptance` receipt for an orchestrator-created change,
or substitute for the implementation review verdict. The implement, review,
and archive gates, the operator acceptance receipt, and the broker's approval
authority SHALL remain the sole authorities for the decisions they own; the
acceptance stage only gates advancement to archive.

#### Scenario: A human-only gate is not released by acceptance

- **WHEN** an `accept` verdict is recorded for a change whose gate resolves to
  human-only
- **THEN** the gate remains unreleased until an operator approval receipt
  releases it

#### Scenario: Operator acceptance of a created change is still required

- **WHEN** an orchestrator-created change holds an `accept` verdict but no
  operator `acceptance` receipt
- **THEN** the created-change acceptance remains outstanding

#### Scenario: The review gate is not replaced

- **WHEN** the acceptance stage returns `accept` for a change
- **THEN** the implementation review's gate behavior, verdict, and findings
  are unchanged and still apply

### Requirement: Incidents have a durable lifecycle linked to their attempt signature

Each incident SHALL carry the stable attempt signature of the failure class and
material identity it represents, so a later attempt on the same signature can
find the incident it belongs to. An incident SHALL progress through a defined
state lifecycle: from `open` to `recovering`, and then to a terminal `resolved`
or `escalated` state. `resolved` and `escalated` SHALL be terminal.

Every incident state transition SHALL be a durable ledger transaction, SHALL be
guarded against an illegal source state, and SHALL be refused with a named
error without altering the record when it is not legal. A terminal incident
SHALL refuse all further transitions. Incident state and signature SHALL
survive a ledger reopen and a process restart.

#### Scenario: An incident records its signature and starts open

- **WHEN** a supervised failure is recorded as an incident
- **THEN** the incident persists with its attempt signature in the `open`
  state and the signature is queryable after the ledger is reopened

#### Scenario: An incident is resolved through the lifecycle

- **WHEN** recovery succeeds for an open incident
- **THEN** the incident moves through `recovering` to `resolved`, durably, and
  remains resolvable after a ledger reopen

#### Scenario: An illegal incident transition is refused

- **WHEN** a transition is requested from a state that is not a legal source
  for it
- **THEN** it is refused with a named error and the incident state is unchanged

#### Scenario: A terminal incident refuses further transitions

- **WHEN** a transition is requested on a `resolved` or `escalated` incident
- **THEN** it is refused with a named error and the incident record is
  unchanged

### Requirement: Known failure classes map to bounded recovery paths

The system SHALL define a closed set of known failure classes and, for each, a
bounded recovery path. The known classes SHALL include: an invalid structured
result after the dispatch's built-in retries are exhausted; a transient
provider failure; a permanent provider error; a delta `MODIFIED` identity
mismatch; a dirty worktree; recurring review findings; a process interruption
with an unreconciled action; and a partial archive with post-archive
fast-check failures.

A failure that does not classify into a known recoverable class SHALL NOT be
recovered automatically: it SHALL be recorded and surfaced for operator
triage. Recovery SHALL never invent a repair path for an unclassified failure.

#### Scenario: Each known class resolves to its bounded path

- **WHEN** a failure is classified for each known failure class
- **THEN** each resolves to its defined bounded recovery path and none is left
  without one

#### Scenario: An unclassified failure is not auto-recovered

- **WHEN** a failure does not match any known recoverable class
- **THEN** no recovery is attempted, and the failure is recorded and surfaced
  for operator triage

### Requirement: Transient provider failures retry under a bound while permanent errors escalate

A transient provider failure — a server error, a timeout, or a connection
reset — SHALL be classified as transient and retried only under a bounded
retry schedule with a capped delay and a limited attempt count. Every retry
SHALL be recorded durably.

A permanent provider error — an authentication, authorization, billing, hard
quota, or configuration error — SHALL be classified as permanent and SHALL NOT
be retried. It SHALL be escalated for operator action instead.

Classification SHALL be evidence-based, and a failure whose class cannot be
determined SHALL be treated as permanent rather than retried. Retrying SHALL
not require a worker, agent, or model decision.

#### Scenario: A transient provider failure retries under a bound

- **WHEN** a dispatch fails with a transient provider server error
- **THEN** it is retried under the bounded schedule, each retry is recorded,
  and exceeding the bound surfaces the failure instead of retrying further

#### Scenario: A permanent provider error is not retried

- **WHEN** a dispatch fails with a permanent authentication, billing, quota, or
  configuration error
- **THEN** no retry is attempted and the failure is escalated for operator
  action

#### Scenario: An undetermined failure class is not retried

- **WHEN** a failure cannot be classified as either transient or permanent
- **THEN** it is treated as permanent and is not retried automatically

### Requirement: Recovery follows a primary-chosen remedy, a cheap fixer, and independent verification

Recovery SHALL NOT replay blindly. For a recoverable incident, the frontier
primary SHALL choose a remedy from the closed set the incident's failure class
permits, based on recorded evidence, and the choice SHALL be journaled before
its side effect. A chosen remedy outside the class's permitted set SHALL be
refused and recorded as a policy violation.

The mechanical repair SHALL be applied by the cheap fixer role. The repair
SHALL be validated by an independent verifier in a session distinct from the
fixer's, reviewing the actual diff. A commit, reset, or resume that consumes a
recovery SHALL proceed only when the verifier's verdict passes and confirms
the reviewed diff; a fixer's own report SHALL never self-certify a repair, a
same-session verdict SHALL NOT count as independent, and a missing or
contradicting verdict SHALL block consumption.

#### Scenario: The primary chooses a remedy from evidence

- **WHEN** a recoverable incident is presented with its evidence
- **THEN** the primary's chosen remedy is recorded before the repair's side
  effect, and a remedy outside the class's permitted set is refused as a
  policy violation

#### Scenario: A fixer claim alone does not consume a repair

- **WHEN** a fixer reports a repair but no independent verifier verdict exists
- **THEN** the commit, reset, or resume is blocked and no recovery effect
  occurs

#### Scenario: A same-session verdict is not independent

- **WHEN** the verifier session is the fixer session
- **THEN** the verdict does not satisfy the recovery gate and the effect is
  blocked

#### Scenario: A verified diff unlocks the recovery effect

- **WHEN** an independent verifier returns a passing verdict that confirms the
  repair against the actual diff
- **THEN** the authorized commit, reset, or resume may proceed

### Requirement: Recovery effects are authorized by an operator-established standing grant

Recovery effects that change durable state — a commit, a reset, or a resume —
SHALL be authorized by a standing grant recorded as durable protected job
policy. The standing grant SHALL be established only by an explicit operator
revision, SHALL name the recovery effects the job may perform unattended and
their bounds, and SHALL survive restarts and `opsx-plan reset` unchanged.

Recovery SHALL consume a standing grant only after the independent verifier
verdict passes. No worker, model session, fixer, verifier, or automated path
SHALL create, widen, or bypass a standing grant; a recovery effect that is not
covered by the grant, or that would exceed its bounds, SHALL be escalated
rather than performed. An absent standing grant SHALL authorize no recovery
effect.

#### Scenario: A covered effect runs under the grant

- **WHEN** a verified recovery requests a commit, reset, or resume that the
  job's standing grant covers and that is within its bounds
- **THEN** the effect proceeds and the grant is recorded as the authorization

#### Scenario: An uncovered effect is escalated

- **WHEN** a verified recovery requests an effect that the standing grant does
  not cover or that would exceed its bound
- **THEN** the effect is not performed and the incident is escalated for
  operator action

#### Scenario: A worker cannot create or widen a grant

- **WHEN** any non-operator path attempts to create, widen, or bypass a
  standing grant
- **THEN** the attempt is refused and the stored policy is unchanged

#### Scenario: An absent grant authorizes nothing

- **WHEN** a job has no standing grant and a verified recovery requests a
  durable effect
- **THEN** the effect is not performed and the request is escalated

### Requirement: Each failure class repairs without altering canonical intent or discarding unrelated work

The delta `MODIFIED` identity mismatch repair SHALL restore the delta's
requirement identity to match the canonical specification while preserving the
canonical intent: the canonical specification SHALL remain the authority for
the requirement's meaning, and the repair SHALL NOT rewrite canonical
requirement semantics to match the delta.

The dirty-worktree recovery SHALL preserve all unrelated user work: tracked
modifications, staged changes, and untracked files that are not part of the
authorized repair SHALL remain intact. Recovery SHALL NOT discard, reset, or
overwrite unrelated work, and SHALL change only the paths its authorized
repair names.

#### Scenario: Delta identity repair preserves canonical intent

- **WHEN** a delta `MODIFIED` requirement identity does not match the
  canonical specification and is repaired
- **THEN** the repaired identity matches the canonical requirement while the
  canonical requirement's meaning is preserved and not rewritten from the
  delta

#### Scenario: Unrelated worktree work is preserved

- **WHEN** the worktree is dirty with modifications, staged changes, and
  untracked files unrelated to the authorized repair and recovery proceeds
- **THEN** all of that unrelated work remains intact, and only the authorized
  repair's paths are changed

#### Scenario: Discarding unrelated work is refused

- **WHEN** a proposed recovery remedy would discard or reset unrelated
  worktree work
- **THEN** it is refused as outside the permitted remedy set and the unrelated
  work is not touched

### Requirement: Partial archive and failed fast checks require fresh review

A partial archive, or an archive whose post-archive fast checks fail, SHALL NOT
be treated as proof of done. Recovery SHALL revalidate the affected change
against the same archive and check evidence an unsupervised run uses, and SHALL
rerun an appropriate fresh review through the existing implement/review/archive
loop when rework is required. Recovery SHALL introduce no separate completion
authority, and the fresh review SHALL be bounded by the change's existing
round budget.

#### Scenario: A partial archive is not treated as done

- **WHEN** recovery finds a change whose archive is partial or whose
  post-archive fast check failed
- **THEN** the change is not treated as done and an appropriate fresh review is
  run through the existing loop

#### Scenario: Recovery adds no completion authority

- **WHEN** recovery repairs archive or completion material
- **THEN** completion is still determined from the existing archive and check
  evidence, and no recovery outcome by itself marks a change or plan complete

### Requirement: A root runtime defect is reported and never self-repaired

A failure whose root cause is a defect in the installed supervision runtime or
service SHALL be classified as a runtime defect. Recovery SHALL record it as an
operator blocker for operator or repository work and SHALL perform no
self-repair: it SHALL NOT edit the installed runtime or service code, reload or
redeploy the service, or dispatch a repair against the installed service.

#### Scenario: A runtime defect becomes an operator blocker

- **WHEN** a failure classifies as a root runtime defect in the installed
  service
- **THEN** recovery records an operator blocker naming the defect and performs
  no automated repair

#### Scenario: The service is never self-edited or self-deployed

- **WHEN** a runtime defect is recorded
- **THEN** no installed runtime or service file is modified and no service
  reload, redeploy, or self-dispatch occurs

### Requirement: Recovery attempts are bounded and durable across reset

Each recovery attempt SHALL be recorded durably under the incident's attempt
signature, and identical recovery attempts for the same signature SHALL be
bounded by the job policy's incident-attempt limit. Signatures and their counts
SHALL survive `opsx-plan reset`: a reset SHALL NOT erase, reduce, or re-baseline
them, and an identical incident that recurs after a reset SHALL continue to
accumulate against the same bound.

When the bound is reached, further identical recovery attempts SHALL be refused
with a named bounded-attempts state and the incident SHALL be escalated for
operator action rather than looping.

#### Scenario: Identical recovery attempts are bounded

- **WHEN** the same incident signature recurs and recovery is attempted beyond
  the job's incident-attempt limit
- **THEN** further identical recovery attempts are refused with a named
  bounded-attempts state and the incident is escalated

#### Scenario: The bound survives a reset

- **WHEN** identical incidents recur across `opsx-plan reset`
- **THEN** their attempt counts accumulate across the resets and the bound is
  not erased or refreshed

### Requirement: Operator surfaces project supervisor state read-only from the ledger

The operator-facing supervision projection SHALL be derived read-only from the
supervisor ledger and the existing plan state. Building the projection SHALL
NOT mutate the ledger, the JSON execution state, or any other durable record,
and SHALL NOT require the worktree execution lock or a live service. When no
supervised job is registered for the resolved plan or worktree, the projection
SHALL be empty and SHALL NOT alter the existing output.

#### Scenario: Projection is read-only

- **WHEN** the projection is built for a registered job
- **THEN** the ledger and JSON execution state are unchanged and no execution lock is acquired

#### Scenario: No registered job yields an empty projection

- **WHEN** the projection is built for a plan with no registered supervised job
- **THEN** it is empty and the existing status and report output is unchanged

### Requirement: The supervision projection exposes job, action, incident, evidence, usage, wait, and budget-limit state

For a registered supervised job the projection SHALL expose: job identity and
its `run_id` linkage, job state and progress timestamps; recent and in-flight
actions with their journal state; open and recent incidents with their
signatures and states; the evidence recorded for actions; observed usage and
budget consumption reconciled against the protected policy limits; open human
and stop waits; and the active policy revision. The projection SHALL include
the evidence and human-approval briefing so an operator can see why a job is
waiting and what authorized the most recent approval.

#### Scenario: A supervised job projects its full state

- **WHEN** the projection is built for an active job with actions, an open human wait, and reconciled usage
- **THEN** it reports the job state, the action and incident views, the evidence, the observed usage against the policy limits, the open wait, and the policy revision

#### Scenario: The evidence and human-approval briefing is recorded

- **WHEN** a job is waiting on a human-only gate
- **THEN** the projection includes the evidence and the human-approval briefing for that wait

### Requirement: Steering requests carry a durable request identity and a safe-boundary acknowledgement

Every operator steering request for a registered supervised job — a policy
revision, pause-after-change, stop or retry, or cancel — SHALL be recorded as
a durable broker transaction that carries a stable request identity. The
service SHALL acknowledge the request only when it reaches a safe boundary for
that request's kind, and the acknowledgement SHALL record the boundary
reached and reference the request identity. A request SHALL remain
acknowledgeable across a service restart, and a stale or duplicated request
SHALL NOT be acknowledged twice.

#### Scenario: A steering request is request-identified and acknowledged at a safe boundary

- **WHEN** an operator submits a steering request and the service reaches the corresponding safe boundary
- **THEN** one durable acknowledgement is recorded referencing the request identity and naming the boundary reached

#### Scenario: A steering request survives restart

- **WHEN** the service stops and restarts with an unacknowledged steering request
- **THEN** the request is still present, is acknowledged once at the next safe boundary, and is not acknowledged twice

### Requirement: Steering notifications are deduplicated across reboot and never lose a gate or approval

Steering and gate notifications SHALL be deduplicated durably so that a reboot
does not replay a notification already delivered, and the durable record of a
gate, approval, or steering request SHALL be committed independently of
notification delivery. A notification failure SHALL NOT lose, delay, or alter
a gate or approval, and satisfying the gate SHALL NOT depend on a
notification being re-sent.

#### Scenario: A reboot does not replay a delivered notification

- **WHEN** a notification has been delivered for a receipt and the service reboots
- **THEN** no duplicate notification is emitted for that receipt

#### Scenario: A notification failure does not lose a gate

- **WHEN** notification delivery fails for a receipt that records an approval
- **THEN** the approval remains durable and satisfies the gate without a re-sent notification

### Requirement: Supervision identities stay distinct from and linked to the run identity

Job, action, incident, and steering-request identifiers SHALL remain distinct
from each other and from the plan `run_id`, and supervision records SHALL
carry the owning `run_id` as link data without redefining it. The projection
SHALL report supervision identifiers separately from `run_id` while exposing
the linkage.

#### Scenario: Job and action ids are distinct from run_id

- **WHEN** a supervised job is projected
- **THEN** job and action identifiers are reported separately from `run_id` and the records carry the owning `run_id` as link data

### Requirement: Cost-per-correct-completion is a defined metric without empirical promises

The projection SHALL define cost-per-correct-completion as the reconciled
supervised cost divided by the count of changes that reached verified
completion without a rework incident, and SHALL present it as a metric
definition with its inputs and limitations named. It SHALL NOT assert an
empirical performance, savings, or quality promise.

#### Scenario: The metric is reported as a definition

- **WHEN** supervised usage and completion evidence exist for a job
- **THEN** the projection reports cost-per-correct-completion with its definition, inputs, and stated limitations, and makes no performance claim

### Requirement: The watchdog is a deterministic service-owned loop with no control-channel dependency

The system SHALL provide a watchdog that supervises registered supervised jobs
from within the trusted service domain. The watchdog loop SHALL be
deterministic: its classification and reconstitution decisions SHALL be
functions of durable ledger state, the authoritative plan state, the recorded
execution identity, and the clock alone, with no dependency on a terminal, a
streamed control channel, or any ephemeral in-memory state.

A tick interrupted by a restart SHALL produce the same decision when it is
re-evaluated from the same durable state, and the loop SHALL run unattended
under the service host with no attached terminal.

#### Scenario: A decision is reproducible from durable state

- **WHEN** a tick is evaluated for a job, then the process is interrupted, and
  the same tick is evaluated again from the same durable state and clock
- **THEN** the two evaluations produce the same classification and the same
  reconstitution decision

#### Scenario: The loop runs with no terminal attached

- **WHEN** the service host runs without an attached terminal or control
  channel
- **THEN** the watchdog still evaluates jobs and records its decisions

### Requirement: Liveness, progress, and deadline are independent watchdog signals

The watchdog SHALL derive three independent signals for each supervised job:

- a **liveness** signal, true only when the job's recorded execution identity
  matches a live process on the current boot (the recorded process start time
  and boot identity, never a bare process id);
- a **progress** signal, true only when the job's durable journal or evidence
  has advanced within the configured progress window; and
- a **deadline** signal, true when the job's execution-elapsed time has reached
  its policy execution deadline, excluding expected human-wait duration.

The signals SHALL be computed and reported separately: a live job with no
recent progress and a job with recent progress but no live owner SHALL be
distinguishable, and no signal SHALL be inferred from another.

#### Scenario: Liveness and progress are reported separately

- **WHEN** a job's recorded owner is a live process but its journal has not
  advanced within the progress window
- **THEN** the liveness signal is true and the progress signal is false, and
  both are reported

#### Scenario: Progress is reported without liveness

- **WHEN** a job's journal advanced recently but its recorded execution
  identity does not match a live process
- **THEN** the progress signal is true and the liveness signal is false

#### Scenario: The deadline signal excludes a human wait

- **WHEN** a job has been waiting on a human-only gate for longer than its
  remaining execution deadline
- **THEN** its deadline signal reflects execution-elapsed time only, with the
  human-wait duration excluded

### Requirement: Jobs are classified as live, quiet, stalled, dead, or an expected human wait

The watchdog SHALL classify each registered supervised job into exactly one of
`live`, `quiet`, `stalled`, `dead`, or `expected_human_wait`. Classification
SHALL be a pure decision over the job's durable state and the three signals and
SHALL NOT itself dispatch, repair, or mutate state.

An open human wait SHALL classify as `expected_human_wait` and take precedence
over the other classes. A job whose recorded owner is live and whose progress
signal is fresh SHALL classify as `live`. A job whose owner is live but whose
progress window has elapsed short of the stall threshold SHALL classify as
`quiet`. A job that is not making progress beyond the stall threshold, or whose
execution deadline has been reached while still active, SHALL classify as
`stalled`. A job that is active and non-terminal whose recorded owner is not
live and is verified quiesced SHALL classify as `dead`.

#### Scenario: An expected human wait takes precedence

- **WHEN** a job has an open human wait and its owner is not live
- **THEN** it classifies as `expected_human_wait`, not `dead`

#### Scenario: A live job making progress is classified live

- **WHEN** a job's owner is live and its journal advanced within the progress
  window
- **THEN** it classifies as `live`

#### Scenario: A live job with no recent progress is quiet

- **WHEN** a job's owner is live and its journal has not advanced within the
  progress window but is short of the stall threshold
- **THEN** it classifies as `quiet`

#### Scenario: No progress beyond the stall threshold is stalled

- **WHEN** a job is active and its journal has not advanced beyond the stall
  threshold, or its execution deadline has been reached
- **THEN** it classifies as `stalled`

#### Scenario: A quiesced non-live owner is dead

- **WHEN** a job is active, non-terminal, and its recorded owner is not live
  and is verified quiesced
- **THEN** it classifies as `dead`

### Requirement: The watchdog takes no model or recovery action during an expected human wait

For a job classified as `expected_human_wait`, the watchdog SHALL take no LLM,
dispatch, recovery, or reconstitution action. A human wait SHALL be treated as
normal durable state, and the watchdog SHALL NOT poll a model, restart the
execution, or fail the job on its account, however long the wait lasts. The
job SHALL wake only through the existing durable receipt scan.

#### Scenario: No action is taken during a human wait

- **WHEN** a job is classified as `expected_human_wait` across repeated ticks
- **THEN** no model dispatch, recovery, or reconstitution is performed and the
  wait remains intact

#### Scenario: The deadline does not fail a human wait

- **WHEN** a human wait lasts longer than the job's execution deadline
- **THEN** the watchdog takes no failing or restarting action for it

### Requirement: Boot reconciliation scans supervised jobs and reconnects before any respawn

On service start, the watchdog SHALL scan the registered supervised jobs and
reconcile each non-terminal job against the ledger and the authoritative plan
state. For a job with a recorded session identity, the watchdog SHALL attempt
to reconnect and adopt the existing session before considering any respawn. A
job whose recorded session is still live SHALL be adopted and SHALL NOT be
respawned; only a job with no adoptable live session SHALL become a
reconstitution candidate. Terminal jobs SHALL be left untouched.

#### Scenario: Boot reconciles each non-terminal job

- **WHEN** the service starts with registered non-terminal supervised jobs
- **THEN** the watchdog scans each one, reconciles it against the ledger and
  authoritative state, and records its classification

#### Scenario: A live session is adopted instead of respawned

- **WHEN** a job's recorded session is still live at boot
- **THEN** the watchdog reconnects and adopts that session and does not spawn a
  replacement

#### Scenario: A terminal job is untouched

- **WHEN** the boot scan encounters a terminal supervised job
- **THEN** no classification action, reconnect, or reconstitution is performed
  for it

### Requirement: Reconstitution happens only after verified quiescence

Before any reconstitution of a job, the watchdog SHALL verify that the prior
worker is quiesced: the kernel-held execution lock is no longer held and no
live process matches the recorded fencing identity. The watchdog SHALL refuse
to reconstitute while a live process still matches the recorded identity, even
when the kernel-held lock has been released, and SHALL NOT interrupt a live
worker's work. Every reconstitution SHALL record which prior owner was replaced.

#### Scenario: A live owner blocks reconstitution

- **WHEN** a job's recorded fencing identity still matches a live process,
  including when the kernel-held lock has been released
- **THEN** reconstitution is refused with a named error and the live owner's
  work is not interrupted

#### Scenario: Verified quiescence permits reconstitution

- **WHEN** the execution lock is free and no live process matches the recorded
  fencing identity
- **THEN** the watchdog may reconstitute the job and records the prior owner it
  replaced

### Requirement: An unreconciled uncertain action blocks reconstitution

When a job has an action that is uncertain or unreconciled, the watchdog SHALL
treat the job as blocking: it SHALL NOT respawn, replay, or otherwise
reconstitute the job until the action is reconciled from evidence, and SHALL
surface the unreconciled action as blocking state. An unconfirmed outcome SHALL
never be treated as safe to respawn.

#### Scenario: An uncertain action blocks reconstitution

- **WHEN** a dead or stalled job has an unreconciled uncertain action
- **THEN** no reconstitution is performed and the uncertain action is surfaced
  as blocking state

#### Scenario: Reconciliation clears the block

- **WHEN** evidence has been recorded and the uncertain action is reconciled
- **THEN** the job is no longer blocked by it and may become a reconstitution
  candidate

### Requirement: Restart backoff is persisted and bounds restart loops

Each reconstitution attempt SHALL be recorded durably under a stable restart
attempt signature for the job, and identical restart attempts SHALL be bounded
by the job policy's incident-attempt limit with a capped backoff delay. The
restart attempt count SHALL survive `opsx-plan reset`: a reset SHALL NOT erase,
reduce, or re-baseline it. When the bound is reached, further automatic
reconstitution SHALL be refused with a named bounded-restarts state and the job
SHALL surface an actionable blocker rather than looping.

#### Scenario: Restart attempts are recorded and delayed under a cap

- **WHEN** a job is repeatedly reconstituted for the same restart signature
- **THEN** each attempt is recorded durably with a capped backoff delay before
  the next

#### Scenario: The restart bound is reached

- **WHEN** reconstitution attempts reach the job's incident-attempt limit
- **THEN** further automatic reconstitution is refused with a named
  bounded-restarts state and an actionable operator blocker is surfaced

#### Scenario: The restart count survives reset

- **WHEN** `opsx-plan reset` runs between identical reconstitution attempts
- **THEN** the restart attempt count continues to accumulate and is not
  re-baselined

### Requirement: Reconstitution events are durable and append-only

Every watchdog classification transition and every reconstitution event SHALL
be recorded as an append-only durable ledger record carrying the job id, the
event kind, the classification, the reason, and the timestamp. The records
SHALL survive a ledger reopen and a process restart, and SHALL NOT be rewritten
or deleted by a later tick.

#### Scenario: A reconstitution event survives a reopen

- **WHEN** a reconstitution event is recorded and the ledger is closed and
  reopened
- **THEN** the event is present with its job id, kind, classification, reason,
  and timestamp

#### Scenario: Records are append-only

- **WHEN** a later watchdog tick runs after events have been recorded
- **THEN** the prior records are unchanged and new records are appended

### Requirement: The watchdog leaves legacy runs and existing lifecycle behavior unchanged

An ordinary, unregistered plan SHALL create no watchdog records and SHALL
require no watchdog backend. The watchdog SHALL introduce no new DAG, stage
machine, or dispatch path, and SHALL NOT release a gate, satisfy a checkpoint,
mark a change done, or alter the existing lifecycle, broker, or
implement/review/archive behavior.

#### Scenario: A legacy run creates no watchdog state

- **WHEN** an ordinary, unregistered plan run executes while the watchdog is
  available
- **THEN** no watchdog record is created for it and its execution is unchanged

#### Scenario: The watchdog cannot progress a change

- **WHEN** the watchdog classifies and reconstitutes a registered job
- **THEN** no change is marked done, no gate is released, and no checkpoint is
  satisfied by the watchdog itself

### Requirement: The supervision fault matrix is proven end to end with real processes and a loopback fake API

The system SHALL provide an automated fault-injection suite that exercises the
supervision stack end to end using real local subprocesses and a loopback fake
OpenCode API, requiring no external network, no paid model call, and no operator
global install or daemon provisioning. The suite SHALL kill the real controller,
the supervised service, and a fake worker at each of the intent, dispatch,
result, and verification checkpoints and then start a fresh service, and it
SHALL observe real continuation or a correct human wait re-derived from durable
state rather than merely calling a fixture method.

#### Scenario: A kill at each checkpoint is followed by real continuation

- **WHEN** the controller, the supervised service, or a fake worker is killed at
  the intent, dispatch, result, or verification checkpoint and a fresh service is
  started
- **THEN** the fresh service reconciles the job from durable state and either
  continues the execution or records a correct human wait, observed through the
  real process and not a fixture stub

#### Scenario: Recovery is observed from durable state

- **WHEN** the suite restarts the service after a kill
- **THEN** the continuation or wait it observes is derived from the durable
  ledger and authority state, not from any in-memory or pre-interruption state

### Requirement: The fault matrix covers the supervision failure scenarios with durable-correctness assertions

The fault-injection suite SHALL cover at least: a lost event, a duplicate
response, a stale approval, a spoofed worker `approve` attempt, a sandbox or
authority bypass attempt, competing run and worker contention, a restart while a
human wait is recorded, a budget reset, unknown cost, a wrong model identity,
and a false completion claim. Every scenario SHALL assert that the durable
ledger and authority state remain correct afterward and that no external effect
is claimed exactly once: deduplication and re-observation SHALL precede any
replay, an unknown or interrupted outcome SHALL never be treated as free or
complete, and a stale approval, a wrong model, or a spoofed worker SHALL fail
closed.

#### Scenario: A spoofed worker approval fails closed

- **WHEN** a real worker-domain subprocess attempts to `approve`, `reset`, or
  run a registered supervised job during the fault matrix
- **THEN** the attempt is refused, no gate is released, and the durable
  authority state is unchanged

#### Scenario: A duplicate response does not double dispatch or double bill

- **WHEN** the fake API returns a duplicate response for a dispatched action
- **THEN** the action is deduplicated and re-observed before any replay, and the
  budget and ledger record one effect

#### Scenario: A restart during a human wait preserves the wait

- **WHEN** the service is restarted while a human wait is recorded
- **THEN** the wait remains durable and is woken only by the durable receipt
  scan, with no model polling or recovery action

#### Scenario: A budget reset and unknown cost do not loosen policy

- **WHEN** `opsx-plan reset` runs between identical attempts, or an action has
  unknown cost
- **THEN** reservations and attempt signatures survive the reset, and unknown
  cost blocks dispatch rather than being treated as free

#### Scenario: A false completion claim is rejected

- **WHEN** a killed worker or a partial archive claims a change or plan is
  complete
- **THEN** completion is determined from canonical plan, archive, and check
  evidence and the false claim is not accepted

### Requirement: Automated supervision checks run under a hermetic fixture guard

The fault-injection suite SHALL install a fixture guard that enforces the
supervision test policy: external network access, paid model calls, and operator
global installs or daemon provisioning SHALL be prohibited in automated checks.
The guard SHALL fail closed — an attempt to reach a non-loopback address, use a
paid model credential, or run a global installer SHALL fail the check rather
than being silently permitted. Permitted resources SHALL be limited to local
loopback fake servers, real local subprocesses, and temporary sandboxes.

#### Scenario: Non-loopback egress is refused

- **WHEN** a supervised check under the fixture guard attempts to connect to a
  non-loopback address
- **THEN** the guard refuses the connection and the check fails closed

#### Scenario: Paid model and global install attempts are refused

- **WHEN** a supervised check under the fixture guard attempts a paid model call
  or an operator global install or daemon provisioning step
- **THEN** the guard refuses it and the check fails closed
