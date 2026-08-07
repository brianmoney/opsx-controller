# Project map
- `core/`: client-neutral controller contract, state schema, phase protocol, and the sole plan-authoring reference.
- `adapters/opencode/`, `adapters/claude-code/`, `adapters/codex-cli/`: client-specific commands, agents, installers, templates.
- `orchestrator/opsx-plan.py` plus installed `lib/orchestrator/`: deterministic plan DAG orchestration, phase dispatch, verification, retries, durable state, reporting.
- `plugins/opsx-controller/` and `skills/opsx-controller/`: distribution surfaces.
- OpenSpec changes live under `openspec/changes/`; active plans under `openspec/plans/`, archived plans under `openspec/plans/archived/`.
- Invariant: controller judgment stays in implement/review/archive workers; orchestrator owns ordering, dispatch, verification, retry policy, and bookkeeping.
- Review is strict: critical, warning, and note findings all block progress; archive only after fresh zero-finding review and verified directory movement.
- Read `AGENTS.md` for repo-specific validation and maintainer deployment requirements.