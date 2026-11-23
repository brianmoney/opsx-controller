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
8. a fresh checkout can run the change’s focused tests and
   `openspec validate --all --strict`, or the audit records exactly why that
   check is unqualified.

The audit SHALL NOT require the next change’s `proposal.md`, design, tasks, or
spec deltas to exist at `HEAD`. Those artifacts are authored by
`opsx-change-writer` after handoff releases the change. The audit SHALL verify
only that the next change’s planned slug is not occupied by a different change —
a directory at the planned slug that declares the same next change is that
change already active, not a collision — and that every dependency of the next
change is archived or recorded as an accepted deferral. A collision SHALL be
classified by plan reachability. When the occupying change is one of the plan’s
changes or a declared dependency, the audit SHALL report a
`resolve-slug-collision` repair owned by the orchestrator, which resolves it by
not starting or authoring into the occupied slug and letting the plan’s normal
pipeline archive the occupying change. When the occupying change is out-of-plan,
the audit SHALL report `handoff_ready: false`, `next_action: stop`, and an
`operator-decision` blocker to resolve or rename the external change; no
orchestrator or worker SHALL archive, rename, move, or rewrite an out-of-plan
change.

The audit SHALL NOT require a remote branch, a configured upstream, a push, or a
pull request. Delivery is local: the committed `HEAD` is the reproducible
baseline. A configured upstream, when one happens to exist, SHALL be reported as
informational and SHALL NOT change the result.

The auditor SHALL report each check as `passed`, `failed`, `waived`, or
`unqualified` with the exact command, evidence path, and hash where applicable.
An `unqualified` evidence check with a recorded carry-forward SHALL NOT by
itself make `handoff_ready` false when the missing artifact is not the sole
basis of an accepted claim; the report SHALL state which claim remains
unqualified. Source, plan, and archive-commit reproducibility checks remain
hard.

The evidence manifest SHALL be read from `HEAD`, never from the worktree. A
committed manifest whose worktree copy is missing SHALL still be read from
`HEAD` and SHALL NOT be treated as a missing artifact. A manifest that exists
only in the worktree SHALL be reported as blocked with a scoped repair to commit
it, never as a pass. Every indexed member — each `accepted[]` entry, the `plan`
object, and each `evidence[]` entry — SHALL declare a non-empty string `path` and
a non-empty string `sha256`; a missing key, empty string, `null`, or non-string
value SHALL be classified as malformed and SHALL NEVER pass. A malformed,
unreadable, or worktree-only manifest SHALL be `unqualified` or blocked, never a
pass.

#### Scenario: An unauthored next change is still handoff-ready
- **WHEN** the next change has no `proposal.md` at `HEAD` because its authoring
  stage has not run, and every other handoff check passes
- **THEN** the audit reports `handoff_ready: true` and does not create a
  next-change intake blocker

#### Scenario: The planned next change is already active
- **WHEN** the active directory at the next change’s planned slug declares that
  same next change, so the change is already authored and in progress
- **THEN** the audit reports the readiness check as `passed`, records no
  collision blocker, and performs no mutation and starts nothing, because the
  change is already the active one

#### Scenario: A plan-reachable change occupies the planned next-change slug
- **WHEN** an active change directory at the next change’s planned slug declares
  a change that is one of the plan’s changes or a declared dependency, so the
  planned slug is occupied
- **THEN** the audit returns `handoff_ready: false`, names the colliding
  directory, and reports a `resolve-slug-collision` repair owned by the
  orchestrator; the orchestrator does not start or author into the occupied
  slug, the plan’s normal pipeline archives the occupying change, and a fresh
  audit passes afterward with no repository mutation by the orchestrator

#### Scenario: An out-of-plan change occupies the planned next-change slug
- **WHEN** an active change directory at the next change’s planned slug declares
  a change that is neither in the plan nor a declared dependency
- **THEN** the audit returns `handoff_ready: false` and `next_action: stop` with
  an `operator-decision` blocker to resolve or rename the external change, and
  no orchestrator or worker archives, renames, moves, or rewrites that
  out-of-plan change

#### Scenario: Local branch without an upstream is handoff-ready
- **WHEN** the branch is committed and no remote upstream exists, and the
  remaining handoff checks pass
- **THEN** the audit reports `handoff_ready: true` and does not request a no-push
  waiver

#### Scenario: Source manifest verifies at HEAD
- **WHEN** every accepted source path exists at `HEAD` with the accepted hash
- **THEN** the source-reproducibility check passes

#### Scenario: Dead evidence paths are unqualified
- **WHEN** a committed evidence manifest references a path that no longer
  exists at `HEAD`
- **THEN** the audit reports the evidence as unqualified rather than passed

#### Scenario: A manifest entry with no usable path cannot pass
- **WHEN** a committed manifest contains an indexed member with an empty,
  missing, or `null` `path` or `sha256`
- **THEN** the audit classifies the manifest as malformed or unreadable, reports
  the evidence check as failed or unqualified, and never passes
  `evidence-at-head`

#### Scenario: A worktree-only manifest is blocked
- **WHEN** an evidence manifest exists in the worktree but is not committed at
  `HEAD`
- **THEN** the audit reports the evidence check as failed with a scoped repair to
  commit the manifest, and does not pass from the worktree copy

#### Scenario: A committed manifest missing from the worktree is read from HEAD
- **WHEN** the committed manifest exists at `HEAD` but its worktree copy is
  missing
- **THEN** the audit reads the manifest from `HEAD` and does not treat the
  missing worktree copy as a missing artifact

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

When the audit returns `handoff_ready: false`, the orchestrator SHALL split the
blockers by `repair.owner` and dispatch `opsx-repo-hygiene` with the exact
blocker list whose `repair.owner` is `opsx-repo-hygiene`. A plan-reachable `resolve-slug-collision` blocker
SHALL be resolved at plan level by the orchestrator, which SHALL NOT start or
author the colliding change, SHALL record which artifact is authoritative, and
SHALL make no repository mutation; the plan’s normal pipeline archives the
occupying change. An out-of-plan collision is not an orchestrator repair: it is
an `operator-decision` stop, and no worker SHALL archive, rename, move, or
rewrite the external change.

The hygiene writer SHALL repair in phases:

1. **safe baseline** — commit the accepted implementation files, the plan, the
   run-state ignore entry, and any perf/evidence script path repair;
2. **evidence** — add or supersede evidence, update manifests and checksums, and
   record what remains unqualified.

A repairable evidence or process blocker SHALL NOT prevent the safe-baseline
commit. An operator authority restriction, such as an archive-only waiver,
still bounds what the writer may commit and remains a stop. The writer SHALL
report each phase, its commit, and what remains.

The hygiene writer MAY add evidence, supersede a manifest entry, and update a
checksum for an artifact it can actually read. It SHALL preserve historical
originals byte-for-byte and SHALL NOT claim a missing artifact is verified. It
SHALL NOT fabricate a hash, command output, or timestamp.

The hygiene writer SHALL stage explicit pathspecs and SHALL NOT include
user-owned files, secrets, unrelated source, or generated state in a commit. It
SHALL NOT change product behavior or close a code-review finding. A code defect
requires a new reviewed change or an explicit re-review by the change owner;
making a failing test pass by editing product code is not a handoff repair.

The writer MAY amend a commit it created in the current repair round while that
commit is still `HEAD`. It SHALL NOT rebase, reset, force-push, or amend a commit
created by another worker, an earlier change, or the operator. If the commit has
descendants, it SHALL create a new commit instead.

After the repair and after an orchestrator-owned slug resolution, the
orchestrator SHALL dispatch a fresh audit. Repair/audit rounds beyond two are
permitted under the advisory-cap requirement when a recorded justification
exists. The orchestrator SHALL stop when no scoped repair remains or when the
blocker requires a human-only decision.

#### Scenario: Baseline commits while evidence remains blocked
- **WHEN** the blocker list contains 41 accepted source files plus an unresolved
  evidence-policy question
- **THEN** the hygiene writer commits the source baseline and plan first, and
  reports the evidence question as the remaining blocker

#### Scenario: Evidence is superseded without losing the original
- **WHEN** a committed manifest points at a retained evidence path whose format
  changed
- **THEN** the hygiene writer adds a superseding entry or versioned file,
  updates the checksum, and leaves the original bytes unchanged

#### Scenario: A missing artifact is not claimed verified
- **WHEN** the original evidence artifact no longer exists anywhere the writer
  can read
- **THEN** the writer records it as unqualified with the dead manifest
  reference, and asserts no verification

#### Scenario: The writer amends its own HEAD commit
- **WHEN** the writer’s own baseline commit is `HEAD` and a correction belongs in
  that commit
- **THEN** it may amend that commit and report the new SHA

#### Scenario: The writer refuses to amend a commit with descendants
- **WHEN** the commit that needs correction already has descendants
- **THEN** the writer creates a new commit instead of rebasing or rewriting
  history

#### Scenario: Repair cannot make the audit pass
- **WHEN** repair rounds still leave a blocker after every scoped repair has
  been attempted
- **THEN** the orchestrator stops before the next change and reports the
  remaining blocker and the exact human-only decision, not the round count

#### Scenario: Source commit repairs the handoff
- **WHEN** the only blocker is that the accepted implementation is uncommitted
- **THEN** the hygiene writer commits exactly those files and the fresh audit
  passes

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

The orchestrator SHALL reserve at least three dispatches per change as a planning
allowance for the handoff audit, hygiene repair, and re-audit. The reserve SHALL
be excluded from `MAX_DISPATCHES` accounting so review cycles do not silently
consume it.

When the remaining allowance is below the reserve, the orchestrator SHALL record
the shortfall and continue under the advisory-cap rule. It SHALL NOT stop solely
because the numeric reserve cannot be honored.

#### Scenario: Handoff reserve is recorded as a planning allowance
- **WHEN** a change starts with a dispatch allowance
- **THEN** the orchestrator records the three-dispatch handoff reserve separately
  from the implementation allowance

#### Scenario: Reserve shortfall is recorded rather than terminal
- **WHEN** the remaining dispatch allowance is below the three-dispatch reserve
- **THEN** the orchestrator records the shortfall, may continue with a
  justification, and does not stop for the arithmetic alone

#### Scenario: Review budget leaves no room for handoff
- **WHEN** the remaining dispatch allowance is less than the reserved handoff
  allowance
- **THEN** the orchestrator records the shortfall, does not stop for the
  arithmetic alone, and continues only with a recorded cap override or stops
  when a real blocker remains

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

### Requirement: Audit stop is reserved for human-only boundaries

`opsx-integration-auditor` SHALL use `next_action: stop` and
`repair.action: operator-decision` only when the blocker requires a true
human-only decision, such as a declared human gate, a safety or credential
exposure, a trust or privacy decision, an operator waiver, an out-of-plan change
occupying the next change’s planned slug, or missing evidence that is the sole
basis of an accepted claim the operator must adjudicate.

The auditor SHALL NOT return `stop` for internal evidence retention, commit
hygiene, an unauthored next change, a missing remote upstream, run-state ignore
work, a perf/evidence script path, a plan-reachable slug collision the
orchestrator can resolve at plan level, or a dispatch or repair-round cap. Those
cases SHALL return `repair` with an explicit action, or an `unqualified`
carry-forward when no scoped repair exists.

#### Scenario: Repairable evidence gap returns repair
- **WHEN** a committed manifest references a retained artifact whose evidence
  entry can be superseded
- **THEN** the auditor returns `next_action: repair` and names the manifest and
  checksum repair, not an operator decision

#### Scenario: An unauthored next change never produces stop
- **WHEN** the next change has no proposal because authoring has not started
- **THEN** the auditor does not return `next_action: stop` and does not create a
  blocker for the missing proposal

#### Scenario: Missing evidence is a carry-forward when the artifact is gone
- **WHEN** an evidence artifact no longer exists and is not the sole basis of an
  accepted claim
- **THEN** the auditor reports it as `unqualified` with a carry-forward record
  and does not escalate it as an operator decision

#### Scenario: A true human-only boundary stops
- **WHEN** the next change is listed in the resolved human-gate set or a
  credential exposure requires an operator decision
- **THEN** the auditor returns `next_action: stop` and names the exact decision

#### Scenario: An out-of-plan slug occupant stops
- **WHEN** an out-of-plan change occupies the next change’s planned slug
- **THEN** the auditor returns `next_action: stop` with an `operator-decision`
  blocker to resolve or rename the external change, and no worker archives,
  renames, moves, or rewrites it

#### Scenario: A plan-reachable slug occupant repairs
- **WHEN** the occupying change is one of the plan’s changes or a declared
  dependency
- **THEN** the auditor returns `next_action: repair` with
  `resolve-slug-collision`, not `stop`

#### Scenario: Caps never justify stop
- **WHEN** the dispatch or repair-round count has reached its advisory limit
- **THEN** the auditor does not use the cap as a stop reason and reports the
  remaining work instead

### Requirement: Orchestrator-owned changes continue autonomously

The plan-level orchestrator SHALL drive every change that is not in the resolved
human-gate set end-to-end without asking the operator for internal repository,
evidence, commit, or intake decisions.

For an orchestrator-owned change, the orchestrator SHALL decide and record at
least:

- authoring or amending the change’s artifacts;
- implementation and corrective rounds;
- evidence retention and repair disposition;
- commit boundaries, including local amendments of its own commits;
- whether the next change’s authoring may start.

The orchestrator SHALL NOT ask a question whose answer is already determined by
the plan, the change artifacts, the audit, or the acceptance tier. It SHALL
record each autonomous decision with its basis in the run ledger.

The orchestrator SHALL stop only for a human gate, an unmet dependency, a
plan/artifact contradiction, a safety or credential exposure, an audit blocker
with no scoped repair, or another human-only decision.

#### Scenario: Internal repair does not become a question
- **WHEN** an audit finds uncommitted source, an ignored-run-state gap, and a
  perf-script default path
- **THEN** the orchestrator dispatches the scoped repair and continues without
  asking the operator to choose commit or evidence policy

#### Scenario: Evidence disposition is decided autonomously
- **WHEN** a committed manifest references a dead evidence path that can be
  superseded from a retained artifact
- **THEN** the orchestrator records the repair decision and does not escalate it
  as an operator decision

#### Scenario: Changes one through three need no internal intake
- **WHEN** a plan declares one human gate on its fourth change
- **THEN** the orchestrator authors, implements, reviews, archives, and hands
  off changes one through three without requesting an internal intake decision

### Requirement: Commits and amendments are local to the run

The orchestrator SHALL exercise commit authority through its archiver and
hygiene workers. It SHALL ensure local commits contain the accepted archive
scope and handoff repairs.

The orchestrator and its workers SHALL NOT push, create a remote branch, open a
pull request, merge a pull request, or require a remote upstream. They SHALL NOT
rewrite commits they do not own, and SHALL NOT amend a commit that has
descendants. Amending is limited to a commit created by the same worker in the
current round while that commit is still `HEAD`.

#### Scenario: Local commit needs no upstream
- **WHEN** the archive commit and handoff repair are complete on a local branch
  with no remote
- **THEN** the run is delivered locally and no push or pull request is attempted

#### Scenario: The orchestrator does not amend another worker’s commit
- **WHEN** a correction belongs to a commit created by a different worker or an
  earlier change
- **THEN** the orchestrator creates a new local commit instead of amending it

#### Scenario: Amending a commit with descendants is refused
- **WHEN** the target commit is not `HEAD`
- **THEN** no rebase or history rewrite is performed and a new commit is created

### Requirement: Dispatch and repair-round caps are advisory

`MAX_DISPATCHES`, the review corrective-round limit, and the repair/audit round
limit SHALL be advisory thresholds, not terminal conditions.

Before exceeding any of them, the orchestrator SHALL record a `cap_override`
ledger entry naming the cap and its value or source, the count already
consumed, the concrete remaining work, why continuing is safer than stopping,
and the next checkpoint at which the decision is re-evaluated.

The orchestrator MAY then continue and SHALL NOT stop solely because a cap was
reached. It SHALL still stop when the remaining work is unsafe, ambiguous,
blocked by a human gate, or requires a human-only decision.

#### Scenario: Dispatch cap is exceeded with a justification
- **WHEN** `MAX_DISPATCHES` is reached while a scoped handoff repair and re-audit
  remain
- **THEN** the orchestrator records the cap, the remaining repair work, and the
  justification, and continues

#### Scenario: Third repair round is justified
- **WHEN** two repair/audit rounds failed for different scoped blockers and a
  third repair is available
- **THEN** the orchestrator records the override and permits the third round
  instead of stopping on the round count

#### Scenario: Cap reached with no safe work left
- **WHEN** a cap is reached and the only remaining action requires a human-only
  decision
- **THEN** the orchestrator stops and reports that human-only decision, not the
  cap

### Requirement: Post-archive findings are carried forward, not reopened

A defect, risk, or missing verification discovered after a change is archived
SHALL be recorded as a carry-forward item in the run ledger. The record SHALL
include the originating archived change, the current change, the locus
(`path[:symbol]`), observed and required behavior, evidence, impact, and whether
the item affects a later planned change.

The orchestrator SHALL NOT reopen the archived change, edit its archived delta,
re-run its review as if it were active, or insert an unplanned follow-up change
into the plan. The archived change stays closed. If the item makes a later
change’s dependency or accepted behavior impossible, that later change blocks or
fails on its own acceptance criteria.

The orchestrator SHALL report all remaining carry-forward items after the plan’s
orchestrator-owned changes complete.

#### Scenario: A defect in change one is found while starting change two
- **WHEN** change two’s tests reveal a defect in change one’s archived
  implementation
- **THEN** the orchestrator records a carry-forward item and continues change
  two without reopening change one

#### Scenario: An archived delta is not edited
- **WHEN** a later fix changes behavior that an archived change introduced
- **THEN** the archived delta and archive directory remain byte-identical and
  the fix is recorded as carry-forward or delivered by a later planned change

#### Scenario: No unplanned change is inserted mid-plan
- **WHEN** a post-archive finding looks like a small standalone fix
- **THEN** the orchestrator does not create a new change in the running plan and
  instead records the follow-up for the final report

#### Scenario: A carry-forward affects a later change
- **WHEN** a carried defect invalidates a contract the next planned change
  depends on
- **THEN** the later change blocks on its own acceptance criteria, with the
  carry-forward item named, and the archived change remains closed

### Requirement: Human gates are resolved and enforced

The orchestrator SHALL resolve the human-gate set in this order:

1. an explicit `HUMAN_GATES` invocation value, when present;
2. the plan frontmatter’s `human_gates` list;
3. the empty list.

An explicit value SHALL replace the frontmatter list rather than add to it. An
explicit `HUMAN_GATES: none` SHALL clear the plan’s gates for that run and be
recorded verbatim. Every resolved entry SHALL be an exact change slug present in
the plan; an unknown slug SHALL be a fail-closed preflight error.

A change in the resolved human-gate set SHALL be operator-gated. The orchestrator
SHALL NOT author, implement, review, archive, or dispatch any work for it. It
SHALL stop before starting the gated change and report the gate and the resume
instruction. A later change that depends on a gated change SHALL NOT start until
the gate is cleared and the dependency is complete.

#### Scenario: Plan frontmatter declares the gate
- **WHEN** the plan declares
  `human_gates: [add-container-admin-role]` and the invocation supplies no
  override
- **THEN** the orchestrator drives the preceding changes and stops before
  `add-container-admin-role`

#### Scenario: Explicit input overrides the plan
- **WHEN** the plan lists a gate but the operator supplies a different
  `HUMAN_GATES` value
- **THEN** the explicit value is authoritative and the plan list is not merged
  into it

#### Scenario: Explicit none clears the plan gates
- **WHEN** the plan lists a gate and the operator supplies `HUMAN_GATES: none`
- **THEN** the gate set is empty for that run and the override is recorded
  verbatim

#### Scenario: Unknown gate slug fails preflight
- **WHEN** a resolved gate names a change that is not in the plan
- **THEN** the orchestrator stops before dispatching any worker and reports the
  unknown slug

#### Scenario: A gated dependency blocks later changes
- **WHEN** a later change depends on a gated change that has not run
- **THEN** the orchestrator does not start the later change and reports the
  unmet gated dependency
