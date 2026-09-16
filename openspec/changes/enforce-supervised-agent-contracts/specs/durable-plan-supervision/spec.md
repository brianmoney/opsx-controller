## ADDED Requirements

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
