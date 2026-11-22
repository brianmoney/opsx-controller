# plan-handoff-gate Specification

## Purpose

Define the mandatory transition between changes in a plan-level
`opsx-orchestrator` run. A change is not complete merely because it is archived;
the repository must be reproducible from `HEAD` and safe for the next change, or
the run must stop and repair the handoff.

## Requirements

### Requirement: A change is not complete until the handoff audit passes

The plan-level orchestrator SHALL run a Phase Transition Gate after each change
is archived and before dispatching any work for the next change.

The gate SHALL dispatch a fresh, read-only `opsx-integration-auditor` and SHALL
not mark the change complete, advance the plan, or start the next change until
the auditor returns `handoff_ready: true`.

The orchestrator SHALL include `handoff_ready` in the run ledger and the final
report. It SHALL NOT report “no open blockers” while `handoff_ready` is false or
unknown.

#### Scenario: Archived change with an uncommitted implementation does not advance
- **WHEN** a change is archived but its accepted implementation files exist only
  in the working tree
- **THEN** the gate returns `handoff_ready: false`, the orchestrator records the
  blocker, and it dispatches no work for the next change

#### Scenario: Handoff-ready change advances
- **WHEN** the implementation, plan, evidence, and next-phase prerequisites are
  reproducible from `HEAD` and the audit passes
- **THEN** the orchestrator records `handoff_ready: true` and may select the next
  change

### Requirement: The handoff audit verifies a reproducible HEAD

The auditor SHALL verify at least:

1. the tracked worktree is clean except for explicitly allowed user-owned or
   ignored paths;
2. every accepted source-manifest path exists at `HEAD` with the accepted
   content;
3. the plan the next change will read exists at `HEAD`;
4. the archive commit contains the accepted implementation paths, or an explicit
   operator waiver is recorded as a blocker;
5. run-local state such as `.opsx-run/` is ignored or outside the repository;
6. committed evidence manifests resolve at `HEAD`, and dead paths are reported
   as unqualified evidence;
7. performance and evidence scripts run from `HEAD` without recreating an
   archived change directory;
8. the next change’s required intake artifacts and dependencies exist, with no
   active-change slug collision;
9. the branch is committed and has an upstream, or a no-push waiver is recorded;
10. a fresh checkout can run the change’s focused tests and
    `openspec validate --all --strict`, or the audit records exactly why that
    check is unqualified.

The auditor SHALL report each check as `passed`, `failed`, `waived`, or
`unqualified` with the exact command, evidence path, and hash where applicable.

#### Scenario: Source manifest verifies at HEAD
- **WHEN** every accepted source path exists at `HEAD` with the accepted hash
- **THEN** the source-reproducibility check passes

#### Scenario: Dead evidence paths are unqualified
- **WHEN** a committed evidence manifest references a path that no longer
  exists at `HEAD`
- **THEN** the audit reports the evidence as unqualified rather than passed

#### Scenario: Perf script recreates an archived change directory
- **WHEN** a perf or evidence script run from `HEAD` would create
  `openspec/changes/<archived-change>/`
- **THEN** the audit fails the handoff and names the script and default path

### Requirement: The handoff audit is read-only and independent

`opsx-integration-auditor` SHALL be a fresh, read-only agent. It SHALL NOT edit,
stage, commit, push, or archive. It SHALL NOT accept the orchestrator’s,
archiver’s, or writer’s summary as evidence; it SHALL inspect the actual
repository, commit contents, manifests, and scripts.

It SHALL be spawned by the orchestrator, not by the writer or archiver under
review.

#### Scenario: Auditor is not the writer or archiver
- **WHEN** the orchestrator dispatches the handoff audit after archive
- **THEN** the auditor is a different session from the change writer,
  implementer, reviewer, and archiver

#### Scenario: Auditor reports rather than repairs
- **WHEN** the auditor finds an uncommitted source file
- **THEN** it returns `handoff_ready: false` with the path and evidence, and
  makes no repository modification

### Requirement: Handoff repair is scoped and re-audited

When the audit returns `handoff_ready: false`, the orchestrator SHALL dispatch
`opsx-repo-hygiene` with the exact blocker list.

The hygiene writer MAY commit the accepted implementation files and the plan,
add run-local state to `.gitignore`, repair handoff-blocking scripts or evidence
paths, and record the repair. It SHALL stage explicit pathspecs and SHALL NOT
include user-owned files, secrets, unrelated source, or generated state in the
commit.

The hygiene writer SHALL NOT change product behavior or close a code-review
finding. A code defect requires a new reviewed change or an explicit re-review
by the change owner; making a failing test pass by editing product code is not a
handoff repair.

After the repair, the orchestrator SHALL dispatch a fresh audit. It SHALL allow
at most two repair/audit rounds. If the second audit still fails, the
orchestrator SHALL stop and report the remaining blockers.

#### Scenario: Source commit repairs the handoff
- **WHEN** the only blocker is that the accepted implementation is uncommitted
- **THEN** the hygiene writer commits exactly those files and the fresh audit
  passes

#### Scenario: Repair cannot make the audit pass
- **WHEN** two repair/audit rounds still leave a blocker
- **THEN** the orchestrator stops before the next change and reports the
  remaining blockers and the exact operator decision needed

#### Scenario: Hygiene writer refuses to invent scope
- **WHEN** the auditor cannot name the accepted implementation set
- **THEN** the hygiene writer returns `blocked` instead of committing the whole
  worktree

### Requirement: Archive-only completion blocks the next change

When the orchestrator or archiver records an archive-only commit while tracked
or untracked source differs from `HEAD`, the orchestrator SHALL treat the run as
`handoff_ready: false` and SHALL stop before the next change unless the operator
explicitly grants authority to continue.

An operator waiver to archive without committing source SHALL be recorded
verbatim in the ledger. It SHALL NOT be converted into a claim that the
implementation is delivered or that the next change may start.

#### Scenario: Operator restricts the archive to archive/spec paths
- **WHEN** the operator authorizes an archive-only commit and implementation
  source remains uncommitted
- **THEN** the orchestrator records the waiver, reports `handoff_ready: false`,
  and does not start the next change

#### Scenario: Implemented and committed change is handoff-ready
- **WHEN** the archive commit includes the accepted implementation paths and the
  other handoff checks pass
- **THEN** the orchestrator records `handoff_ready: true` and may continue

### Requirement: Handoff work has reserved budget

The orchestrator SHALL reserve at least three dispatches per change for the
handoff audit, hygiene repair, and re-audit. A review-cycle cap or dispatch cap
SHALL NOT be exhausted before those reserved dispatches are available.

When a budget is explicitly supplied, the orchestrator SHALL record the reserve
and stop before the next change if the reserve cannot be honored.

#### Scenario: Review budget leaves no room for handoff
- **WHEN** the remaining dispatch budget is less than the reserved handoff
  allowance
- **THEN** the orchestrator stops and reports the budget blocker instead of
  starting the next change

#### Scenario: Handoff reserve is used
- **WHEN** the audit fails and repair plus re-audit are required
- **THEN** those dispatches consume the reserved handoff allowance rather than
  the next change’s implementation budget

### Requirement: The handoff report is machine-readable and actionable

The auditor SHALL return one final JSON object containing at least:

- `status`: `ready` or `blocked`;
- `handoff_ready`: boolean;
- `change`: the change id;
- `head`: the commit audited;
- `checks`: an array of check id, result, command, evidence, and hash;
- `blockers`: an array of blocker id, severity, location, evidence, and the
  exact repair or operator decision needed;
- `next_action`: `continue`, `repair`, or `stop`.

The orchestrator SHALL copy the result into the ledger and final report without
downgrading a blocker or changing a result from `failed` to `waived` without an
explicit operator record.

#### Scenario: Blocked audit names the repair
- **WHEN** the accepted source is uncommitted
- **THEN** the JSON names the source paths, the HEAD check that failed, and the
  exact pathspec commit the hygiene writer should perform

#### Scenario: Audit result is preserved
- **WHEN** the orchestrator records a blocked audit
- **THEN** the ledger keeps the auditor’s `status`, `handoff_ready`, checks, and
  blockers unchanged, with the auditor session id and evidence hashes
