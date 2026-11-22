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
SHALL include every blocking finding and, for each, its severity, relevant file
or symbol, observed behavior, and required behavior. `CORRECTIVE GUIDANCE`
SHALL prescribe the implementation approach or invariants necessary to correct
the findings. `VERIFY` SHALL name the focused regressions and validation
commands required to demonstrate the correction.

The reviewer SHALL return an empty `fix_prompt` only for a zero-finding passing
verdict.

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

A review worker SHALL additionally return a machine-readable `findings` array
carrying the same findings the prose handoff describes. Each entry SHALL
provide its `severity`, a `locus` array, and a `statement` describing the
observed and required behavior.

Every `locus` entry SHALL be a repository-root-relative path, optionally
suffixed with `:<symbol>` naming the function, class, or constant the finding
concerns. A locus entry SHALL NOT be an absolute path, a bare symbol without
its file, or a path relative to a subdirectory of the repository.

The `findings` array SHALL be present and empty for a passing verdict.

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
  findings in its `FINDINGS` section
- **THEN** its `findings` array carries two corresponding entries, each with a
  severity, at least one locus, and a statement

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
