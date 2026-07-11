## 1. Command Surface

- [x] 1.1 Add `opsx-plan logs [plan]` using the same explicit/env/active-plan resolution contract as other operator-facing plan commands.
- [x] 1.2 Make the default command print the selected log path and a tail of that log.
- [x] 1.3 Add deterministic selectors for change id and stage.
- [x] 1.4 Add a listing mode for available logs for the resolved plan.
- [x] 1.5 Add a follow mode for an in-progress run.

## 2. Log Selection Rules

- [x] 2.1 Resolve the default target log from recorded plan state metadata when a usable last-stage log is recorded for the matching change or plan.
- [x] 2.2 Fall back to deterministic `.opsx-plan/logs/` ordering when recorded state metadata does not identify a usable log.
- [x] 2.3 Apply change-id and stage filters consistently across both state-backed and directory-fallback selection paths.
- [x] 2.4 Emit a clear no-match message when no log satisfies the requested selection instead of printing an empty tail.

## 3. Verification

- [x] 3.1 Add unit tests for default latest-log selection from recorded state metadata.
- [x] 3.2 Add unit tests for fallback latest-log selection by log-directory ordering.
- [x] 3.3 Add unit tests for change-id and stage filter combinations.
- [x] 3.4 Add unit tests for list output and follow-mode target selection.
- [x] 3.5 Add unit tests for missing-log handling.
- [x] 3.6 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 3.7 Run `openspec validate add-plan-logs-command --strict`.
- [x] 3.8 Re-run `bash adapters/opencode/install.sh --global --verify` because the orchestrator install target changes.
