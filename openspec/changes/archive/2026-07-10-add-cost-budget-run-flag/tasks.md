## 1. Run Flag And CLI Surface

- [x] 1.1 Add a `--budget-usd` float flag to `opsx-plan run`.
- [x] 1.2 Validate that non-positive values behave as "no spend cap" or are rejected consistently with existing run-budget flag semantics.
- [x] 1.3 Preserve existing `opsx-plan run` behavior when the flag is omitted.

## 2. Budget Enforcement

- [x] 2.1 Accumulate estimated stage costs from telemetry records for the active run only.
- [x] 2.2 Stop dispatching new stages once the cumulative estimated cost reaches or exceeds the configured USD cap.
- [x] 2.3 Never interrupt a stage already in flight; enforce the budget only at stage-dispatch boundaries.
- [x] 2.4 Leave plan state resumable after a spend-budget stop, matching other clean run-stop paths.

## 3. Unresolved Cost Reporting

- [x] 3.1 Track how many stages contributed resolved estimated cost toward the budget calculation.
- [x] 3.2 Track how many stages in the run completed with unresolved cost.
- [x] 3.3 Report both counts clearly when a run stops because of `--budget-usd`.

## 4. Verification

- [x] 4.1 Add unit tests for a run that stops after reaching the spend cap.
- [x] 4.2 Add unit tests for a run that completes without reaching the spend cap.
- [x] 4.3 Add unit tests for runs with mixed estimated and unresolved stage costs.
- [x] 4.4 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 4.5 Run `openspec validate add-cost-budget-run-flag --strict`.
- [x] 4.6 Re-run `bash adapters/opencode/install.sh --global --verify` because the orchestrator install target changes.
