## 1. Fix leaderboard model attribution

- [ ] 1.1 In `_build_leaderboard` (`lib/metrics/aggregator.py:900-930`),
  collect all distinct implementer models per change (not just the last)
- [ ] 1.2 For a change that used multiple implementer models, produce one
  leaderboard entry per unique combination or a composite label showing the
  model progression
- [ ] 1.3 Add a test in `tests/lib/metrics/` that feeds telemetry records
  with two distinct implementer models and asserts the leaderboard preserves
  both (or produces the expected composite entry)
- [ ] 1.4 Run `python3 -m unittest discover -t . -s tests` and confirm no
  regressions
