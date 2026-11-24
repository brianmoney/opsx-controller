# Design: add-plan-convergence-controls

## Context

`opsx-orchestrator` is a prompt-only conductor. It holds no edit or shell
capability and delegates every write and every command to a worker. The
contracts that matter here are therefore agent instruction bodies and the specs
that constrain them:

- `adapters/opencode/agents/opsx-orchestrator.md` owns sequencing, dispatch,
  the run ledger, the review loop, and the split decision.
- `adapters/opencode/agents/opsx-review.md` owns the review verdict, the
  corrective handoff, and the coverage matrix.
- `core/phase-agents/implementer.md` (generated into every adapter) owns
  implementation order and the focused checks.
- `adapters/opencode/agents/opsx-change-writer.md` owns the authored change's
  acceptance surface.
- `adapters/opencode/agents/opsx-integration-auditor.md` and
  `opsx-repo-hygiene.md` own handoff verification and repair.
- `core/plan-authoring.md` and `adapters/opencode/commands/opsx-plan.md` own
  how a plan is sized before it is written.
- `openspec/specs/adaptive-plan-splitting/spec.md` is the merged capability
  spec that owns sizing, exact split authorizations, eligibility,
  freeze/materialization, and the after-two-rounds decision this change
  constrains.

The current contracts were built to prevent premature stopping. The
`add-autonomous-plan-continuation` change made `MAX_DISPATCHES`, review
corrective rounds, and repair/audit rounds advisory, so a run with known safe
work would finish instead of stopping on arithmetic. That was correct for
dispatch and handoff repair. It was applied to review corrective rounds without
asking whether the remaining work belonged in the same change.

The KF Admin Panel phase-2 run shows the cost. Six of eight slices archived in
about 18.5 hours, but only slice C was clean on first review. Five of six
non-trivial slices took 4–5 review cycles and a cap override. Six archived
slices consumed 26 reviewer sessions and 29 implementer sessions, plus 17
`coder` bookkeeping sessions, for about 107 child sessions overall. The run
spent roughly 16.5M input tokens, 1.76M output tokens, and 600M cache-read
tokens, while cost telemetry reported `$0`. The current five-page D slice is
repeating the same shape: broad implementation, review, corrective round.

The failure mode is not "too few rounds". It is "the change is the wrong unit,
and the reviewer is discovering the unit one concern at a time". Adding round
six cannot converge a change whose accepted surface was never bounded.

## Goals / Non-Goals

**Goals:**

- Make two corrective rounds the default ceiling and make split/re-scope the
  default response after it.
- Set a one-directional precedence so a plan can tighten but never loosen
  controller policy, and so a split cannot reset the corrective-round budget.
- Classify every finding so only a defect introduced by the reviewed diff earns
  another corrective round.
- Make the first review full-scope, and make later reviews full-scope too, so
  the blocking set shrinks instead of growing.
- Trigger a slice-size checkpoint on the first review when the blocking set is
  broad or spans multiple named surfaces.
- Bound the authored change surface before implementation.
- Put the highest-risk correctness work first and verify it with the change's
  exact focused tests.
- Reduce record-only ceremony without weakening the Phase Transition Gate.
- Make operator attestation authoritative for the state it names.
- Record real per-stage tokens and cost, and never present unresolved cost as
  `$0`.
- Prevent a stale idle marker from interrupting a live child.

**Non-Goals:**

- Changing the deterministic `opsx-plan` / `opsx-run` state machine, the
  compiled TOML manifest, or any TOML key.
- Changing acceptance tiers, human gates, the local-only delivery model, or the
  hard handoff reproducibility checks.
- Reopening archived changes or inserting unplanned changes mid-plan.
- Fixing the pricing catalog or re-pricing stored telemetry. The separate
  `add-cost-reprice-option` change owns pricing corrections; this change owns
  honest reporting of what the orchestrator observed.
- Proving live model behavior in tests. The tests prove the shipped prompt
  contract and a pure decision protocol.
- Making split permission implicit in a round count, a cap override, a reviewer
  verdict, or a supervisor role. Only an exact `split_authorizations` entry or
  the plan's own explicit split permission counts, and the materializer executes
  either one.

## Decisions

### D1. The corrective-round budget is a convergence rule, not an advisory cap

A **corrective round** is one corrective implementation dispatch followed by the
review that verifies it. The initial review is not a corrective round. The
default budget is **two corrective rounds per change**.

The first two corrective rounds may be authorized by the orchestrator as an
internal decision, as today. After the second corrective round's review still
fails, the orchestrator SHALL NOT create a third-round `cap_override`. It SHALL
choose and execute one autonomous action:

1. **Split** — `opsx-split-materializer` is the sole executor of every
   structural split. Select the plan's exact `split_authorizations` entry when
   one exists. When no exact entry exists, a split is permitted only where the
   plan's own split rules or plan text permit an unpreauthorized split; the
   standard change-writer then authors the child set to the same exactness
   invariants (complete obligation map, dependency remap, aggregate exit) and
   the materializer executes it. Where the plan reserves split approval to the
   operator, the orchestrator does not split autonomously: it honors the plan's
   declared operator checkpoint or does not split.
2. **Re-scope** — narrow the current change to its accepted surface, carry the
   remainder forward as a new slice with an explicit obligation mapping, and
   continue.
3. **Carry-forward** — when neither split nor re-scope is permitted by the plan,
   record the remainder as a carry-forward item and continue with independent
   work.

There is no operator-authorization path for internal convergence. The phrase
"continue with one bounded corrective round" is not an option. A proposed
re-scope/authority decision is a ledger record, not a terminal stop.

Rationale: the observed 4–5-cycle slices did not fail because the fifth round
was unsafe. They failed because the contract let the orchestrator treat "more
of the same change" as the default. A `cap_override` that names remaining work
cannot answer whether that work belongs in this change; a split checkpoint can.

`MAX_DISPATCHES` and the repair/audit round limit remain advisory. A
corrective-round override is refused even when `MAX_DISPATCHES` has headroom,
and a dispatch override never authorizes a corrective round.

### D2. Every finding is classified before another round

Each blocking finding is classified as exactly one of:

- **`introduced_by_diff`** — the reviewed diff introduced the observed failure,
  or newly exposed or worsened an existing path. This is the only class that
  earns a corrective round.
- **`adjacent_scope`** — the behavior is real but outside the change's accepted
  success parameters: a next-slice concern, a neighboring page, a broader error
  taxonomy, or a hardening item the change did not accept.
- **`pre_existing`** — the defect predates the reviewed diff and is not newly
  exposed or worsened by it. It is a carry-forward item.

The reviewer supplies `introduced_by_diff` (`yes|no|unknown`) and
`adjacent_scope` (`true|false`) on every finding. The orchestrator derives the
classification with this precedence:

1. `adjacent_scope: true` → `adjacent_scope`;
2. else `introduced_by_diff: yes` → `introduced_by_diff`;
3. else `pre_existing`.

`introduced_by_diff: unknown` is not a fourth class and cannot earn a round:
the orchestrator must resolve it from the diff and evidence first, then record
the classification. A disputed classification is adjudicated by the
orchestrator with the evidence packet, not by another reviewer round.

Consequences:

- `adjacent_scope` findings are recorded as a new slice under the plan's
  scope-preserving split rules or as a documented follow-up. They are not fixed
  in the current change and do not block it; the orchestrator does not ask the
  operator to accept the re-scope.
- `pre_existing` findings are carry-forward items with their locus, observed and
  required behavior, evidence, impact, and disposition. They do not reopen an
  archived change.
- A regression introduced by a correction is `introduced_by_diff`; it consumes
  the current corrective round's review, not a fresh entitlement beyond the
  budget.

### D3. A broad first review is a split checkpoint, not a correction round

The first review is a sizing checkpoint as well as a verdict. It is **broad**
when either condition holds:

- blocking findings require correction in two or more distinct named surfaces
  from: API contract, CLI, page/UI, freshness, error taxonomy, cache
  invariants; or
- the change claims three or more acceptance scenarios that cannot be proved by
  one focused check; or
- the first review's blocking finding set spans three or more distinct loci and
  is not explained by one shared root cause.

When the first review is broad, the orchestrator SHALL make the split-or-
re-scope decision **before** dispatching any corrective implementation. It does
not spend a corrective round discovering that the slice is too large.

The same checkpoint applies when implementation itself reveals that the change
exceeds the maximum surface heuristic in D8, even if the first review has not
run yet. The checkpoint is evaluated at the earliest evidence, not only after
two failed rounds.

If an exact `split_authorizations` entry exists, it is selected and
materialized. If not, the orchestrator splits only where the plan's own split
rules or text permit an unpreauthorized split — the change-writer authors the
child set to authorization-equivalent exactness and `opsx-split-materializer`
executes it — or it narrows and carries the remainder forward, then continues.
Where the plan reserves split approval to the operator, the plan's declared
checkpoint governs. The orchestrator does not ask the operator for a
convergence decision and does not leave a terminal decision pending.

### D4. The first review, and every re-review, is full-scope

The reviewer's job is to establish the accepted surface, not to walk the diff
one concern at a time. The first review SHALL:

- read the change's accepted success parameters, proposal, design, tasks, and
  spec deltas, plus the actual diff;
- derive and report the full coverage matrix;
- enumerate every blocking finding it can establish from that scope, including
  findings outside the diff that the accepted scope requires;
- classify each finding as in D2.

A re-review SHALL also read the whole accepted surface and the prior findings.
A reviewer may not introduce a current-slice blocking finding in a later round
unless it cites new evidence, a changed diff, or a prior finding that was not
actually closed. If it could have established the failure in round 1 from the
same evidence, it is a reviewer process defect; the orchestrator records the
process defect and does not treat the late finding as a reason to grant another
round beyond the budget.

This replaces the current "Cycle 1–2 review the stated diff; Cycle 3+ full
scope" discipline. The first review is the cheapest place to find the whole
surface, and it is the only place that can justify a split before correction.

### D5. Review judges the accepted success parameters, not the next slice

The reviewer judges only the change's accepted success parameters and the
coverage matrix derived from them. It does not expand into the next slice's
concerns, hardening the plan did not accept, or a preferred future design.

- An `adjacent_scope` finding is recorded and proposed as a new slice; it never
  blocks the active change by itself.
- An evidence-format preference (log shape, transcript style, preferred artifact
  type, ordering of evidence fields) is not blocking when the behavior is
  otherwise proved by a command, test, or observed reproduction.
- A missing preferred artifact is not a defect when an equivalent proof exists
  and is named. The reviewer records the substitution as a note.
- A false claim is still blocking. This decision lowers ceremony, not
  truthfulness.

### D6. The change writer bounds the surface before writing

`opsx-change-writer` authors exactly one concern. Before writing the proposal
and spec deltas it SHALL enumerate:

- the error taxonomy: every error class, envelope, and terminal state the
  change's behavior can produce;
- scope and registry cases: missing, invalid, valid, disabled, aliased,
  duplicate, and unauthorized cases that apply;
- cache invariants: key composition, collision behavior, invalidation,
  single-flight, stale-while-revalidate, and no-write/no-subprocess rules;
- parity checks: for each pair of surfaces that must agree (API/CLI/page);
- guards: authentication, authorization, tenant, and limit checks.

The enumeration is the acceptance surface. If it exceeds the maximum heuristic
in D8, the writer SHALL split or narrow before writing rather than authoring an
oversized change and letting review discover it. The orchestrator may create the
child changes only where the plan preauthorizes an exact entry or its own split
rules permit an unpreauthorized split; either way `opsx-split-materializer`
executes the structural split and the orchestrator does not ask the operator to
approve a convergence decision.

The writer's tasks and acceptance scenarios stay proportional to that surface.
It does not add release-grade hardening to a `poc` change, and it does not
enumerate speculative cases the plan has not accepted.

### D7. Implementation leads with the highest-risk correctness path

The implementer orders work by correctness risk, not by page order or visible
progress:

1. scope/registry validation and authorization;
2. error taxonomy and envelope behavior;
3. cache invariants and concurrency;
4. freshness and persistence semantics;
5. page/UI wiring;
6. presentation and polish.

If a change has no scope or cache layer, start at the highest-risk invariant it
does have. The implementer runs the exact focused tests the change defines for
each step. It does not perform broad refactors, rename unrelated code, or add a
new test layer to prove a behavior already proved at a lower layer.

It stops only for a real contract conflict: the corrective handoff contradicts
the accepted artifacts, a required prerequisite is missing, or the environment
cannot run the required check. It does not stop to ask a stylistic question and
does not return `blocked` because one round's automatable work does not fit.

### D8. Maximum change-surface heuristic

A change is sized against all of these dimensions:

| Dimension | Maximum before split or narrowing |
|---|---|
| Named surfaces | 3 (one primary plus at most two supporting) |
| Pages / endpoints / routes | 3 total |
| Acceptance scenarios | 5 |
| Tasks | 12 |
| Expected production diff | 400 changed lines (additions + deletions), excluding tests, fixtures, and docs |

A **named surface** is a distinct externally observable contract: API endpoint
group, CLI command group, page/UI route, freshness/persistence contract, error
taxonomy, or cache layer. A page that adds an API endpoint counts as both.

The mandatory split rule is independent of the numeric limits: a change that
spans **API + CLI + page + freshness + error taxonomy** (or three or more named
surfaces) SHALL be split before it is written. There is no review override for
this rule.

Exceeding a numeric threshold requires the author to split or narrow. The
mandatory cross-surface split is never waived by a review override or an
operator authorization inside the run. The D slice's five pages exceed the page
limit even before its API and freshness work is counted.

### D9. Bookkeeping is bounded per stage

The orchestrator still delegates every write, so it still needs a ledger writer.
The bound is:

- at most **one ledger-writer dispatch per stage**, which appends the stage's
  checkpoint and all record-only events in a single batch;
- a record-only update (a status line, a dispatch count, a copied coverage
  array) SHALL NOT justify a separate `coder` session;
- workers return their records in their normal output contract; the
  orchestrator batches them into the stage boundary write;
- a stage with no decision change and no new evidence requires no writer
  dispatch.

The evidence packet is minimal: change/stage/HEAD, diff range or staged paths,
commands with exit codes and counts, artifact paths with hashes, claim status,
findings with classification, and the next action. Image digests, UID/GID, and
effective roots appear only when the change's behavior depends on them.

Rationale: 17 `coder` sessions for ledger work is not a correctness cost the
operator should pay. One batched writer per stage preserves the durable ledger
without a session per line.

### D10. Operator attestation is authoritative for the state it names

An operator attestation is an explicit statement in the invocation, a dispatch
reply, or a recorded message. It is authoritative for its named subject. The
orchestrator and auditor SHALL NOT re-derive, re-check, or ask again about a
state the operator has confirmed.

An attestation record contains:

- `subject` — `quiescence`, `authority`, `gate`, `evidence`, or `scope`;
- `statement` — the operator's words, verbatim;
- `scope` — the exact change, commit, or artifact it covers;
- `source` — invocation, message, or ledger;
- `recorded_at` — the run timestamp.

Rules:

- "The child is quiescent" ends the question. The orchestrator does not poll the
  session, restart a probe, or require a second confirmation.
- "I authorize committing the accepted source" authorizes that commit; the
  hygiene writer records it and proceeds.
- "That gate is cleared" is authoritative for the named gate, recorded verbatim;
  the orchestrator does not re-derive the gate.
- Attestation cannot make a false repository claim true, cannot substitute for a
  hard content check the auditor can run, and cannot widen the change's accepted
  scope. It is authority and state, not evidence of code behavior.
- An attestation that conflicts with a hard repository fact is recorded as a
  conflict and stops for a human-only decision; the orchestrator does not
  silently prefer either side.

### D11. Per-stage tokens and cost are recorded honestly

Each stage record includes:

- `tokens_in`, `tokens_out`, `cache_read_tokens` where the harness reports them;
- `cost_status`: `estimated`, `unresolved`, `unavailable`, or `not-reported`;
- `estimated_cost` and its pricing catalog version when estimated;
- `usage_source` — the telemetry record, the child's report, or the operator.

The run ledger and final report include:

- per-stage tokens and cost status;
- run totals for the three token categories;
- a `priced_subtotal` plus `unpriced_stage_count`, never a bare `$0` when any
  stage is unresolved or unavailable;
- a `$0.00` total only when every stage is estimated at zero or the run truly
  used zero tokens.

A stage with observed usage and an unresolved cost is reported as `unpriced`,
not folded into zero. If the harness or telemetry does not report usage at all,
the stage is `not-reported`, and the final report says the burn is unknown
rather than inventing a number.

Rationale: the KF run's `$0` was not a real cost; it was a telemetry gap that
made the run look free. Token counts still expose the burn, and an explicit
`unpriced` label prevents a false zero from becoming a planning input.

### D12. A stale idle marker is not an interruption

An idle session marker is only an observation. It does not interrupt a stage
when:

- the ledger records the stage as in flight; and
- a live child session exists for that stage; and
- the child has produced progress more recently than the marker, or the marker
  is contradicted by the child's in-flight state.

In that case the orchestrator records `stale_idle_marker` with the marker source
and the evidence that the child is live, and continues waiting. It does not
re-dispatch the stage, cancel the child, open a duplicate session, or start the
next stage.

A stage is interrupted only when no live child exists and no terminal result is
available, the ledger shows the stage in flight, and the absence is confirmed by
the harness or the operator. Even then, reconcile once from the ledger before
re-dispatching. Do not poll: the child's completion notification or a single
status-scout report is the next event.

### D13. Handoff ceremony is proportional, not weaker

The Phase Transition Gate and every hard check remain:

`tracked-tree-clean`, `source-manifest-at-head`, `plan-at-head`,
`archive-commit-implementation-paths`, `run-state-ignored`,
`evidence-at-head`, `perf-script-from-head`, `fresh-checkout-checks`,
`local-only-delivery`, and `next-change-readiness`.

What changes is the evidence burden around them:

- a recorded operator attestation satisfies the state or authority question it
  names, without re-derivation;
- evidence already recorded in the ledger with a matching hash is not
  re-collected; the auditor reads the existing path and hash;
- a live child plus a clean ledger that already establish quiescence and stage
  state are not re-derived by a fresh probe;
- the auditor and hygiene writer return the minimal fields that prove their
  checks, not duplicate transcripts;
- the hygiene writer still repairs in phases and still refuses to invent scope.

The auditor may not waive `source-manifest-at-head`, `plan-at-head`, or
`archive-commit-implementation-paths` by attestation: those are repository
content facts it can read. It may accept attestation for quiescence, authority,
and a gate the operator owns.

### D14. Precedence and relationship to the merged adaptive-plan-splitting capability

`add-adaptive-plan-splitting` is merged (PR #43); its normative text is
`openspec/specs/adaptive-plan-splitting/spec.md`. Its exact
`split_authorizations` schema, pre-implementation sizing checkpoint,
eligibility rules, obligation and dependency preservation, freeze,
materialization, binding, and ledger-idempotence rules are unchanged, and they
remain the only path by which a planned change is structurally replaced.

What changes is its after-two-rounds decision requirement, and this change
carries that change as a delta on the merged spec: *Two corrective rounds
trigger an explicit split-or-continue decision* is renamed and modified to *Two
corrective rounds trigger an explicit split-or-re-scope decision*. The decision
set loses "continue with one bounded corrective round" and becomes split |
re-scope | carry-forward. Its "proposed re-scope/authority decision" becomes a
ledger record rather than a terminal stop.

Precedence, when the merged capability and this change are both in force:

1. This change's D1 budget governs how many corrective rounds are allowed.
2. Adaptive splitting's authority, eligibility, obligation mapping, dependency
   remap, freeze, materialization, binding, and ledger-idempotence rules are
   unchanged, and `opsx-split-materializer` remains the only structural-split
   executor, per D1.
3. Adaptive splitting's "continue" is replaced by split, re-scope, or
   carry-forward; there is no operator-authorization path for internal
   convergence.
4. The controller-level requirements in this change's spec deltas
   (`plan-handoff-gate`: *Review convergence has a default two-round corrective
   budget* and *Controller specs set the floor and a plan may only tighten
   them*; `adaptive-plan-splitting`: *Two corrective rounds trigger an explicit
   split-or-re-scope decision*) are the normative source for the budget and the
   decision set.

No duplicate split machinery is added. Implementation reconciles the merged
capability's test and fixture assertions and the agent/documentation wording, per
the proposal's relationship section. A plan with no `split_authorizations` still
works: the default after two rounds is a plan-permitted split, a re-scope, or a
narrow/carry-forward decision that continues independent authorized work.

The merged spec's other requirements are unchanged. Where they say a refusal
"returns the proposed decision" — a vague split instruction, an undeclared
supervisor capability, an unavailable materializer, plan drift — the refusal
still fails closed for that split, and the resulting proposal is a ledger record
reported in the run, not an operator question and not a raw-count stop.

### D15. Controller specs set the floor; a plan may tighten but never loosen them

An accepted plan is active authority for its own run, but it cannot lower the
controller contract. Where a plan's text and a controller capability spec
prescribe different behavior for the same situation, the tighter rule SHALL
govern.

- A plan MAY tighten: reserve a split for its declared operator checkpoint,
  shorten a corrective budget, narrow the change or surface limits, add a gate,
  or raise an evidence bar.
- A plan SHALL NOT loosen: permit a bounded third corrective round, widen a
  surface or scope, remove a gate, lower an acceptance tier, or weaken a stop
  condition or a hard handoff check.
- Plan silence leaves the controller rule unchanged; silence is not permission.
- A plan provision that would loosen controller policy is void for that run. The
  orchestrator records it as plan drift and does not follow it.

Rationale: the observed overrun came from lifting a controller ceiling, and the
cheapest way to reintroduce it is plan prose written under the softer rule. A
one-directional precedence keeps plan-specific autonomy — including a plan that
reserves split approval to itself — while the controller floor stays fixed.

### D16. Split children inherit corrective-round consumption by blocker class

A child of a split does not start with a fresh two-round budget for work that
already consumed rounds in its parent. Consumption is tracked by blocker class,
not by change slug.

- A blocker class that consumed a corrective round in the parent is inherited as
  consumed by the child that owns that obligation. After two consumed rounds the
  inherited allowance for that class is zero.
- A blocker class first introduced by a child's own diff accrues under that
  child's budget.
- The ledger records, per child, the inherited blocker classes and their
  consumed counts, so a resumed run reads the same allowance instead of
  recomputing a fresh one.
- The rule creates no stop: a child that exhausts its inherited allowance enters
  the same split-or-re-scope decision as any other change.

Rationale: without inheritance a split is a budget reset, and the same failure
returns as several children each earning two rounds — the observed 4–5-cycle
pattern multiplied by the child count.

## Risks / Trade-offs

- **Splitting adds plan and handoff overhead.** Mitigation: the split checkpoint
  is only mandatory for the cross-surface case or a broad first review; a
  narrow change still gets two corrective rounds. Splitting is bounded by the
  plan's own split permission — an exact `split_authorizations` entry or the
  plan's explicit rules — and by the existing materializer.
- **Autonomous splitting over-splits.** Mitigation: splits must follow the
  plan's scope-preserving rules and preserve obligation mapping; a split that
  would change requirements, acceptance tiers, or human gates is refused and the
  orchestrator narrows/carries forward instead. No operator question is created.
- **Splitting becomes a corrective-budget reset.** Mitigation: D16 inherits the
  parent's corrective-round consumption by blocker class, so a child receives no
  fresh budget for work that already consumed rounds, and a child cannot be used
  to launder a non-converging change into several short budgets.
- **Classification disputes stall a round.** Mitigation: the orchestrator
  adjudicates with the evidence packet; `unknown` is resolved before a round,
  not deferred to another reviewer.
- **Adjacent scope becomes a dumping ground.** Mitigation: every adjacent item
  must name its proposed slice and is not silently dropped; it becomes real only
  through a plan-permitted split, a re-scope, or a reported follow-up.
- **Cost fields add reporting ceremony.** Mitigation: only the three token
  categories and a cost status are required; `not-reported` is an honest,
  cheap value. The report never invents a number.
- **Attestation can be wrong.** Mitigation: it is authoritative for state and
  authority, not for repository content or code behavior; a conflict with a
  hard fact stops for a human-only decision.
- **The stale-marker rule delays recovery.** Mitigation: it applies only while a
  live child exists; a confirmed dead child still reconciles and re-dispatches.

## Migration

No data migration. New ledger fields are additive and optional on resume:

- a missing classification is `unknown` and must be adjudicated before another
  corrective round;
- a missing token field is `not-reported`, not zero;
- a missing attestation record means no attestation exists;
- a run already past two corrective rounds resumes at the split-or-re-scope
  decision, not with a third override.

Existing plans without `split_authorizations` keep working: they do not return a
proposed re-scope/authority decision to the operator. They make the same
split-or-re-scope decision under the plan's own permission — a plan-permitted
unpreauthorized split executed by `opsx-split-materializer`, or narrowing to the
accepted surface with the remainder carried forward — record the decision and
any proposal in the ledger, and continue independent authorized work. The
deterministic controller, compiled TOML schema, acceptance tiers, human gates,
and archived changes are untouched. The shipped agents are OpenCode-only today;
the implementer change is authored in `core/phase-agents/implementer.md` and
regenerated, never hand-edited in an adapter.

## Open Questions

**None blocking.** The cross-change ambiguity that earlier drafts recorded is
resolved: `add-adaptive-plan-splitting` merged (PR #43), and its bounded-continue
option is replaced by the split-or-re-scope decision through the requirement
delta in `specs/adaptive-plan-splitting/spec.md`. Precedence is settled by D15 —
controller specs set the floor, an accepted plan may tighten but never loosen
them, and the tighter rule governs a conflict. There is no operator
authorization path for internal convergence and no operator question to leave
open; a plan that reserves split approval to itself is a tightening the
orchestrator honors, not an ambiguity.

Two bounded implementation notes remain — implementer decisions, not operator
questions:

- The merged capability's `decide_convergence` fixture branch and its
  `ConvergenceDecisionTests` encode `bounded-continue`; they are replaced with
  the split | re-scope | carry-forward outcomes in the same file, keeping every
  split authority, obligation, freeze, materialization, and idempotence
  assertion intact.
- The archived `add-adaptive-plan-splitting` change copy is historical and
  read-only; only the synced capability spec and the tests carry the new rule.
