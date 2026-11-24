# Handoff — KF Admin Panel orchestrator thread

**Date:** 2026-11-24  
**Status:** Thread moved here from the KF Admin Panel session. The admin-panel
orchestrator run is intentionally interrupted after Slice D landed and before
Slice H started. `opsx-controller` contains the follow-up change for the
orchestrator/sub-agent corrections.

This document is the context handoff for continuing the discussion in
`opsx-controller`. It is not an OpenSpec change artifact.

## 1. Where the work lives

### Admin plan run

- Repository/worktree: `/mnt/ssd/projects/kf-admin-panel/knowledge-forge-l0-integration`
- Branch: `integration/admin-panel-l0`
- Plan: `openspec/plans/admin-panel-plan.md`
- Committed plan SHA-256: `7b5c031c43d21105b73b39bb1e8eec686b424f01727ad31ad7962746e674256d`
- Current HEAD: `221e5ec5` (D evidence repair)
- Run ledger: `.opsx-run/20261123T160816Z-admin-panel-plan/`
- Orchestrator session: `ses_df6dc2dacffeDSM9lT15QJYLJI` — “OpenCode orchestrator: KF Admin Panel plan run”
- The run was interrupted at 14:32Z on 2026-11-24 after the D handoff passed.
- Do **not** use the original dirty `plan/admin-panel` worktree for this run.
- Do **not** resume the older `ses_df9a0672affeSJtn36Ex0ivmiG` run or dispatch
  `correct-7`.

### L0 state (complete)

- L0 plan acceptance commit: `ec9475c4`
- Superseded `add-admin-panel-ui` retirement commit: `dee6ab0b`
- Active-authority banner commit: `0a81b254`
- L0 ledger: `.opsx-run/20261123T145936Z-admin-panel-recovery/`
- Completion record: `L0-COMPLETE.md` in that ledger
- Strict validation: 55/0
- `add-admin-panel-ui` is retired as superseded, **not archived**; its delta was
  not applied.
- Canonical specs unchanged; no product/deploy diff at L0.

### Opsx-controller

- Repository: `/home/brian/code/opsx-controller`
- Current `main`: `95b83e9` (merge of `add-autonomous-plan-continuation`)
- Merged changes:
  - `add-plan-handoff-gate` (#41)
  - `add-autonomous-plan-continuation` (#42), installed
- Authored, not implemented:
  - `openspec/changes/add-plan-convergence-controls/`
- In-flight, uncommitted, not authored by the convergence change:
  - `openspec/changes/add-adaptive-plan-splitting/`
  - Agent/spec/source edits supporting it
- The two changes conflict on the post-two-round “continue” rule; see §5.

## 2. Current admin plan state

Phase 2 has **seven of eight slices archived**:

| Slice | Change | State |
|---|---|---|
| A | `add-admin-read-cache-contract` | archived `a81af512`; handoff passed |
| B | `add-admin-review-nudge-reads` | archived `2201c731`; handoff passed |
| C | `add-admin-panel-shell-overview` | archived `2a9f567a`; handoff passed |
| E | `add-admin-scoped-search` | archived `239ea034`; handoff passed |
| G | `add-admin-scoped-catalog` | archived `1491adf6`; handoff passed |
| I | `add-admin-drive-catalog` | archived `5e98bfb5`; handoff passed |
| D | `add-admin-operations-pages` | archived `9b2aefd5`; evidence repair `221e5ec5`; handoff READY |
| H | `qualify-admin-panel-read-ui` | **selected, not started** |
| Phase 3 | `add-admin-panel-actions` | not started |
| Phase 4 | `add-container-admin-role` | human gate; release qualification still active |

D closed with:

- archive `9b2aefd5` — 5 added + 1 modified requirement
- evidence repair `221e5ec5` — additive superseding index only
- final audit: `handoff_ready: true`, 9/10 checks passed, two historical
  warning-only blockers on archived authoring bytes
- D handoff reserve was overrun: 5 dispatches versus the documented 3
- worktree clean at `221e5ec5`; H selected but not started

H is the phase-2 qualification slice: cold first-request dashboard with unchanged
production warmup, warm/default path, overview p95, 16 screenshots, exact
eight-page inventory, union route/no-process/no-write checks, one-process cache
union, cross-consumer C7-F1 verification, and C7-F4/F5 closure. It has not
started.

## 3. Decisions already made

The operator’s operating model for plan runs:

- Orchestrator drives plan slices end-to-end without asking for internal repo,
  evidence, commit, or intake decisions.
- The only human gate in this plan is `add-container-admin-role` (phase 4). It
  also depends on `add-container-release-qualification` being archived.
- Local commits only; the orchestrator may amend its own commits; no push, PR,
  merge, or remote branch.
- Post-archive findings are carry-forward items. Do not reopen an archived
  change or insert an unplanned follow-up change mid-plan.
- Handoff gate runs after every archived change and loops until
  `handoff_ready: true`.
- `MAX_DISPATCHES` and repair/audit round caps are advisory.
- Explicit operator attestation is authoritative for the state it names; do not
  re-derive it.
- A stale `idle_outcome: interrupted` marker is not a current interruption when
  a live child is working.
- The goal is overnight unattended operation: no hourly interaction, no
  convergence questions, no internal-policy stops.

The L0 plan has:

```yaml
status: accepted
acceptance: phase
human_gates:
  - add-container-admin-role
```

## 4. What `opsx-controller` already contains

### `add-plan-handoff-gate` (merged #41)

- Mandatory Phase Transition Gate after archive.
- `opsx-integration-auditor` read-only audit.
- `opsx-repo-hygiene` scoped repair.
- Archiver implementation-path scope; archive-only is not handoff-ready.
- `handoff_ready` in the ledger and final report.
- Handoff reserve.

### `add-autonomous-plan-continuation` (merged #42, installed)

- Orchestrator autonomy for orchestrator-owned changes.
- Local commits; may amend its own commits; no push/PR.
- Advisory caps with recorded justification.
- Post-archive findings carry forward.
- `human_gates` resolution from plan frontmatter or `HUMAN_GATES`.
- Evidence repair is repairable/unqualified, not an operator question.
- Reviewer adversarial coverage matrix.
- Change-writer `observed`/`required` carry-forward fields.

### `add-plan-convergence-controls` (authored, valid, not implemented)

Path: `openspec/changes/add-plan-convergence-controls/`

- 16 requirements: 2 modified, 14 added
- 56 tasks, all unchecked
- `openspec validate add-plan-convergence-controls --strict` → valid
- `openspec validate --all --strict` → 30/30

It corrects the observed convergence failure:

- two corrective rounds default; after that, **autonomous split or re-scope**,
  never an operator question;
- finding classification: `introduced_by_diff`, `adjacent_scope`,
  `pre_existing`; only the first earns a corrective round;
- broad first review triggers a split checkpoint before correction;
- change-writer one-concern sizing and maximum-surface heuristic;
- reviewer full-scope first review, no adjacent-scope creep;
- implementer high-risk correctness path first;
- bounded ledger/bookkeeping/evidence ceremony;
- operator attestation authoritative;
- per-stage token/cost burn reporting;
- stale idle-marker handling.

The earlier draft incorrectly included “continue only under explicit operator
authorization.” That has been removed. The corrected rule is: **split or
re-scope autonomously and continue; never ask for convergence authorization.**
If no split/re-scope is permitted by the plan, narrow and carry the remainder
forward; still continue with independent work.

## 5. Open conflict to resolve

The worktree already contains an uncommitted `add-adaptive-plan-splitting`
change. It permits one bounded “continue” corrective round after two rounds
without a split. `add-plan-convergence-controls` replaces that with autonomous
split/re-scope and no operator-authorization path.

Both cannot be normative unchanged. `add-plan-convergence-controls` records the
precedence and requires the adaptive-splitting “continue” wording to be
reconciled during implementation. Decide before implementing/running either
change.

## 6. What the run taught us

- The slice split fixed the monolithic deadlock: seven slices archived, no
  abandoned work, every handoff green.
- It did **not** fix convergence cost. Six non-trivial slices used 26 reviewer
  sessions and 29 implementer sessions; five of six needed 3–5 review cycles and
  cap overrides. Only Slice C was clean on first review.
- The run spent roughly 16.5M input tokens, 1.76M output tokens, and 600M
  cache-read; cost telemetry reports `$0`.
- Seventeen `coder` bookkeeping sessions were overhead; one bounded ledger
  writer per stage is enough.
- D was the largest remaining slice (five pages). It landed after two review
  rounds, one corrective cycle, and an evidence-repair handoff overrun. It is a
  concrete example of why the convergence controls matter.
- The parent session’s `idle_outcome: interrupted` stayed stale after the
  operator continued; the live child was the source of truth.
- L0 initially deadlocked because no configured role could commit the plan
  replacement and retire the active proposal; the operator-authorized committer
  applied the two prepared patches manually. A future plan-committer/L0 role
  remains a design gap.

## 7. Next actions

1. **Decide H.** The run is interrupted with H selected but not started. Either
   resume H as-is, or implement/install `add-plan-convergence-controls` first so
   H runs under the autonomous split/re-scope rule.
2. **Resolve `add-adaptive-plan-splitting` vs `add-plan-convergence-controls`.**
   Do not run both unchanged.
3. **Resume H** in the integration worktree only:
   - worktree: `/mnt/ssd/projects/kf-admin-panel/knowledge-forge-l0-integration`
   - branch: `integration/admin-panel-l0`
   - plan hash: `7b5c031c…`
   - H is `qualify-admin-panel-read-ui`
4. After H: phase 3 `add-admin-panel-actions`, then stop at the phase-4 human
   gate `add-container-admin-role`.
5. Keep local-only commits; do not push/PR.
6. If H repeats the 4–5-cycle pattern, split it using the plan’s split
   checkpoints under the new convergence policy rather than granting another
   override.
7. Add real cost/token reporting before the next long run.

## 8. Key references

### Admin plan

- Plan: `openspec/plans/admin-panel-plan.md`
- Run ledger: `.opsx-run/20261123T160816Z-admin-panel-plan/`
- D archive: `9b2aefd5`; D evidence repair: `221e5ec5`
- H is the next slice; phase 3 and phase 4 remain.

### Opsx-controller changes

- `openspec/changes/add-plan-handoff-gate/` (archived/merged)
- `openspec/changes/add-autonomous-plan-continuation/` (archived/merged)
- `openspec/changes/add-plan-convergence-controls/` (authored, valid)
- `openspec/changes/add-adaptive-plan-splitting/` (in-flight, uncommitted)

### Earlier analysis artifacts

- `/tmp/opencode/admin-phase1-review/REPORT.md`
- `/tmp/opencode/admin-change-sizing/handoff-analysis.md`
- `/tmp/opencode/verify-autonomous-plan-continuation.md`
- `/tmp/opencode/verify-autonomous-fixes.md`

## 9. One-line state

**Seven of eight phase-2 slices are archived; D landed and its handoff is ready;
the run is intentionally stopped before H; the next decision is whether to
implement the convergence controls before resuming H, and the immediate open
conflict is `add-plan-convergence-controls` versus `add-adaptive-plan-splitting`.**
