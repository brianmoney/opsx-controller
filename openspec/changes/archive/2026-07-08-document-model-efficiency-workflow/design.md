## Context

The `plan-run-observability` capability is feature-complete through Phase 3: telemetry writing, cost estimation, aggregation, CLI reporting, and static HTML dashboard export all work. The final phase 4 change `document-model-efficiency-workflow` is a documentation-only change that teaches operators how to use the built infrastructure to make data-driven model selection decisions.

The documentation must be self-contained enough to serve as a reference without requiring operators to read the full telemetry plan or spec, but concise enough to be skimmable. It must explain the *why* and *how* of model benchmarking without prescribing a single "best" model.

## Goals / Non-Goals

**Goals:**

- Document the end-to-end operator workflow for model efficiency comparison.
- Explain the three cost types (per-token list-price-equivalent, subscription-amortized, unresolved) and when each appears.
- Provide concrete command examples for the full benchmarking loop.
- Cover pricing catalog maintenance: how to add/update entries.
- Warn against invalid comparisons (different plan complexity, mixed billing modes without proper context).
- Make the document discoverable from `core/README.md` and `orchestrator/README.md`.
- Be explicit about limitations and caveats.

**Non-Goals:**

- Do not recommend a specific model or model combination.
- Do not automate model selection.
- Do not add code, tests, or installers.
- Do not create a new spec requirement for tool behavior (all tool behavior is already specified).
- Do not duplicate the pricing catalog spec or the report/dashboard spec — reference them instead.

## Decisions

### 1. Single document in `core/` rather than inline docs or workshop tutorial

The workflow is placed in `core/model-efficiency-workflow.md` because:
- `core/` already contains protocol and contract docs; a workflow doc fits naturally alongside them.
- A single file is easy to discover and reference from multiple places (READMEs, AGENTS.md, the plan itself).
- It avoids scattering workflow guidance across multiple files, which would make maintenance and discovery harder.

The document is linked from `core/README.md` (as a top-level section reference) and `orchestrator/README.md` (in the CLI commands section) to give operators two natural discovery paths: exploring the core contract vs. exploring the orchestrator CLI.

### 2. Document sections organized around the benchmarking loop, not tool-by-tool

The document is organized as a workflow narrative:

1. **Quick Start**: Concrete commands for the full loop in 6 steps.
2. **Configuring Model Sets**: How `OPSX_*_MODEL` env vars map to telemetry model identity and how to compare two sets.
3. **Running Comparable Plans**: What makes a plan "comparable" — same changes, same order, same toolchain — and what breaks comparability.
4. **Maintaining the Pricing Catalog**: How to add/update entries in `lib/pricing/catalog.toml`, what fields are required, and what happens when a model is missing from the catalog.
5. **Interpreting Cost Estimates**: The three cost statuses, per-token vs. subscription-amortized cost calculation, and how to read cost in reports and dashboards.
6. **Comparing Model Combinations**: How to use the leaderboard, per-change table, and dashboard sections to compare reliability, speed, rework, and cost across runs.
7. **Limitations and Caveats**: Known limitations — list-price-equivalent only, no provider billing reconciliation, subscription denominator configurability, complexity bias, unknown model identity.
8. **Reference**: Links to related docs (telemetry schema, pricing catalog spec, report command spec, dashboard command spec, plan metadata schema).

This narrative flow follows the operator's natural journey: set up, run, maintain, interpret, compare, and understand boundaries.

### 3. Concrete command examples use real `opsx-plan` subcommands

Every workflow step includes runnable command examples:

```bash
opsx-plan compile <plan.toml>
OPSX_IMPLEMENTER_MODEL=deepseek/deepseek-v4-pro opsx-plan run <plan.toml>
opsx-plan report <plan.toml> --json
opsx-plan dashboard <plan.toml>
```

This ensures operators can follow along with a real plan rather than reading abstract descriptions.

### 4. Invalid comparison guidance is explicit with concrete examples

The document gives concrete examples of invalid comparisons:

- **Complexity bias**: Running a bugfix change against one model set and a feature-build change against another. The feature-build change is inherently harder, so its metrics (more rounds, more tokens, more cost) are not the model's "fault."
- **Plan content mismatch**: Comparing telemetry from two plans that contain different changes.
- **Mixed billing**: Comparing per-token costs against subscription-amortized costs without acknowledging the denominator assumption.

Each invalid comparison includes a mitigation (e.g., "run both model sets against the same plan, then compare").

### 5. Limitations section is explicit and prominent

The Limitations and Caveats section explicitly lists every known limitation from the plan: list-price-equivalent pricing only, no live provider pricing, no billing reconciliation, subscription denominator is operator-configured, unknown model identity produces unresolvable costs, comparable plans must use identical changes. This meets the plan's success criterion that "limitations and caveats are explicit."

## Risks / Trade-offs

- [Risk] The document describes tool behavior that could drift if the tools change. -> Mitigation: the document references the authoritative spec for each feature; the spec is the source of truth, not the workflow doc.
- [Risk] Operators might skip the Limitations section and draw invalid conclusions. -> Mitigation: the invalid-comparison guidance is in its own prominent section, and caveats are called out inline where relevant (e.g., "subscription costs require a configured denominator — without one, costs show as unresolved").
- [Risk] A documentation-only change feels lightweight vs. the plan's success criteria. -> Expectation: the plan lists `document-model-efficiency-workflow` as a separate Phase 4 change, not bundled with earlier code changes. The documentation is the deliverable; there is no code to write at this phase.

## Migration Plan

No migration is required. The new documentation is additive and does not modify any existing file behavior. Existing tools (report, dashboard, compile, run) operate identically before and after this change.

## Open Questions

- Should the workflow doc include screenshots or sample dashboard HTML snippets? (Design proposes inline command output examples and dashboard column descriptions only; screenshots age quickly and HTML snippets are large. Operators can generate real dashboards from their own data.)
- Should the workflow doc be versioned alongside the telemetry schema version? (Design proposes referencing the catalog version and schema version from the pricing catalog and telemetry schema docs rather than duplicating them.)
