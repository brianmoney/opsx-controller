## ADDED Requirements

### Requirement: Reviews persist their findings per round in controller state

The controller SHALL persist every finding returned by a review in that
change's per-round history, retaining at minimum each finding's severity,
normalized locus set, and statement.

Persistence SHALL be additive: the existing `finding_counts`, `last_review`,
and `latest_fix_prompt` fields SHALL retain their current meaning and shape.

Per-round findings SHALL survive for the life of the change record so that
recurrence can be computed from controller state alone, without reading stage
logs.

#### Scenario: Findings from every round remain inspectable

- **WHEN** a change has completed four review rounds
- **THEN** its state record contains the findings reported in each of those
  four rounds, not only the most recent

#### Scenario: Existing state fields are unchanged

- **WHEN** a review result is applied
- **THEN** `finding_counts`, `last_review.fix_prompt`, and
  `latest_fix_prompt` hold the same values they held before findings were
  persisted

### Requirement: The orchestrator derives finding identity by normalizing loci

The orchestrator SHALL compute finding identity itself and SHALL NOT accept an
identity assigned by the review worker.

Each locus entry SHALL be normalized before comparison by trimming surrounding
whitespace, backticks, and trailing punctuation, converting path separators to
POSIX form, and resolving the path portion against the repository's tracked
files when the entry is a unique path suffix of exactly one tracked file. A
locus entry SHALL be compared as a `<normalized-path>` or
`<normalized-path>:<symbol>` pair, with the symbol portion compared exactly.

A locus entry that resolves ambiguously, or that matches no tracked file, SHALL
be retained in its trimmed form rather than discarded, and SHALL still
participate in comparison.

Two findings SHALL be considered the same *finding identity* when their
normalized locus sets are equal.

#### Scenario: Varying path depth resolves to one identity

- **WHEN** one round reports the locus `result_contract.py` and a later round
  reports `agents/executors/result_contract.py`, and exactly one tracked file
  matches that suffix
- **THEN** both normalize to the same tracked path and are treated as the same
  locus

#### Scenario: Reviewer-supplied identifiers are ignored

- **WHEN** a review returns findings carrying their own id or slug fields
- **THEN** the orchestrator computes identity from the normalized locus sets
  and the supplied identifiers do not affect recurrence accounting

#### Scenario: Unresolvable locus still participates

- **WHEN** a finding cites a path that matches no tracked file
- **THEN** that locus is retained in normalized form and can still recur
  across rounds

### Requirement: Recurrence is counted per locus across distinct rounds

The orchestrator SHALL count, for each normalized locus, the number of
distinct rounds in which a *blocking* finding cited it.

A finding SHALL be blocking when its severity is one that gates the review
verdict under the active configuration: `critical` SHALL always be blocking;
`warning` SHALL be blocking unless warnings are configured to be skipped; and
`note` SHALL be blocking unless notes are configured to be skipped.

Counting SHALL be cumulative across the change's rounds and SHALL NOT require
the rounds to be consecutive.

Multiple findings citing the same locus within a single round SHALL increment
that locus by one, not by the number of findings.

#### Scenario: Non-consecutive recurrence accumulates

- **WHEN** a blocking finding cites the same locus in rounds 4, 5, 7, and 8
- **THEN** that locus has a recurrence count of four

#### Scenario: Non-blocking severities do not accumulate

- **WHEN** notes are configured to be skipped and a note-severity finding
  cites a locus in three rounds
- **THEN** that locus has a recurrence count of zero

#### Scenario: Repeated citation within one round counts once

- **WHEN** two blocking findings in the same round cite the same locus
- **THEN** that locus recurrence count increases by one for that round

### Requirement: A plan key sets the finding-recurrence ceiling

The plan `[plan]` table SHALL accept an integer key `finding_recurrence_limit`
naming the number of distinct rounds a single locus may be cited by a blocking
finding before the change is halted.

The key SHALL default to `0`, and `0` SHALL mean recurrence halting is
disabled.

The plan loader SHALL reject a negative value with a clear error rather than
coercing it.

The key SHALL be plan-level only; there SHALL NOT be a per-change override,
matching `max_rounds`, `no_progress_limit`, and
`escalate_after_review_fails`.

The synthesized single-change configuration used by the manifest-free runner
SHALL set the key to `0`.

#### Scenario: Key absent leaves recurrence halting disabled

- **WHEN** a plan manifest omits `finding_recurrence_limit`
- **THEN** the loaded configuration carries the value `0` and no change in
  that run is halted for recurrence

#### Scenario: Negative ceiling is rejected

- **WHEN** a plan manifest sets `finding_recurrence_limit` to a negative
  integer
- **THEN** plan loading fails with an error naming the key and no run starts

#### Scenario: Ceiling survives derived-manifest round trip

- **WHEN** a configuration carrying a non-zero `finding_recurrence_limit` is
  serialized to a derived manifest and reloaded for round-trip verification
- **THEN** the reloaded configuration carries the same value and the
  round-trip comparison reports no divergence

### Requirement: Reaching the recurrence ceiling halts the change for triage

The orchestrator SHALL halt a change, rather than dispatch another implement
round, when `finding_recurrence_limit` is greater than zero and any locus
reaches that recurrence count. It SHALL mark the change failed and SHALL record
a distinct result value identifying recurrence as the cause.

The recorded reason SHALL name the offending locus and the rounds in which it
was cited, so an operator can judge a false positive without reading stage
logs.

The recurrence result SHALL be treated as non-retryable, consistent with the
existing no-progress ceiling.

Recurrence halting SHALL be evaluated only after a failing review verdict, and
SHALL NOT prevent a passing review from advancing the change to archive.

#### Scenario: Ceiling reached stops further rounds

- **WHEN** `finding_recurrence_limit` is `3`, a review fails, and a locus
  reaches its third citing round
- **THEN** the change is marked failed with a recurrence-specific result and
  no further implement dispatch occurs for it

#### Scenario: Halt reason identifies the stall

- **WHEN** a change is halted for recurrence
- **THEN** the recorded reason names the locus and the rounds in which it was
  cited by a blocking finding

#### Scenario: Passing review is unaffected by recurrence history

- **WHEN** a locus has been cited in as many rounds as the ceiling but the
  current review verdict passes the gate
- **THEN** the change advances to archive and is not halted

#### Scenario: Recurrence result is not retried

- **WHEN** a change has been halted for recurrence and the run is resumed
- **THEN** that change is not automatically returned to the implement phase

### Requirement: Review dispatch carries the prior round's finding loci

The controller SHALL supply the normalized loci reported by the previous
round's blocking findings as an input field to the review worker, for any
review dispatch after the first round of a change.

The field SHALL be present and explicitly empty for a change's first review
round.

This input SHALL be advisory context for consistent naming only; the
orchestrator SHALL continue to compute recurrence from its own normalization
regardless of how the reviewer responds to it.

#### Scenario: Second review round receives prior loci

- **WHEN** a change's round-1 review fails with blocking findings citing two
  loci
- **THEN** the round-2 review dispatch input carries both normalized loci

#### Scenario: First review round receives an empty field

- **WHEN** a change is dispatched for its first review
- **THEN** the prior-loci input field is present and explicitly empty

### Requirement: Reviews without structured findings degrade safely

The orchestrator SHALL apply the review verdict and finding counts exactly as
it does today when a review result omits the structured findings array. It
SHALL record that the round contributed no recurrence evidence, and SHALL NOT
fail the change on account of the omission.

A change whose reviews never return structured findings SHALL never be halted
for recurrence, regardless of the configured ceiling.

#### Scenario: Legacy reviewer output still drives the loop

- **WHEN** a review returns a verdict and finding counts but no findings array
- **THEN** the change advances or retries exactly as it would have before
  recurrence detection existed

#### Scenario: Missing findings cannot trigger a halt

- **WHEN** `finding_recurrence_limit` is `3` and no review for a change has
  returned structured findings
- **THEN** that change is never halted for recurrence
