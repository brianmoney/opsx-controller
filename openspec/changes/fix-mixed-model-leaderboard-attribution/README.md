# fix-mixed-model-leaderboard-attribution

The leaderboard construction in lib/metrics/aggregator.py overwrites per-change implementer models with the last record, losing attribution when escalation changes the model mid-run.
