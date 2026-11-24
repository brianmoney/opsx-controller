# reviewer-corrective-handoff Specification

## Purpose

Define the durable corrective handoff contract between reviewer and implementer
workers so that every failed review provides a self-contained, machine-readable
handoff and every retry implementer can act on it without ambiguity.

## Requirements

### Requirement: Failed reviews produce a self-contained corrective handoff

When a review worker returns a failing verdict, its `fix_prompt` SHALL be a
self-contained corrective handoff with labeled `CHANGE`, `FINDINGS`,
`CORRECTIVE GUIDANCE`, and `VERIFY` sections.

The `CHANGE` section SHALL identify the active change. The `FINDINGS` section
SHALL include every blocking `introduced_by_diff` finding and, for each, its
severity, relevant file or symbol, observed behavior, and required behavior.
`CORRECTIVE GUIDANCE` SHALL prescribe the implementation approach or invariants
necessary to correct the findings. `VERIFY` SHALL name the focused regressions
and validation commands required to demonstrate the correction.

The reviewer SHALL return an empty `fix_prompt` only for a passing verdict with
no `introduced_by_diff` blocking findings.

Each blocking finding SHALL cite the violated accepted requirement and a
concrete failing case with expected versus observed behavior. Its verification
requirements SHALL address the failure mechanism, not merely task labels or
implementer claims. Reviewers SHALL independently inspect the required
execution path and observation producer, limiting synthetic fixtures and test
doubles to the evidence scope they actually exercise.

Explicitly accepted nonessential deferrals reflected in the current
scope/specs/tasks SHALL be disclosed in the summary or artifact references,
not in the corrective handoff, `findings`, or finding counts, including as
`note`. A worker's deferral label SHALL NOT waive essential accepted-scope
behavior, unsupported required guarantees, or active unchecked tasks.

Every finding SHALL carry `introduced_by_diff` (`yes`, `no`, or `unknown`) and
`adjacent_scope` (`true` or `false`). The reviewer SHALL classify it with this
precedence:

1. `adjacent_scope: true` → `adjacent_scope`;
2. else `introduced_by_diff: yes` → `introduced_by_diff`;
3. else `pre_existing`.

Only `introduced_by_diff` findings SHALL appear in the prose `FINDINGS` section,
the `fix_prompt`, or the machine-readable `findings` array. An
`adjacent_scope` finding SHALL be returned in `out_of_scope[]` with
`classification: "adjacent_scope"` and SHALL NOT block the active change. A
`pre_existing` finding SHALL be returned in `out_of_scope[]` with
`classification: "pre_existing"` and is a carry-forward item, not corrective
scope. `introduced_by_diff: unknown` SHALL NOT enter the corrective handoff; the
orchestrator resolves it from the diff and evidence before another round.

A review worker SHALL additionally return a machine-readable `findings` array
carrying the same current-slice findings the prose handoff describes. Each entry
SHALL provide its `severity`, a `locus` array, a `statement` describing the
observed and required behavior, `introduced_by_diff`, and `adjacent_scope`.

Every `locus` entry SHALL be a repository-root-relative path, optionally
suffixed with `:<symbol>` naming the function, class, or constant the finding
concerns. A locus entry SHALL NOT be an absolute path, a bare symbol without
its file, or a path relative to a subdirectory of the repository.

The `findings` array SHALL be present and empty for a passing verdict.
`out_of_scope[]` SHALL be empty when every finding is current-slice scope; a
passing verdict MAY carry `out_of_scope[]` entries.

The prose `fix_prompt` SHALL remain the implementer's corrective input; the
`findings` array SHALL NOT replace it or change its required sections.

#### Scenario: Review failure includes actionable findings
- **WHEN** a reviewer finds a missing regression and incorrect terminal-state
  handling for change `preserve-handoff-reports`
- **THEN** its failed review handoff identifies that change, describes both
  findings with their severity and relevant code locations, directs the
  correction, and names the required regression and validation command

#### Scenario: Clean review has no corrective handoff
- **WHEN** a reviewer returns `verdict=pass` with all finding counts zero
- **THEN** it returns an empty `fix_prompt` and an empty `findings` array

#### Scenario: Structured findings accompany the prose handoff
- **WHEN** a reviewer returns a failing verdict describing two blocking
  `introduced_by_diff` findings in its `FINDINGS` section
- **THEN** its `findings` array carries two corresponding entries, each with a
  severity, at least one locus, a statement, `introduced_by_diff`, and
  `adjacent_scope`

#### Scenario: Loci are repository-root-relative
- **WHEN** a finding concerns the function `_apply_coordination_outcome` in
  `src/knowledge_forge/sources/gmail/intake.py`
- **THEN** its locus entry is
  `src/knowledge_forge/sources/gmail/intake.py:_apply_coordination_outcome`
  rather than a bare filename, a bare symbol, or an absolute path

#### Scenario: Structured findings do not displace the prose handoff
- **WHEN** a failing review returns both a `fix_prompt` and a `findings` array
- **THEN** the `fix_prompt` still contains all four labeled sections and is
  what the next implementer receives as corrective scope

#### Scenario: A blocking finding carries a concrete failure mechanism

- **WHEN** tests pass by trusting supplied fixture assertions but the accepted requirement needs observations from a live execution path
- **THEN** the reviewer cites that requirement, the false-green case, and verification that distinguishes the supplied assertions from actual producer observations

#### Scenario: An adjacent finding stays out of the corrective handoff
- **WHEN** a review finds a real defect in the next slice's page while the
  active change's accepted success parameters are met
- **THEN** the finding is `adjacent_scope`, appears in `out_of_scope[]`, and is
  absent from `FINDINGS`, `fix_prompt`, and `findings`

#### Scenario: A pre-existing finding is a carry-forward, not a correction
- **WHEN** a review finds a defect that predates the diff and the diff neither
  exposes nor worsens it
- **THEN** the finding is `pre_existing`, appears in `out_of_scope[]`, and does
  not enter the corrective handoff

#### Scenario: A pass may still report out-of-scope items
- **WHEN** every accepted success parameter is met and one adjacent concern was
  found
- **THEN** the verdict is `pass`, `fix_prompt` and `findings` are empty, and
  `out_of_scope[]` records the adjacent concern

#### Scenario: A finding without classification is incomplete
- **WHEN** a finding omits `introduced_by_diff` or `adjacent_scope`
- **THEN** the review is incomplete and the orchestrator does not use the
  finding to authorize a corrective round

### Requirement: Retry implementers execute the reviewer corrective handoff
An implementer receiving a non-empty `LATEST_FIX_PROMPT` SHALL treat every
finding, corrective guideline, and verification requirement in that handoff as
the highest-priority scope for the retry round.

If current change artifacts or live repository evidence make a requested
correction contradictory or unsafe, the implementer SHALL return its existing
blocked result instead of inventing an alternative requirement.

A retry implementer SHALL correct the root cause and use appropriate focused
verification, including a meaningful false-green regression where applicable.
If the same critical recurs after a correction, the workers SHALL reassess why
the earlier fix failed before another patch. The reviewer SHALL independently
verify closure and retain the same locus for the same defect, explaining why
the earlier correction was insufficient.

#### Scenario: Fresh implementer receives a failed review handoff
- **WHEN** a failed review advances a change from review to a new implementation
  round
- **THEN** the next implementer receives the persisted complete corrective
  handoff and implements its listed corrections and verification requirements

#### Scenario: Handoff contradicts current change artifacts
- **WHEN** the corrective handoff requires behavior that conflicts with the
  active change specification
- **THEN** the implementer returns a blocked result identifying the conflict
  rather than guessing which requirement wins

#### Scenario: A recurring critical requires root-cause reassessment

- **WHEN** the same critical defect survives an implementation correction
- **THEN** the next corrective handoff preserves its locus and explains the inadequate prior fix
- **AND** the implementer reassesses the execution/evidence path and verifies the root-cause correction before claiming closure

### Requirement: Corrective handoffs persist across retry dispatch
The controller SHALL persist a failed review's complete corrective handoff in
both `last_review.fix_prompt` and `latest_fix_prompt`, and SHALL supply it as
`LATEST_FIX_PROMPT` to the next implementation dispatch. A clean review SHALL
clear `latest_fix_prompt` only after recording the passing result.

#### Scenario: Multiple findings survive state persistence
- **WHEN** a reviewer returns a failed handoff containing multiple labeled
  findings
- **THEN** the per-change state retains the complete handoff and the next
  implementation input contains the same labeled sections

#### Scenario: Passing review clears stale retry scope
- **WHEN** a later review passes with zero findings after a prior failed review
- **THEN** `latest_fix_prompt` is empty and subsequent controller state does not
  present the prior corrective handoff as active work

### Requirement: Plan-level review prompts carry an adversarial coverage matrix

Before dispatching a plan-level change review, the orchestrator SHALL derive and
include a required coverage matrix for the change’s surfaces. The matrix SHALL
include, where applicable:

- expected read errors: permission denied, unreadable file, malformed persisted
  state, missing state, and the required success/unavailable/error envelope;
- invalid parameters and cache-key collisions, including a valid default request
  followed by an invalid value that previously shared the same cache key;
- maximum allowed response or page size, not only the default size;
- concurrent cache, single-flight, stale-while-revalidate, and no-write or
  no-subprocess invariants;
- next-phase compatibility: files and contracts the next change will rely on or
  modify.

The matrix SHALL be derived from the accepted spec, design, and changed
surfaces. It SHALL NOT be a generic checklist unrelated to the change.

#### Scenario: Error-path cases are required
- **WHEN** a change adds read handlers over generated or persisted state
- **THEN** the review matrix includes permission-denied, unreadable, malformed,
  and missing-state cases and the required response classification for each

#### Scenario: Cache-collision case is required
- **WHEN** a change caches handler results keyed by request parameters
- **THEN** the matrix includes a valid-default-then-invalid-parameter case and
  requires the invalid request to keep its protocol error

#### Scenario: Maximum-size case is required
- **WHEN** a change exposes a bounded page or response
- **THEN** the matrix includes the documented maximum, not only the default

#### Scenario: Next-phase compatibility is required
- **WHEN** the next planned change will modify the same files or consume the new
  contract
- **THEN** the matrix includes the files and contract points the next change
  depends on

### Requirement: Reviewers report coverage for every required case

The plan-level reviewer SHALL report each required matrix case as `covered`,
`not-applicable`, or `not-covered` with evidence.

A case marked `covered` SHALL cite the actual test, command, or observed
behavior. A case marked `not-covered` SHALL be a blocking finding when it
concerns accepted behavior or a required invariant; otherwise it SHALL be an
explicit follow-up.

A review that only re-runs the writer’s existing tests SHALL NOT satisfy this
requirement.

#### Scenario: Uncovered required case blocks acceptance
- **WHEN** a required permission-denied case is not covered by a test or an
  observed reproduction
- **THEN** the reviewer returns a blocking finding with the case id, expected
  behavior, and observed behavior

#### Scenario: Not-applicable case is justified
- **WHEN** a matrix case does not apply to the change
- **THEN** the reviewer records `not-applicable` with a one-sentence reason

#### Scenario: Coverage is machine-readable
- **WHEN** the reviewer returns its verdict
- **THEN** the final JSON includes a coverage array with case id, result, and
  evidence for every required case

### Requirement: Post-archive findings are carry-forward items, not reopened changes

When a reviewer, implementer, or auditor finds a defect or missing verification
in an already archived change while working on a later change, the finding SHALL
be recorded as a carry-forward item rather than a finding against the active
change. The record SHALL name the archived change, the active change, the locus
(`path[:symbol]`), observed and required behavior, evidence, impact, and whether
it affects a later planned change. Every surface that emits a carry-forward item
— including `opsx-change-writer` and the plan mode of `opsx-implementer` — SHALL
carry those fields; a contract that omits `observed` or `required` is
incomplete.

A carry-forward item SHALL NOT cause the archived change to be reopened, its
archived delta to be edited, or an unplanned change to be inserted into the
running plan. The reviewer SHALL NOT return a `fix` verdict or a non-empty
corrective handoff whose scope is the archived change.

If the archived defect is newly exposed or newly worsened by the active change’s
diff, the finding SHALL be reported against the active change instead, with both
the active locus and the archived origin named.

`opsx-change-writer` and `opsx-implementer` SHALL NOT repair a prior archived
change’s findings as part of a later change, including changes 2 and 3, unless
that later change’s own accepted scope independently requires the behavior. They
SHALL report the finding as out of scope for the active change and leave it to
the carry-forward record.

The reviewer SHALL keep the adversarial coverage matrix and the `coverage[]`
output contract for the active change; carry-forward classification changes only
which change owns a finding, not the required coverage.

#### Scenario: Reviewing change two finds a defect in change one
- **WHEN** the change-two review finds that an archived change-one handler
  returns the wrong error for a missing file
- **THEN** the reviewer records a carry-forward item naming change one and the
  handler locus, returns no corrective handoff for the archived change, and
  reports the change-two verdict on change two’s own scope

#### Scenario: A newly exposed defect belongs to the active change
- **WHEN** change two’s diff makes an archived change-one path reachable in a new
  way and that path now fails
- **THEN** the finding is reported against change two, with the archived origin
  named, because the active diff introduced the observable failure

#### Scenario: Change writer declines prior archived scope
- **WHEN** `opsx-change-writer` is authoring change two and a prior archived
  finding is present in the dispatch context
- **THEN** it does not amend the archived change, records the finding as
  out-of-scope carry-forward, and authors only change two

#### Scenario: Implementer declines prior archived scope
- **WHEN** `opsx-implementer` is implementing change three and the dispatch
  names a defect in change one’s archived implementation
- **THEN** it does not edit the archived change or adopt the archived finding as
  change three’s scope; if change three’s own accepted scope requires the same
  behavior, it implements that scope against change three and records the
  archived origin in its report

#### Scenario: Carry-forward does not block the next change by itself
- **WHEN** a carry-forward item has no effect on the next change’s dependencies
  or accepted behavior
- **THEN** the orchestrator records it and proceeds with the next planned change
  instead of stopping or reopening the archive

#### Scenario: Carry-forward that affects a later change blocks that change
- **WHEN** a carry-forward item invalidates a contract a later planned change
  depends on
- **THEN** the later change fails or blocks on its own acceptance criteria with
  the carry-forward item named, and the archived change remains closed

### Requirement: The first review and every re-review establish the full accepted surface

The first review of a change SHALL be full-scope: it SHALL read the change's
accepted success parameters, proposal, design, tasks, and spec deltas, the
actual diff, and the evidence packet, and it SHALL enumerate every blocking
finding it can establish from that scope, not only findings visible in the
stated diff.

Every re-review SHALL also read the whole accepted surface and the prior
findings. It SHALL NOT review only the last correction. It SHALL report each
prior finding as closed, still open, or superseded.

A reviewer SHALL NOT introduce a current-slice blocking finding in a later round
when the same evidence was available in round 1 and the diff has not changed in
a way that exposes it. Such a finding SHALL be recorded as a reviewer process
defect and SHALL NOT by itself extend the corrective-round budget. New evidence,
a changed diff, or a prior finding that was not actually closed MAY justify a
later current-slice finding.

Severity SHALL remain stable for the same defect class across rounds. A class may
change severity only with an explicit adjudication note that names the new
evidence.

#### Scenario: The first review establishes the whole accepted surface
- **WHEN** a change's accepted scope includes a scope/registry case, an error
  envelope, a cache invariant, and a page
- **THEN** the first review reports findings for all four areas it can establish,
  not only for the latest diff hunk

#### Scenario: Serial discovery is a process defect
- **WHEN** round 3 reports a current-slice defect whose evidence was present and
  unchanged in round 1
- **THEN** the orchestrator records a reviewer process defect and does not grant
  an additional corrective round solely for that finding

#### Scenario: New evidence permits a later finding
- **WHEN** a correction changes the diff and newly exposes a current-slice
  failure
- **THEN** the reviewer may report it as `introduced_by_diff` in the next round

#### Scenario: A re-review reads the whole artifact
- **WHEN** a second review follows a narrow correction
- **THEN** it reads the accepted scope and the full diff, not only the correction,
  and reports each prior finding's status

#### Scenario: Severity is stable across rounds
- **WHEN** the same defect class is blocking in round 1 and advisory in round 2
- **THEN** the reviewer records an explicit adjudication note naming the reason
  for the change

### Requirement: Reviewers judge the slice's accepted success parameters

The reviewer SHALL judge only the change's accepted success parameters and the
coverage matrix derived from them. It SHALL NOT expand the review into the next
slice's concerns, hardening the plan did not accept, or a preferred future
design.

A real behavior outside the accepted success parameters SHALL be recorded as
`adjacent_scope` and proposed as a new slice; it SHALL NOT block the active
change. A real defect that the change's accepted scope requires SHALL block.

An evidence-format preference — log shape, transcript style, preferred artifact
type, evidence field ordering, or a preferred command spelling — SHALL NOT be a
blocking finding when the behavior is otherwise proved by a test, command, or
observed reproduction. A missing preferred artifact SHALL NOT block when an
equivalent proof is named. The reviewer SHALL record the substitution as a note,
not a finding.

A false claim SHALL remain blocking, and a defect in accepted behavior SHALL
remain blocking. This requirement lowers ceremony, not truthfulness.

#### Scenario: The next slice's concern is adjacent
- **WHEN** a review of the current slice finds that the next planned page will
  need a different cache key
- **THEN** the reviewer records it as `adjacent_scope` and does not block the
  current slice

#### Scenario: An evidence-format preference does not block
- **WHEN** the writer retained a command transcript instead of the reviewer's
  preferred log format and the behavior is proved
- **THEN** the reviewer records a note and does not return a blocking finding

#### Scenario: An equivalent proof is enough
- **WHEN** a preferred artifact is missing but an equivalent test, command, or
  reproduction is named with its result
- **THEN** the reviewer accepts the equivalent proof and does not block on the
  missing preferred artifact

#### Scenario: A false claim still blocks
- **WHEN** the evidence contradicts a claim in the change's report
- **THEN** the reviewer returns a blocking finding even if the evidence format is
  acceptable

#### Scenario: Unaccepted hardening is a follow-up
- **WHEN** a `poc` change could be hardened beyond its accepted smoke path
- **THEN** the reviewer records the hardening as a follow-up, not a blocking
  finding

### Requirement: Coverage separates current-slice failures from adjacent scope

The reviewer SHALL keep one `coverage[]` entry per supplied matrix case. Each
entry SHALL include:

- `scope` — `current-slice` or `adjacent`;
- `result` — `covered`, `not-applicable`, or `not-covered`;
- `evidence` — the test, command, or observed reproduction and its result.

A `not-covered` current-slice case that concerns accepted behavior or a required
invariant SHALL be a blocking finding. A `not-covered` adjacent case SHALL be
recorded and SHALL NOT block the active change. A next-phase compatibility case
SHALL be `adjacent` unless the change's accepted success parameters require the
next slice's behavior now.

A coverage entry that omits `scope` SHALL be incomplete. A `not-covered` case
SHALL NOT be promoted to `covered` because a later slice will cover it.

#### Scenario: A current-slice gap blocks
- **WHEN** a required current-slice permission-denied case has no test or
  reproduction
- **THEN** the reviewer returns a blocking finding with the case id, expected
  behavior, and observed behavior

#### Scenario: An adjacent gap does not block
- **WHEN** a next-phase compatibility case is `not-covered` and the current
  change's accepted parameters do not require it
- **THEN** the reviewer records it as `adjacent` and does not block the current
  slice

#### Scenario: Next-phase compatibility is adjacent by default
- **WHEN** the matrix includes a file the next change will modify
- **THEN** the coverage entry is `adjacent` unless the current change's accepted
  success parameters require that file's behavior now

#### Scenario: Coverage is machine-readable and scoped
- **WHEN** the reviewer returns its verdict
- **THEN** every `coverage[]` entry carries `case`, `scope`, `result`, and
  `evidence`

#### Scenario: A future slice is not current proof
- **WHEN** a current-slice case is `not-covered` because the next change will
  exercise it
- **THEN** the reviewer does not mark it `covered` and records the current-slice
  gap
