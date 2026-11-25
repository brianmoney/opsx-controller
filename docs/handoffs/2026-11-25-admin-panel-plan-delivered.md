# Handoff — KF Admin Panel plan delivered (all four phases)

**Date:** 2026-11-25
**Status:** **Plan COMPLETE.** Phases 2, 3 and 4 are archived, committed and
handoff-ready. The plan has no remaining runnable node.
**Supersedes:** `2026-11-24-admin-panel-resume-H.md`,
`2026-11-24-admin-panel-orchestrator-thread.md`, and the phase-3-only close that
was superseded by the second amendment.

## 1. Final state

- Worktree: `/mnt/ssd/projects/kf-admin-panel/knowledge-forge-l0-integration`
- Branch: `integration/admin-panel-l0`, **HEAD `fed2eb9a`**, tracked tree clean
- Plan: `openspec/plans/admin-panel-plan.md`, SHA-256
  `b397e8b24dca21131d18f8f04524a8d72b1f2be8c0158cd48b8b026de4c4d93a`
  (62,311 B), `acceptance: phase`, **no `human_gates`**
- Ledger: `.opsx-run/20261125T133206Z-admin-panel-plan/`
- All 10 plan changes archived; donor `add-admin-panel-ui` neither archived nor
  applied; local commits only, nothing pushed

| Phase | Result |
|---|---|
| 2 — Read-Only Panel | 8/8 nodes archived, `handoff_ready: true` (final `3b1e56a9`) |
| 3 — Host Actions / `web/` retirement | `add-admin-panel-actions` at `7ac1a583` |
| 4 — Container Admin Role | `add-container-admin-role` at **`fed2eb9a`**, `handoff_ready: true`, `blockers: []` |

## 2. The second amendment — what changed and why

The operator's stated goal was to finish quickly because the project was
over-engineered. Rather than build the full container qualification, the plan
was amended at `5b2a48ba` to make phase 4 a **structural, compose-level
deliverable**.

Withdrawn from phase 4: the live `docker inspect` harness, the three compose
scenario gates, the admin-restart-vs-jobs-heartbeat check, start-to-ready
timing, the SSH-tunnel page exercise, the `/readyz` behavioural proof, and every
page-render-under-mounts check. Withdrawn prerequisites: the
`human_gates` entry and the `add-container-release-qualification` archive.

Phase 4 therefore ships the hash-pinned web closure, the `admin` entrypoint role
with its preflight and container-mode action refusal, the compose `admin`
service under `profiles: ["admin"]` with the full read-only isolation contract,
a fast structural test, and docs. Its eight success parameters are all
config-level and need no container.

**This is not a release qualification.** No release, cutover or container-parity
claim is made anywhere.

## 3. Verification (parent-verified, not taken on report)

- `tests/container/test_container_admin_role.py` + `test_container_compose_contract.py`: **53 passed**
- `tests/container tests/admin`: **622 passed / 497 subtests**
- `openspec validate --all --strict`: **55/0**; semantic duplicates **0**
- Archived `tasks.md`: **16 checked / 0 unchecked**
- Compose contract matches the amendment exactly (four `ro` mounts, no socket,
  eight-name env allowlist, `read_only`, `init`, bounded `tmpfs`, admin profile,
  loopback-only port)
- Lock pins match host: fastapi 0.135.3, uvicorn 0.42.0, jinja2 3.1.6,
  starlette 1.10.0, pydantic 2.14.4, pydantic_core 2.50.5, anyio 4.16.0,
  h11 0.16.0, markupsafe 3.0.4
- Withdrawn-feature leak check: clean — no `docker inspect`, SSH-tunnel,
  start-to-ready or `docker run` in the new test or docs

## 4. Known defect (recorded, not repaired)

The phase-4 stage writer emitted its final two ledger events (lines 49–50,
`archive_complete` and `record_complete`) **without a `seq` field**, so seq
numbering stops at 48 while the file has 50 events. The ledger is append-only,
so those bytes were left intact; their logical positions are recorded in a
correction event at seq 51. Content is otherwise well-formed and consistent with
independently verified facts. If you later add a schema check, this is the one
file that will trip it.

## 5. What is genuinely left

**Nothing in this plan.** The remaining work is outside its scope:

- `add-container-release-qualification` is still **active** (13/28 tasks, §6
  operator evidence 0/15). It is no longer a plan prerequisite, but any actual
  release of the container role still needs it.
- `AP-ACTIONS-04` (phase 3) — accepted advisory: unbounded in-memory action
  capture; persisted log already capped. Non-blocking follow-up.
- `scripts/admin_perf/handoff.py` — dead reference to the withdrawn evidence
  proof. No gate depends on it.
- Cost telemetry remains `not-reported` for the whole run; no `$0` is claimed.

## 6. Convergence-controls follow-ups (opsx-controller, unchanged)

`adaptive-plan-splitting/spec.md:25` "split-or-continue" wording;
`adapters/opencode/agents/opsx-review.md:712-721` `out_of_scope[]` example;
`core/plan-authoring.md` "3 named surfaces" vs ">=3" tension;
`tests/adapters/convergence_fixture.py:298` fixture flag.


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
