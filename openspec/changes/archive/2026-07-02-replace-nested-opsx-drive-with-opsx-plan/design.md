## Context

`opsx-plan` currently owns dependency ordering and change creation, but delegates all implementation execution to a long-lived `/opsx-drive` controller run. That nested controller duplicates orchestration logic, persists a separate per-change state file, and can continue burning time after the useful implementation work is already complete. The change is intentionally scoped to the OpenCode adapter path so the nested-controller replacement can be proven in one place before any broader adapter rewrite.

## Goals / Non-Goals

**Goals:**
- Make `opsx-plan` the only orchestrator for plan execution on the OpenCode adapter.
- Replace one long-lived `/opsx-drive` invocation with bounded implement, review, and archive worker invocations.
- Preserve strict review gating, bounded retry rules, and archive verification without relying on nested controller state.
- Keep plan runs observable through phase-specific state and logs that let an operator see where time is going.
- Leave a manual `/opsx-drive <change>` path available for single-change operator use while removing it from the plan runner's critical path.

**Non-Goals:**
- Rewriting the Claude Code or Codex CLI adapters in the same change.
- Removing `/opsx-drive` from the repository entirely.
- Changing the OpenSpec authoring (`/opsx-ff`) create stage.
- Loosening the review gate so warnings or notes can pass automatically.

## Decisions

1. `opsx-plan` will own the phase loop directly.
Rationale: the current split between plan orchestration and controller orchestration creates opaque hangs and duplicate durable state. The outer script already owns dependency order, retries, and completion verification, so it is the right place to own phase progression too.
Alternatives considered: keep `/opsx-drive` and add more timeout/reconcile logic. Rejected because the main failure is not missing retries; it is the nested long-lived control loop itself.

2. OpenCode plan execution will invoke the existing phase workers as one-shot subprocesses.
Rationale: the implementer, reviewer, and archiver already encode the expensive LLM judgment. Reusing them keeps the redesign small while removing the extra controller layer. `opsx-plan` will build the input block each worker needs, invoke one worker per stage, and parse the single-line JSON result.
Alternatives considered: move phase logic into Python. Rejected because it would replace one problem with a much larger rewrite.

3. Plan-owned state will become authoritative for direct execution.
Rationale: when `opsx-plan` owns the loop, it should also own round, phase, fix prompt, archive result, and last-log bookkeeping. This removes the current dependency on `.opencode/opsx-controller/<change>.json` for recovery and verification. The state model can reuse the controller's concepts, but it should live inside `.opsx-plan/<plan>.state.json` as the single source of truth for plan runs.
Alternatives considered: keep writing both state files. Rejected because dual authoritative state would preserve drift and reconciliation problems.

4. Archive completion remains evidence-driven.
Rationale: the redesign is about control flow, not lowering safety. `opsx-plan` should still require a fresh archive worker success payload, dated archive directory evidence, archive commit evidence, and post-archive fast checks before marking a change done.
Alternatives considered: trust worker exit code or stdout. Rejected because the current orchestrator already correctly distrusts those signals.

5. `/opsx-drive` remains a manual compatibility path for now.
Rationale: removing the manual single-change controller in the same change would expand scope unnecessarily. The plan runner will stop invoking it, but operators can still use it directly until follow-on cleanup changes decide whether to deprecate or remove it.
Alternatives considered: delete `/opsx-drive` immediately. Rejected to keep the change small and reversible.

## Risks / Trade-offs

- [More logic in `opsx-plan`] -> Mitigation: keep worker intelligence in the existing phase agents and limit new Python logic to state transitions, invocation, and verification.
- [Worker invocation contracts may drift from current controller input blocks] -> Mitigation: define the direct stage input contract explicitly in the new capability spec and cover it with regression tests.
- [Resume behavior could regress during migration off nested controller state] -> Mitigation: add interrupted-run and review-failure recovery tests before removing `/opsx-drive` from plan execution.
- [OpenCode-specific solution may not generalize cleanly] -> Mitigation: scope this change to the OpenCode adapter first and only abstract stage invocation surfaces that prove necessary.

## Migration Plan

1. Extend `opsx-plan` state records with phase-loop fields needed for direct execution.
2. Add OpenCode stage invocation wiring for implement, review, and archive worker runs.
3. Update reconcile and retry behavior to use plan-owned state instead of nested controller state for OpenCode runs.
4. Update orchestrator and adapter documentation to describe direct stage execution and the remaining manual `/opsx-drive` path.
5. Verify on a real OpenSpec change path, then keep `/opsx-drive` available only as a manual fallback outside plan runs.

## Open Questions

- Should the per-stage invocation templates be OpenCode-only defaults, or should this change also introduce generic `implement_invoke` / `review_invoke` / `archive_invoke` manifest fields for future adapters?
- Should `opsx-plan` preserve a full per-phase history payload comparable to controller schema v3, or only the subset needed for retries, diagnostics, and completion reporting?
