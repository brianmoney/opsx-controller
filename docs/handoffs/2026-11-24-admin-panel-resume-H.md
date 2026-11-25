> **SUPERSEDED 2026-11-25 — do not follow this document's resume steps.**
> The admin panel run has since been **closed at `complete-through-phase-3`**.
> Phase 2 was re-planned (the harness/cold evidence children were withdrawn by
> commit `feaae21e`) and phase 3 `add-admin-panel-actions` landed at `7ac1a583`.
> See `2026-11-25-admin-panel-run-closed-phase3.md` for current state and the
> phase-4 launch conditions.

# Handoff — Resume KF Admin Panel run (fresh orchestrator session)

**Date:** 2026-11-24
**Status:** Ready for launch. The convergence-controls policy is merged (PR #44) and installed (`install.sh --global --verify` run 2026-11-24; installed `opsx-orchestrator` carries the split-or-re-scope policy).
**Purpose:** Launch context for a FRESH `opsx-orchestrator` session resuming the interrupted admin-panel plan run. This document supersedes the resume instructions in `2026-11-24-admin-panel-orchestrator-thread.md`, which remains the full run history.

## 1. Do not resume old sessions or worktrees

- Do **not** resume `ses_df9a0672affeSJtn36Ex0ivmiG` (old run) or `ses_df6dc2dacffeDSM9lT15QJYLJI` (prior orchestrator session, since repurposed for controller work).
- Do **not** dispatch `correct-7`.
- Do **not** use the original dirty `plan/admin` worktree.

## 2. Where the run lives

- Worktree: `/mnt/ssd/projects/kf-admin-panel/knowledge-forge-l0-integration`
- Branch: `integration/admin-panel-l0`; HEAD: `8e8468bc` ("plan(admin-panel): pre-authorize qualify-admin-panel-read-ui split")
- Plan: `openspec/plans/admin-panel-plan.md`
- Plan sha256: `b1bf41d3677c8945a0afdd82fd313990ef93235a9202b2379ec5530268c809ac` — **supersedes** `7b5c031c…`, which the run ledger still binds. Re-bind the new revision at resume (record amendment commit `8e8468bc` and the new hash).
- Run ledger: `.opsx-run/20261123T160816Z-admin-panel-plan/` — `events.jsonl` is strictly append-only (append content events, then a `ledger_verified` event recording the pre-append prefix hash; never rewrite the prefix); `checkpoint.md` is the mutable state doc.

## 3. Run state

- Phase 2: **7 of 8 slices archived** — A `a81af512`, B `2201c731`, C `2a9f567a`, E `239ea034`, G `1491adf6`, I `5e98bfb5`, D `9b2aefd5` + evidence repair `221e5ec5`; every handoff passed.
- Next: **H = `qualify-admin-panel-read-ui`** (selected, never started; its change artifacts still need authoring via `opsx-change-writer` before implementation).
- Then phase 3: `add-admin-panel-actions`.
- **Stop** at the phase-4 human gate: `add-container-admin-role` (the plan's only `human_gates` entry; also requires `add-container-release-qualification` archived — remaining plan work).
- L0 recovery is complete; see the thread handoff §1.

## 4. Policy in force (convergence controls, installed)

- **Two-round corrective budget.** A corrective round = one corrective implementation dispatch + its verifying review. After the second failing review: no third round (refused even as `cap_override`), no operator convergence question, no stop on round count.
- **Finding classification before any round:** only blocking `introduced_by_diff` findings earn a corrective round; `adjacent_scope` → proposed slice/follow-up; `pre_existing` → carry-forward; `unknown` is adjudicated from evidence first.
- **After two rounds: split-or-re-scope.** H carries an exact `split_authorizations` entry (children `qualify-admin-panel-harness-proofs` then `qualify-admin-panel-cold-evidence`, obligation map and aggregate exit in the plan frontmatter). Splits execute through `opsx-split-materializer` only. Children inherit consumed corrective rounds by blocker class — a split cannot reset the budget.
- **Precedence:** controller policy is the floor; a plan may tighten, never loosen. The plan's "one bounded correction" checkpoint prose is void where it conflicts with the two-round budget. The plan's operator-approval reservation for unnamed splits (its "Approve any split not named…" operator hook) stands — but H's entry is named and exact, so selecting it needs no further approval.
- **Plan fencing for H:** no split may weaken the phase-2 cold proof or turn a failed/missing proof into a follow-up.
- Local commits only: no push, PR, merge, or remote branch. Operator attestation is authoritative for what it names. A stale idle marker does not interrupt a live child. Bounded bookkeeping (one ledger-writer dispatch per stage). Per-stage token/cost recording with `cost_status` — a bare `$0` is forbidden while any stage is unpriced.

## 5. Resume checklist for the fresh session

1. Verify the worktree: branch `integration/admin-panel-l0`, HEAD `8e8468bc`, clean tree.
2. Ledger: append a resume event binding the plan revision (`8e8468bc`, sha256 `b1bf41d3…`), then a `ledger_verified` event.
3. Select H; record the sizing assessment against the maximum-surface heuristic; author H's change artifacts via `opsx-change-writer` if absent; then implement.
4. After archive: Phase Transition Gate with a fresh `opsx-integration-auditor`; advance only on `handoff_ready: true`; `opsx-repo-hygiene` only for the repairs it names.
5. Then phase 3; stop at the phase-4 human gate.

## 6. Open follow-ups (non-blocking)

- **Cost telemetry reports `$0`:** the custom usage plugin works in opencode v1 but not v2 (sessions run in `opencode2`) — investigation pending; the authored `add-cost-reprice-option` change exists in opsx-controller and is related.
- `fix-mixed-model-leaderboard-attribution`: parked in opsx-controller, deliberately deferred ("triage the orchestrator first").
- D's 5-dispatch handoff overrun was recorded as reserve shortfall/closed-overrun (`events.jsonl` seq 76/78), never labeled `cap_override` — bookkeeping-schema gap.
- Convergence-ship follow-ups in opsx-controller: `openspec/specs/adaptive-plan-splitting/spec.md:25` retains "split-or-continue" wording (decide retain vs align); `adapters/opencode/agents/opsx-review.md:712-721` `out_of_scope[]` example pins `adjacent_scope: true` (cosmetic); `core/plan-authoring.md` "3 named surfaces" vs ">=3" table tension; `tests/adapters/convergence_fixture.py:298` contradictory-but-harmless fixture flag.

## 7. Key references

- Full run history and decision trail: `docs/handoffs/2026-11-24-admin-panel-orchestrator-thread.md` (this repo).
- Convergence controls: merged PR #44 (implementation `521fccc`, archive `ede3608`); adaptive plan splitting: merged PR #43.
- H split design rationale: obligation map covers the full 21-obligation set including the five the plan's "Owns" sentence omits (R2/R8/R11 unions, 8.2 union, C7-F1); see this repo's thread handoff §10.
