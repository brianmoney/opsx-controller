## Why

When a plan escalates the implementer model mid-run (e.g. rounds 1-2 use
`deepseek-v4-basic` and round 3+ uses `deepseek-v4-ultra`), the leaderboard
construction in `lib/metrics/aggregator.py:900-930` iterates all telemetry
records for the change and overwrites `impl_model` with each subsequent
record. The **last** implement record wins, so an escalated run appears to
have used only the escalation model — the base model is lost.

This misattributes mixed-model runs: an operator comparing two plans cannot
see that a plan succeeded because escalation promoted to a stronger model.

## What

- Track all distinct implementer models used by a change rather than only
  the last one.
- For changes that span multiple implementer models, produce one leaderboard
  entry per unique model combination (or a composite label).
- Ensure the change-level summary correctly reports the run's model history
  without overwriting earlier stages.

## Impact

- `lib/metrics/aggregator.py` — triple-grouping loop and entry construction.
- Tests in `tests/lib/metrics/` for mixed-model leaderboard entries.
