# Conventions
- Keep client-neutral semantics in `core/`; adapters should reference rather than restate shared plan-authoring rules.
- Orchestrator is deterministic and stdlib-only; avoid moving LLM judgment into orchestration code.
- Durable state and phase outputs are verified against repository ground truth; ambiguous or unparseable results fail closed.
- Plan manifests are TOML dependency DAGs generated from markdown; review dependencies and gates with `run --dry-run` before unattended execution.
- Preserve `.openspec.yaml` within change directories through archive moves.