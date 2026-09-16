---
title: Durable Plan Supervision
doc_type: implementation-plan
status: proposed
updated: 2026-09-12
---

# Durable Plan Supervision

## Purpose

`opsx-plan` today is a foreground, operator-driven orchestrator: a human runs
`opsx-plan run`, watches it, and approves `pause_before` changes. It has
sequencing, gates, archive ground truth, and reconciliation, but no durable
supervision record, no independent watchdog, no process-ownership fencing, no
enforced human-only approval path, and no budget policy that survives
`opsx-plan reset`. This plan adds a durable supervision layer on top of that
existing ground truth: a frontier primary OpenCode session owns an entire
plan, inexpensive pinned subagents do the mechanical work, a durable ledger
records intent and evidence, and a service-managed watchdog reconstitutes the
job after restarts. It preserves `opsx-plan`'s sequencing, gates, and
verification rather than replacing them. It is authored for the **current**
compiler and runnable surface: it does not assume the new
`pause_before_human_only` flag exists yet, and its own compiled manifest must
keep `review_created = true` and use only currently supported keys.

### Requirements Carried Forward

#### Human-Selected Choices

The human selected the following direction and constraints:

- A frontier primary OpenCode session owns the entire plan; supporting skills
  and agents plus a durable supervision record and a watchdog back it.
- All subagents must run on capable but inexpensive, explicitly pinned models.
- Acceptance/review and failure-repair agents reduce the need for the human to
  monitor and intervene in ordinary plan execution.
- Human-only before-change checkpoints require an explicit flag.

#### Supporting Conversation Semantics

This plan carries forward the following proposed semantics from the discussion;
implementation details selected here remain reviewable at the named gates:

- `opsx-plan` sequencing, gates, and archive ground truth are retained as the
  authority for what "done" means.
- An acceptance reviewer with distinct `accept` / `fix` / `escalate` outcomes
  is separate from the implementation reviewer. A cheap investigator/fixer and
  an independent cheap verifier support it; hard judgments return to the
  primary rather than being pushed down to an expensive subagent.
- An explicit `pause_before_human_only` flag is introduced with these
  semantics: legacy `pause_before = true` with the new flag absent defaults to
  human-only; an explicit `false` delegates approval to the supervised job's
  policy-bound authority; `pause_before_human_only = true` without
  `pause_before = true` is invalid.
- Operator-only approval must be genuinely inaccessible to worker processes.
  A `--human` flag, a TTY check, a token in the worker environment, and a
  same-UID `chmod` scheme are rejected as insufficient.
- There are no model-policy downgrades, and approval is bound to an exact
  checkpoint/material revision; acceptance is revision-bound separately.
- Restarts and `opsx-plan reset` cannot bypass an approval or its record.

- A human wait is persisted as normal durable state; there is no LLM polling
  and no stall recovery for it.
- All subagents are capable but inexpensive and explicitly pinned. The
  planning example is a DeepSeek V4.1 Flash-class model; its exact
  availability and identifier are unverified here; no model ID or price is
  invented.
- One supervised job per worktree; the authority broker governs only
  registered supervised jobs.
- The journal records action intent before side effects and reconciles
  evidence rather than claiming exactly-once external effects.
- The narrow trust model treats the local OS owner as trust root; the trust
  boundary is enforced with isolated principals rather than same-UID
  conventions.

### Implementation Defaults Selected by This Plan

The following are defaults **selected by this plan** and open to honest human
rejection at the named gates:

- A schema-versioned SQLite supervisor ledger holding job, action, incident,
  and protected job-policy records, separate from the existing authoritative
  JSON execution state.
- Stdlib-only Python reusable, client-neutral core modules, with the OpenCode
  session bridge implemented first.
- A Linux OS-managed service first; other adapter and OS supervision come
  later while existing all-adapter execution remains compatible.
- A consolidated `opsx-plan supervise` subcommand surface rather than many new
  binaries.
- No new required model roles for existing users.
- Optional `supervisor`, `supervised_author`, `acceptance_reviewer`, `fixer`,
  and `verifier` roles. The `supervisor` is a pinned frontier model exempt
  from the inexpensive allowlist but still subject to the total budget; every
  worker, author, create, implement, review, archive, and escalation model
  must be on the operator-maintained inexpensive allowlist. Supervised
  features validate that their required roles resolve before dispatch.
- All existing create/implement/review/archive and escalation routes obey the
  cheap-worker policy under supervision. Because create currently uses the
  frontier controller model, supervised mode uses `supervised_author` for a
  cheap author override without changing the legacy compile role.
- No silent fallback or inheritance. Requested and observed usage identities
  are recorded separately; a mismatch blocks. Unknown or interrupted usage
  reservations are retained rather than released.
- Both native Task and subprocess worker paths are tracked.
- The primary cannot run arbitrary Bash or dispatch arbitrary Task agents; it
  reads evidence and invokes a tracked service tool. Worker tool/subagent use
  is a constrained allowlist with no recursive arbitrary dispatch.
- Model credentials and network egress flow through a trusted model gateway
  or an equivalently enforced isolated transport, so no reusable provider
  credential or unrestricted egress lets a cheap constraint be bypassed.

### Validation Baseline

- Python: `python3 -m unittest discover -t . -s tests` from the repository
  root (stdlib only).
- Node: `node tests/opencode/test-opsx-usage-emitter.js` when any runtime or
  adapter-facing surface changes.
- OpenSpec: `openspec validate <slug> --strict` for each change once its
  artifacts exist.
- New Python packages ship an `__init__.py` so unittest discovery sees them.
- The create stage for every change authors the required artifacts —
  `proposal.md`, `design.md`, spec deltas, and `tasks.md` — and does not
  implement.
- Test policy: local loopback fake servers, real local subprocesses, and a
  temporary installer sandbox are permitted. External network access, paid
  model calls, and operator global installs or daemon provisioning are
  prohibited in automated checks.
- Real inexpensive-model qualification is an operator `(manual)` follow-up
  and is never an unmarked blocking task.

### Existing Interfaces Retained

This plan builds on and does not replace these existing capabilities:
`plan-manifest-lifecycle`, `plan-operator-cli`,
`adapter-aware-plan-compilation`, `adapter-model-configuration`,
`plan-run-observability`, `plan-driven-opencode-execution`,
`shared-orchestrator-installation`, `universal-installer`,
`orchestrator-module-layout`, `implementer-model-escalation`, and
`task-completeness-gates`. Existing `opsx-plan` manifest keys, commands, and
per-adapter execution continue to work for operators who never register a
supervised job. Legacy jobs keep their existing JSON handling without a
backend dependency; the one intentional behavior change is that an ordinary
mutating run may now be rejected when it would race a supervised execution,
as documented by the lock change.

### Bootstrap Constraint and Gate Policy

The current compiler and manifest loader do not understand
`pause_before_human_only`; unknown keys are silently dropped today. This
document therefore compiles under the **current** compiler with no
hypothetical keys. Its runnable manifest must use only existing keys:
`pause_before = true` on the gated changes, released by a human running
`opsx-plan approve`. `review_created = true` is maintained so auto-created
changes still require operator `accept`.

The new flag only becomes usable after the flag-schema change has shipped, the
reinstalled runtime has been verified, and the operator authority boundary is
enforced. There is no self-hosting of an incomplete supervisor: this plan runs
under the legacy human-approved model throughout. Do not instruct the compiler
to emit `supervise` keys or future flag fields into the runnable manifest.

## Capability Ownership

One new capability is proposed: **`durable-plan-supervision`** (proposed; see
this section). It owns the durable supervisor ledger and its schema versioning,
the versioned protected job-policy schema, the supervised job/action/incident
model, the permanent job ownership and worktree execution lock, broker-enforced
approval authority, the OpenCode session bridge and supervised agent contracts,
the supervised lifecycle and acceptance stage, budgets and bounded incident
recovery, the watchdog and boot reconstitution, the supervision observability
projections, and the supervision service packaging.

It is deliberately separate from `plan-manifest-lifecycle` (which owns plans,
manifests, state, and archive semantics), from `plan-operator-cli` (which owns
the existing command surface), from `plan-run-observability` (which owns
existing telemetry and reports), and from `plan-driven-opencode-execution`
(which owns the existing direct-dispatch execution model). Those capabilities
gain supervised integration points but keep their current contracts.

No other new capability is introduced. Supervisor role names
(`supervisor`, `supervised_author`, `acceptance_reviewer`, `fixer`,
`verifier`) are model roles, not capabilities.

## Phase 1: Contracts and Configuration Foundations

### Change: `define-supervision-ledger-contract`

**Purpose:** Establish the durable, versioned supervisor ledger, the protected
job-policy schema, and the client-neutral supervision contract that every
later change builds on.

**Depends on:** None. This is the first change of the plan and establishes the
proposed capability; it may proceed immediately.

**Capabilities:** `durable-plan-supervision` (proposed; see Capability Ownership), `orchestrator-module-layout`, `plan-manifest-lifecycle`, `shared-orchestrator-installation`.

**Scope:** Add a stdlib-only `lib/supervisor/` runtime package (with
`__init__.py`) defining a schema-versioned SQLite ledger for supervisor jobs,
actions, incidents, and a versioned protected job-policy record. The policy
record carries the authority configuration, the model and cheap allowlist
selection, the manifest-snapshot hash, budgets, deadlines, and explicit
operator revisions. Keep the ledger separate from the authoritative JSON
execution state and outside any writable worktree, in external service-owned
storage; enforce module-import cycle discipline across the package. Define the
schema-version and journal migration mechanism, record identity (job id, action
id, incident id, existing `run_id` linkage), and the trusted-location contract
with repository-relative references stored as data. Wire the initial package
into the installer package copy so intermediate installs work. Document the
supervision contract in `core/plan-supervision.md`: job/action/incident
lifecycles, journal-before-side-effects, evidence reconciliation, ownership
fields, the single-supervised-job-per-worktree invariant, and path semantics
consistent with the isolation boundary.

**Out of scope:** Any CLI command, the pause flag, model-role resolution, the
session bridge, watchdog behavior, and any change to the existing JSON state
file.

**Success parameters:** `lib/supervisor/__init__.py` and the ledger module
exist; `tests/supervisor/test_ledger_contract.py` (new package with
`__init__.py`) creates, reopens, and migrates a ledger, asserts the job-policy
schema version and forward-only migration, simulates an interrupted write and
asserts transaction recovery, and asserts the trusted-location contract
rejects a repo-internal writable path; `core/plan-supervision.md` covers every
listed contract element; `python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
define-supervision-ledger-contract --strict` passes.

### Change: `add-human-only-pause-flag`

**Purpose:** Add the `pause_before_human_only` manifest key and its validation
semantics across every canonical surface that defines the manifest.

**Depends on:** `define-supervision-ledger-contract`, because the flag's
persisted human-only semantics and validation are defined by the accepted
supervision contract.

**Capability:** `durable-plan-supervision`, `plan-manifest-lifecycle`, `adapter-aware-plan-compilation`,
`plan-operator-cli`.

**Scope:** Implement validation: `true` without `pause_before = true` is an
error; legacy `pause_before = true` with the key absent resolves to human-only;
an explicit `false` delegates approval to the supervised job's policy-bound
authority. Update all canonical manifest surfaces in one change: the loader,
the compiler schema guidance, the canonical sample markdown and TOML pair, the
`core/plan-authoring.md` reference, the manifest skill schema and auditor, and
derived-manifest generation. Preserve resolved semantics in compiled output
and loaded manifest data. Add invalid non-boolean tests. Do not add the broker
or the service machinery; enforcing the flag happens in the broker and
lifecycle changes.

**Out of scope:** Enforcing the flag at runtime, the `opsx-plan supervise`
surface, model-role resolution, and behavior changes for manifests that never
set the new key.

**Success parameters:** `tests/supervisor/test_pause_flag.py` asserts the
three resolution cases, a named error for `true` without `pause_before = true`,
invalid non-boolean rejection, and unchanged legacy manifest loading; the
canonical sample pair, `core/plan-authoring.md`, skill schema/auditor, and
derived-manifest tests cover the key; existing compiler and manifest tests
still pass; `python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
add-human-only-pause-flag --strict` passes.

### Change: `register-supervised-model-roles`

**Purpose:** Register optional supervised model roles, the inexpensive
allowlist, and model-policy fields without requiring anything new of existing
users.

**Depends on:** `define-supervision-ledger-contract`, because the role and
allowlist definitions populate the protected job-policy schema and the
existing model-role resolution.

**Capability:** `durable-plan-supervision`, `adapter-model-configuration`, `implementer-model-escalation`,
`shared-orchestrator-installation`.

**Scope:** Add optional `supervisor`, `supervised_author`,
`acceptance_reviewer`, `fixer`, and `verifier` roles, resolvable like existing
roles but never required for legacy runs. Define the operator-maintained
allowlist and the versioned model-policy payloads, including explicit stage
mapping, the allowlist-exempt supervisor classification, and the
requested/observed identity record. Define pure fail-closed, mismatch, and
unknown/interrupted-retention decisions without applying them to live dispatch
or budgets; the journal and budget changes consume those decisions. Make the
supervised author override explicit while leaving the legacy compile role
unchanged. Define the policy inside the protected job policy, not as future
plan keys. Extend the model resolver, activation, `opsx-plan models`,
`opsx-plan doctor`, and setup documentation/examples, and verify the existing
supervisor package installation continues to include the new model-policy
module.

**Out of scope:** Changing any default model for non-supervised runs, adding
new required roles, choosing a concrete vendor model ID, pricing changes, and
defining the concrete agent prompts (the agent-contracts change does that).

**Success parameters:** `tests/supervisor/test_supervised_models.py` asserts
that a manifest with no supervised roles resolves and dispatches exactly as
before, that a pure policy check reports a missing, unallowlisted, or
identifier-syntax-invalid role as blocking with a named reason, that the
supervisor is budget-counted but allowlist-exempt, that no fallback or
inheritance occurs, that the requested/observed mismatch and
unknown/interrupted-retention predicates return the defined decisions, and
that the supervised-author mapping leaves the legacy compile role unchanged;
resolver/activation/doctor/models/docs examples cover the new roles; a
temporary install deploys the complete `lib/supervisor` package; doctor
reports a missing or differing installed supervisor module as stale;
`python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
register-supervised-model-roles --strict` passes.

## Phase 2: Authority Boundary and Human-Only Enforcement

### Change: `establish-operator-authority-boundary`

**Purpose:** Choose, document, and demonstrate a viable isolation backend that
makes the operator approval authority genuinely inaccessible to model
sessions, and fail closed on hosts that cannot provide it.

**Depends on:** `define-supervision-ledger-contract`, because the authority
store's trusted location and path semantics are fixed by the storage contract.

**Capability:** `durable-plan-supervision`, `plan-operator-cli`, `orchestrator-module-layout`.

**Scope:** Adopt a narrow trust model in which the local OS owner is the trust
root and Linux isolated principals are the baseline. The service runs under a
trusted OS identity; every model session, including the frontier primary, runs
in the constrained worker domain so the primary is never a privileged daemon.
Provide a separate operator endpoint authenticated by OS peer credentials and
a restricted worker-actions endpoint; operator credentials are never exposed
to a model. Protect service code, configuration, ledger, policy, and the
manifest snapshot outside the worktree, treating the editable repo copy as
untrusted, and ensure repo hooks, tests, and commands execute in the worker
domain, never the privileged service. Add the `opsx-plan supervise` namespace
that reports backend capability and refuses to enable supervision when the
backend is unavailable, with a named unsupported-host error and no silent
downgrade. Provisioning is manual; fail closed when the backend is unsupported.
Add fixtures that assert detection, fail-closed behavior, and worker-domain
denial of writes to the authority store, plus a mandatory activation probe and
a real restricted-process smoke test when the platform backend is available.
Keep independent diagnostics (`doctor`, `status`, `logs`, `report`) available
without the boundary.

**Out of scope:** Enforcing individual approvals (the next change does that),
the session bridge, budgets, watchdog, fake-only security claims, and
provisioning accounts automatically.

**Success parameters:** `tests/supervisor/test_authority_boundary.py` asserts
backend detection, fail-closed refusal with a named unsupported-host error,
worker-domain write denial under the fixture, the operator/worker endpoint
split, and the activation probe; `opsx-plan supervise` reports backend status;
`core/plan-supervision.md` records the selected backend and rejected
alternatives; `python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
establish-operator-authority-boundary --strict` passes.

### Change: `enforce-broker-mediated-approvals`

**Purpose:** Make a broker in the trusted authority domain the sole approval
authority for registered supervised jobs, with durable receipts and no
dependency on the held execution lock.

**Depends on:** `establish-operator-authority-boundary` and
`add-human-only-pause-flag`, because the broker can only be non-forgeable once
the operator boundary exists, and it enforces the new flag's human-only
semantics.

**Capability:** `durable-plan-supervision`, `plan-operator-cli`, `plan-manifest-lifecycle`.

**Scope:** Scope the broker to registered supervised worktrees/jobs only.
Within a registered job, ordinary `approve`, `approve --all`, `approve P<N>`,
`reset`, `run`, `run-one`, `opsx-run`, and `accept` are broker mediated; unregistered
legacy jobs keep their existing JSON handling without a backend dependency.
Make the broker the sole approval authority and treat the JSON execution state
as a projection, not a competing phase authority. Guard against a worker
editing the JSON or plan to drop supervised fields escaping active
registration by enforcing a protected snapshot plus an external registration
record. Distinguish permanent job ownership from the worktree execution lock:
human approval uses an operator OS-authenticated path, delegated `false` uses
a scoped job service action, and approval/acceptance receipts and pause or
steer requests are broker database transactions with a durable wake-up rather
than the same held execution lock, so a human wait retains ownership and
cannot block approval. Resume rechecks the material inputs before dispatch.
Bind each approval to the exact checkpoint and material revision by hashing
only the material gate inputs, so unrelated updates do not invalidate while
stale material inputs do; require plan/policy revisions to be explicit. Allow
bounded authorized supervisor resets while forbidding blanket worker resets.
Do not disable read-only diagnostics; status and logs remain available.

**Out of scope:** The acceptance stage, budgets, the session bridge, the
watchdog, and changing legacy unregistered-job JSON handling.

**Success parameters:** `tests/supervisor/test_broker_approvals.py` asserts
that a worker-domain subprocess cannot approve, reset, or run in a registered
job; that a stale material revision does not satisfy a gate while an unrelated
update does not invalidate; that `approve --all`, `approve P<N>`, and
`accept` are broker mediated; that a worker dropping supervised JSON/plan
fields cannot escape registration; that receipts wake the service without the
held execution lock; that legacy unregistered jobs keep prior JSON handling;
and that the three flag-semantics cases resolve as specified;
`python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
enforce-broker-mediated-approvals --strict` passes.

## Phase 3: Execution Ownership, Budgets, and Action Journal

### Change: `add-process-singleton-lock`

**Purpose:** Separate permanent job ownership from a worktree execution lock so
competing mutating commands cannot race, while legacy ordinary runs keep
working without the supervisor ledger or principal.

**Depends on:** `define-supervision-ledger-contract`, because ownership
fencing records and boot identity are persisted in the supervisor ledger.

**Capability:** `durable-plan-supervision`, `plan-operator-cli`, `orchestrator-module-layout`.

**Scope:** Add a worktree execution lock that mutating `opsx-plan` paths
(`run`, `reset`, supervised recovery, and the single-change handler shared by
`opsx-run` and `opsx-plan run-one`) acquire, and keep permanent
job ownership as durable ledger state distinct from the lock.
Approval/acceptance receipts and pause/steer requests are broker database
transactions with a durable wake-up and do not require the held execution
lock. Record process ownership using process start time and boot
identity, not a bare PID, and fence stale owners by verifying the previous
worker is quiesced before takeover. Refuse a second supervised job for the
same worktree. Legacy ordinary runs acquire the lock without requiring the
supervisor ledger or a separate principal; document the intentional new
rejection when an ordinary run would race a supervised execution, and preserve
legacy behavior otherwise. Keep independent diagnostics (`doctor`, `status`,
`logs`, `report`, `dashboard`) runnable without the lock.

**Out of scope:** Dispatching actions, budgets, the watchdog, the session
bridge, and broker approval semantics.

**Success parameters:** `tests/supervisor/test_singleton_lock.py` asserts
mutual exclusion between two mutating processes, verified-quiesced takeover,
refusal to take over a live owner, no lock requirement for receipts, legacy
run compatibility including the documented race rejection, and diagnostics
running during a held lock; `python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
add-process-singleton-lock --strict` passes.

### Change: `add-supervision-budgets`

**Purpose:** Enforce durable budgets over all model usage that survive
`opsx-plan reset`, reserving before dispatch and refusing to treat unknown
cost as free.

**Depends on:** `define-supervision-ledger-contract`, because durable
per-incident attempts, usage reservations, and telemetry records live in the
supervised storage.

**Capability:** `durable-plan-supervision`, `plan-run-observability`, `adapter-model-configuration`.

**Scope:** Add total, per-action, cost, and elapsed budgets covering worker,
author, auxiliary model calls, create, retries, and escalation, with no
double billing of duplicate results. Reserve budget before dispatch and
reconcile observed usage after, retaining unknown or interrupted reservations
rather than releasing them. The frontier `supervisor` primary session is not
a dispatch path this change owns: its invocation is wired by
`add-opencode-session-bridge`, which routes primary usage through the same
reserve/reconcile boundary. Persist per-incident attempt signatures across
`opsx-plan reset` so identical resets cannot loop without bound. Keep the
active execution timeout separate from human-wait duration so an expected wait
does not consume the execution deadline. Hard USD limits must account for
provider token caps and headroom and must not pretend to enforce an immediate
exact limit; budget increases are operator-only. Collect the core metrics
before report, keep separate role telemetry while preserving the legacy schema,
and avoid the unrelated active leaderboard-attribution change. Apply bounded
transient backoff and surface genuine human blockers as actionable state.

**Out of scope:** Deciding incident repair, the session bridge, reporting UI,
and changing legacy telemetry fields.

**Success parameters:** `tests/supervisor/test_supervision_budgets.py`
asserts reservation-before-dispatch, reconciliation without double billing,
retention of unknown/interrupted reservations, budget survival across reset,
per-action and total limits, human-wait duration excluded from the execution
deadline, operator-only increases, unknown pricing blocking with a named
error, and bounded backoff; core metric collection is covered before report;
`python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
add-supervision-budgets --strict` passes.

### Change: `add-action-journal-dispatch`

**Purpose:** Dispatch every supervised action through a durable, intent-first
journal that integrates the existing run engine with model, budget, and
sandbox policy, with explicit uncertainty and evidence reconciliation.

**Depends on:** `add-process-singleton-lock`, `add-supervision-budgets`,
`enforce-broker-mediated-approvals`, and `register-supervised-model-roles`,
because dispatch must hold the execution lock, reserve budget, honor broker
authority, and enforce model policy before side effects.

**Capability:** `durable-plan-supervision`, `plan-manifest-lifecycle`, `orchestrator-module-layout`.

**Scope:** Journal action intent before any side effect, then write a
transactional dispatch record with action id, owning job, session, and process
identity. Integrate the existing `opsx-plan run` engine and its inner
create/implement/review/archive dispatch to the journal, model policy, budget
reservation, and worker sandbox rather than introducing a new DAG. Mark
actions explicitly uncertain when the outcome cannot be confirmed and
reconcile with evidence before replay. Never claim exactly-once external
effects through a lease: deduplicate and re-observe before replaying.
Revalidate the immutable job plan and policy before each action. Track native
Task and subprocess dispatch paths in the same journal.

**Out of scope:** The session bridge transport, incident repair policy, and
report output.

**Success parameters:** `tests/supervisor/test_action_journal.py` asserts
intent durability before a simulated side effect, integration of the existing
run engine's inner stages, policy/model/budget gating before dispatch,
uncertain-action reconciliation, duplicate deduplication, replay only after
re-observation, and a policy/plan change blocking dispatch;
`python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
add-action-journal-dispatch --strict` passes.

## Phase 4: Session Bridge and Agent Contracts

### Change: `add-opencode-session-bridge`

**Purpose:** Bridge the frontier primary OpenCode session to a
service-managed headless session, reconnecting or re-briefing from authority
and ledger rather than transcript alone.

**Depends on:** `add-action-journal-dispatch`, because bridge events are hints
reconciled against the action journal, and because the journal transitively
carries the model, budget, broker, and lock policy.

**Capability:** `durable-plan-supervision`, `plan-driven-opencode-execution`.

**Scope:** Implement the OpenCode session bridge over the action journal with
a documented versioned API for create, prompt, result-schema, lookup, and
abort, including a version capability check. The service owns session lifetime
and runs the frontier session headless; an existing interactive chat starts or
attaches to that service-managed session via the primary session linkage.
Record request and action IDs before prompting so a lost launch
acknowledgement is resolvable through a discoverable request identity. Treat
streamed events as hints only; handle lost, duplicate, and no-replay cases by
polling authoritative session state. On reconnect or a new bounded briefing,
derive context from the authority store, the ledger, active and uncertain
actions, budgets, and previous failed remedies rather than transcript-only
replay. Keep the existing direct-dispatch execution model working for
non-supervised runs.

**Out of scope:** Non-OpenCode adapters, agent allowlists, and lifecycle
registration commands.

**Success parameters:** `tests/supervisor/test_session_bridge.py`, using a
loopback fake OpenCode server and real local subprocesses with fake model
inputs and no external network, asserts the documented API and version
capability check, ID recording before prompt side effects, lost-ack recovery
via request identity, service-owned session lifetime, reconnect briefing from
authority plus ledger, hint-only event handling, duplicate and out-of-order
tolerance, and authoritative poll fallback; `python3 -m unittest discover -t .
-s tests` and `node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec
validate add-opencode-session-bridge --strict` passes.

### Change: `enforce-supervised-agent-contracts`

**Purpose:** Install and enforce concrete supervised agents and a supervision
skill, constrain workers to pinned models and tool/subagent allowlists, and
route hard judgments back to the primary.

**Depends on:** `register-supervised-model-roles`,
`establish-operator-authority-boundary`, and `add-opencode-session-bridge`,
because agent definitions and allowlists are pinned per registered role,
enforced by the operator boundary, and applied at the session bridge.

**Capability:** `durable-plan-supervision`, `plan-driven-opencode-execution`, `task-completeness-gates`.

**Scope:** Define concrete agent definitions for `supervisor`,
`acceptance_reviewer`, `fixer`, and `verifier`, plus the supervision skill,
and install them with installer verification in the same change. Enforce
per-role contracts: a constrained tool and subagent allowlist with no
recursive arbitrary dispatch; the primary can read evidence and invoke only
the tracked service tool, never arbitrary Bash or Task agents. Route
mechanical work to the cheap investigator/fixer and independent verifier, and
return hard judgments to the primary instead of an expensive subagent. Enforce
model credentials and network egress through a trusted model gateway or
equivalently enforced isolated transport, with a named enforcement step before
a prompt executes rather than usage detection afterward. Track native Task and
subprocess delegation in the action journal. Surface a spoofed or escalated
worker as a blocked policy violation.

**Out of scope:** The acceptance stage itself, budget policy, watchdog
behavior, and changing the legacy reviewer/archiver agents.

**Success parameters:** `tests/supervisor/test_agent_contracts.py` asserts the
installed concrete agents and skill, a worker cannot dispatch a non-allowlisted
agent or bypass policy via a shell, pre-prompt gateway/transport enforcement,
Task and subprocess paths being journaled, the primary's bounded service tool,
and verifier independence; the installer verifies the new agents and skill;
`python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
enforce-supervised-agent-contracts --strict` passes.

## Phase 5: Lifecycle, Acceptance, Recovery, and Watchdog

### Change: `add-supervised-plan-lifecycle`

**Purpose:** Add the supervised job registration and lifecycle commands, stop
boundaries, and completion verification, treating a human wait as normal
durable state.

**Depends on:** `add-action-journal-dispatch`,
`enforce-broker-mediated-approvals`, and
`enforce-supervised-agent-contracts`, because this is the first live execution
surface and must run only after action, budget, broker, and model/agent
protection are in place.

**Capability:** `durable-plan-supervision`, `plan-manifest-lifecycle`, `plan-operator-cli`,
`task-completeness-gates`.

**Scope:** Add the `opsx-plan supervise` lifecycle commands: register, start,
inspect, resume, pause, drain, and cancel. Registration persists the manifest
snapshot, repo and worktree, standing permissions, model allowlist and
budgets, and primary session linkage. Add explicit pause and drain stop
boundaries and explicit cancellation effects. Persist a human wait as normal
durable state with no LLM polling and no stall recovery. Verify completion from
underlying plan, archive, and fast-check evidence rather than worker claims,
and rerun an appropriate fresh review rather than treating a prior archive as
proof of done when fast checks or partial archive need revalidation. Report
`(manual)` tasks as operator checklists without marking the run falsely
incomplete. Preserve the existing implement/review/archive loop and its gates
as the authority for progression.

**Out of scope:** The acceptance reviewer, incident repair, watchdog
reconstitution, and changing how non-supervised runs determine completion.

**Success parameters:** `tests/supervisor/test_supervision_lifecycle.py`
asserts each lifecycle command, persisted registration fields, pause/drain
boundaries, cancellation effects, human-wait persistence without polling,
completion only from plan/archive/check evidence with fresh review where
required, and correct `(manual)` reporting; `python3 -m unittest discover -t .
-s tests` and `node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec
validate add-supervised-plan-lifecycle --strict` passes.

### Change: `add-acceptance-review-stage`

**Purpose:** Add a distinct, revision-bound acceptance stage over real
artifacts with separate accept, fix, and escalate outcomes from the
implementation reviewer.

**Depends on:** `add-supervised-plan-lifecycle`,
`enforce-supervised-agent-contracts`, and `register-supervised-model-roles`,
because acceptance is a lifecycle stage performed by the pinned acceptance
reviewer under the agent contract.

**Capability:** `durable-plan-supervision`, `task-completeness-gates`, `plan-manifest-lifecycle`.

**Scope:** Add an acceptance stage that reviews the actual artifacts — the
accepted plan, canonical specs, proposal, design, tasks, dependency edges, and
delta identity — against the change intent and returns `accept`, `fix`, or
`escalate`. Keep it distinct from the implementation reviewer and bind its
verdict to the exact artifact revision, separately from approval checkpoint
binding. On `fix`, route to the cheap investigator/fixer and then run a fresh
acceptance over the new revision. Run the created-change check and capture the
revision immediately, before the service accepts. Record the acceptance
verdict and revision in the ledger, and never let acceptance release a human
gate. A stale acceptance for a different revision does not satisfy the stage.

**Out of scope:** Replacing or reinterpreting the existing review gate, budget
policy, and acceptance for non-supervised runs.

**Success parameters:** `tests/supervisor/test_acceptance_review.py` asserts
the artifact review set, the three outcomes, distinctness from the
implementation review verdict, revision binding, fresh acceptance after a fix,
the immediate created-check/revision capture, stale-verdict rejection, and
that acceptance cannot release a human gate; `python3 -m unittest discover -t
. -s tests` and `node tests/opencode/test-opsx-usage-emitter.js` pass;
`openspec validate add-acceptance-review-stage --strict` passes.

### Change: `add-bounded-incident-recovery`

**Purpose:** Recover from known failure classes through an explicit
primary-chosen, independently verified repair rather than blind replay.

**Depends on:** `add-supervised-plan-lifecycle` and
`enforce-supervised-agent-contracts`, because recovery repairs through
lifecycle boundaries and uses the cheap fixer and independent verifier under
the agent contract, transitively carrying journal and budget policy.

**Capability:** `durable-plan-supervision`, `finding-recurrence-detection`, `plan-manifest-lifecycle`.

**Scope:** Implement bounded recovery for the known classes: invalid JSON
after built-in retries are exhausted; transient provider 5xx distinguished
from permanent auth/billing/config errors; a delta `MODIFIED` identity
mismatch repaired while preserving canonical intent; a dirty worktree where
all unrelated user work is preserved and only authorized changes proceed;
recurring findings; process interruption; and partial archive side effects
with post-archive fast checks. Recovery must have the primary explicitly
choose a remedy from evidence, then have the cheap fixer apply it, then have
the independent verifier validate the actual diff before any authorized
commit, reset, or resume; recovery commits use a standing grant. A partial
archive or failed fast check that needs rework gets an appropriate fresh
review rather than treating old archive existence as done. A root runtime
defect in the installed service is reported for operator/repo work and cannot
self-edit or self-deploy. Keep incident signatures durable across reset and
refuse unbounded identical loops.

**Out of scope:** Watchdog liveness detection, observability projections, and
automatic repair of the installed service itself.

**Success parameters:** `tests/supervisor/test_incident_recovery.py` asserts
each named class maps to its bounded repair path, that transient 5xx and
permanent errors diverge, that delta identity repair preserves canonical
intent, that all unrelated worktree work is preserved, that recovery follows
primary-chosen fixer plus independent diff verification before commit/reset/
resume, that partial archive rework gets a fresh review, and that identical
incident loops are bounded; `python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
add-bounded-incident-recovery --strict` passes.

### Change: `add-watchdog-reconstitution`

**Purpose:** Add a deterministic service-owned watchdog loop that reconciles
jobs on boot and distinguishes liveness, progress, deadline, and expected
human waits without a terminal dependency.

**Depends on:** `add-supervised-plan-lifecycle`,
`add-acceptance-review-stage`, and `add-bounded-incident-recovery`, because
the loop must observe the real session and wire completed decisions to
recovery.

**Capability:** `durable-plan-supervision`, `plan-run-observability`, `plan-operator-cli`.

**Scope:** Add a deterministic service-owned loop with no terminal or control
channel dependency. Track liveness, progress, and deadline as separate
signals. On boot, scan supervised jobs and reconcile them against the ledger
and authoritative state, attempting reconnect before any respawn. Persist
restart backoff against the budget so restart loops are bounded. Treat unknown
outcomes as blocking duplicate action until reconciled. Classify jobs as live,
quiet, stalled, dead, or an expected human wait, and take no LLM or recovery
action for an expected human wait. Verify a prior worker is quiesced before
reconstitution and surface reconstitution events to the operator.

**Out of scope:** Budget policy, incident repair policy, and creating the OS
service unit.

**Success parameters:** `tests/supervisor/test_watchdog.py`, using real local
processes with fake inputs, a fake clock, and a loopback fake API, asserts
each classification, separate liveness/progress/deadline signals, reconnect
before respawn, persisted bounded restart backoff, no action on an expected
human wait, safe reconstitution only after quiescence, and boot-scan
reconciliation; `python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
add-watchdog-reconstitution --strict` passes.

## Phase 6: Observability, Regression, and Packaging

### Change: `add-supervision-observability`

**Purpose:** Project supervisor state and operator steering into reports
without polluting existing model leaderboards.

**Depends on:** `add-action-journal-dispatch`, `add-supervision-budgets`,
`add-bounded-incident-recovery`, and `add-supervised-plan-lifecycle`, because
the report is a projection of journaled progress, budget state, incidents, and
lifecycle waits.

**Capability:** `durable-plan-supervision`, `plan-run-observability`, `plan-operator-cli`.

**Scope:** Extend `opsx-plan status` and `opsx-plan report` (and the dashboard
where applicable) with supervisor job progress, action and incident views,
evidence state, observed usage, manual waits, budget limitations, and operator
steering: policy revisions, pause after change, stop/retry, and cancel, each
with a request id, safe-boundary acknowledgement, and notifications
deduplicated across reboot. Record the evidence and human-approval briefing.
A notification failure must never lose a gate or approval. Add
cost-per-correct-completion metrics as definitions only, with no empirical
performance promises. Maintain separate job and action ids while linking the
existing `run_id` schema, keep supervisor role usage out of the existing
per-change model leaderboard, and document the JSON field models and
limitations.

**Out of scope:** Any modification to the reviewer/leaderboard attribution
logic, new model-metrics semantics, and the unrelated active mixed-model
leaderboard attribution change.

**Success parameters:** `tests/supervisor/test_supervision_report.py` asserts
the status/report JSON fields for job progress, incidents, evidence, usage,
human-approval briefing, waits, budget limits, and steering request
acknowledgements with deduplicated notifications, that a notification failure
does not lose a gate, that job/action ids stay distinct with `run_id` linkage,
that cost-per-correct-completion is defined without performance promises, and
that supervisor role tokens are absent from the existing leaderboard
projection; documentation covers the field models;
`python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
add-supervision-observability --strict` passes.

### Change: `add-supervision-fault-injection-tests`

**Purpose:** Prove the supervision fault matrix end to end with real local
processes and a fake model API, so no external network, paid model, or global
install is required.

**Depends on:** `add-action-journal-dispatch`, `add-supervision-budgets`,
`add-supervised-plan-lifecycle`, `add-acceptance-review-stage`,
`add-bounded-incident-recovery`, `add-watchdog-reconstitution`, and
`enforce-broker-mediated-approvals`, because the matrix exercises those
components end to end, including spoofed and stale approvals.

**Capability:** `durable-plan-supervision`, `plan-run-observability`, `plan-manifest-lifecycle`.

**Scope:** Add a loopback fake OpenCode API fixture and a fault-injection
suite that kills the real controller, supervisor, and a fake worker at the
intent, dispatch, result, and verification checkpoints, then starts a fresh
service and observes real continuation or a correct human wait rather than
merely calling a fixture method. Cover lost events, duplicate responses, stale
approval, a spoofed worker `approve` attempt, sandbox/authority bypass
attempts, competing run/worker contention, restart while a human wait is
recorded, budget reset, unknown cost, wrong model, and false completion
claims. Each scenario asserts the durable ledger and authority state remain
correct and no external effect is claimed exactly once. The fixture guard
prohibits external network, paid models, and global installs.

**Out of scope:** Real model qualification, external network tests, and
operator global installs.

**Success parameters:** `tests/supervisor/test_supervision_faults.py` (new
package with `__init__.py`) covers every listed scenario using real local
processes and the loopback fake API, including real kill/restart continuation
observed from a fresh service; the fixture guard enforces the test policy;
`python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
add-supervision-fault-injection-tests --strict` passes.

### Change: `add-supervision-service-packaging`

**Purpose:** Package and install the supervision service and skills for Linux,
activating unattended operation only after all foundations pass.

**Depends on:** `add-supervision-fault-injection-tests`,
`establish-operator-authority-boundary`, `enforce-broker-mediated-approvals`,
`add-supervision-budgets`, and `add-supervision-observability`, because
unattended activation is gated on the full ownership, authorization, budget,
and regression foundations plus operator observability.

**Capability:** `durable-plan-supervision`, `shared-orchestrator-installation`, `universal-installer`,
`orchestrator-module-layout`.

**Scope:** Package the supervision runtime with systemd unit templates and
provisioning documentation, deploying the service and skills through the
existing orchestrator/installer path while leaving all existing adapter
execution compatible. Install the service disabled by default, with account
changes disabled by default; explicit operator activation is a separate
documented step and the activation probe must pass, so no unattended action
runs before ownership, authorization, and budget foundations are complete.
Extend `opsx-plan doctor` for the service. Keep other adapters and operating
systems explicitly future work with a fail-closed unsupported path. A
temporary installer sandbox tests real start/kill/restart of the installed
commands with the fake API and service manager, without daemon provisioning.

**Out of scope:** Non-Linux service managers, non-OpenCode session bridges,
automatic activation or account changes on install, and real privileged
provisioning.

**Success parameters:** `tests/supervisor/test_supervision_service.py` asserts
the installer deploys the disabled service, the unit templates and
provisioning docs exist, `doctor` reports service/schema/backend state, the
activation probe is mandatory and fails closed on an unsupported host, and a
temporary sandbox start/kill/restart of installed commands with the fake API
succeeds; `python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass; `openspec validate
add-supervision-service-packaging --strict` passes.

## Recommended Sequence

1. Accept the storage, ledger, and protected job-policy contract in
   `define-supervision-ledger-contract` first; it is the automatic
   proposed-capability gate and every later change depends on it.
2. Land `add-human-only-pause-flag` and `register-supervised-model-roles`;
   both depend only on the contract and are independent of each other.
3. Establish the authority boundary in
   `establish-operator-authority-boundary`, then enforce it in
   `enforce-broker-mediated-approvals`. Do not enable supervised authorization
   behavior before both are complete.
4. Land the execution foundations: `add-process-singleton-lock` and
   `add-supervision-budgets` may proceed in parallel, then
   `add-action-journal-dispatch` once the lock, budgets, broker, and model
   roles all exist, because all policy must precede side effects.
5. Build the OpenCode session bridge, then the agent contracts that consume
   it: `add-opencode-session-bridge`, then
   `enforce-supervised-agent-contracts`.
6. Build the live lifecycle first, after all model and agent protection:
   `add-supervised-plan-lifecycle`, then `add-acceptance-review-stage`,
   `add-bounded-incident-recovery`, and finally
   `add-watchdog-reconstitution`, which observes the real session and wires
   recovery decisions.
7. Project state for operators with `add-supervision-observability`, prove the
   fault matrix with `add-supervision-fault-injection-tests`, and finish with
   the activation-gated `add-supervision-service-packaging`.

Real inexpensive-model qualification is an operator follow-up. Any task line
capturing it is marked `(manual)` or expressed as proposal prose, so it never
traps an unattended run.

## Overall Completion Criteria

The plan is complete when a frontier primary OpenCode session can own a whole
plan under durable supervision: intent and evidence are journaled before side
effects; permanent ownership is distinct from a worktree execution lock;
one job per worktree is enforced; budgets cover all model calls, reserve
before dispatch, and survive reset; human-only approval is broker-enforced and
inaccessible to workers for registered jobs; the `pause_before_human_only`
semantics are validated and enforced; the acceptance stage reviews real
artifacts, is revision-bound, and escalates hard judgments to the primary;
recovery follows a primary-chosen fixer and independent diff verification;
the deterministic watchdog reconstitutes jobs after restarts and recognizes
expected human waits; completion is verified from plan/archive/check evidence;
`(manual)` tasks never falsely fail a run; observability reports job,
incident, budget, wait, and steering state without polluting existing
leaderboards; and the Linux service installs disabled and activates only after
the operator gate and probe. Unsupervised legacy behavior is preserved except
for the documented contention rejection, both test suites pass, and
`openspec validate --all` passes.

## Explicit Non-Goals

This plan does not modify the unrelated active mixed-model leaderboard
attribution change, and it does not re-plan the stale prior surface-trim
plan. It does not rebuild the existing controller, replace the existing
implement/review/archive loop or review gate, alter non-supervised completion
semantics, add new required model roles, change the legacy compile model role,
invent a vendor model ID or price, support non-Linux service managers or
non-OpenCode session bridges in this pass, implement during the create stage,
automatically activate the supervisor or change accounts on install, or
produce a TOML manifest as a deliverable. No commit or installer run is part
of authoring this document.

## Suggested Manual Gates

Because of the bootstrap constraint, these gates use the only key the current
compiler and loader support: `pause_before = true`, released by a human running
`opsx-plan approve`. Do not emit `pause_before_human_only` or any `supervise`
key into the runnable manifest; the new flag becomes usable only after
`add-human-only-pause-flag` ships, the reinstall is verified, and
`establish-operator-authority-boundary` is enforced.

- **Automatic storage-contract gate:** the compiler will add
  `pause_before = true` to `define-supervision-ledger-contract` because it
  introduces the proposed `durable-plan-supervision` capability. Keep it: the
  human accepts the schema, protected policy, storage location, and contract
  before anything builds on it.
- **Authority boundary:** add `pause_before = true` to
  `establish-operator-authority-boundary`. This is the first trust-boundary
  change; the operator should confirm the selected isolation backend, the
  endpoint split, and the fail-closed behavior before enforcement depends on
  it.
- **Human-only enforcement:** add `pause_before = true` to
  `enforce-broker-mediated-approvals`. This change alters the approval trust
  posture, so a human should accept the broker scope, the operator
  OS-authenticated path, and the rejection of TTY, `--human`, worker-env
  token, and same-UID `chmod` schemes.
- **Unattended activation:** add `pause_before = true` to
  `add-supervision-service-packaging`. This is the point where unattended
  supervision could run; activation must be a deliberate operator decision
  after the fault-injection suite, the activation probe, and all foundations
  are green.
- **Real model qualification (manual):** after the automated plan completes,
  the operator qualifies the actual inexpensive pinned models on real
  infrastructure, confirms availability and pricing, and populates the
  allowlist. This is operator follow-up, not an automatable task.
