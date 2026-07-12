"""Metrics aggregation for plan-run telemetry and state.

Deterministic, read-only: reads ``.opsx-plan/telemetry/<plan_name>.jsonl``
and ``.opsx-plan/<plan_name>.state.json``, returns typed aggregation results.
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------


@dataclass
class PlanMetrics:
    """Plan-level aggregation metrics."""

    plan_name: str = ""
    run_id: str = ""
    total_changes: int = 0
    completed_changes: int = 0
    failed_changes: int = 0
    blocked_changes: int = 0
    incomplete_changes: int = 0
    completion_rate: Optional[float] = None
    success_rate: Optional[float] = None
    total_duration_ms: Optional[int] = None
    total_tokens: Optional[int] = None
    total_estimated_cost: Optional[float] = None
    estimated_cost_changes: int = 0
    unresolved_cost_changes: int = 0
    unknown_cost_changes: int = 0


@dataclass
class ChangeMetrics:
    """Per-change aggregation metrics."""

    change_id: str
    status: str = "incomplete"  # completed, failed, blocked, incomplete
    total_rounds: int = 0
    duration_ms: Optional[int] = None
    tokens: Optional[int] = None
    estimated_cost: Optional[float] = None
    cost_status: str = "unavailable"  # estimated, partial, unresolved, unavailable
    first_pass_review: Optional[bool] = None
    review_failures: int = 0
    no_progress: bool = False
    max_rounds_exceeded: bool = False
    archive_failed: bool = False
    fast_check_failed: bool = False


@dataclass
class StageAggregates:
    """Stage-level aggregation statistics."""

    average_rounds: Optional[float] = None
    median_rounds: Optional[float] = None
    average_duration_implement: Optional[float] = None
    average_duration_review: Optional[float] = None
    average_duration_archive: Optional[float] = None
    review_failure_rate: Optional[float] = None
    average_tokens_per_change: Optional[float] = None
    average_cost_per_change: Optional[float] = None


@dataclass
class ModelLeaderboardEntry:
    """A single model-combination leaderboard entry."""

    implementer_model: Optional[str] = None
    reviewer_model: Optional[str] = None
    archiver_model: Optional[str] = None
    change_count: int = 0
    success_rate: Optional[float] = None
    first_pass_rate: Optional[float] = None
    average_rounds: Optional[float] = None
    average_duration_ms: Optional[float] = None
    average_tokens: Optional[float] = None
    average_cost: Optional[float] = None


@dataclass
class AggregationResult:
    """Top-level aggregation result."""

    plan_metrics: PlanMetrics = field(default_factory=PlanMetrics)
    change_metrics: list[ChangeMetrics] = field(default_factory=list)
    stage_aggregates: StageAggregates = field(default_factory=StageAggregates)
    model_leaderboard: list[ModelLeaderboardEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class AggregationError(Exception):
    """Raised when aggregation cannot proceed (e.g., no repo root)."""

    pass


# ---------------------------------------------------------------------------
# Telemetry reading
# ---------------------------------------------------------------------------


def _read_telemetry(
    repo: Path, plan_name: str
) -> tuple[list[dict], list[str]]:
    """Read and parse a telemetry JSONL file.

    Returns ``(records, warnings)``.  Warnings are emitted for missing
    files, empty files, and unparseable lines.
    """
    telemetry_path = repo / ".opsx-plan" / "telemetry" / f"{plan_name}.jsonl"
    warnings: list[str] = []

    if not telemetry_path.is_file():
        warnings.append(f"telemetry file not found: {telemetry_path}")
        return [], warnings

    records: list[dict] = []
    for lineno, line in enumerate(
        telemetry_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            warnings.append(
                f"telemetry line {lineno}: invalid JSON, skipping"
            )
            continue
        if not isinstance(obj, dict):
            warnings.append(
                f"telemetry line {lineno}: not a JSON object, skipping"
            )
            continue
        records.append(obj)

    if not records:
        warnings.append("telemetry file contains zero records")

    return records, warnings


def _group_by_run_id(records: list[dict]) -> dict[str, list[dict]]:
    """Group telemetry records by ``run_id``.

    Records without a ``run_id`` are grouped under the empty string.
    """
    groups: dict[str, list[dict]] = {}
    for r in records:
        rid = r.get("run_id", "")
        groups.setdefault(rid, []).append(r)
    return groups


def _select_run(
    records: list[dict], run_id: Optional[str] = None
) -> tuple[list[dict], str, list[str]]:
    """Select records for a specific run.

    When *run_id* is ``None``, defaults to the run with the latest
    ``started_at``.  Returns ``(selected_records, selected_run_id,
    warnings)``.
    """
    groups = _group_by_run_id(records)

    if not groups:
        return [], "", ["no run groups found in telemetry"]

    if run_id is not None:
        if run_id in groups:
            return groups[run_id], run_id, []
        else:
            return [], "", [f"run_id {run_id!r} not found in telemetry"]

    # Default to latest run by started_at
    latest_rid = ""
    latest_ts = ""
    for rid, recs in groups.items():
        for r in recs:
            started = r.get("started_at", "")
            if started > latest_ts:
                latest_ts = started
                latest_rid = rid

    if latest_rid:
        return groups[latest_rid], latest_rid, []

    # If no timestamps, use first group
    first = next(iter(groups.keys()))
    return groups[first], first, []


def _model_key(record: dict) -> Optional[str]:
    """Return ``"provider:model_id"`` for a record, or ``None`` if unknown."""
    model = record.get("model", {})
    provider = (model.get("provider") or "").strip()
    model_id = (model.get("model_id") or "").strip()
    if not provider or not model_id:
        return None
    return f"{provider}:{model_id}"


# ---------------------------------------------------------------------------
# State reading
# ---------------------------------------------------------------------------


def _read_state(
    repo: Path, plan_name: str
) -> tuple[Optional[dict], list[str]]:
    """Read the plan state file.

    Returns ``(state_dict, warnings)``.  *state_dict* is ``None`` when
    the file is missing.
    """
    state_path = repo / ".opsx-plan" / f"{plan_name}.state.json"
    warnings: list[str] = []

    if not state_path.is_file():
        warnings.append(f"plan state file not found: {state_path}")
        return None, warnings

    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        warnings.append(f"failed to read state file: {exc}")
        return None, warnings

    if not isinstance(state, dict):
        warnings.append("state file is not a JSON object")
        return None, warnings

    return state, warnings


def _get_change_ids_from_state(state: dict) -> list[str]:
    """Extract change ids from plan state."""
    changes = state.get("changes", {})
    if isinstance(changes, dict):
        return sorted(changes.keys())
    return []


def _get_change_ids_from_telemetry(records: list[dict]) -> list[str]:
    """Extract unique change ids from telemetry records."""
    ids: set[str] = set()
    for r in records:
        cid = r.get("change_id", "")
        if cid:
            ids.add(cid)
    return sorted(ids)


# ---------------------------------------------------------------------------
# Plan-level aggregation
# ---------------------------------------------------------------------------


def _is_completed_stage(record: dict) -> bool:
    """Return True if a telemetry record represents a completed stage."""
    status = record.get("status", "")
    if status == "completed":
        return True
    result = record.get("result", {})
    if isinstance(result, dict) and result.get("stage_status") in (
        "completed",
        "passed",
        "archived",
    ):
        return True
    return False


def _plan_aggregation(
    state: Optional[dict],
    records: list[dict],
    plan_name: str,
    run_id: str,
    warnings: list[str],
    change_metrics_list: list[ChangeMetrics],
) -> tuple[PlanMetrics, list[str]]:
    """Compute plan-level metrics from state and telemetry.

    Change-status counts (completed, failed, blocked, incomplete) are
    derived from *change_metrics_list* so they follow the same
    state+telemetry logic used for per-change status.
    """
    # Derive change-status counts from change-level metrics
    total_changes = len(change_metrics_list)
    completed_changes = sum(
        1 for c in change_metrics_list if c.status == "completed"
    )
    failed_changes = sum(
        1 for c in change_metrics_list if c.status == "failed"
    )
    blocked_changes = sum(
        1 for c in change_metrics_list if c.status == "blocked"
    )
    incomplete_changes = sum(
        1 for c in change_metrics_list if c.status == "incomplete"
    )

    # Data-quality warnings: orphan telemetry changes
    state_change_ids = (
        _get_change_ids_from_state(state) if state else []
    )
    telemetry_change_ids = _get_change_ids_from_telemetry(records)
    state_changes = state.get("changes", {}) if state else {}
    telemetry_change_set = set(telemetry_change_ids)

    for cid in telemetry_change_ids:
        if cid not in state_change_ids:
            warnings.append(
                f"change {cid!r} found in telemetry but not in plan state"
            )

    # Warn for changes in state with no telemetry records
    for cid in state_change_ids:
        if cid not in telemetry_change_set:
            warnings.append(
                f"change {cid!r} in plan state has no telemetry records"
            )

    # Completion and success rates
    completion_rate: Optional[float] = None
    if total_changes > 0:
        completion_rate = completed_changes / total_changes

    success_denom = completed_changes + failed_changes
    success_rate: Optional[float] = None
    if success_denom > 0:
        success_rate = completed_changes / success_denom

    # Summations from completed stage records
    total_duration_ms: Optional[int] = None
    total_tokens: Optional[int] = None
    total_estimated_cost: Optional[float] = None
    estimated_cost_changes: set[str] = set()
    unresolved_cost_changes: set[str] = set()
    unknown_cost_changes: set[str] = set()
    any_duration = False
    any_tokens = False
    any_cost = False
    seen_schema_versions: set[int] = set()
    unresolved_cost_count = 0

    for r in records:
        cid = r.get("change_id", "")

        # Schema version tracking
        sv = r.get("schema_version")
        if isinstance(sv, int) and sv not in seen_schema_versions:
            seen_schema_versions.add(sv)

        # Duration — only completed stage records
        if _is_completed_stage(r):
            dur = r.get("duration_ms")
            if isinstance(dur, (int, float)) and dur >= 0:
                if total_duration_ms is None:
                    total_duration_ms = 0
                total_duration_ms += int(dur)
                any_duration = True

        # Tokens
        usage = r.get("usage", {})
        tok = usage.get("total_tokens")
        if isinstance(tok, (int, float)) and tok >= 0:
            if total_tokens is None:
                total_tokens = 0
            total_tokens += int(tok)
            any_tokens = True

        # Cost
        cost = r.get("cost", {})
        cost_status = cost.get("status", "unavailable")
        est_cost = cost.get("estimated_cost")

        if cost_status == "estimated":
            if isinstance(est_cost, (int, float)):
                if total_estimated_cost is None:
                    total_estimated_cost = 0.0
                total_estimated_cost += float(est_cost)
                any_cost = True
            if cid:
                estimated_cost_changes.add(cid)
        elif cost_status == "unresolved":
            if cid:
                unresolved_cost_changes.add(cid)
            unresolved_cost_count += 1

        # Model identity
        if _model_key(r) is None:
            if cid:
                unknown_cost_changes.add(cid)

    if not any_duration:
        total_duration_ms = None
    if not any_tokens:
        total_tokens = None
    if not any_cost:
        total_estimated_cost = None

    # Warn about unknown schema versions
    if seen_schema_versions:
        for ver in sorted(seen_schema_versions):
            if ver != 1:
                warnings.append(
                    f"telemetry contains records with unknown schema_version {ver}"
                )

    # Warn about unresolved cost records
    if unresolved_cost_count > 0:
        warnings.append(
            f"{unresolved_cost_count} record(s) with cost.status='unresolved'"
        )

    # Partial-cost changes (both estimated and unresolved records) are
    # counted in both estimated_cost_changes and unresolved_cost_changes.
    unresolved_only = unresolved_cost_changes
    unknown_only = unknown_cost_changes - estimated_cost_changes - unresolved_cost_changes

    metrics = PlanMetrics(
        plan_name=plan_name,
        run_id=run_id,
        total_changes=total_changes,
        completed_changes=completed_changes,
        failed_changes=failed_changes,
        blocked_changes=blocked_changes,
        incomplete_changes=incomplete_changes,
        completion_rate=completion_rate,
        success_rate=success_rate,
        total_duration_ms=total_duration_ms,
        total_tokens=total_tokens,
        total_estimated_cost=total_estimated_cost,
        estimated_cost_changes=len(estimated_cost_changes),
        unresolved_cost_changes=len(unresolved_only),
        unknown_cost_changes=len(unknown_only),
    )

    return metrics, warnings


# ---------------------------------------------------------------------------
# Change-level aggregation
# ---------------------------------------------------------------------------


def _change_aggregation(
    state: Optional[dict],
    records: list[dict],
    plan_name: str,
    warnings: list[str],
) -> tuple[list[ChangeMetrics], list[str]]:
    """Compute per-change metrics."""
    # Group records by change_id
    by_change: dict[str, list[dict]] = {}
    for r in records:
        cid = r.get("change_id", "")
        if cid:
            by_change.setdefault(cid, []).append(r)

    # Collect all change ids
    state_ids = _get_change_ids_from_state(state) if state else []
    telemetry_ids = sorted(by_change.keys())
    all_ids = sorted(set(state_ids + telemetry_ids))

    state_changes = state.get("changes", {}) if state else {}

    results: list[ChangeMetrics] = []

    for cid in all_ids:
        ch_state = state_changes.get(cid, {})
        ch_records = by_change.get(cid, [])

        # Determine raw state status
        state_status = ch_state.get("status", "")

        # Total rounds
        max_round = 0
        for r in ch_records:
            rnd = r.get("round", 0)
            if isinstance(rnd, (int, float)) and rnd > max_round:
                max_round = int(rnd)
        state_round = ch_state.get("round", 0)
        if isinstance(state_round, (int, float)) and state_round > max_round:
            max_round = int(state_round)
        total_rounds = max_round

        # Duration, tokens, cost
        duration_ms: Optional[int] = None
        tokens: Optional[int] = None
        estimated_cost: Optional[float] = None
        any_duration = False
        any_tokens = False
        any_cost = False
        has_estimated = False
        has_unresolved = False

        review_failures = 0
        review_stage_count = 0
        first_pass_possible = True
        has_explicit_pass_verdict = False
        no_progress = False
        max_rounds_exceeded = False
        archive_failed = False
        fast_check_failed = False
        implement_round1_success = False
        has_round1_implement = False
        has_archive = False
        archive_completed = False
        last_review_round = 0
        last_review_verdict = ""

        for r in ch_records:
            dur = r.get("duration_ms")
            if isinstance(dur, (int, float)) and dur >= 0:
                if duration_ms is None:
                    duration_ms = 0
                duration_ms += int(dur)
                any_duration = True

            usage = r.get("usage", {})
            tok = usage.get("total_tokens")
            if isinstance(tok, (int, float)) and tok >= 0:
                if tokens is None:
                    tokens = 0
                tokens += int(tok)
                any_tokens = True

            cost = r.get("cost", {})
            cs = cost.get("status", "unavailable")
            ec = cost.get("estimated_cost")
            if cs == "estimated":
                has_estimated = True
                if isinstance(ec, (int, float)):
                    if estimated_cost is None:
                        estimated_cost = 0.0
                    estimated_cost += float(ec)
                    any_cost = True
            elif cs == "unresolved":
                has_unresolved = True

            stage = r.get("stage", "")

            # Review stages
            if stage == "review":
                review_stage_count += 1
                rnd = r.get("round", 0)
                result_data = r.get("result", {})
                verdict = result_data.get("verdict", "")
                if verdict == "fail":
                    review_failures += 1
                if verdict == "pass":
                    has_explicit_pass_verdict = True
                # Track last (highest-round) review verdict
                if isinstance(rnd, (int, float)) and rnd >= last_review_round:
                    last_review_round = int(rnd)
                    last_review_verdict = verdict
                # First-pass: round 1 review verdict "pass", only one review stage
                if rnd != 1:
                    first_pass_possible = False

            # Implement stages
            if stage == "implement":
                rnd = r.get("round", 0)
                if rnd == 1:
                    has_round1_implement = True
                    r_status = r.get("status", "")
                    result_data = r.get("result", {})
                    if r_status == "completed" or result_data.get("stage_status") == "completed":
                        implement_round1_success = True

            # Archive stages
            if stage == "archive":
                has_archive = True
                if _is_completed_stage(r):
                    archive_completed = True

        if not any_duration:
            duration_ms = None
        if not any_tokens:
            tokens = None
        if not any_cost:
            estimated_cost = None

        # Cost status
        if has_estimated and has_unresolved:
            cost_status = "partial"
        elif has_estimated:
            cost_status = "estimated"
        elif has_unresolved:
            cost_status = "unresolved"
        else:
            cost_status = "unavailable"

        # Derive change status from state + telemetry
        # Terminal failure = last review failed OR archive exists and didn't complete
        terminal_failure = (
            (last_review_verdict == "fail")
            or (has_archive and not archive_completed)
        )

        if state_status in ("done", "completed"):
            if terminal_failure:
                status = "failed"
                warnings.append(
                    f"change {cid!r}: state marks done but telemetry shows "
                    "non-passing outcomes; marking as failed"
                )
            else:
                status = "completed"
        elif state_status in ("failed", "error"):
            status = "failed"
        elif state_status == "blocked":
            status = "blocked"
        else:
            status = "incomplete"

        # Warn when telemetry shows a completed archive but plan state
        # does not mark the change done.
        if (
            has_archive
            and archive_completed
            and state_status not in ("done", "completed")
        ):
            warnings.append(
                f"change {cid!r}: telemetry shows completed archive but "
                "plan state does not mark it done"
            )

        # First pass review: requires explicit "pass" verdict
        first_pass_review: Optional[bool] = None
        if (
            first_pass_possible
            and review_stage_count == 1
            and review_failures == 0
            and has_explicit_pass_verdict
        ):
            first_pass_review = True
        elif review_stage_count > 0:
            first_pass_review = False

        # Archive failed
        if has_archive and not archive_completed:
            archive_failed = True

        # Max rounds exceeded (don't set for archive failures at the round limit)
        state_max_rounds = ch_state.get("max_rounds", 5)
        if (
            total_rounds >= state_max_rounds
            and status == "failed"
            and not archive_failed
        ):
            max_rounds_exceeded = True

        # No progress
        no_progress_streak = ch_state.get("no_progress_streak", 0)
        if isinstance(no_progress_streak, (int, float)) and no_progress_streak >= 1:
            no_progress = True

        # Fast check failed
        if has_round1_implement and not implement_round1_success:
            fast_check_failed = True

        results.append(
            ChangeMetrics(
                change_id=cid,
                status=status,
                total_rounds=total_rounds,
                duration_ms=duration_ms,
                tokens=tokens,
                estimated_cost=estimated_cost,
                cost_status=cost_status,
                first_pass_review=first_pass_review,
                review_failures=review_failures,
                no_progress=no_progress,
                max_rounds_exceeded=max_rounds_exceeded,
                archive_failed=archive_failed,
                fast_check_failed=fast_check_failed,
            )
        )

    return results, warnings


# ---------------------------------------------------------------------------
# Stage-level aggregates
# ---------------------------------------------------------------------------


def _stage_aggregation(
    completed_changes: list[ChangeMetrics],
    records: list[dict],
) -> StageAggregates:
    """Compute stage-level aggregates from completed changes."""
    # Rounds
    rounds = [c.total_rounds for c in completed_changes if c.total_rounds > 0]
    average_rounds: Optional[float] = None
    median_rounds: Optional[float] = None
    if rounds:
        average_rounds = statistics.mean(rounds)
        median_rounds = statistics.median(rounds)

    # Stage durations — only completed stage records
    impl_durations: list[int] = []
    review_durations: list[int] = []
    archive_durations: list[int] = []

    for r in records:
        if not _is_completed_stage(r):
            continue
        stage = r.get("stage", "")
        dur = r.get("duration_ms")
        if not isinstance(dur, (int, float)) or dur < 0:
            continue
        if stage == "implement":
            impl_durations.append(int(dur))
        elif stage == "review":
            review_durations.append(int(dur))
        elif stage == "archive":
            archive_durations.append(int(dur))

    average_duration_implement: Optional[float] = None
    average_duration_review: Optional[float] = None
    average_duration_archive: Optional[float] = None

    if impl_durations:
        average_duration_implement = statistics.mean(impl_durations)
    if review_durations:
        average_duration_review = statistics.mean(review_durations)
    if archive_durations:
        average_duration_archive = statistics.mean(archive_durations)

    # Review failure rate — counted from all review stages (not just
    # completed changes), matching the denominator.
    total_review_failures = sum(
        1 for r in records
        if r.get("stage") == "review"
        and r.get("result", {}).get("verdict") == "fail"
    )
    total_review_stages = sum(
        1 for r in records if r.get("stage") == "review"
    )
    review_failure_rate: Optional[float] = None
    if total_review_stages > 0:
        review_failure_rate = total_review_failures / total_review_stages

    # Avg tokens from all completed changes with token data (decoupled
    # from cost — tokens are available even when cost is unresolved).
    all_with_tokens = [
        c for c in completed_changes if c.tokens is not None
    ]
    average_tokens_per_change: Optional[float] = None
    if all_with_tokens:
        average_tokens_per_change = statistics.mean(
            [c.tokens for c in all_with_tokens]
        )

    # Avg cost still requires changes with estimated (non-unresolved) cost.
    estimated_for_cost = [
        c
        for c in completed_changes
        if c.estimated_cost is not None
        and c.cost_status not in ("unresolved", "unavailable")
    ]
    average_cost_per_change: Optional[float] = None
    if estimated_for_cost:
        cost_vals = [c.estimated_cost for c in estimated_for_cost]
        if cost_vals:
            average_cost_per_change = statistics.mean(cost_vals)

    return StageAggregates(
        average_rounds=average_rounds,
        median_rounds=median_rounds,
        average_duration_implement=average_duration_implement,
        average_duration_review=average_duration_review,
        average_duration_archive=average_duration_archive,
        review_failure_rate=review_failure_rate,
        average_tokens_per_change=average_tokens_per_change,
        average_cost_per_change=average_cost_per_change,
    )


# ---------------------------------------------------------------------------
# Model combination leaderboard
# ---------------------------------------------------------------------------


def _build_leaderboard(
    all_changes: list[ChangeMetrics],
    records: list[dict],
) -> list[ModelLeaderboardEntry]:
    """Build model-combination leaderboard entries.

    The triple leaderboard includes all changes — completed, failed,
    blocked, and incomplete — so every model combination that touched a
    change produces a row.
    """
    # Group records by change_id
    by_change: dict[str, list[dict]] = {}
    for r in records:
        cid = r.get("change_id", "")
        if cid:
            by_change.setdefault(cid, []).append(r)

    entries: list[ModelLeaderboardEntry] = []

    # Helper: compute leaderboard metrics for a group of changes
    def _compute_entry(
        implementer_model: Optional[str],
        reviewer_model: Optional[str],
        archiver_model: Optional[str],
        changes_for_entry: list[str],
    ) -> ModelLeaderboardEntry:
        n = len(changes_for_entry)
        if n == 0:
            return ModelLeaderboardEntry(
                implementer_model=implementer_model,
                reviewer_model=reviewer_model,
                archiver_model=archiver_model,
            )

        entry_rounds: list[int] = []
        entry_durations: list[float] = []
        entry_tokens: list[float] = []
        entry_costs: list[float] = []
        first_pass_count = 0
        success_count = 0

        for c in all_changes:
            if c.change_id not in changes_for_entry:
                continue
            if c.total_rounds > 0:
                entry_rounds.append(c.total_rounds)
            if c.duration_ms is not None:
                entry_durations.append(float(c.duration_ms))
            if c.tokens is not None:
                entry_tokens.append(float(c.tokens))
            if c.estimated_cost is not None and c.cost_status not in (
                "unresolved",
                "unavailable",
            ):
                entry_costs.append(float(c.estimated_cost))
            if c.first_pass_review is True:
                first_pass_count += 1
            if c.status == "completed":
                success_count += 1

        success_rate: Optional[float] = None
        if n > 0:
            success_rate = success_count / n

        first_pass_rate: Optional[float] = None
        if n > 0:
            first_pass_rate = first_pass_count / n

        avg_rounds: Optional[float] = None
        if entry_rounds:
            avg_rounds = statistics.mean(entry_rounds)

        avg_duration: Optional[float] = None
        if entry_durations:
            avg_duration = statistics.mean(entry_durations)

        avg_tokens: Optional[float] = None
        if entry_tokens:
            avg_tokens = statistics.mean(entry_tokens)

        avg_cost: Optional[float] = None
        if entry_costs:
            avg_cost = statistics.mean(entry_costs)

        return ModelLeaderboardEntry(
            implementer_model=implementer_model,
            reviewer_model=reviewer_model,
            archiver_model=archiver_model,
            change_count=n,
            success_rate=success_rate,
            first_pass_rate=first_pass_rate,
            average_rounds=avg_rounds,
            average_duration_ms=avg_duration,
            average_tokens=avg_tokens,
            average_cost=avg_cost,
        )

    # Full triple leaderboard (model combination for all three roles)
    triple_groups: dict[tuple, list[str]] = {}
    for cid, recs in by_change.items():
        # Get latest implement, review, archive models
        impl_model: Optional[str] = None
        rev_model: Optional[str] = None
        arch_model: Optional[str] = None

        for r in recs:
            stage = r.get("stage", "")
            mk = _model_key(r)
            if mk is not None:
                if stage == "implement":
                    impl_model = mk
                elif stage == "review":
                    rev_model = mk
                elif stage == "archive":
                    arch_model = mk

        # Include all changes (not only completed) in the triple leaderboard
        # so model combinations for failed/incomplete changes also produce rows.
        c_metrics = next(
            (c for c in all_changes if c.change_id == cid), None
        )
        if c_metrics is None:
            continue

        triple = (impl_model or "unknown",
                  rev_model or "unknown",
                  arch_model or "unknown")
        triple_groups.setdefault(triple, []).append(cid)

    for (impl, rev, arch), changes in sorted(triple_groups.items()):
        entries.append(
            _compute_entry(
                implementer_model=impl,
                reviewer_model=rev,
                archiver_model=arch,
                changes_for_entry=changes,
            )
        )

    return entries


# ---------------------------------------------------------------------------
# Main aggregation entry point
# ---------------------------------------------------------------------------


def aggregate(
    repo_root: str | Path,
    plan_name: str,
    run_id: Optional[str] = None,
) -> AggregationResult:
    """Run the full aggregation pipeline.

    Args:
        repo_root: Path to the repository root.
        plan_name: Plan name used for telemetry and state file lookup.
        run_id: Optional specific run to aggregate. When ``None``, the
            latest run is selected automatically.

    Returns:
        An ``AggregationResult`` with plan metrics, per-change metrics,
        stage aggregates, model leaderboard entries, and warnings.

    Raises:
        AggregationError: When *repo_root* is not a valid directory.
    """
    repo = Path(repo_root).resolve()
    if not repo.is_dir():
        raise AggregationError(f"repo_root is not a directory: {repo}")

    all_warnings: list[str] = []

    # 1. Read telemetry
    records, telemetry_warnings = _read_telemetry(repo, plan_name)
    all_warnings.extend(telemetry_warnings)

    # 2. Select run
    selected_records, selected_run_id, run_warnings = _select_run(
        records, run_id
    )
    all_warnings.extend(run_warnings)

    # 3. Read state
    state, state_warnings = _read_state(repo, plan_name)
    all_warnings.extend(state_warnings)

    # 4. Change-level aggregation (run first so plan can use derived statuses)
    change_metrics_list, change_warnings = _change_aggregation(
        state, selected_records, plan_name, all_warnings
    )
    all_warnings = change_warnings

    # 5. Plan-level aggregation (uses change metrics for status counts)
    plan_metrics, plan_warnings = _plan_aggregation(
        state,
        selected_records,
        plan_name,
        selected_run_id,
        all_warnings,
        change_metrics_list,
    )
    all_warnings = plan_warnings

    # 6. Stage-level aggregates
    completed = [
        c for c in change_metrics_list if c.status == "completed"
    ]
    stage_aggregates = _stage_aggregation(completed, selected_records)

    # 7. Model leaderboard (all changes, not only completed)
    leaderboard = _build_leaderboard(change_metrics_list, selected_records)

    return AggregationResult(
        plan_metrics=plan_metrics,
        change_metrics=change_metrics_list,
        stage_aggregates=stage_aggregates,
        model_leaderboard=leaderboard,
        warnings=all_warnings,
    )
