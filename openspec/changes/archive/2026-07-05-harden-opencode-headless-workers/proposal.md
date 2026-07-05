## Why

OpenCode-backed `opsx-plan` runs can fail before a phase worker emits its required JSON when a worker searches outside the repository for missing guidance files and triggers an unattended permission prompt. The resulting controller error is reported as generic invalid JSON, obscuring the real permission failure and blocking otherwise simple plan progress.

## What Changes

- Harden OpenCode worker instructions so a missing repo-root `AGENTS.md` is non-fatal and never causes parent-directory or external-directory searches.
- Make OpenCode worker external-directory permissions fail closed in headless runs while preserving explicit access to installed global OpenCode prompt files.
- Improve `opsx-plan` parsing diagnostics so permission-denied worker transcripts are reported as permission failures rather than generic JSON-shape failures.
- Add regression coverage for worker prompt constraints, external-directory permission defaults, and permission-rejection log parsing.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `plan-driven-opencode-execution`: OpenCode direct worker execution must remain non-interactive when optional repo guidance is absent and must surface permission-rejection transcripts with an actionable error.

## Impact

- Affected code: `adapters/opencode/agents/*.md`, `orchestrator/opsx-plan.py`, `tests/orchestrator/test_opsx_plan.py`.
- Affected runtime behavior: direct OpenCode plan runs fail closed without interactive permission prompts and report permission failures accurately.
- No API, dependency, or storage format changes.
