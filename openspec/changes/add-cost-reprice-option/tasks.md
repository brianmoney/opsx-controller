# add-cost-reprice-option Tasks

## 1. Reprice helper

- [x] 1.1 Add `reprice_record(record, repo=None)` to `lib/orchestrator/cost.py`: return a shallow copy of *record* whose `cost` is `estimate_stage_cost(record.get("usage", {}), record.get("model", {}), repo=repo)`. No I/O, no input mutation, total for missing/partial `usage`/`model` (those already yield `unresolved`/`unavailable`).
- [x] 1.2 Cover it with a unit test: a record with a now-covered model becomes `estimated`; a record with a still-missing model stays `unresolved`; the input record is unchanged.

## 2. Aggregator hook

- [x] 2.1 Add an optional `record_transform` keyword to `lib/metrics/aggregator.py::aggregate` and apply it to the selected records immediately after `_select_run`, before `collect_core_metrics` and the change/plan/stage/leaderboard aggregations.
- [x] 2.2 Keep `lib/metrics` free of `lib.orchestrator` imports (the caller supplies the transform).
- [x] 2.3 Test: a fake transform that flips a record's `cost` to `estimated` changes the plan total; the JSONL on disk is unchanged; two calls with the same transform produce identical results.

## 3. Report plumbing

- [x] 3.1 Add `--reprice` to the `report` subparser in `orchestrator/opsx-plan.py`.
- [x] 3.2 In `lib/orchestrator/report.py::cmd_report`, build the transform from `cost.reprice_record` when `--reprice` is set, pass it to `aggregate`, and apply it to the `--change` re-read path as well.
- [x] 3.3 Emit a repriced notice naming the pricing catalog version (table mode line and additive JSON fields); emit nothing when the flag is absent.
- [x] 3.4 Test: `cmd_report` with `--reprice` on a telemetry fixture with an `unresolved` record now covered by the catalog shows the recomputed total; without the flag the stored total is shown; the telemetry file is unchanged in both.

## 4. Dashboard plumbing

- [x] 4.1 Add `--reprice` to the `dashboard` subparser in `orchestrator/opsx-plan.py`.
- [x] 4.2 In `lib/orchestrator/dashboard.py::cmd_dashboard`, pass the transform to `aggregate` when `--reprice` is set and embed the repriced notice + catalog version in the generated HTML.
- [x] 4.3 Test: generated HTML with `--reprice` names the catalog version and reflects the recomputed total; without the flag it is unchanged.

## 5. Docs and validation

- [x] 5.1 Document `--reprice` in `core/model-efficiency-workflow.md` (catalog-versioning / reproducibility section) and in `orchestrator/README.md`.
- [x] 5.2 Run `python3 -m unittest discover -t . -s tests` and `node tests/opencode/test-opsx-usage-emitter.js` from the repository root; both pass.
- [x] 5.3 Run `openspec validate add-cost-reprice-option --strict`.
- [x] 5.4 Maintainer note: re-run `bash install.sh --global --verify` after merge to deploy the orchestrator change.
