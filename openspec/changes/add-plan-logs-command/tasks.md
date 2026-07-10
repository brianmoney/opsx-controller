## 1. Command Surface

- [ ] 1.1 Add `opsx-plan logs [plan]` using the same explicit/env/active-plan resolution contract as other operator-facing plan commands.
- [ ] 1.2 Make the default command print the selected log path and a tail of that log.
- [ ] 1.3 Add deterministic selectors for change id and stage.
- [ ] 1.4 Add a listing mode for available logs for the resolved plan.
- [ ] 1.5 Add a follow mode for an in-progress run.

## 2. Log Selection Rules

- [ ] 2.1 Resolve the default target log from recorded plan state metadata when a usable last-stage log is recorded for the matching change or plan.
- [ ] 2.2 Fall back to deterministic `.opsx-plan/logs/` ordering when recorded state metadata does not identify a usable log.
- [ ] 2.3 Apply change-id and stage filters consistently across both state-backed and directory-fallback selection paths.
- [ ] 2.4 Emit a clear no-match message when no log satisfies the requested selection instead of printing an empty tail.

## 3. Verification

- [ ] 3.1 Add unit tests for default latest-log selection from recorded state metadata.
- [ ] 3.2 Add unit tests for fallback latest-log selection by log-directory ordering.
- [ ] 3.3 Add unit tests for change-id and stage filter combinations.
- [ ] 3.4 Add unit tests for list output and follow-mode target selection.
- [ ] 3.5 Add unit tests for missing-log handling.
- [ ] 3.6 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [ ] 3.7 Run `openspec validate add-plan-logs-command --strict`.
- [ ] 3.8 Re-run `bash adapters/opencode/install.sh --global --verify` because the orchestrator install target changes.
