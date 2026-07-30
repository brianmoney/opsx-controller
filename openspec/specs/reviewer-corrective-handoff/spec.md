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

#### Scenario: Review failure includes actionable findings
- **WHEN** a reviewer finds a missing regression and incorrect terminal-state
  handling for change `preserve-handoff-reports`
- **THEN** its failed review handoff identifies that change, describes both
  findings with their severity and relevant code locations, directs the
  correction, and names the required regression and validation command

#### Scenario: Clean review has no corrective handoff
- **WHEN** a reviewer returns `verdict=pass` with all finding counts zero
- **THEN** it returns an empty `fix_prompt`

### Requirement: Retry implementers execute the reviewer corrective handoff
An implementer receiving a non-empty `LATEST_FIX_PROMPT` SHALL treat every
finding, corrective guideline, and verification requirement in that handoff as
the highest-priority scope for the retry round.

If current change artifacts or live repository evidence make a requested
correction contradictory or unsafe, the implementer SHALL return its existing
blocked result instead of inventing an alternative requirement.

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
