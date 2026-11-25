# Handoff — KF Admin Panel run closed through phase 3

**Date:** 2026-11-25
**Status:** Admin panel plan run **CLOSED** at `complete-through-phase-3`. Phase 4 deferred to a separate run.
**Supersedes:** `2026-11-24-admin-panel-resume-H.md` and §1–§11 of
`2026-11-24-admin-panel-orchestrator-thread.md`. Those documents are retained as
history but their next actions are void.

## 1. Disposition

The operator selected option C: conclude the run where it genuinely completed
and treat the container phase as its own later run.

| Phase | State |
|---|---|
| 1 — Archived Foundation | delivered historically (L0 `ec9475c4`); never a runnable node |
| 2 — Read-Only Panel | **complete**, 8/8 nodes archived, every handoff `ready` |
| 3 — Host Actions / `web/` retirement | **complete**, `handoff_ready: true`, commit `7ac1a583` |
| 4 — Container Admin Role | **not dispatched, not started**, deferred |

- Authority worktree: `/mnt/ssd/projects/kf-admin-panel/knowledge-forge-l0-integration`
- Branch: `integration/admin-panel-l0`; HEAD `7ac1a5832277dc418b315145c8bc6c7ecd2b594e`
- Plan: `openspec/plans/admin-panel-plan.md`, SHA-256
  `77e6e7bd59314006abf333dcc4aa84731573f10ce16f7e3031cd2d40fadc0245` (60,555 B),
  `acceptance: phase`, `human_gates: [add-container-admin-role]`
- Ledger: `.opsx-run/20261125T133206Z-admin-panel-plan/` — `events.jsonl` 34
  events (seq 32–34 are the close), `checkpoint.md`, `run-outcome.md`, `evidence/`
- Tracked tree clean. Local commits only; nothing pushed.

## 2. What changed since the 2026-11-24 handoffs

Both prior handoffs are **materially out of date**. Do not follow them.

- The cold-evidence blockage is **gone by withdrawal, not by fix**. Commit
  `feaae21e` (2026-11-25) withdrew `qualify-admin-panel-harness-proofs` and
  `qualify-admin-panel-cold-evidence`, dropped the `split_authorizations` entry,
  and replaced them with one behavior-only node,
  `finish-admin-panel-read-surfaces`. No evidence packet, manifest, hash
  binding, screenshot set, benchmark or traceability ledger is required anywhere
  in phases 2–3.
- Phase 2 therefore closed at 8 nodes, ending `3b1e56a9`.
- Phase 3 `add-admin-panel-actions` closed at `7ac1a583` (23 implementation
  paths, 70 `web/` deletions) after 3 review rounds, 1 corrective round,
  convergence 1/2, 0 critical findings.
- The old `20261123T160816Z` run-outcome's BLOCKED disposition is superseded.

## 3. The plan revision question — settled

The operator had edited the plan frontmatter to `human_gates: none` in the
integration worktree. That edit:

- was **never committed** and **never bound to a dispatch**;
- existed solely to force phase 4 into *this* run;
- was **reverted** at operator direction once option C was chosen.

Both revisions are preserved under the ledger's `evidence/` directory
(`d75252d0…` patch, `80843bff…` full working copy). The committed `77e6e7bd…`
revision is the sole authority again, so phase 4 keeps **both** its human gate
and its external prerequisite.

## 4. What blocks phase 4

Two independent blockers, neither waived, no `cap_override` used:

1. `add-container-release-qualification` is **not archived**. Active, 13/28
   tasks, §6 "Phase 6 implementation and operator evidence" **0/15**. §6
   includes `6.15 [operator]` — a clean-host, digest-bound release gate that the
   tasks file states is operator-run and never inferred from the repo suite.
   Cited at plan Phase 4 `Depends on:`/`Scope:`, Operator Decision 5, Suggested
   Manual Gates, Overall Completion Criteria, Recommended Sequence step 6.
2. The `add-container-admin-role` human gate was never released for a run. Clear
   it with `HUMAN_GATES: none` at invocation or a **committed** plan amendment —
   not an uncommitted working copy.

## 5. Next run (phase 4 only)

1. Archive `add-container-release-qualification`, including operator task 6.15.
2. Release the human gate as in §4.2.
3. Branch from `7ac1a583`; re-verify base and plan hash `77e6e7bd…`.
4. Dispatch `add-container-admin-role` at acceptance tier `phase`.

## 6. Carry-forward (not blocking phases 2–3)

- `scripts/admin_perf/handoff.py` — legacy evidence assembler still references
  the withdrawn cold/harness proof. Dead reference; no gate depends on it.
- `src/knowledge_forge/admin/actions.py` — `AP-ACTIONS-04` accepted advisory:
  unbounded in-memory action capture (persisted log already capped).
- `add-container-release-qualification` §6 — blocks phase 4.

## 7. Operator observations recorded

- "I already ran the admin panel and took a look at it. It looks good." —
  recorded as **host-panel operator experience only**. It is not a measured
  result, not phase-4 evidence, not a release-qualification claim, and not
  container parity. Phase 4 remains unqualified.
- Cost telemetry remains `not-reported` for the whole run; the authored
  `add-cost-reprice-option` change in opsx-controller is the related follow-up.
  No `$0` total was or should be claimed.

## 8. Convergence-controls follow-ups still open (opsx-controller)

Unchanged by this run: `adaptive-plan-splitting/spec.md:25` "split-or-continue"
wording; `adapters/opencode/agents/opsx-review.md:712-721` `out_of_scope[]`
example; `core/plan-authoring.md` "3 named surfaces" vs ">=3" tension;
`tests/adapters/convergence_fixture.py:298` fixture flag.
