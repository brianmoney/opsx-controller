## MODIFIED Requirements

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
