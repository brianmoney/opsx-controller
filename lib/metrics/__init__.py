"""Metrics aggregation package.

Provides typed dataclasses and a deterministic aggregator that reads
plan-scoped telemetry JSONL files and plan state to compute efficiency KPIs
for plan runs, individual changes, stage aggregates, and model-combination
leaderboards.
"""

from lib.metrics.aggregator import (
    AggregationError,
    AggregationResult,
    ChangeMetrics,
    ModelLeaderboardEntry,
    PlanMetrics,
    StageAggregates,
    aggregate,
)

__all__ = [
    "AggregationError",
    "AggregationResult",
    "ChangeMetrics",
    "ModelLeaderboardEntry",
    "PlanMetrics",
    "StageAggregates",
    "aggregate",
]
