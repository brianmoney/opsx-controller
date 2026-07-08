## 1. Create Model Efficiency Workflow Document

- [x] 1.1 Create `core/model-efficiency-workflow.md` with the following sections:
  - Quick Start (6-step benchmarking loop with concrete commands)
  - Configuring Model Sets (env var mapping to telemetry, model comparison setup)
  - Running Comparable Plans (same changes, same order, same toolchain; what makes plans comparable/incomparable)
  - Maintaining the Pricing Catalog (add/update entries in `lib/pricing/catalog.toml`, required fields, unknown model behavior)
  - Interpreting Cost Estimates (per-token list-price-equivalent, subscription-amortized, unresolved — when each appears, how to read them)
  - Comparing Model Combinations (using leaderboard, per-change table, and dashboard for reliability/speed/rework/cost)
  - Limitations and Caveats (list-price only, no billing reconciliation, denominator configuration, complexity bias, unknown models, plan comparability)
  - Reference (links to telemetry schema, pricing catalog spec, report spec, dashboard spec, plan compile doc)
- [x] 1.2 Include concrete `opsx-plan` command examples for each workflow step.
- [x] 1.3 Include explicit invalid-comparison warnings with mitigations.
- [x] 1.4 Ensure all referenced command names, file paths, and field names match the actual implementation.

## 2. Link from Existing Documentation

- [x] 2.1 Add a link to `core/model-efficiency-workflow.md` from `core/README.md` in a new "Model Efficiency Workflow" section or at the end of the existing content.
- [x] 2.2 Add a link to `core/model-efficiency-workflow.md` from `orchestrator/README.md` in the CLI commands or reporting section.

## 3. Verification

- [x] 3.1 Read through `core/model-efficiency-workflow.md` and verify all command examples match the current `opsx-plan` CLI (subcommands, flags, output paths).
- [x] 3.2 Verify all referenced spec sections (pricing catalog, telemetry schema, report, dashboard) exist in `openspec/specs/plan-run-observability/spec.md`.
- [x] 3.3 Run `openspec validate document-model-efficiency-workflow --strict`.
