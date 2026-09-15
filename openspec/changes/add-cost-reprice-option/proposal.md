# Proposal: add-cost-reprice-option

## Why

Telemetry records store cost at write time, pinned to the pricing catalog
version current then. The storage contract is deliberately append-only and
reproducible: a record keeps its original `cost.pricing_catalog_version` and
`cost.price_snapshot` forever, and the aggregator, `report`, and `dashboard`
never mutate telemetry or state. That is the right default, but it means a
record estimated while the catalog was incomplete stays `cost.status =
"unresolved"` (or stays priced at a stale/incorrect rate) even after the
catalog is corrected. Operators have no supported way to see what a finished
run would cost under the current catalog.

The concrete case: the `durable-plan-supervision` run recorded 70 of 76 stage
records as `unresolved` because the pinned route models (`commandcode` /
`opencode-go` deepseek, `moonshotai/kimi-k3`) and the OpenAI reasoning-token
category were missing from `lib/pricing/catalog.toml`. After correcting the
catalog, the stored telemetry still reports `$0.14` while a recompute against
the current catalog yields roughly `$1.01`. Rewriting stored telemetry would
violate the append-only/reproducibility requirements, so the correction must
be a read-time view.

## What Changes

- Add an optional, non-mutating `--reprice` flag to `opsx-plan report` and
  `opsx-plan dashboard`. When set, each selected telemetry record's `cost`
  object is recomputed **in memory** from the record's stored `usage` and
  `model` fields against the currently loaded pricing catalog, using the same
  estimation routine as stage dispatch, before aggregation.
- Reprice output is explicit: the report and dashboard state that costs were
  repriced and name the catalog version used, so a repriced figure is never
  confused with the stored figure.
- Telemetry, state, and all repository files remain untouched; without the
  flag, output is byte-for-byte identical to today.
- Records that still cannot be priced after reprice remain `unresolved` with
  their reason; reprice never fabricates a cost.

## Impact

- **Code:** `lib/orchestrator/cost.py` (a record-level reprice helper that
  reuses `estimate_stage_cost`); `lib/metrics/aggregator.py` (an optional,
  generic record-transform hook on the read path); `lib/orchestrator/report.py`
  and `lib/orchestrator/dashboard.py` (flag plumbing and output annotation);
  `orchestrator/opsx-plan.py` (argparse flags).
- **Specs:** `plan-run-observability` gains requirements for the read-only
  reprice behaviour on both commands; existing non-mutation and determinism
  requirements are preserved and extended to cover reprice.
- **Tests:** aggregator reprice semantics (recomputed value, still-unresolved
  passthrough), non-mutation of the JSONL, determinism, and CLI flag wiring.
- **Docs:** `core/model-efficiency-workflow.md` documents reprice alongside the
  catalog-versioning reproducibility notes; `orchestrator/README.md` gains the
  flag.
- **Compatibility:** purely additive. No telemetry schema change, no new
  runtime dependency, default behaviour unchanged.
