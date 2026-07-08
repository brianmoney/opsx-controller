## Why

The model efficiency telemetry infrastructure is complete: `opsx-plan` writes durable telemetry with usage and cost estimates for every stage invocation, an aggregation API computes efficiency KPIs, and `opsx-plan report` and `opsx-plan dashboard` expose those metrics as tables, JSON, and static HTML.

But operators need more than the tools — they need a clear workflow for using them to answer the question the whole telemetry stack was built for: *which model choices are measurably better for my workload?* Without documentation, an operator must reverse-engineer cost interpretation, pricing maintenance, comparison methodology, and caveat boundaries from the CLI help and source code.

## What Changes

- Add `core/model-efficiency-workflow.md`: a comprehensive operator workflow document covering the full benchmarking loop: selecting model sets, running comparable plans, maintaining the pricing catalog, interpreting cost types (per-token list-price-equivalent vs. subscription-amortized vs. unresolved), reading cost statuses in reports and dashboards, and comparing model combinations across runs.
- Include explicit guidance on avoiding invalid comparisons: when two plans differ materially in change complexity, comparing their raw cost or duration numbers is misleading. The document explains what constitutes a valid comparison and how to control for complexity.
- Include a quick-start section with concrete command examples using `opsx-plan compile`, `opsx-plan run`, `opsx-plan report`, and `opsx-plan dashboard`.
- Link the new document from `core/README.md` and `orchestrator/README.md` so operators discover it naturally alongside the existing workflow and CLI docs.

No changes to code, telemetry writing, cost estimation, aggregation, reports, dashboards, or control loop behavior.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Adds a model efficiency workflow document to the `core/` directory and links it from existing docs. No functional spec requirements are modified — the existing report and dashboard requirements already define the operator-facing tool surface documented by this workflow.

### New Capabilities

- None.

## Impact

- New file: `core/model-efficiency-workflow.md`.
- Modified files: `core/README.md` and `orchestrator/README.md` (added links to the new workflow document).
- No code changes, no test changes, no installer changes.
