# Proposal: add-plan-convergence-controls

## Why

The plan-level run on the KF Admin Panel proved that the current autonomy
contract can drive changes to archive, but not that it converges. In phase 2,
6 of 8 slices archived in about 18.5 hours. Only one slice (C) was clean on its
first review. Five of the six non-trivial slices needed 4–5 review cycles and a
cap override to finish:

| Observed signal | Value |
|---|---|
| Phase-2 slices archived | 6 of 8, in ~18.5 hours |
| Slices clean on first review | 1 (slice C) |
| Non-trivial slices needing 4–5 cycles | 5 of 6 |
| Reviewer sessions | 26 |
| Implementer sessions | 29 |
| `coder` bookkeeping sessions | 17 |
| Child sessions overall | ~107 |
| Input / output / cache-read tokens | ~16.5M / ~1.76M / ~600M |
| Cost telemetry reported | `$0` |

The current D slice (five pages) is repeating the pattern: broad implementation,
review, corrective round. The run is not failing because the workers are
incompetent; it is failing because the contract rewards adding another round over
deciding that the slice is the wrong unit.

Five defects compound:

1. **Corrective rounds have no default ceiling.** The prior change made every
   cap advisory, so the two-round review allowance can be exceeded with a
   `cap_override`. A cap override names remaining work, not whether the work
   belongs in this change. Four or five rounds become the cheapest path.
2. **Findings are not classified.** A finding introduced by the diff, a concern
   adjacent to the accepted scope, and a pre-existing defect all earn the same
   response: another implementation round. Adjacent concerns therefore arrive
   one per round, and the review never converges on the slice.
3. **Re-reviews are narrow.** The first review establishes a subset of the
   accepted surface; later reviews discover one more concern each time. The
   reviewer's cycle discipline explicitly permits a narrow first review, so
   serial discovery is contract-compliant.
4. **Slices are oversized and the split checkpoint is late.** The plan-authoring
   guidance says "one concern" but gives no maximum surface. A change spanning
   an API contract, a CLI, a page, freshness semantics, and an error taxonomy is
   written as one change, and the split decision is considered only after two
   failed rounds. The D slice's five pages are the same shape.
5. **Ceremony hides the burn.** Every record-only update can spawn a `coder`
   session; `~107` child sessions produced only six archived slices. Cost
   telemetry reports `$0` while the run spent ~16.5M input tokens, so the
   operator cannot see the convergence cost.

The operator also supplies explicit state — "the child is quiescent", "I
authorize this commit", "that gate is cleared". The current contract can
re-derive the same state or ask again, and a stale idle marker can be mistaken
for a current interruption while a live child is still working. Both behaviors
add ceremony without adding truth.

## What Changes

- **A default two-round convergence budget.** Corrective rounds default to two
  per change. After two corrective rounds the orchestrator does not grant a
  third-round `cap_override`. It SHALL autonomously split or re-scope the slice
  and continue; it SHALL NOT stop or ask the operator for convergence
  authorization.

  If the plan provides an exact split authorization and materializer, use them.
  Otherwise use the plan’s scope-preserving split rules and split checkpoints to
  create the replacement slices, or narrow the current change to its accepted
  surface and carry the remainder forward as a new slice. If neither is
  permitted by the plan, record the remainder as a carry-forward item and
  continue with independent work. Internal convergence never becomes an
  operator question.

  `MAX_DISPATCHES` and repair/audit rounds remain advisory; the corrective-round
  budget is no longer a generic advisory cap.
- **Finding classification before another round.** Every finding is classified
  as `introduced_by_diff`, `adjacent_scope`, or `pre_existing`. Only
  `introduced_by_diff` earns a corrective round. `adjacent_scope` becomes a new
  slice or a proposed split; `pre_existing` is a carry-forward item. An
  `unknown` classification must be adjudicated from evidence before it can earn
  a round.
- **Automatic slice-size checkpoints.** If the first review finds a broad set of
  blocking issues, or the change spans multiple surfaces (API + CLI + page +
  freshness + error taxonomy, or three or more named surfaces), the orchestrator
  splits the slice before another correction. It performs the split or
  re-scope autonomously and continues; it does not return a terminal decision
  request or wait for an operator.
- **A full-scope first review and no adjacent-scope creep.** The first review
  covers the whole accepted scope, not just the diff. Re-reviews are also
  full-scope. A reviewer that could have established a current-slice failure in
  round 1 cannot introduce it in round 3 without new evidence. Coverage reports
  distinguish current-slice failures from adjacent scope.
- **Evidence and behavior are judged separately.** A finding must cite the
  accepted success parameter it violates. Evidence-format preferences (log
  shape, preferred artifact, transcript style) do not block when behavior is
  otherwise proved. The coverage matrix is kept.
- **Plan and change authoring size the surface before writing.** A maximum
  change-surface heuristic covers pages/endpoints/surfaces, acceptance
  scenarios, task count, and expected diff size. A change that spans API + CLI +
  page + freshness + error taxonomy must be split. The change writer enumerates
  the error taxonomy, scope/registry cases, cache invariants, parity checks, and
  guards before writing, and keeps the acceptance surface proportional.
- **Implementation leads with the highest-risk correctness path.** Scope and
  registry behavior, the error taxonomy, cache invariants, and freshness come
  before page/UI work. The implementer runs the exact focused tests the change
  defines, avoids broad refactors, and stops only for a real contract conflict.
- **Bounded bookkeeping.** At most one ledger writer per stage; record-only
  updates are batched and do not justify a separate `coder` session per update.
  The evidence packet is minimal: only fields that prove the claim.
- **Operator attestation is authoritative.** A recorded operator statement about
  quiescence, authority, or a gate is accepted as stated. The orchestrator and
  auditor do not re-derive it or ask again. Attestation does not make a false
  repository claim true or widen scope.
- **Per-stage cost and burn reporting.** Each stage records tokens in, tokens
  out, cache-read tokens, and cost status. An unpriced stage is reported as
  `unpriced`, never folded into `$0`; a genuine zero is distinguished from an
  unresolved estimate. The final report includes the run's token and cost burn.
- **A stale idle marker does not interrupt a live child.** An idle session
  marker is not an interruption when a live child is working and the ledger says
  the stage is in flight. The orchestrator records the observation and waits; it
  does not re-dispatch, cancel, or start a duplicate.
- **Proportional handoff ceremony.** The Phase Transition Gate and every hard
  reproducibility check stay. The auditor and hygiene writer accept recorded
  operator attestations and already hash-bound ledger evidence, and do not
  require a full state re-derivation when a live child plus a clean ledger
  already establish it.

## Impact

- **Agents/surfaces:** `adapters/opencode/agents/opsx-orchestrator.md`,
  `opsx-review.md`, `opsx-change-writer.md`, `opsx-integration-auditor.md`,
  `opsx-repo-hygiene.md`, and the canonical
  `core/phase-agents/implementer.md` plus its generated adapter outputs.
- **Plan authoring:** `core/plan-authoring.md`,
  `adapters/opencode/commands/opsx-plan.md`, and the vendored Codex plugin copy
  of the reference.
- **Specs:** modified and added requirements in `plan-handoff-gate`; added
  requirements in `reviewer-corrective-handoff`; added requirements in
  `plan-authoring-guidance`.
- **Tests/fixtures:** a new convergence contract test and pure decision fixture;
  updated handoff-gate, orchestrator-contract, and autonomous-continuation
  assertions; generated-phase-agent parity.
- **Docs:** `AGENTS.md`, `README.md`, `docs/adapters.md`, and the cost
  interpretation section of `core/model-efficiency-workflow.md`.
- **Compatibility:** the deterministic `opsx-plan` / `opsx-run` state machine,
  the compiled TOML manifest, acceptance tiers, human gates, and archived
  changes are unchanged. This is a prompt/contract change plus its tests. It
  narrows the prior "all caps are advisory" rule for corrective rounds because
  that rule is the direct cause of the observed overrun.

## Relationship to the in-flight adaptive-splitting change

An untracked `add-adaptive-plan-splitting` change already adds sizing, exact
`split_authorizations`, and a split-or-continue decision after two corrective
rounds. This change keeps that split machinery and reuses its checkpoints, but
tightens the post-two-round default: "continue with one bounded corrective
round" is replaced by autonomous split or re-scope. There is no operator
authorization path for internal convergence. If the adaptive-splitting change
lands first, its continue wording must be reconciled during implementation; the
split mechanics and authority rules are otherwise unchanged. See `design.md`
for the exact precedence.
