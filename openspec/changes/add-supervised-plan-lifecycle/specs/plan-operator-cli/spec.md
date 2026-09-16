## ADDED Requirements

### Requirement: `opsx-plan supervise` provides the supervised job lifecycle commands

The `opsx-plan supervise` namespace SHALL provide the lifecycle commands
`register`, `start`, `inspect`, `resume`, `pause`, `drain`, and `cancel`
alongside its existing capability, probe, and serve commands.

`register` SHALL record the supervised job for the resolved plan and SHALL
fail with the named unsupported-host error on a host without a supported
isolation backend. `start`, `resume`, `pause`, `drain`, and `cancel`
targeting a worktree with no registered supervised job SHALL exit non-zero
with a named unknown-job error. A command requesting an illegal transition
SHALL exit non-zero with a named illegal-transition error, and a mutating
command targeting a terminal job SHALL exit non-zero with a named
terminal-job error.

Mutating lifecycle commands for a live job SHALL be mediated through the
operator OS-authenticated path, consistent with broker mediation: a
worker-domain process SHALL NOT be able to invoke them. When the required
authority path is unreachable, the command SHALL fail closed with the named
broker-unavailable error rather than acting unmediated.

`inspect` SHALL be a read-only projection of the job: its state, recorded
waits, policy revision, budget posture, and recent actions and incidents.
It SHALL require neither the worktree execution lock nor a live service, and
SHALL exit non-zero with a named unknown-job error when no registered job
exists.

These commands SHALL NOT affect legacy unregistered runs: an operator who
never registers a supervised job observes no behavior change in any existing
command.

#### Scenario: Register then start a supervised job

- **WHEN** an operator runs `opsx-plan supervise register` for a plan on a
  supported host and then `opsx-plan supervise start`
- **THEN** the job is recorded with its full registration fields and
  transitions to `active`

#### Scenario: Lifecycle commands fail closed with named errors

- **WHEN** a mutating lifecycle command targets an unregistered worktree, an
  illegal transition, a terminal job, an unreachable authority path, or an
  unsupported host
- **THEN** it exits non-zero naming the corresponding error — unknown-job,
  illegal-transition, terminal-job, broker-unavailable, or unsupported-host
  — and records nothing

#### Scenario: Pause, drain, resume, and cancel drive the state machine

- **WHEN** the operator runs `pause`, `drain`, `resume`, or `cancel` for a
  registered job in a legal source state
- **THEN** each command records its durable effect and drives the documented
  transition, and the effects are visible to a later `inspect`

#### Scenario: Inspect is read-only and lock-free

- **WHEN** an operator runs `inspect` for a registered job while its
  execution holds the worktree lock or no service is live
- **THEN** the command prints the job's state, waits, policy revision,
  budget posture, and recent actions and incidents without acquiring the
  lock or contacting a service

#### Scenario: A worker process cannot invoke lifecycle mutation

- **WHEN** a worker-domain process attempts to invoke a mutating lifecycle
  verb, including through the worker-actions endpoint
- **THEN** the attempt is refused and no lifecycle effect is recorded

### Requirement: Operator documentation describes the supervised lifecycle

The operator-facing `opsx-plan` documentation SHALL describe the supervised
job lifecycle: the state machine, each lifecycle command with its named
errors, the pause-versus-drain stop boundaries, cancellation effects, the
durable human wait, evidence-based completion with fresh review on
revalidation, and the `(manual)` operator checklist reporting.

#### Scenario: The lifecycle surface is documented

- **WHEN** an operator reads the documented `opsx-plan supervise` reference
- **THEN** it covers the state machine, every lifecycle command, the stop
  boundaries, cancellation, human waits, completion evidence semantics, and
  the manual-task checklist
