"""Metrics aggregation package.

Provides typed dataclasses and a deterministic aggregator that reads
plan-scoped telemetry JSONL files and plan state to compute efficiency KPIs
for plan runs, individual changes, stage aggregates, and model-combination
leaderboards.
"""

from lib.metrics.aggregator import (
    SUPERVISOR_FAMILY_ROLES,
    AggregationError,
    AggregationResult,
    ChangeMetrics,
    CoreMetrics,
    ModelLeaderboardEntry,
    PlanMetrics,
    RoleMetrics,
    StageAggregates,
    aggregate,
    collect_core_metrics,
    filter_leaderboard_records,
    is_supervisor_family_role,
)

__all__ = [
    "SUPERVISOR_FAMILY_ROLES",
    "AggregationError",
    "AggregationResult",
    "ChangeMetrics",
    "CoreMetrics",
    "ModelLeaderboardEntry",
    "PlanMetrics",
    "RoleMetrics",
    "StageAggregates",
    "aggregate",
    "collect_core_metrics",
    "filter_leaderboard_records",
    "is_supervisor_family_role",
]
