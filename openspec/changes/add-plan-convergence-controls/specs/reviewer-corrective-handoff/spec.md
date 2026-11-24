## MODIFIED Requirements

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

## ADDED Requirements

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
