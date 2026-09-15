# Design: add-cost-reprice-option

## Context

- `lib/orchestrator/cost.py::estimate_stage_cost(usage, model, ...)` already
  maps a `(usage, model)` pair to the telemetry `cost` schema
  (`status`, `pricing_catalog_version`, `price_snapshot`, `unresolved_reason`,
  `estimated_cost`). It is the exact routine used at stage dispatch.
- `lib/metrics/aggregator.py::aggregate(repo_root, plan_name, run_id=None)`
  reads the telemetry JSONL, selects a run, and computes change, plan, stage,
  leaderboard, and core metrics. It is deliberately decoupled from
  `lib.orchestrator` and is documented as read-only and deterministic.
- `report.py` and `dashboard.py` (both under `lib/orchestrator/`) are the only
  callers of `aggregate` and already own output formatting. `report.py` also
  re-reads telemetry directly when `--change` narrows the leaderboard.
- Spec requirements already forbid mutation of telemetry/state by the
  aggregator, report, and dashboard, and require historical records to keep
  their original catalog version and `price_snapshot`.

## Goals / Non-Goals

**Goals:**

- Expose the already-computed-elsewhere recomputation as a read-time view on
  `report` and `dashboard`, opt-in via `--reprice`.
- Keep the metric layer free of `lib.orchestrator` imports and keep the JSONL
  untouched.
- Make repriced output self-identifying (flags + catalog version).

**Non-Goals:**

- Rewriting or migrating stored telemetry.
- Changing telemetry schema, the estimation formula, or default output.
- Repricing the plugin usage sidecar or ledger reservations.

## Decisions

### D1. A generic record-transform hook on `aggregate`, not a `lib.metrics` import of cost

`aggregate` gains an optional keyword `record_transform: Callable[[dict],
dict] | None`. When provided, it is applied to the selected records once,
immediately after run selection and before `collect_core_metrics` and all
downstream aggregation, so every consumer sees the transformed records. The
aggregator itself stays ignorant of pricing and imports nothing from
`lib.orchestrator`; the caller supplies the transform. This preserves the
existing layering and keeps the aggregator unit-testable with a trivial
transform.

Alternatives rejected: importing `lib.orchestrator.cost` from `lib.metrics`
(couples the metric layer upward); recomputing inside each downstream
aggregation function (duplicated, error-prone); a CLI-only one-off in report
(dashboard would diverge).

### D2. The transform is a thin, pure record-level helper in `cost.py`

`lib/orchestrator/cost.py` gains `reprice_record(record)` returning a shallow
copy with `cost` replaced by `estimate_stage_cost(record["usage"],
record["model"], repo=...)`. It reuses the dispatch-time routine verbatim, so
repriced `cost` objects have identical shape and status vocabulary and a
record that cannot be priced stays `unresolved`. The helper performs no I/O and
does not mutate its input.

`repo` is threaded so the installed runtime resolves `lib.pricing` correctly,
matching how the dispatch path calls the estimator.

### D3. Report's `--change` re-read also uses the transform

`report.py` re-reads telemetry and re-selects the run to rebuild the leaderboard
when `--change` is set. That path SHALL apply the same transform so filtered
and unfiltered output agree. The transform is constructed once in
`cmd_report` and reused.

### D4. Repriced output announces itself; default output is untouched

When `--reprice` is set, report prints a single line (both table and JSON
modes; JSON adds additive fields) naming the catalog version used, and the
dashboard embeds the same notice. When the flag is absent, no annotation is
emitted and behaviour is byte-for-byte the previous behaviour. This keeps
repriced figures from being mistaken for the stored record.

## Risks / Trade-offs

- **Repriced figures are non-reproducible against stored history.** That is
  inherent and intended: the JSONL remains the record of what was estimated at
  write time; `--reprice` is an explicit, current-catalog view. The catalog
  version is reported to make the distinction unambiguous.
- **A transform that raises would break report.** The flag is opt-in and the
  helper is total for well-formed records; malformed records are already
  skipped by the telemetry reader.
