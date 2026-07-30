## ADDED Requirements

### Requirement: Leaderboard preserves all implementer models used by a change

The model leaderboard in `opsx-plan report` SHALL attribute each distinct
implementer-model combination used by a change, not only the last one.

When a change's implementer model changes across rounds (e.g. due to
escalation), the leaderboard SHALL reflect that the change used multiple
models. The exact representation (multiple entries, composite label, or
footnoted primary model) is a design decision for the implementer, but the
requirement is that no model used by the change is silently discarded.

#### Scenario: single-model change
- GIVEN a change that used "deepseek-v4-basic" in all its implement rounds
- WHEN the leaderboard is built
- THEN it SHALL produce one entry with implementer model "deepseek-v4-basic"

#### Scenario: escalated change with two models
- GIVEN a change that used "deepseek-v4-basic" in rounds 1-2 and
  "deepseek-v4-ultra" in rounds 3-4
- WHEN the leaderboard is built
- THEN both models SHALL be reflected in the output (either as separate
  entries or as a composite label that preserves both identifiers)
