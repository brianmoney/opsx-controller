## ADDED Requirements

### Requirement: Supervised repair verdicts never self-certify completion

A `fixer` report of repaired SHALL NOT by itself mark any work complete:
the independent `verifier` SHALL validate the actual diff and evidence
before any authorized commit, reset, or resume consumes the repair. The
`verifier` verdict SHALL be derived from the artifacts and diff under
inspection, never from the fixer session's own account. Supervised repair
verdicts SHALL feed, but never replace, the existing implement, review, and
archive task-completeness gates: an unchecked automatable task SHALL remain
blocking regardless of any fixer or verifier verdict.

#### Scenario: A fixer claim is validated before it is consumed

- **WHEN** a `fixer` session reports a repair complete
- **THEN** the repair is not consumed by any commit, reset, or resume until
  the independent `verifier` validates the actual diff and records its
  verdict

#### Scenario: A contradicting verifier blocks the repair

- **WHEN** the `verifier` examines a fixer's diff and finds the repair
  incorrect or incomplete
- **THEN** its verdict records the failure and the repair is not treated as
  complete, regardless of the fixer's report

#### Scenario: Repair verdicts do not satisfy the task-completeness gates

- **WHEN** a change has unchecked automatable tasks and a supervised repair
  verdict exists
- **THEN** the existing implementer, reviewer, and archiver completeness
  gates still apply exactly as before, and the repair verdict neither checks
  a task nor waives a gate

#### Scenario: Every repair-consuming transition consumes the gate

- **WHEN** a resume, dispatch, service reset, or delegated release would
  consume a recorded repair
- **THEN** the transition reads the change's latest recorded fixer report and
  verifier verdict, and is refused with a recorded `policy_violation` unless
  an independent verifier verdict reviewed the real diff
