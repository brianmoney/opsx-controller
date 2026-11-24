## MODIFIED Requirements

### Requirement: Dispatch and repair-round caps are advisory

`MAX_DISPATCHES` and the repair/audit round limit SHALL be advisory thresholds,
not terminal conditions. The review corrective-round budget is a convergence
rule governed by *Review convergence has a default two-round corrective
budget*; it is not a generic advisory cap and SHALL NOT be exceeded by a
`cap_override`.

Before exceeding a dispatch or repair/audit cap, the orchestrator SHALL record a
`cap_override` ledger entry naming the cap and its value or source, the count
already consumed, the concrete remaining work, why continuing is safer than
stopping, and the next checkpoint at which the decision is re-evaluated.

The orchestrator MAY then continue and SHALL NOT stop solely because a dispatch
or repair/audit cap was reached. It SHALL still stop when the remaining work is
unsafe, ambiguous, blocked by a human gate, or requires a human-only decision. A
dispatch or repair/audit override SHALL NOT authorize an additional corrective
round, and a `cap_override` that names a corrective round SHALL be refused.

#### Scenario: Dispatch cap is exceeded with a justification
- **WHEN** `MAX_DISPATCHES` is reached while a scoped handoff repair and re-audit
  remain
- **THEN** the orchestrator records the cap, the remaining repair work, and the
  justification, and continues

#### Scenario: Third repair round is justified
- **WHEN** two repair/audit rounds failed for different scoped blockers and a
  third repair is available
- **THEN** the orchestrator records the override and permits the third round
  instead of stopping on the round count

#### Scenario: Cap reached with no safe work left
- **WHEN** a dispatch or repair/audit cap is reached and the only remaining
  action requires a human-only decision
- **THEN** the orchestrator stops and reports that human-only decision, not the
  cap

#### Scenario: A corrective round cannot be overridden as a dispatch cap
- **WHEN** the two-round corrective budget is exhausted and `MAX_DISPATCHES`
  still has headroom
- **THEN** the orchestrator does not create a `cap_override` for a third
  corrective round, and selects the split-or-authorize path instead

## ADDED Requirements

### Requirement: Review convergence has a default two-round corrective budget

A **corrective round** is one corrective implementation dispatch followed by the
review that verifies it. The initial review is not a corrective round. The
default budget SHALL be **two corrective rounds per change**.

For the first two corrective rounds, the orchestrator MAY authorize the round as
an autonomous decision when the prior review's blocking findings are eligible
under *Findings are classified before another corrective round*.

After the second corrective round ends with a failing review, the orchestrator
SHALL NOT grant a third corrective round by default and SHALL NOT stop or ask the
operator for convergence authorization. It SHALL choose exactly one autonomous
action:

1. **Split** — select an exact `split_authorizations` entry for the parent and
   materialize it through the authorized split path when available; otherwise
   use the plan's scope-preserving split rules and split checkpoints to create
   replacement slices.
2. **Re-scope** — narrow the current change to its accepted surface, carry the
   remainder forward as a new slice with an explicit obligation mapping, and
   continue.
3. **Carry-forward** — when neither split nor re-scope is permitted by the plan,
   record the remaining finding or obligation as a carry-forward item and
   continue with independent authorized work.

The orchestrator SHALL record the split/re-scope/carry-forward decision in the
ledger before dispatching the next stage. It SHALL NOT widen scope, change a
gate, skip a failing acceptance criterion, or create an unplanned requirement to
avoid the decision.

A split, re-scope, or carry-forward that would change requirements, human gates,
or acceptance tiers is outside this authority. In that case the orchestrator
SHALL narrow to the accepted surface, carry the remainder forward, and continue;
it SHALL NOT ask the operator to authorize the convergence step.

This requirement constrains the corrective-round authority in
*Orchestrator-owned changes continue autonomously*: a third corrective round is
not an internal autonomous decision; the autonomous decision is how to split,
re-scope, or carry forward. `MAX_DISPATCHES` and the repair/audit round limit
remain advisory and do not authorize a third corrective round.

#### Scenario: Two failed corrective rounds trigger an autonomous split
- **WHEN** a change's second corrective round ends with a failing review and the
  plan contains an exact split authorization for that parent
- **THEN** the orchestrator selects and materializes the authorized replacement
  and continues, without asking the operator and without granting a third
  corrective round

#### Scenario: No exact split authorization uses the plan rules
- **WHEN** two corrective rounds are exhausted and the plan has no exact
  `split_authorizations` entry but has scope-preserving split rules and
  checkpoints
- **THEN** the orchestrator performs the split or re-scope under those rules,
  records the obligation mapping, and continues

#### Scenario: Split is not permitted, carry forward and continue
- **WHEN** two corrective rounds are exhausted and neither a split nor a
  re-scope is permitted by the plan
- **THEN** the orchestrator records the remaining finding or obligation as a
  carry-forward item and continues with independent authorized work

#### Scenario: No operator question for convergence
- **WHEN** the two-round budget is exhausted
- **THEN** the orchestrator does not ask the operator for authorization to
  continue, does not return a terminal proposed decision, and does not stop

#### Scenario: A split cannot change requirements or gates
- **WHEN** the only available split would change a requirement, acceptance tier,
  or human gate
- **THEN** the orchestrator narrows the current change to its accepted surface,
  carries the remainder forward, and continues without asking the operator

#### Scenario: Independent work continues
- **WHEN** one change is being split or re-scoped and another planned change is
  ready and already authorized
- **THEN** the orchestrator may drive the independent change without treating the
  in-progress split as authority to widen the blocked change

### Requirement: Findings are classified before another corrective round

Before the orchestrator dispatches another corrective round, every finding SHALL
be classified as exactly one of:

- `introduced_by_diff` — the reviewed diff introduced the observed failure, or
  newly exposed or worsened an existing path;
- `adjacent_scope` — the behavior is real but outside the change's accepted
  success parameters, including a next-slice concern, a neighboring page, a
  broader error taxonomy, or unaccepted hardening;
- `pre_existing` — the defect predates the reviewed diff and is not newly
  exposed or worsened by it.

The reviewer SHALL supply `introduced_by_diff` (`yes|no|unknown`) and
`adjacent_scope` (`true|false`) on every finding. The orchestrator SHALL derive
the classification with this precedence:

1. `adjacent_scope: true` → `adjacent_scope`;
2. else `introduced_by_diff: yes` → `introduced_by_diff`;
3. else `pre_existing`.

`unknown` SHALL NOT earn a corrective round. The orchestrator SHALL resolve it
from the diff, the accepted scope, and the evidence first, and SHALL record the
resulting classification and its basis. A classification dispute SHALL be
adjudicated by the orchestrator from the evidence packet, not by another reviewer
round.

Only a blocking `introduced_by_diff` finding SHALL earn a corrective round.
`adjacent_scope` findings SHALL be recorded as a new slice and SHALL NOT be
fixed in the current change or block it. A new slice becomes real through an
exact split authorization or the plan's scope-preserving split rules; otherwise
the item is a documented follow-up. The orchestrator SHALL NOT ask the operator
to approve the re-scope. `pre_existing` findings SHALL be recorded as
carry-forward items with locus, observed and required behavior, evidence,
impact, and disposition; they SHALL NOT reopen an archived change.

A regression introduced by a correction is `introduced_by_diff` and is evaluated
by that corrective round's review; it does not create an entitlement beyond the
corrective-round budget.

#### Scenario: An adjacent-scope finding does not earn a round
- **WHEN** a review reports a real defect in the next slice's page while the
  active change's accepted success parameters are met
- **THEN** the finding is classified `adjacent_scope`, proposed as a new slice,
  and does not block or extend the active change

#### Scenario: A pre-existing defect is carried forward
- **WHEN** a review reports a defect that predates the diff and the diff neither
  exposes nor worsens it
- **THEN** the orchestrator classifies it `pre_existing` and records a
  carry-forward item instead of a corrective round

#### Scenario: An introduced defect earns the corrective round
- **WHEN** the reviewed diff introduces a wrong error envelope for an accepted
  scope case
- **THEN** the finding is classified `introduced_by_diff` and is eligible for the
  current corrective budget

#### Scenario: An unknown classification is resolved before a round
- **WHEN** a reviewer marks `introduced_by_diff: unknown`
- **THEN** the orchestrator adjudicates it from the diff and evidence, records
  the classification and basis, and does not dispatch a round on the unknown
  value

#### Scenario: A mixed finding set only corrects the diff
- **WHEN** one review contains one introduced defect, two adjacent concerns, and
  one pre-existing defect
- **THEN** only the introduced defect enters the corrective handoff, the two
  adjacent concerns become proposed slices, and the pre-existing defect becomes
  a carry-forward item

#### Scenario: Adjacent scope is not inserted as an unplanned change
- **WHEN** an adjacent-scope finding looks like a small standalone fix
- **THEN** the orchestrator records it as a proposed slice or follow-up and does
  not insert an unplanned change into the running plan

### Requirement: Broad first reviews trigger a slice split checkpoint

The first review SHALL serve as a sizing checkpoint as well as a verdict. A
first review is **broad** when any of these holds:

- blocking findings require correction in two or more distinct named surfaces
  from: API contract, CLI, page/UI, freshness, error taxonomy, cache invariants;
- the change claims three or more acceptance scenarios that cannot be proved by
  one focused check; or
- the blocking finding set spans three or more distinct loci and is not
  explained by one shared root cause.

When the first review is broad, the orchestrator SHALL make the split-or-
re-scope decision before dispatching any corrective implementation. It SHALL NOT
spend a corrective round discovering that the slice is too large.

The same checkpoint applies when implementation evidence shows the change
exceeds the maximum surface heuristic before the first review runs. The
checkpoint is evaluated at the earliest evidence, not only after two failed
rounds.

When an exact `split_authorizations` entry exists and an authorized
materialization path is available, the orchestrator SHALL select and materialize
it before any further correction.

When no exact authorization exists, or no authorized materializer is available,
the orchestrator SHALL perform the split or re-scope under the plan's
scope-preserving split rules and split checkpoints using the standard
change-writer, then continue. It SHALL NOT return a terminal proposed decision
and SHALL NOT ask the operator to authorize the split.

When the plan has no split rule that covers the remainder, the orchestrator SHALL
narrow the current change to its accepted surface, carry the remainder forward as
a new slice or follow-up, and continue with independent work.

A reviewer verdict, a `cap_override`, or general autonomy SHALL NOT substitute
for the plan's split rules or for a change to requirements, acceptance tiers, or
human gates.

#### Scenario: A multi-surface first review splits before correction
- **WHEN** the first review of a change finds blocking issues in its API
  contract, its page, and its error taxonomy
- **THEN** the orchestrator makes the split-or-re-scope decision before any
  corrective implementation and does not grant a corrective round

#### Scenario: A five-page change triggers the checkpoint
- **WHEN** a change's first review covers five pages and finds blocking issues
  across them
- **THEN** the orchestrator splits or re-scopes the slice before correcting all
  five in place

#### Scenario: An exact authorization is materialized before correction
- **WHEN** the first review is broad and the plan contains an exact
  `split_authorizations` entry for the parent
- **THEN** the orchestrator selects and materializes the replacement before any
  further correction

#### Scenario: No exact authorization uses the plan split rules
- **WHEN** the first review is broad and no exact split authorization exists
- **THEN** the orchestrator performs the split or re-scope under the plan's
  scope-preserving split rules and continues, without asking the operator

#### Scenario: No materializer uses the standard change-writer
- **WHEN** an exact split authorization exists but no authorized materialization
  path is available
- **THEN** the orchestrator creates the split under the plan rules with the
  standard change-writer and continues, without improvising a new requirement or
  gate

#### Scenario: One shared root cause avoids an unnecessary split
- **WHEN** the first review finds three blocking loci that all follow from one
  shared root cause inside the accepted scope
- **THEN** the review is not broad solely because of the locus count, and the
  orchestrator may authorize a corrective round within the budget

#### Scenario: A reviewer recommendation is not split authority
- **WHEN** a reviewer recommends a split but the plan contains no exact
  authorization
- **THEN** the orchestrator applies the plan's split rules, or narrows and
  carries forward, and continues without asking the operator

#### Scenario: No operator question for a broad review
- **WHEN** the first review is broad
- **THEN** the orchestrator does not ask the operator to authorize a corrective
  round or a split; it applies the convergence rule and continues

### Requirement: Bookkeeping writes are bounded per stage

The orchestrator SHALL use at most one ledger-writer dispatch per stage. That
dispatch SHALL append the stage checkpoint and all record-only events in one
batch. A record-only update, such as a status line, a dispatch count, or a copied
coverage array, SHALL NOT justify a separate `coder` session.

Workers SHALL return their records in their normal output contract. The
orchestrator SHALL batch those records into the stage-boundary write. A stage
with no decision change and no new evidence SHALL NOT require a writer dispatch.

The evidence packet SHALL be minimal: change, stage, HEAD, diff range or staged
paths, commands with exit codes and counts, artifact paths with hashes, claim
status, findings with classification, and the next action. Digests, UID/GID, and
effective roots SHALL appear only when the change's behavior depends on them.

#### Scenario: Many record-only updates use one writer
- **WHEN** a stage produces a checkpoint update and several copied coverage and
  dispatch records
- **THEN** the orchestrator dispatches one ledger writer for the stage and does
  not spawn a `coder` session per record

#### Scenario: A no-change stage needs no writer
- **WHEN** a stage completes with no decision change and no new evidence
- **THEN** the orchestrator does not dispatch a ledger writer for that stage

#### Scenario: The packet carries only proof
- **WHEN** a documentation change has no image or process behavior
- **THEN** its evidence packet omits image digests and UID/GID instead of
  collecting fields that prove nothing

### Requirement: Operator attestation is authoritative for the state it names

An operator attestation is an explicit statement in the invocation, a dispatch
reply, or a recorded message. The orchestrator and auditor SHALL treat it as
authoritative for its named subject and SHALL NOT re-derive, re-check, or ask
again about that subject.

An attestation record SHALL contain:

- `subject` — `quiescence`, `authority`, `gate`, `evidence`, or `scope`;
- `statement` — the operator's words, verbatim;
- `scope` — the exact change, commit, or artifact it covers;
- `source` — invocation, message, or ledger;
- `recorded_at` — the run timestamp.

A quiescence attestation ends the quiescence question: the orchestrator SHALL
NOT poll the child, restart a probe, or require a second confirmation. An
authority attestation authorizes the named commit or action; the scoped writer
records it and proceeds. A gate attestation is authoritative for the named gate
and is recorded verbatim.

Attestation SHALL NOT make a false repository claim true, SHALL NOT substitute
for a hard content check the auditor can run, and SHALL NOT widen the change's
accepted scope. When an attestation conflicts with a hard repository fact, the
orchestrator SHALL record the conflict and stop for a human-only decision; it
SHALL NOT silently prefer either side.

#### Scenario: Quiescence attestation is not re-derived
- **WHEN** the operator states that a child is quiescent and no terminal result
  has arrived
- **THEN** the orchestrator records the attestation and does not poll or re-probe
  the session before proceeding

#### Scenario: Authority attestation is used as stated
- **WHEN** the operator authorizes committing the accepted source baseline
- **THEN** the hygiene writer records the authorization verbatim and commits the
  named pathspecs without asking again

#### Scenario: Gate attestation clears the named gate
- **WHEN** the operator states that a named human gate is cleared
- **THEN** the orchestrator records the statement and does not re-derive the gate
  state

#### Scenario: Attestation cannot waive a hard content check
- **WHEN** the operator attests that the source is at `HEAD` but the accepted
  path is missing at `HEAD`
- **THEN** the auditor still reports the content check as failed and does not
  treat the attestation as a pass

#### Scenario: Conflicting attestation stops
- **WHEN** an attestation says the tree is clean but the tracked tree has
  uncommitted accepted source
- **THEN** the orchestrator records the conflict and stops for a human-only
  decision instead of silently choosing a side

#### Scenario: Attestation does not widen scope
- **WHEN** an operator attests that a neighbor page is acceptable but the change
  did not accept it
- **THEN** the item remains `adjacent_scope` and does not become accepted work

### Requirement: Per-stage cost and token burn are recorded

Each stage record SHALL include:

- `tokens_in`, `tokens_out`, and `cache_read_tokens` when the harness or child
  reports them;
- `cost_status`: `estimated`, `unresolved`, `unavailable`, or `not-reported`;
- `estimated_cost` and its pricing catalog version when estimated;
- `usage_source` — telemetry, the child's report, or the operator.

The run ledger and final report SHALL include per-stage tokens and cost status,
run totals for the three token categories, a `priced_subtotal`, and an
`unpriced_stage_count`. The orchestrator SHALL NOT report a bare `$0` or
`$0.00` total when any stage is unresolved or unavailable. A stage with observed
usage and an unresolved cost SHALL be reported as `unpriced`, not folded into
zero. A stage with no reported usage SHALL be `not-reported`, and the final
report SHALL say the burn is unknown rather than inventing a number.

A `$0.00` total SHALL be reported only when every stage is estimated at zero or
the run truly used zero tokens.

#### Scenario: Unresolved cost is not a zero
- **WHEN** a stage reports 1.2M input tokens and cost telemetry is `unresolved`
- **THEN** the ledger records the tokens, reports the stage as `unpriced`, and
  the final report does not present the run total as `$0`

#### Scenario: Zero usage is a genuine zero
- **WHEN** every stage used zero tokens with a known model
- **THEN** the report may show `$0.00` because the zero is estimated, not
  unresolved

#### Scenario: Missing usage is unknown, not zero
- **WHEN** neither the harness nor the child reports token usage for a stage
- **THEN** the stage is `not-reported` and the final report says the burn is
  unknown

#### Scenario: The run report carries the burn
- **WHEN** a run finishes
- **THEN** the final report names per-stage tokens, the three run token totals,
  the priced subtotal, and the number of unpriced stages

### Requirement: A stale idle marker does not interrupt a live child

An idle session marker is an observation, not an interruption. The orchestrator
SHALL NOT treat it as an interruption when:

- the ledger records the stage as in flight;
- a live child session exists for that stage; and
- the child has produced progress more recently than the marker, or the marker
  is contradicted by the child's in-flight state.

In that case the orchestrator SHALL record a `stale_idle_marker` observation
with the marker source and the evidence that the child is live, and continue
waiting. It SHALL NOT re-dispatch the stage, cancel the child, open a duplicate
session, or start the next stage.

A stage is interrupted only when no live child exists, no terminal result is
available, and the ledger records the stage as in flight, and the absence is
confirmed by the harness or the operator. The orchestrator SHALL reconcile once
from the ledger before re-dispatching. It SHALL NOT poll repeatedly; the child's
completion notification or one status-scout report is the next event.

#### Scenario: Idle marker with a live child is stale
- **WHEN** a status report says a session is idle while the ledger records the
  stage in flight and the child is still producing progress
- **THEN** the orchestrator records `stale_idle_marker`, does not cancel or
  re-dispatch, and continues waiting

#### Scenario: A missing child is reconciled before re-dispatch
- **WHEN** no live child exists, no terminal result is available, and the ledger
  says the stage is in flight
- **THEN** the orchestrator reconciles once from the ledger, records the evidence,
  and only then re-dispatches the stage

#### Scenario: A terminal result is processed, not interrupted
- **WHEN** the child has returned a terminal result and an idle marker also
  exists
- **THEN** the orchestrator processes the result and does not treat the marker as
  an interruption

#### Scenario: A stale marker is recorded with evidence
- **WHEN** the orchestrator ignores an idle marker under this requirement
- **THEN** the ledger names the marker source and the live-child evidence it used

### Requirement: Implementation order and focused verification are fixed by the slice

For an orchestrator-owned change, the implementer SHALL order work by
correctness risk:

1. scope and registry validation, including authorization;
2. error taxonomy and envelope behavior;
3. cache invariants and concurrency;
4. freshness and persistence semantics;
5. page/UI wiring;
6. presentation and polish.

When a change has no scope or cache layer, the implementer SHALL start at the
highest-risk invariant the change does have. The implementer SHALL run the exact
focused tests the change defines and SHALL NOT perform a broad refactor, rename
unrelated code, or add a new test layer to prove a behavior already proved at a
lower layer.

The implementer SHALL stop only for a real contract conflict: the corrective
handoff contradicts the accepted artifacts, a required prerequisite is missing,
or the environment cannot run the required check. It SHALL NOT stop for a
stylistic question or because one round's automatable work does not fit.

#### Scenario: Correctness precedes the page
- **WHEN** a change has scope/registry validation, an error taxonomy, a cache
  layer, and a page
- **THEN** the implementer completes and verifies the scope, error, and cache
  work before the page

#### Scenario: The change's focused tests are the evidence
- **WHEN** the change defines focused tests for an error envelope and a cache
  collision
- **THEN** the implementer runs those tests and does not substitute a broad
  repository suite or a new end-to-end layer

#### Scenario: A contract conflict stops
- **WHEN** the corrective handoff requires behavior that contradicts the
  accepted spec
- **THEN** the implementer returns `blocked` with the exact conflict

#### Scenario: A stylistic question does not stop
- **WHEN** the implementer could name a file or helper more elegantly
- **THEN** it does not return `blocked` and does not broaden the diff

### Requirement: Handoff evidence is proportional

The Phase Transition Gate SHALL keep every hard reproducibility check:
`tracked-tree-clean`, `source-manifest-at-head`, `plan-at-head`,
`archive-commit-implementation-paths`, `run-state-ignored`, `evidence-at-head`,
`perf-script-from-head`, `fresh-checkout-checks`, `local-only-delivery`, and
`next-change-readiness`.

The auditor and hygiene writer SHALL accept a recorded operator attestation for
the state or authority question it names, and SHALL NOT re-derive that state.
Evidence already recorded in the ledger with a matching hash SHALL NOT be
re-collected; the auditor SHALL read the recorded path and hash. When a live
child and a clean ledger already establish quiescence and stage state, the
orchestrator SHALL NOT dispatch a fresh probe to re-derive them.

The auditor and hygiene writer SHALL return the minimal fields that prove their
checks, not duplicate transcripts. The hygiene writer SHALL still repair in
phases, stage explicit pathspecs, preserve historical originals, and refuse to
invent scope.

An attestation SHALL NOT waive a hard content check the auditor can read from
`HEAD`; `source-manifest-at-head`, `plan-at-head`, and
`archive-commit-implementation-paths` remain machine-verified.

#### Scenario: Recorded evidence is reused
- **WHEN** the ledger already records an accepted source path and its matching
  `sha256`
- **THEN** the auditor reads that path at `HEAD` and does not require a duplicate
  transcript

#### Scenario: Live child and clean ledger need no re-probe
- **WHEN** a live child is working, the ledger is clean, and the stage is in
  flight
- **THEN** the orchestrator does not dispatch a quiescence probe and does not
  re-derive the stage state

#### Scenario: A hard content check still runs
- **WHEN** the operator attests that the source is committed but
  `source-manifest-at-head` is unverified
- **THEN** the auditor still verifies the accepted paths at `HEAD`

#### Scenario: Hygiene accepts recorded authority
- **WHEN** the operator has already authorized the accepted-source commit and the
  attestation is in the ledger
- **THEN** the hygiene writer stages the named pathspecs without asking for the
  authorization again

#### Scenario: A missing hash is still collected
- **WHEN** a ledger evidence entry has a path but no hash
- **THEN** the auditor collects the hash instead of treating the entry as proved

#### Scenario: Duplicate transcripts are not required
- **WHEN** an audit can prove a check from one retained command log and its hash
- **THEN** it does not require a second copy of the same output
