## Context

`opsx-plan` already owns the direct OpenCode implement, review, retry, and archive loop for plan-backed runs. That loop persists state in `.opsx-plan/<plan-name>.state.json`, writes stage logs under `.opsx-plan/logs/`, and verifies archive completion from repository evidence.

The gap is command ergonomics: running one accepted OpenSpec change currently requires a TOML plan manifest, even though the direct loop can execute a single change when provided with a matching config and state record.

## Goals / Non-Goals

**Goals:**

- Provide `opsx-run <change-id>` for exactly one existing OpenSpec change.
- Reuse the current OpenCode direct worker loop, state schema, retry policy, and archive verification.
- Keep `opsx-plan` available for multi-change DAG execution and expose an equivalent single-change subcommand for script users.
- Install the same orchestrator script as both `opsx-plan` and `opsx-run` for the OpenCode adapter.

**Non-Goals:**

- Do not create or author OpenSpec changes from `opsx-run`.
- Do not replace `/opsx-drive`; it remains the manual agent-managed controller path.
- Do not add parallel execution, dependency handling, or plan compilation to the single-change runner.
- Do not introduce a new persisted state format.

## Decisions

- Synthesize a one-change plan config in `opsx-plan.py` instead of writing a temporary `.toml` file. This avoids a new artifact type and lets the runner use the same in-memory config shape as `cmd_run`.
- Name single-change state deterministically, such as `run-<change-id>`, so interrupted runs can resume without requiring a plan file. This keeps the durable state under `.opsx-plan/` and separates ad hoc single-change runs from plan-named runs.
- Dispatch through `run_direct_change()` only for OpenCode defaults. This change targets the existing direct OpenCode worker path and avoids expanding legacy adapter behavior.
- Support both `opsx-plan run-one <change-id>` and `opsx-run <change-id>`. The subcommand is useful when invoking the script directly; the executable alias gives operators the requested no-plan command.
- Fail before dispatch when `openspec/changes/<change-id>` is not authored. The single-change runner should not silently create or repair change artifacts.

## Risks / Trade-offs

- [Risk] Separate single-change state names can leave multiple state files for the same OpenSpec change when operators alternate between plan and run modes. -> Mitigation: document the state naming and keep repository archive evidence as the final completion gate.
- [Risk] `opsx-run` may be expected to work for non-OpenCode adapters. -> Mitigation: scope the requirement to OpenCode direct workers and return a clear error if direct worker invocations are unavailable.
- [Risk] A dirty tracked worktree could mix unrelated edits into a single-change run. -> Mitigation: preserve the existing `require_clean_tracked` guard before starting worker dispatch.

## Migration Plan

No migration is required. Existing plan manifests, `/opsx-drive`, and `.opsx-plan/<plan>.state.json` files continue to work. Installing the OpenCode adapter after this change places an additional `opsx-run` executable alias beside `opsx-plan`.

## Open Questions

- None.
