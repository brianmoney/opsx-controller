## ADDED Requirements

### Requirement: Model efficiency workflow documentation is available and complete

The repository SHALL include a model efficiency workflow document at `core/model-efficiency-workflow.md` that describes the end-to-end operator workflow for benchmarking OPSX model choices using plan-run telemetry, cost estimation, aggregation, reporting, and dashboard exports.

The document SHALL cover the following topics:

- **Quick Start**: A concrete step-by-step benchmarking loop using `opsx-plan` subcommands (`compile`, `run`, `report`, `dashboard`) with example invocations.
- **Configuring Model Sets**: How `OPSX_IMPLEMENTER_MODEL`, `OPSX_REVIEWER_MODEL`, and `OPSX_ARCHIVER_MODEL` environment variables map to telemetry model identity, and how to configure two model sets for comparison.
- **Running Comparable Plans**: What requirements make two plan runs comparable — identical changes in identical order with identical toolchain configuration — and what breaks comparability (different changes, different plan content, different adapter versions).
- **Maintaining the Pricing Catalog**: How to add and update entries in `lib/pricing/catalog.toml`, including required fields, per-token vs. subscription entry format, and how unknown models appear in reports.
- **Interpreting Cost Estimates**: The meaning and rendering of each cost status (`estimated`, `unresolved`, `unavailable`) in reports and dashboards. The difference between per-token list-price-equivalent cost and subscription-amortized effective cost. How total, average, and per-change costs are computed from stage estimates.
- **Comparing Model Combinations**: How to use the leaderboard table (success rate, first-pass rate, average rounds, average duration, average tokens, average cost) alongside the per-change detail table, failure breakdown, and dashboard to compare reliability, speed, rework, and cost across runs.
- **Limitations and Caveats**: An explicit list of known limitations including at minimum: costs are list-price-equivalent only (not actual billing), no live provider pricing or billing reconciliation, subscription cost amortization depends on an operator-configured denominator, unknown model identity produces unresolvable costs, comparable plans must use identical changes to be valid comparisons, and correlating model choice with change complexity requires operator judgment.

#### Scenario: Document covers all required topics

- **WHEN** an operator reads `core/model-efficiency-workflow.md`
- **THEN** the document includes sections covering Quick Start, Configuring Model Sets, Running Comparable Plans, Maintaining the Pricing Catalog, Interpreting Cost Estimates, Comparing Model Combinations, and Limitations and Caveats

#### Scenario: Document includes runnable command examples

- **WHEN** the document describes the benchmarking workflow
- **THEN** each step includes a concrete `opsx-plan` command invocation using actual subcommands and flags available in the installed orchestrator

#### Scenario: Document explicitly lists limitations

- **WHEN** the document reaches the Limitations and Caveats section
- **THEN** it states at minimum: costs are list-price-equivalent, subscription amortization requires operator configuration, unknown models produce unresolvable costs, and valid comparisons require identical plan content

### Requirement: Documentation is discoverable from existing README files

The model efficiency workflow document SHALL be linked from `core/README.md` and `orchestrator/README.md` so that operators exploring the core contract or the orchestrator CLI can reach it without searching the file system.

#### Scenario: Linked from core README

- **WHEN** an operator reads `core/README.md`
- **THEN** the document includes a reference or link to `core/model-efficiency-workflow.md`

#### Scenario: Linked from orchestrator README

- **WHEN** an operator reads `orchestrator/README.md`
- **THEN** the document includes a reference or link to `core/model-efficiency-workflow.md`

### Requirement: Workflow documentation references stay current

The workflow documentation SHALL reference stable identifiers (field names, file paths, command names) that match the installed implementation. References to specific spec sections SHALL use section heading text rather than line numbers or unstable anchors.

#### Scenario: Command names match installed CLI

- **WHEN** a command is referenced in the workflow document (e.g., `opsx-plan compile`, `opsx-plan report --json`)
- **THEN** the command name and flag names match the subparser and argument definitions in `orchestrator/opsx-plan.py`

#### Scenario: File paths match actual files

- **WHEN** a file path is referenced in the workflow document (e.g., `.opsx-plan/telemetry/`, `lib/pricing/catalog.toml`)
- **THEN** the path corresponds to the actual file or directory created by the orchestrator or expected by supporting modules

### Requirement: Documentation does not prescribe a specific model choice

The workflow documentation SHALL describe *how* to compare model combinations but SHALL NOT recommend or prescribe a single universal model, model provider, or model combination. It SHALL present the comparison methodology and let operators apply their own criteria.

#### Scenario: No single model recommendation

- **WHEN** the document discusses model comparison results
- **THEN** it does not state that any specific model or combination is the "best," "recommended," or "default" choice
