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

`MAX_DISPATCHES` and the repair/audit round limit SHALL be advisory thresholds,
not terminal conditions. The review corrective-round budget is a convergence
rule governed by *Review convergence has a default two-round corrective
budget*; it is not a generic advisory cap and SHALL NOT be exceeded by a
`cap_override`.

Before exceeding a dispatch or repair/audit cap, the orchestrator SHALL record a
`cap_override` ledger entry naming the cap and its value or source, the count
already consumed, the concrete remaining work, why continuing is safer than
stopping, and the next checkpoint at which the decision is re-evaluated.

The orchestrator MAY then continue and SHALL NOT stop solely because a dispatch
or repair/audit cap was reached. It SHALL still stop when the remaining work is
unsafe, ambiguous, blocked by a human gate, or requires a human-only decision. A
dispatch or repair/audit override SHALL NOT authorize an additional corrective
round, and a `cap_override` that names a corrective round SHALL be refused.

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
- **WHEN** a dispatch or repair/audit cap is reached and the only remaining
  action requires a human-only decision
- **THEN** the orchestrator stops and reports that human-only decision, not the
  cap

#### Scenario: A corrective round cannot be overridden as a dispatch cap
- **WHEN** the two-round corrective budget is exhausted and `MAX_DISPATCHES`
  still has headroom
- **THEN** the orchestrator does not create a `cap_override` for a third
  corrective round, and selects the split-or-re-scope decision instead

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

### Requirement: Review convergence has a default two-round corrective budget

A **corrective round** is one corrective implementation dispatch followed by the
review that verifies it. The initial review is not a corrective round. The
default budget SHALL be **two corrective rounds per change**.

For the first two corrective rounds, the orchestrator MAY authorize the round as
an autonomous decision when the prior review's blocking findings are eligible
under *Findings are classified before another corrective round*.

After the second corrective round ends with a failing review, the orchestrator
SHALL NOT grant a third corrective round by default and SHALL NOT stop or ask the
operator for convergence authorization. It SHALL choose exactly one autonomous
action:

1. **Split** — `opsx-split-materializer` SHALL execute every structural split.
   When the plan contains an exact `split_authorizations` entry for the parent,
   the orchestrator SHALL select that exact entry and materialize it through
   `opsx-split-materializer`. When it does not, the orchestrator SHALL split only
   where the plan's own split rules or plan text permit an unpreauthorized
   split; the standard change-writer then authors the child set to
   authorization-equivalent exactness — a complete obligation map, a dependency
   remap, and an aggregate exit — and the materializer executes it. Where the
   plan reserves split approval to the operator, the orchestrator SHALL NOT
   split autonomously; it SHALL honor the plan's declared operator checkpoint,
   re-scope, or carry forward.
2. **Re-scope** — narrow the current change to its accepted surface, carry the
   remainder forward as a new slice with an explicit obligation mapping, and
   continue.
3. **Carry-forward** — when neither split nor re-scope is permitted by the plan,
   record the remaining finding or obligation as a carry-forward item and
   continue with independent authorized work.

The orchestrator SHALL record the split/re-scope/carry-forward decision in the
ledger before dispatching the next stage. A proposed re-scope/authority decision
SHALL be recorded there too, naming the parent, the proposed child set, the
obligation and dependency mapping, the aggregate exit, and the missing authority
or plan permission; it SHALL NOT be returned as a terminal stop. The orchestrator
SHALL NOT widen scope, change a gate, skip a failing acceptance criterion, or
create an unplanned requirement to avoid the decision, and SHALL NOT invent a
materializer or a plan edit.

A child of a split SHALL inherit the parent's corrective-round consumption by
blocker class: a blocker class that consumed a corrective round in the parent
SHALL NOT receive a fresh two-round budget in a child, while a blocker class
introduced by a child's own diff accrues under that child's budget. The ledger
SHALL record the inherited classes and their consumed counts per child.

A split, re-scope, or carry-forward that would change requirements, human gates,
or acceptance tiers is outside this authority. In that case the orchestrator
SHALL narrow to the accepted surface, carry the remainder forward, and continue;
it SHALL NOT ask the operator to authorize the convergence step.

This requirement constrains the corrective-round authority in
*Orchestrator-owned changes continue autonomously*: a third corrective round is
not an internal autonomous decision; the autonomous decision is how to split,
re-scope, or carry forward. `MAX_DISPATCHES` and the repair/audit round limit
remain advisory and do not authorize a third corrective round.

#### Scenario: Two failed corrective rounds trigger an autonomous split
- **WHEN** a change's second corrective round ends with a failing review and the
  plan contains an exact split authorization for that parent
- **THEN** the orchestrator selects and materializes the authorized replacement
  and continues, without asking the operator and without granting a third
  corrective round

#### Scenario: No exact split authorization uses the plan's own split permission
- **WHEN** two corrective rounds are exhausted and the plan has no exact
  `split_authorizations` entry but its own split rules or text permit an
  unpreauthorized split
- **THEN** the standard change-writer authors the child set with a complete
  obligation map, a dependency remap, and an aggregate exit,
  `opsx-split-materializer` executes it, and the orchestrator continues

#### Scenario: A plan-reserved split approval is honored
- **WHEN** two corrective rounds are exhausted, a split is indicated, and the
  plan reserves split approval to the operator
- **THEN** the orchestrator does not split autonomously; it honors the plan's
  declared checkpoint, or narrows to the accepted surface and carries the
  remainder forward, and it does not invent a materializer or a plan edit

#### Scenario: A split child inherits the parent's consumed rounds
- **WHEN** a corrective round was spent on a blocker class in the parent and a
  child owns that blocker class's obligation
- **THEN** the child's ledger record inherits the consumed count for that class
  and the child does not receive a fresh two-round budget for it

#### Scenario: A proposed decision is recorded, not returned as a stop
- **WHEN** a split is indicated and neither an exact authorization nor a plan
  split permission exists
- **THEN** the orchestrator records the proposed re-scope/authority decision in
  the ledger, narrows or carries forward as the plan permits, and continues
  instead of returning a terminal stop

#### Scenario: Split is not permitted, carry forward and continue
- **WHEN** two corrective rounds are exhausted and neither a split nor a
  re-scope is permitted by the plan
- **THEN** the orchestrator records the remaining finding or obligation as a
  carry-forward item and continues with independent authorized work

#### Scenario: No operator question for convergence
- **WHEN** the two-round budget is exhausted
- **THEN** the orchestrator does not ask the operator for authorization to
  continue, does not return a terminal proposed decision, and does not stop

#### Scenario: A split cannot change requirements or gates
- **WHEN** the only available split would change a requirement, acceptance tier,
  or human gate
- **THEN** the orchestrator narrows the current change to its accepted surface,
  carries the remainder forward, and continues without asking the operator

#### Scenario: Independent work continues
- **WHEN** one change is being split or re-scoped and another planned change is
  ready and already authorized
- **THEN** the orchestrator may drive the independent change without treating the
  in-progress split as authority to widen the blocked change

### Requirement: Findings are classified before another corrective round

Before the orchestrator dispatches another corrective round, every finding SHALL
be classified as exactly one of:

- `introduced_by_diff` — the reviewed diff introduced the observed failure, or
  newly exposed or worsened an existing path;
- `adjacent_scope` — the behavior is real but outside the change's accepted
  success parameters, including a next-slice concern, a neighboring page, a
  broader error taxonomy, or unaccepted hardening;
- `pre_existing` — the defect predates the reviewed diff and is not newly
  exposed or worsened by it.

The reviewer SHALL supply `introduced_by_diff` (`yes|no|unknown`) and
`adjacent_scope` (`true|false`) on every finding. The orchestrator SHALL derive
the classification with this precedence:

1. `adjacent_scope: true` → `adjacent_scope`;
2. else `introduced_by_diff: yes` → `introduced_by_diff`;
3. else `pre_existing`.

`unknown` SHALL NOT earn a corrective round. The orchestrator SHALL resolve it
from the diff, the accepted scope, and the evidence first, and SHALL record the
resulting classification and its basis. A classification dispute SHALL be
adjudicated by the orchestrator from the evidence packet, not by another reviewer
round.

Only a blocking `introduced_by_diff` finding SHALL earn a corrective round.
`adjacent_scope` findings SHALL be recorded as a new slice and SHALL NOT be
fixed in the current change or block it. A new slice becomes real through an
exact split authorization or the plan's scope-preserving split rules; otherwise
the item is a documented follow-up. The orchestrator SHALL NOT ask the operator
to approve the re-scope. `pre_existing` findings SHALL be recorded as
carry-forward items with locus, observed and required behavior, evidence,
impact, and disposition; they SHALL NOT reopen an archived change.

A regression introduced by a correction is `introduced_by_diff` and is evaluated
by that corrective round's review; it does not create an entitlement beyond the
corrective-round budget.

#### Scenario: An adjacent-scope finding does not earn a round
- **WHEN** a review reports a real defect in the next slice's page while the
  active change's accepted success parameters are met
- **THEN** the finding is classified `adjacent_scope`, proposed as a new slice,
  and does not block or extend the active change

#### Scenario: A pre-existing defect is carried forward
- **WHEN** a review reports a defect that predates the diff and the diff neither
  exposes nor worsens it
- **THEN** the orchestrator classifies it `pre_existing` and records a
  carry-forward item instead of a corrective round

#### Scenario: An introduced defect earns the corrective round
- **WHEN** the reviewed diff introduces a wrong error envelope for an accepted
  scope case
- **THEN** the finding is classified `introduced_by_diff` and is eligible for the
  current corrective budget

#### Scenario: An unknown classification is resolved before a round
- **WHEN** a reviewer marks `introduced_by_diff: unknown`
- **THEN** the orchestrator adjudicates it from the diff and evidence, records
  the classification and basis, and does not dispatch a round on the unknown
  value

#### Scenario: A mixed finding set only corrects the diff
- **WHEN** one review contains one introduced defect, two adjacent concerns, and
  one pre-existing defect
- **THEN** only the introduced defect enters the corrective handoff, the two
  adjacent concerns become proposed slices, and the pre-existing defect becomes
  a carry-forward item

#### Scenario: Adjacent scope is not inserted as an unplanned change
- **WHEN** an adjacent-scope finding looks like a small standalone fix
- **THEN** the orchestrator records it as a proposed slice or follow-up and does
  not insert an unplanned change into the running plan

### Requirement: Broad first reviews trigger a slice split checkpoint

The first review SHALL serve as a sizing checkpoint as well as a verdict. A
first review is **broad** when any of these holds:

- blocking findings require correction in two or more distinct named surfaces
  from: API contract, CLI, page/UI, freshness, error taxonomy, cache invariants;
- the change claims three or more acceptance scenarios that cannot be proved by
  one focused check; or
- the blocking finding set spans three or more distinct loci and is not
  explained by one shared root cause.

When the first review is broad, the orchestrator SHALL make the split-or-
re-scope decision before dispatching any corrective implementation. It SHALL NOT
spend a corrective round discovering that the slice is too large.

The same checkpoint applies when implementation evidence shows the change
exceeds the maximum surface heuristic before the first review runs. The
checkpoint is evaluated at the earliest evidence, not only after two failed
rounds.

When an exact `split_authorizations` entry exists, the orchestrator SHALL select
that entry and `opsx-split-materializer` SHALL execute the split before any
further correction.

When no exact authorization exists, the orchestrator SHALL split only where the
plan's own split rules or text permit an unpreauthorized split. In that case the
standard change-writer SHALL author the child set to the same exactness
invariants — a complete obligation map, a dependency remap, and an aggregate
exit — and `opsx-split-materializer` SHALL execute the split. Where the plan
reserves split approval to the operator, the orchestrator SHALL honor the plan's
declared checkpoint instead of splitting autonomously.

When no materializer is available, or the plan permits neither an exact
authorization nor an unpreauthorized split, the orchestrator SHALL re-scope:
narrow the current change to its accepted surface, carry the remainder forward
as a new slice or follow-up, and continue with independent work. It SHALL NOT
return a terminal proposed decision, SHALL NOT ask the operator to authorize the
split, and SHALL NOT improvise a materializer or a plan edit.

A reviewer verdict, a `cap_override`, or general autonomy SHALL NOT substitute
for the plan's split rules or for a change to requirements, acceptance tiers, or
human gates.

#### Scenario: A multi-surface first review splits before correction
- **WHEN** the first review of a change finds blocking issues in its API
  contract, its page, and its error taxonomy
- **THEN** the orchestrator makes the split-or-re-scope decision before any
  corrective implementation and does not grant a corrective round

#### Scenario: A five-page change triggers the checkpoint
- **WHEN** a change's first review covers five pages and finds blocking issues
  across them
- **THEN** the orchestrator splits or re-scopes the slice before correcting all
  five in place

#### Scenario: An exact authorization is materialized before correction
- **WHEN** the first review is broad and the plan contains an exact
  `split_authorizations` entry for the parent
- **THEN** the orchestrator selects and materializes the replacement before any
  further correction

#### Scenario: No exact authorization uses the plan's split permission
- **WHEN** the first review is broad and no exact split authorization exists, but
  the plan's own split rules or text permit an unpreauthorized split
- **THEN** the change-writer authors the child set with a complete obligation
  map, a dependency remap, and an aggregate exit, the materializer executes it,
  and the orchestrator continues without asking the operator

#### Scenario: No materializer re-scopes and carries forward
- **WHEN** a split is indicated but `opsx-split-materializer` is unavailable, or
  the plan permits neither an exact authorization nor an unpreauthorized split
- **THEN** the orchestrator narrows the change to its accepted surface, carries
  the remainder forward, and continues, without improvising a materializer, a
  new requirement, or a gate

#### Scenario: One shared root cause avoids an unnecessary split
- **WHEN** the first review finds three blocking loci that all follow from one
  shared root cause inside the accepted scope
- **THEN** the review is not broad solely because of the locus count, and the
  orchestrator may authorize a corrective round within the budget

#### Scenario: A reviewer recommendation is not split authority
- **WHEN** a reviewer recommends a split but the plan contains no exact
  authorization
- **THEN** the orchestrator applies the plan's split rules, or narrows and
  carries forward, and continues without asking the operator

#### Scenario: No operator question for a broad review
- **WHEN** the first review is broad
- **THEN** the orchestrator does not ask the operator to authorize a corrective
  round or a split; it applies the convergence rule and continues

### Requirement: Bookkeeping writes are bounded per stage

The orchestrator SHALL use at most one ledger-writer dispatch per stage. That
dispatch SHALL append the stage checkpoint and all record-only events in one
batch. A record-only update, such as a status line, a dispatch count, or a copied
coverage array, SHALL NOT justify a separate `coder` session.

Workers SHALL return their records in their normal output contract. The
orchestrator SHALL batch those records into the stage-boundary write. A stage
with no decision change and no new evidence SHALL NOT require a writer dispatch.

The evidence packet SHALL be minimal: change, stage, HEAD, diff range or staged
paths, commands with exit codes and counts, artifact paths with hashes, claim
status, findings with classification, and the next action. Digests, UID/GID, and
effective roots SHALL appear only when the change's behavior depends on them.

#### Scenario: Many record-only updates use one writer
- **WHEN** a stage produces a checkpoint update and several copied coverage and
  dispatch records
- **THEN** the orchestrator dispatches one ledger writer for the stage and does
  not spawn a `coder` session per record

#### Scenario: A no-change stage needs no writer
- **WHEN** a stage completes with no decision change and no new evidence
- **THEN** the orchestrator does not dispatch a ledger writer for that stage

#### Scenario: The packet carries only proof
- **WHEN** a documentation change has no image or process behavior
- **THEN** its evidence packet omits image digests and UID/GID instead of
  collecting fields that prove nothing

### Requirement: Operator attestation is authoritative for the state it names

An operator attestation is an explicit statement in the invocation, a dispatch
reply, or a recorded message. The orchestrator and auditor SHALL treat it as
authoritative for its named subject and SHALL NOT re-derive, re-check, or ask
again about that subject.

An attestation record SHALL contain:

- `subject` — `quiescence`, `authority`, `gate`, `evidence`, or `scope`;
- `statement` — the operator's words, verbatim;
- `scope` — the exact change, commit, or artifact it covers;
- `source` — invocation, message, or ledger;
- `recorded_at` — the run timestamp.

A quiescence attestation ends the quiescence question: the orchestrator SHALL
NOT poll the child, restart a probe, or require a second confirmation. An
authority attestation authorizes the named commit or action; the scoped writer
records it and proceeds. A gate attestation is authoritative for the named gate
and is recorded verbatim.

Attestation SHALL NOT make a false repository claim true, SHALL NOT substitute
for a hard content check the auditor can run, and SHALL NOT widen the change's
accepted scope. When an attestation conflicts with a hard repository fact, the
orchestrator SHALL record the conflict and stop for a human-only decision; it
SHALL NOT silently prefer either side.

#### Scenario: Quiescence attestation is not re-derived
- **WHEN** the operator states that a child is quiescent and no terminal result
  has arrived
- **THEN** the orchestrator records the attestation and does not poll or re-probe
  the session before proceeding

#### Scenario: Authority attestation is used as stated
- **WHEN** the operator authorizes committing the accepted source baseline
- **THEN** the hygiene writer records the authorization verbatim and commits the
  named pathspecs without asking again

#### Scenario: Gate attestation clears the named gate
- **WHEN** the operator states that a named human gate is cleared
- **THEN** the orchestrator records the statement and does not re-derive the gate
  state

#### Scenario: Attestation cannot waive a hard content check
- **WHEN** the operator attests that the source is at `HEAD` but the accepted
  path is missing at `HEAD`
- **THEN** the auditor still reports the content check as failed and does not
  treat the attestation as a pass

#### Scenario: Conflicting attestation stops
- **WHEN** an attestation says the tree is clean but the tracked tree has
  uncommitted accepted source
- **THEN** the orchestrator records the conflict and stops for a human-only
  decision instead of silently choosing a side

#### Scenario: Attestation does not widen scope
- **WHEN** an operator attests that a neighbor page is acceptable but the change
  did not accept it
- **THEN** the item remains `adjacent_scope` and does not become accepted work

### Requirement: Per-stage cost and token burn are recorded

Each stage record SHALL include:

- `tokens_in`, `tokens_out`, and `cache_read_tokens` when the harness or child
  reports them;
- `cost_status`: `estimated`, `unresolved`, `unavailable`, or `not-reported`;
- `estimated_cost` and its pricing catalog version when estimated;
- `usage_source` — telemetry, the child's report, or the operator.

The run ledger and final report SHALL include per-stage tokens and cost status,
run totals for the three token categories, a `priced_subtotal`, and an
`unpriced_stage_count`. The orchestrator SHALL NOT report a bare `$0` or
`$0.00` total when any stage is unresolved or unavailable. A stage with observed
usage and an unresolved cost SHALL be reported as `unpriced`, not folded into
zero. A stage with no reported usage SHALL be `not-reported`, and the final
report SHALL say the burn is unknown rather than inventing a number.

A `$0.00` total SHALL be reported only when every stage is estimated at zero or
the run truly used zero tokens.

#### Scenario: Unresolved cost is not a zero
- **WHEN** a stage reports 1.2M input tokens and cost telemetry is `unresolved`
- **THEN** the ledger records the tokens, reports the stage as `unpriced`, and
  the final report does not present the run total as `$0`

#### Scenario: Zero usage is a genuine zero
- **WHEN** every stage used zero tokens with a known model
- **THEN** the report may show `$0.00` because the zero is estimated, not
  unresolved

#### Scenario: Missing usage is unknown, not zero
- **WHEN** neither the harness nor the child reports token usage for a stage
- **THEN** the stage is `not-reported` and the final report says the burn is
  unknown

#### Scenario: The run report carries the burn
- **WHEN** a run finishes
- **THEN** the final report names per-stage tokens, the three run token totals,
  the priced subtotal, and the number of unpriced stages

### Requirement: A stale idle marker does not interrupt a live child

An idle session marker is an observation, not an interruption. The orchestrator
SHALL NOT treat it as an interruption when:

- the ledger records the stage as in flight;
- a live child session exists for that stage; and
- the child has produced progress more recently than the marker, or the marker
  is contradicted by the child's in-flight state.

In that case the orchestrator SHALL record a `stale_idle_marker` observation
with the marker source and the evidence that the child is live, and continue
waiting. It SHALL NOT re-dispatch the stage, cancel the child, open a duplicate
session, or start the next stage.

A stage is interrupted only when no live child exists, no terminal result is
available, and the ledger records the stage as in flight, and the absence is
confirmed by the harness or the operator. The orchestrator SHALL reconcile once
from the ledger before re-dispatching. It SHALL NOT poll repeatedly; the child's
completion notification or one status-scout report is the next event.

#### Scenario: Idle marker with a live child is stale
- **WHEN** a status report says a session is idle while the ledger records the
  stage in flight and the child is still producing progress
- **THEN** the orchestrator records `stale_idle_marker`, does not cancel or
  re-dispatch, and continues waiting

#### Scenario: A missing child is reconciled before re-dispatch
- **WHEN** no live child exists, no terminal result is available, and the ledger
  says the stage is in flight
- **THEN** the orchestrator reconciles once from the ledger, records the evidence,
  and only then re-dispatches the stage

#### Scenario: A terminal result is processed, not interrupted
- **WHEN** the child has returned a terminal result and an idle marker also
  exists
- **THEN** the orchestrator processes the result and does not treat the marker as
  an interruption

#### Scenario: A stale marker is recorded with evidence
- **WHEN** the orchestrator ignores an idle marker under this requirement
- **THEN** the ledger names the marker source and the live-child evidence it used

### Requirement: Implementation order and focused verification are fixed by the slice

For an orchestrator-owned change, the implementer SHALL order work by
correctness risk:

1. scope and registry validation, including authorization;
2. error taxonomy and envelope behavior;
3. cache invariants and concurrency;
4. freshness and persistence semantics;
5. page/UI wiring;
6. presentation and polish.

When a change has no scope or cache layer, the implementer SHALL start at the
highest-risk invariant the change does have. The implementer SHALL run the exact
focused tests the change defines and SHALL NOT perform a broad refactor, rename
unrelated code, or add a new test layer to prove a behavior already proved at a
lower layer.

The implementer SHALL stop only for a real contract conflict: the corrective
handoff contradicts the accepted artifacts, a required prerequisite is missing,
or the environment cannot run the required check. It SHALL NOT stop for a
stylistic question or because one round's automatable work does not fit.

#### Scenario: Correctness precedes the page
- **WHEN** a change has scope/registry validation, an error taxonomy, a cache
  layer, and a page
- **THEN** the implementer completes and verifies the scope, error, and cache
  work before the page

#### Scenario: The change's focused tests are the evidence
- **WHEN** the change defines focused tests for an error envelope and a cache
  collision
- **THEN** the implementer runs those tests and does not substitute a broad
  repository suite or a new end-to-end layer

#### Scenario: A contract conflict stops
- **WHEN** the corrective handoff requires behavior that contradicts the
  accepted spec
- **THEN** the implementer returns `blocked` with the exact conflict

#### Scenario: A stylistic question does not stop
- **WHEN** the implementer could name a file or helper more elegantly
- **THEN** it does not return `blocked` and does not broaden the diff

### Requirement: Handoff evidence is proportional

The Phase Transition Gate SHALL keep every hard reproducibility check:
`tracked-tree-clean`, `source-manifest-at-head`, `plan-at-head`,
`archive-commit-implementation-paths`, `run-state-ignored`, `evidence-at-head`,
`perf-script-from-head`, `fresh-checkout-checks`, `local-only-delivery`, and
`next-change-readiness`.

The auditor and hygiene writer SHALL accept a recorded operator attestation for
the state or authority question it names, and SHALL NOT re-derive that state.
Evidence already recorded in the ledger with a matching hash SHALL NOT be
re-collected; the auditor SHALL read the recorded path and hash. When a live
child and a clean ledger already establish quiescence and stage state, the
orchestrator SHALL NOT dispatch a fresh probe to re-derive them.

The auditor and hygiene writer SHALL return the minimal fields that prove their
checks, not duplicate transcripts. The hygiene writer SHALL still repair in
phases, stage explicit pathspecs, preserve historical originals, and refuse to
invent scope.

An attestation SHALL NOT waive a hard content check the auditor can read from
`HEAD`; `source-manifest-at-head`, `plan-at-head`, and
`archive-commit-implementation-paths` remain machine-verified.

#### Scenario: Recorded evidence is reused
- **WHEN** the ledger already records an accepted source path and its matching
  `sha256`
- **THEN** the auditor reads that path at `HEAD` and does not require a duplicate
  transcript

#### Scenario: Live child and clean ledger need no re-probe
- **WHEN** a live child is working, the ledger is clean, and the stage is in
  flight
- **THEN** the orchestrator does not dispatch a quiescence probe and does not
  re-derive the stage state

#### Scenario: A hard content check still runs
- **WHEN** the operator attests that the source is committed but
  `source-manifest-at-head` is unverified
- **THEN** the auditor still verifies the accepted paths at `HEAD`

#### Scenario: Hygiene accepts recorded authority
- **WHEN** the operator has already authorized the accepted-source commit and the
  attestation is in the ledger
- **THEN** the hygiene writer stages the named pathspecs without asking for the
  authorization again

#### Scenario: A missing hash is still collected
- **WHEN** a ledger evidence entry has a path but no hash
- **THEN** the auditor collects the hash instead of treating the entry as proved

#### Scenario: Duplicate transcripts are not required
- **WHEN** an audit can prove a check from one retained command log and its hash
- **THEN** it does not require a second copy of the same output

### Requirement: Controller specs set the floor and a plan may only tighten them

A controller capability spec SHALL set the floor for a plan-level run. Where an
accepted plan's own text and a controller spec prescribe different behavior for
the same situation, the tighter rule SHALL govern. A plan SHALL NOT loosen a
controller policy, corrective-round budget, cap, gate, acceptance tier, scope
limit, or stop condition.

An accepted plan MAY tighten the controller floor. Tightening includes reserving
a split or another structural decision for the plan's declared operator
checkpoint, shortening a budget, narrowing a change or surface limit, adding a
gate, and raising an evidence bar. The orchestrator SHALL apply a tightening
provision in place of the looser controller default.

Plan silence SHALL leave the controller rule unchanged; silence is not
permission. A plan provision that would loosen controller policy SHALL be void
for that run, and the orchestrator SHALL record it as plan drift rather than
follow it.

#### Scenario: A plan's bounded third corrective round is void
- **WHEN** plan prose permits one extra corrective round after the two-round
  budget is exhausted
- **THEN** the provision is void, the orchestrator records it as plan drift, and
  the split-or-re-scope decision governs instead of a third round

#### Scenario: A plan reserving split approval stands
- **WHEN** the plan reserves split approval to its own declared operator
  checkpoint
- **THEN** the orchestrator does not split autonomously even where the controller
  policy would permit a plan-permitted unpreauthorized split, and it honors the
  declared checkpoint or re-scopes and carries forward

#### Scenario: Plan silence does not relax the controller rule
- **WHEN** a plan says nothing about the corrective-round budget
- **THEN** the two-round default and every other controller rule apply unchanged
