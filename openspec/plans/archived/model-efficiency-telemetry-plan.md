---
title: Model Efficiency Telemetry And Dashboard Plan
doc_type: implementation-plan
status: proposed
updated: 2026-07-05
---

# Model Efficiency Telemetry And Dashboard Plan

## Purpose

Add durable telemetry, cost estimation, and reporting surfaces so OPSX plan runs
can be compared across implementer, reviewer, and archiver model choices. The
goal is to make model selection measurable by reliability, speed, rework, and
cost rather than by anecdote.

The plan separates raw evidence capture from cost policy and presentation. That
keeps the runtime path minimal first, then layers in pricing tables,
aggregations, and dashboard exports once the data is available.

## Capability Ownership

`plan-run-observability` is a proposed capability for plan-run telemetry,
efficiency metrics, model cost normalization, and local reporting artifacts. It
is separate from `plan-driven-opencode-execution` because it describes
cross-run measurement and analysis rather than the control loop semantics for a
single run.

## Phase 1: Telemetry Foundation

### Change: `define-plan-run-telemetry-schema`

**Purpose:** Define the durable telemetry contract for plan, change, and stage
invocations before any runtime writes begin.

**Depends on:** None. This may be developed independently because it is a
specification and documentation baseline.

**Capability:** `plan-run-observability` (proposed; see Capability Ownership).

**Scope:** Add OpenSpec requirements for telemetry records that include plan
name, run id, change id, stage, round, status, timestamps, duration, invocation
command shape, model identity fields, parsed worker result summary, token usage
fields when available, and cost-estimation placeholders. Document where records
are stored under `.opsx-plan/` and how historical records remain readable as the
schema evolves.

**Out of scope:** Implementing runtime telemetry writes, pricing tables,
dashboards, or provider-specific usage extraction.

**Success parameters:** OpenSpec validation passes; the telemetry schema names
required and optional fields; the schema distinguishes missing usage from zero
usage; the schema explains how subscription and per-token billing modes will be
represented without requiring actual cost calculation yet.

### Change: `record-direct-stage-telemetry`

**Purpose:** Persist one telemetry record for each direct OpenCode stage worker
invocation.

**Depends on:** `define-plan-run-telemetry-schema`.

**Capability:** `plan-run-observability`.

**Scope:** Extend `orchestrator/opsx-plan.py` so direct implement, review, and
archive dispatches record started and ended timestamps, duration, outcome, log
path, change id, stage, round, relevant status fields, and best-effort model
identity. Store append-only JSONL telemetry under `.opsx-plan/telemetry/` and
link the latest stage telemetry entry from the existing plan state. Add unit
tests around successful stages, timeouts, spawn errors, and invalid worker JSON.

**Out of scope:** Token parsing, price calculation, HTML dashboards, or metrics
rollups.

**Success parameters:** Every direct stage attempt creates exactly one telemetry
entry; existing resume behavior still works; telemetry is written for both
success and failure outcomes; tests prove entries contain enough identifiers to
join back to plan state and logs.

### Change: `capture-worker-usage-metadata`

**Purpose:** Capture token usage and provider metadata when worker logs expose it
without making telemetry dependent on any single client format.

**Depends on:** `record-direct-stage-telemetry`.

**Capability:** `plan-run-observability`.

**Scope:** Add a conservative usage extraction layer that can populate input,
output, cached-input, reasoning, and total token fields from structured worker
output or recognizable log metadata. Preserve `null` for unavailable values and
record the extraction source. Add tests proving unknown formats do not fail a
run or fabricate usage.

**Out of scope:** Calling provider APIs for usage, estimating tokens from raw
text, or calculating dollar cost.

**Success parameters:** Token fields remain nullable; known structured examples
populate usage fields; unrecognized logs produce telemetry with usage marked
unavailable; stage completion semantics remain unchanged.

## Phase 2: Pricing And Cost Normalization

### Change: `add-model-pricing-catalog`

**Purpose:** Provide a maintained local catalog for per-token and subscription
model pricing.

**Depends on:** `define-plan-run-telemetry-schema`.

**Capability:** `plan-run-observability`.

**Scope:** Add a versioned pricing catalog file and loader covering provider,
model id, billing mode, currency, per-million input/output/cached/reasoning
rates when applicable, subscription-period price when applicable, effective
date, and notes. Document how operators update rates and how unknown models are
handled. Add validation tests for malformed, missing, and unknown pricing
entries.

**Out of scope:** Fetching live provider prices, enforcing pricing freshness, or
changing installed agent model configuration.

**Success parameters:** The catalog can represent token-billed and
subscription-billed models; unknown model lookups return a clear unresolved
result; tests cover representative catalog entries without requiring network
access.

### Change: `estimate-stage-token-costs`

**Purpose:** Attach normalized estimated dollar costs to stage telemetry when
usage and pricing data are available.

**Depends on:** `capture-worker-usage-metadata` and `add-model-pricing-catalog`.

**Capability:** `plan-run-observability`.

**Scope:** Calculate list-price-equivalent cost for token-billed models and
effective amortized cost for subscription models when an observed subscription
usage denominator is configured. Store cost status, pricing catalog version,
price snapshot fields, and unresolved reasons in telemetry. Add tests for
per-token pricing, cached tokens, missing usage, unknown model pricing, and
subscription models without enough denominator data.

**Out of scope:** Billing reconciliation, invoices, automatic provider usage
downloads, or deciding which model is best.

**Success parameters:** Cost estimates are reproducible from stored telemetry;
missing inputs produce explicit unresolved states; historical telemetry keeps
the pricing snapshot used at calculation time; no stage fails only because cost
cannot be estimated.

## Phase 3: Metrics And Reporting

### Change: `aggregate-plan-efficiency-metrics`

**Purpose:** Convert raw telemetry into plan, change, phase, and model-combo
metrics.

**Depends on:** `estimate-stage-token-costs`.

**Capability:** `plan-run-observability`.

**Scope:** Add aggregation code that computes completion rate, change success
rate, first-pass review rate, average and median rounds, duration per stage,
duration per completed change, review failure rate, no-progress rate,
max-rounds rate, archive failure rate, fast-check failure rate, tokens per
completed change, estimated cost per completed change, and model-combination
leaderboards. Include tests for partial runs, failed runs, unknown costs, and
multi-round changes.

**Out of scope:** Rendering a dashboard UI or modifying plan execution policy
based on metrics.

**Success parameters:** Aggregates can be generated from telemetry and state
files after a run; metrics clearly separate unknown cost from zero cost; tests
cover successful, blocked, failed, and incomplete changes.

### Change: `add-opsx-plan-report-command`

**Purpose:** Expose efficiency metrics through a deterministic local CLI report.

**Depends on:** `aggregate-plan-efficiency-metrics`.

**Capability:** `plan-run-observability`.

**Scope:** Add an `opsx-plan report` command that reads `.opsx-plan/` telemetry
and state, then emits human-readable tables and machine-readable JSON. Support
filters for plan name, run id, change id, stage, model, and date range. Include
documentation and tests for report output stability.

**Out of scope:** HTML dashboard export, live server mode, or graphical charts.

**Success parameters:** Operators can run a report without rerunning a plan;
JSON output is stable enough for external dashboards; report output identifies
unresolved costs and missing usage explicitly.

### Change: `export-plan-efficiency-dashboard`

**Purpose:** Generate a static dashboard artifact for comparing model efficiency
across OPSX runs.

**Depends on:** `add-opsx-plan-report-command`.

**Capability:** `plan-run-observability`.

**Scope:** Add a static HTML or Markdown dashboard export with plan summary,
model leaderboard, per-change table, failure breakdown, cost breakdown, rounds
histogram, and stage timeline. Keep the artifact local and deterministic so it
can be regenerated from telemetry. Add tests or snapshot checks for the export
structure.

**Out of scope:** Hosted services, authentication, live refresh, JavaScript-heavy
analytics, or remote telemetry upload.

**Success parameters:** A completed or partial run can produce a dashboard file;
the dashboard makes unknown usage/cost visually distinct from zero usage/cost;
model-combination comparisons include reliability, speed, rework, and cost
columns.

## Phase 4: Operator Workflow

### Change: `document-model-efficiency-workflow`

**Purpose:** Document how operators benchmark different OPSX model choices using
the new telemetry and reporting flow.

**Depends on:** `export-plan-efficiency-dashboard`.

**Capability:** `plan-run-observability`.

**Scope:** Update repo documentation with a workflow for selecting model sets,
running comparable plans, maintaining pricing data, interpreting subscription
effective cost, reading unresolved cost states, and comparing list-price cost
against actual amortized subscription cost. Include guidance on avoiding invalid
comparisons when plans differ materially in complexity.

**Out of scope:** Recommending a single universal model choice or automating
model selection.

**Success parameters:** Documentation explains the difference between
per-token, list-price-equivalent, and subscription-amortized costs; users can
reproduce a model comparison from local telemetry; limitations and caveats are
explicit.

## Recommended Sequence

1. Implement `define-plan-run-telemetry-schema` first and review the schema
   before runtime writes begin.
2. Implement `record-direct-stage-telemetry` and `capture-worker-usage-metadata`
   to create reliable raw evidence.
3. Implement `add-model-pricing-catalog` in parallel with telemetry extraction
   if desired, then connect them with `estimate-stage-token-costs`.
4. Implement aggregation and CLI reporting before dashboard export so every
   displayed metric has a deterministic source.
5. Finish with documentation after the exact command names, file locations, and
   limitations are known.

## Overall Completion Criteria

The series is complete when OPSX plan runs write durable telemetry for every
direct stage attempt, can attach reproducible cost estimates when model usage
and pricing data are available, can aggregate efficiency KPIs by plan, change,
stage, and model combination, and can render both CLI and static dashboard
reports without network access.

## Explicit Non-Goals

This plan does not add hosted telemetry, centralized analytics, automatic
provider billing ingestion, live price fetching, automatic model selection,
parallel plan execution, or behavior changes to the implement-review-archive
control loop beyond recording telemetry around it.

## Suggested Manual Gates

Add `pause_before = true` to `define-plan-run-telemetry-schema` in the compiled
manifest because it introduces the proposed `plan-run-observability` capability.
Consider adding a manual gate before `estimate-stage-token-costs` if pricing
policy needs stakeholder approval before cost numbers appear in reports.
