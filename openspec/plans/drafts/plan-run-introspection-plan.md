---
title: Plan-Run Introspection and Truthful Status
doc_type: implementation-plan
status: proposed
updated: 2026-09-17
---

# Plan-Run Introspection and Truthful Status

## Purpose

Improve operator introspection for `opsx-plan` runs. This plan was authored
from defects and gaps observed while operating the live
`durable-plan-supervision` run on 2026-09-17:

1. `opsx-plan status` reconciles and persists state on every invocation
   (`lib/orchestrator/cmd_status.py:44-45`). Invoked while that run was live,
   it rewrote plan state and labeled a demonstrably running change "recovered
   from interrupted run".
2. There is no machine-readable per-round history: answering "how did round N
   go" requires manual parsing of `.opsx-plan/<plan>.state.json` and
   ANSI-heavy stage logs.
3. Stage logs cannot be queried for the final worker JSON envelope, although
   the entrypoint already contains the parsing helpers
   (`orchestrator/opsx-plan.py:976`, `:1015`).
4. Supervised incident/action journal data has no cross-job, filterable query
   surface outside per-job `supervise inspect`.

All changes extend existing capabilities (`plan-operator-cli`,
`plan-run-observability`, `durable-plan-supervision`); no new capability
directories are proposed, so no `Capability Ownership` section appears. This
document is a draft under `openspec/plans/drafts/` with no TOML manifest; it
must be compile-tested before any unattended run.

## Phase 1: Truthful Status

### Change: `fix-status-read-only-reconciliation`

**Purpose:** Stop `opsx-plan status` from rewriting plan state while another
process holds the execution lock, and surface live in-flight activity
(holder, stage, round, model, elapsed) instead of a stale interruption label.

**Depends on:** None.

**Capabilities:** `plan-operator-cli`, `plan-run-observability`.

**Scope:**

- When a live execution-lock holder exists (`.opsx-plan/execution-lock.json`
  plus `holder_is_live`, `lib/supervisor/lock.py:167`, schema `:442-454`),
  `status` must not persist reconciliation output. It displays the persisted
  state plus a live overlay and keeps today's exit-code semantics.
- A change whose worker snapshot
  `.opsx-plan/workers/<plan>/<change>.json` says `status: running` (fields at
  `orchestrator/opsx-plan.py:647-671`) must never be relabeled "recovered
  from interrupted run"; the live stage, round, model, and elapsed time are
  shown instead.
- Add `--no-reconcile` as an explicit display-only mode that performs no
  writes even when no live holder exists.
- Add `--json` with a documented, stable status field model (run holder,
  per-change phase/round/state, live marker).
- Regression test: with a simulated live lock holder and a running worker
  snapshot, `cmd_status` leaves `.opsx-plan/<plan>.state.json` byte-identical
  and prints the live marker.

**Out of scope:** Changing reconcile-on-status behavior when no live holder
exists; the escalation trigger semantics (round arithmetic versus failed-review
count); repairing the run that produced the observation; any change to `run`,
`approve`, `accept`, or archive behavior.

**Success parameters:**

- `python3 -m unittest tests.orchestrator.test_cmd_status
  tests.orchestrator.test_opsx_plan` passes, including the new regression test
  asserting no state write under a live holder.
- `opsx-plan status --json` on `durable-plan-supervision` emits the documented
  fields, and tests/orchestrator/test_command_golden.py pins the human output.
- `python3 -m unittest discover -t . -s tests` and
  `node tests/opencode/test-opsx-usage-emitter.js` pass.

## Phase 2: Machine-Readable Introspection

### Change: `add-run-history-and-log-inspection`

**Purpose:** Answer "what happened in each round" and "what did the worker
actually return" with stable, machine-readable output instead of hand-parsing
state and ANSI logs.

**Depends on:** None. This change is independent of the status fix above and
may proceed in parallel with it; sequencing it second is a navigation
preference, not a dependency.

**Capabilities:** `plan-operator-cli`, `plan-run-observability`.

**Scope:**

- `opsx-plan history <change> [--json]`: read the plan state's per-change
  `history` entries and `last_review`
  (`lib/orchestrator/state.py:30-36`, `:95-123`) and emit them with the
  current phase, round, and escalation state. Human output summarizes one
  line per entry; `--json` emits the documented field model.
- `opsx-plan logs --last-envelope [--change C] [--stage S]`: extract the final
  worker JSON envelope from the selected stage log by wiring the existing
  `_find_last_envelope` / `parse_stage_json` helpers
  (`orchestrator/opsx-plan.py:976`, `:1015`) into `cmd_logs`
  (`lib/orchestrator/cmd_logs.py:19`); exit nonzero with a named diagnostic
  when no envelope exists.
- Document both field models next to the command reference; the schema is the
  interface contract, so no separate docs-only change.
- Extend tests/orchestrator/test_opsx_plan.py (envelope parsing) and
  tests/orchestrator/test_cmd_logs.py / test_logs.py (selection, flag).

**Out of scope:** Status reconcile behavior (Phase 1); incidents and actions
(Phase 3); telemetry, cost, report, and dashboard surfaces; any modification
of log content.

**Success parameters:**

- `opsx-plan history add-bounded-incident-recovery --json` on
  `durable-plan-supervision` returns the recorded round history (rounds 1-2,
  including the failed round-2 review findings) with phase/status/summary.
- `opsx-plan logs --change add-bounded-incident-recovery --stage review
  --last-envelope` returns the round-2 review envelope, or exits nonzero with
  a named reason; unit-tested against fixture logs.
- `python3 -m unittest discover -t . -s tests` and
  `node tests/opencode/test-opsx-usage-emitter.js` pass.

## Phase 3: Contingent Incident Query

### Change: `add-incident-journal-query`

**Purpose:** Give operators a cross-job, filterable, read-only query over
supervised incidents, their bounded attempts, and linked journal actions.

**Depends on:** None mechanically in this document. This change is deferred
and contingent: it is enabled only if the archived results of the
durable-plan-supervision plan leave the gaps recorded in the Contingency
Analysis section below. Its prerequisites live in a different plan, so the
operator resolves the contingency manually and records that decision here
before enabling the change.

**Capabilities:** `durable-plan-supervision`, `plan-operator-cli`.

**Scope:** See the Contingency Analysis section; the final scope is the
residual gap only, fixed when the contingency resolves. Intended shape:
extend the existing `opsx-plan supervise` namespace with an `incidents`
query that is cross-job by default and filters by state, class, signature,
run id, attempt count, and age, with `--json` output; incident detail
including signature, run id, `updated_at`, attempt count, and the linked
`remedy_choice` action/evidence; plus an actions/evidence view that wires
`query_actions_by_run` (`lib/supervisor/ledger.py:2070`). The query is
strictly read-only against the ledger.

**Out of scope:** Duplicating the `opsx-plan status`/`report`/dashboard
incident projections owned by `add-supervision-observability`; ledger schema
changes unless the contingency analysis proves a missing field; any
remediation, transition, or mutation verb; new capabilities.

**Success parameters:** Pending contingency resolution. When enabled:
`opsx-plan supervise incidents --json` lists incidents across jobs with the
documented fields; filters and detail are covered by tests under
tests/supervisor/; a ledger-hash assertion proves the query mutates nothing;
both test suites pass.

## Contingency Analysis: incident query vs durable-plan-supervision

Documentation only; no compile directive. This section records what
`add-incident-journal-query` needs and compares it against what the
durable-plan-supervision plan creates.

### What the query needs (the spec)

| # | Operator need | Input required |
|---|---|---|
| R1 | List incidents across all jobs, not one job at a time | incident rows without a `job_id` filter |
| R2 | Filter by state (`open`/`recovering`/`resolved`/`escalated`) | `incidents.state` |
| R3 | Filter by failure class | `incidents.kind` |
| R4 | Look up by stable identity/signature | `incidents.signature`, `incident_attempts.signature` |
| R5 | See bounded attempt counts and first/last seen | `incident_attempts(attempt_count, first_seen_at, last_seen_at)` |
| R6 | Follow run/job linkage | `incidents.run_id`, `incidents.job_id` |
| R7 | See the chosen remedy and its evidence | journaled action evidence `remedy_choice` |
| R8 | Query journal actions by run/state and browse evidence | actions/evidence rows |
| R9 | Machine-readable output with a documented field model | stable incident-centric JSON schema |
| R10 | Read-only guarantee | no ledger writes on query |

### What durable-plan-supervision creates

| Delivered by | Surface | Status and evidence |
|---|---|---|
| `add-bounded-incident-recovery` (in flight when this draft was written) | incidents table v8 with `signature`; states and guarded transitions; `record_incident` / `transition_incident` / `get_incident` / `list_incidents`; `incident_attempts` keyed by signature; `remedy_choice` evidence; signature queryable after reopen | `lib/supervisor/ledger.py:80-87`, `:302-315`, `:449-457`, `:663-688`, `:1606`, `:1701`, `:1709`, `:1928-1970`; `lib/supervisor/recovery.py:204-207`, `:847-878`; change delta spec `openspec/changes/add-bounded-incident-recovery/specs/durable-plan-supervision/spec.md:3-38` |
| `add-action-journal-dispatch` (done) | journaled actions and evidence; `list_actions`, `query_actions_by_run`, `list_evidence`, `list_uncertain_actions` | `lib/supervisor/ledger.py:2063`, `:2070`, `:2171`, `:2208` |
| `add-supervision-observability` (pending; plan-doc section only, no change directory yet) | Extend `opsx-plan status` / `report` / dashboard with job progress, action and incident views, evidence state, usage, waits, budget limits, operator steering; document JSON field models | `openspec/plans/durable-plan-supervision-plan.md:756-795` |
| `plan-operator-cli` capability (implemented) | `opsx-plan supervise inspect [--job-id] [--json]`: per-job recent-10 actions and incidents | `lib/orchestrator/cmd_supervise.py:469-558`; `openspec/specs/plan-operator-cli/spec.md:981-985`, `:1018-1019` |
| `plan-run-observability` capability (implemented) | core metrics in report; supervisor leaderboard exclusion | `lib/orchestrator/report.py:345`; `openspec/specs/plan-run-observability/spec.md:1596-1656` |

### Residual gap after observability lands

If `add-supervision-observability` ships as scoped, it owns the
status/report/dashboard incident projection. It does not by itself promise:
cross-job listing (R1), the filters (R2-R5), incident detail including
signature / run id / attempts / remedy linkage (R4-R7), an actions/evidence
query surface (R8), or an incident-centric JSON model (R9). That residual set
is the intended scope of `add-incident-journal-query`.

### Enablement decision rule

After the durable-plan-supervision run archives:

1. Inspect the delivered `opsx-plan status` / `report --json` incident output
   from `add-supervision-observability`.
2. If it already provides cross-job, filtered incident/action queries in
   machine-readable form, drop this change or reduce it to the specific
   missing filters.
3. Otherwise enable this change with the residual scope above and record the
   decision and date in this document.

## Recommended Sequence

1. `fix-status-read-only-reconciliation` — data-integrity defect first; it is
   the one that produced misleading state.
2. `add-run-history-and-log-inspection` — independent of the first change and
   parallelizable; sequenced second only so status semantics stabilize first.
3. `add-incident-journal-query` — deferred; gated on the enablement decision
   rule after durable-plan-supervision archives.

## Overall Completion Criteria

- Phase 1 and Phase 2 changes are archived with both test suites green.
- `opsx-plan status` no longer writes state while a live lock holder exists,
  proven by the regression test.
- `opsx-plan history <change> --json` and `opsx-plan logs --last-envelope`
  are documented and covered by tests.
- The Phase 3 contingency is explicitly resolved: the change is either
  enabled with the residual scope above or dropped/reduced, with the decision
  recorded in this document.
- `opsx-plan compile openspec/plans/drafts/plan-run-introspection-plan.md -o
  <tmp>.toml --force` succeeds once the draft is promoted out of `drafts/`.

## Explicit Non-Goals

- The escalation trigger semantics (`(round - 1) >= escalate_after_review_fails`
  counts rounds, not failed reviews) — tracked separately.
- Telemetry, cost, and leaderboard changes.
- Any mutating supervision action (pause, retry, cancel, approve) from the
  new query surfaces.
- Adapter-specific surfaces (claude-code, codex-cli, dsh) beyond what the
  shared `opsx-plan` CLI already provides.

## Suggested Manual Gates

- `fix-status-read-only-reconciliation`: suggested `pause_before = true` — it
  changes the mutation semantics of a public command contract; the operator
  confirms the live-holder rule and the `--no-reconcile`/`--json` interface
  before it runs.
- `add-run-history-and-log-inspection`: no gate; read-only additions.
- `add-incident-journal-query`: no gate, but its `deferred` state already
  requires an explicit operator enablement decision.
