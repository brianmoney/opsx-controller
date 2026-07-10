---
title: OpenCode Plugin Token Usage Plan
doc_type: implementation-plan
status: proposed
updated: 2026-07-09
---

# OpenCode Plugin Token Usage Plan

## Purpose

Capture reliable per-stage token usage for `opsx-plan` runs by using an
OpenCode plugin that emits machine-readable usage data while OpenCode observes
model events. This avoids scraping aggregate `opencode stats`, reverse
engineering OpenCode DB tables, or guessing usage from logs.

The plan separates contract, plugin emission, and orchestrator consumption so
each change stays reviewable and quota-bounded.

## Capability Ownership

This extends `plan-run-observability`, because it improves the telemetry usage
source that already feeds cost estimation, reports, and dashboards.

## Phase 1: Usage Contract

### Change: `define-opencode-plugin-usage-contract`

**Purpose:** Define the sidecar file contract and execution context used to join
OpenCode plugin token events back to exact `opsx-plan` stages.

**Depends on:** None.

**Capability:** `plan-run-observability`.

**Scope:** Add OpenSpec requirements for an OpenCode usage sidecar emitted per
stage. Define the environment variables passed by `opsx-plan`, including plan
name, run id, change id, stage, round, and usage sidecar path. Define normalized
usage fields, event types, final-vs-incremental semantics, malformed-record
handling, timeout behavior, and precedence relative to existing worker JSON and
log metadata usage extraction.

**Out of scope:** Implementing the plugin, changing `opsx-plan`, or backfilling
historical telemetry.

**Success parameters:** Strict OpenSpec validation passes; the contract explains
how `usage_source = "opencode_plugin"` is populated; timeout and ambiguous data
cases preserve unavailable usage rather than fabricating token counts.

## Phase 2: Plugin Emission

### Change: `add-opencode-usage-emitter-plugin`

**Purpose:** Add an OpenCode plugin that emits raw token usage events to the
stage sidecar file.

**Depends on:** `define-opencode-plugin-usage-contract`.

**Capability:** `plan-run-observability`.

**Scope:** Add a minimal OpenCode plugin under the OpenCode adapter that listens
to token-bearing events such as `message.updated` and final turn/session events
such as `session.idle`. When `OPSX_USAGE_PATH` is set, append JSONL records with
model identity, input tokens, output tokens, cached input tokens, reasoning
tokens, total tokens, request count, latency when available, and event
timestamp. Update the OpenCode installer to deploy the plugin and verify its
presence.

**Out of scope:** Orchestrator telemetry consumption, report changes, dashboard
changes, or DB/export scraping.

**Success parameters:** The plugin is inert outside `opsx-plan` stage runs; it
writes valid JSONL when stage environment variables are present; installer
verification confirms plugin deployment; tests or fixture validation cover final
and incremental event shapes.

## Phase 3: Orchestrator Consumption

### Change: `consume-opencode-usage-sidecar`

**Purpose:** Populate `opsx-plan` telemetry usage and cost from the OpenCode
plugin sidecar.

**Depends on:** `add-opencode-usage-emitter-plugin`.

**Capability:** `plan-run-observability`.

**Scope:** Extend `orchestrator/opsx-plan.py` to create a per-stage usage
sidecar path, pass stage context environment variables to the OpenCode
subprocess, read the sidecar after stage completion, normalize the best
available final or latest usage event, set `usage_source = "opencode_plugin"`,
and run existing cost estimation. Preserve existing precedence: worker JSON
usage first, recognized log metadata second, plugin sidecar third, unavailable
usage last. Add tests for valid sidecar, missing sidecar, malformed sidecar,
timeout with incremental usage, and cost estimation after plugin usage.

**Out of scope:** Historical telemetry backfill, hosted telemetry, provider API
calls, or changing model selection policy.

**Success parameters:** New `opsx-plan` runs populate token fields when the
plugin emits usage; missing or malformed sidecars do not fail plan execution;
cost estimates become available when usage and catalog pricing are present;
OpenCode adapter install verification passes.

## Recommended Execution

Implement all three changes sequentially. Pause after the contract change if the
sidecar schema or hook assumptions need operator review before plugin work.
