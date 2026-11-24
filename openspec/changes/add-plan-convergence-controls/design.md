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
- Making split authority implicit in a round count, a cap override, a reviewer
  verdict, or a supervisor role.

## Decisions

### D1. The corrective-round budget is a convergence rule, not an advisory cap

A **corrective round** is one corrective implementation dispatch followed by the
review that verifies it. The initial review is not a corrective round. The
default budget is **two corrective rounds per change**.

The first two corrective rounds may be authorized by the orchestrator as an
internal decision, as today. After the second corrective round's review still
fails, the orchestrator SHALL NOT create a third-round `cap_override`. It SHALL
choose and execute one autonomous action:

1. **Split** — select an exact `split_authorizations` entry for the parent and
   materialize it; if none exists, use the plan's scope-preserving split rules
   and checkpoints with the standard change-writer.
2. **Re-scope** — narrow the current change to its accepted surface, carry the
   remainder forward as a new slice with an explicit obligation mapping, and
   continue.
3. **Carry-forward** — when neither split nor re-scope is permitted by the plan,
   record the remainder as a carry-forward item and continue with independent
   work.

There is no operator-authorization path for internal convergence. The phrase
"continue with one bounded corrective round" is not an option.

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
materialized. If not, the orchestrator performs the split or re-scope under the
plan's scope-preserving rules with the standard change-writer, or narrows and
carries the remainder forward, then continues. It does not ask the operator and
does not leave a terminal decision pending.

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
child changes under the plan's scope-preserving split rules; it does not ask the
operator to approve the split.

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

### D14. Precedence and relationship to adaptive splitting

The in-flight `add-adaptive-plan-splitting` change adds the exact
`split_authorizations` schema, sizing, materializer, and a split-or-continue
decision after two corrective rounds. This change reuses all of that. The only
conflict is its "continue" option, which permits one bounded third round without
a split or re-scope.

Precedence when both are present:

1. This change's D1 budget governs how many corrective rounds are allowed.
2. Adaptive splitting's authority, eligibility, obligation mapping, freeze,
   materialization, binding, and ledger-idempotence rules are unchanged.
3. Adaptive splitting's "continue" is replaced by autonomous split or
   re-scope; there is no operator-authorization path for internal convergence.
4. Constructor-level rules in this change's spec delta
   (`Review convergence has a default two-round corrective budget` and
   `Broad first reviews trigger a slice split checkpoint`) are the normative
   source for the budget.

If adaptive splitting is merged first, its continue sentence must be reconciled
during implementation. This is a genuine cross-change ambiguity, not a reason to
duplicate the split machinery. If it is not merged, this change still works:
the plan has no `split_authorizations`, so the default after two rounds is an
autonomous split or re-scope under the plan's scope-preserving rules, or a
narrow/carry-forward decision that continues independent work.

## Risks / Trade-offs

- **Splitting adds plan and handoff overhead.** Mitigation: the split checkpoint
  is only mandatory for the cross-surface case or a broad first review; a
  narrow change still gets two corrective rounds. Splitting is bounded by the
  exact authorization and existing materializer.
- **Autonomous splitting over-splits.** Mitigation: splits must follow the
  plan's scope-preserving rules and preserve obligation mapping; a split that
  would change requirements, acceptance tiers, or human gates is refused and the
  orchestrator narrows/carries forward instead. No operator question is created.
- **Classification disputes stall a round.** Mitigation: the orchestrator
  adjudicates with the evidence packet; `unknown` is resolved before a round,
  not deferred to another reviewer.
- **Adjacent scope becomes a dumping ground.** Mitigation: every adjacent item
  must name its proposed slice and is not silently dropped; it becomes real only
  through an authorized split/re-scope or a reported follow-up.
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
- a run already past two corrective rounds resumes at the split-or-authorize
  decision, not with a third override.

Existing plans without `split_authorizations` keep working: they return the
proposed re-scope/authority decision instead of a split. The deterministic
controller, compiled TOML schema, acceptance tiers, human gates, and archived
changes are untouched. The shipped agents are OpenCode-only today; the
implementer change is authored in `core/phase-agents/implementer.md` and
regenerated, never hand-edited in an adapter.

## Open Questions

**One genuine ambiguity:** the in-flight `add-adaptive-plan-splitting` change
already permits one bounded "continue" corrective round after the two-round
decision. This change intentionally narrows that to explicit operator
authorization. The two changes cannot both be normative as written. The
resolution is D14: this change's budget wins, adaptive splitting's split
mechanics survive. If the maintainer prefers to keep the one-round continue, the
two-round budget becomes three rounds in practice and the observed 4–5-cycle
pattern is not corrected. That is a product decision, not an implementation
detail.
