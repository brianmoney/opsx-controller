# add-plan-convergence-controls Tasks

## 1. Contracts and fixtures

- [ ] 1.1 Add `tests/adapters/convergence_fixture.py`: a stdlib-only, pure
  protocol fixture with `classify_finding(adjacent_scope, introduced_by_diff)`
  using the precedence in the `plan-handoff-gate` delta,
  `convergence_decision(rounds_used, findings, first_review, surfaces,
  split_authorization)` returning one of `correct|split|rescope|carry-forward`,
  and
  `cost_summary(stages)` returning token totals, priced subtotal, unpriced count,
  and a never-bare-`$0` presentation flag. Add
  `stale_status_decision(ledger_in_flight, live_child, progress_after_marker,
  terminal_result)` returning `wait|reconcile-once|process-result`.
- [ ] 1.2 Add `tests/adapters/test_plan_convergence_controls.py` with static
  contract assertions over `adapters/opencode/agents/opsx-orchestrator.md`,
  `opsx-review.md`, `opsx-change-writer.md`, `opsx-integration-auditor.md`,
  `opsx-repo-hygiene.md`, `core/phase-agents/implementer.md`,
  `core/plan-authoring.md`, and `adapters/opencode/commands/opsx-plan.md` for
  the two-round budget, finding classification, first-review split checkpoint,
  operator attestation, cost/burn reporting, stale idle handling, bounded
  bookkeeping, implementer risk order, and maximum surface heuristic.
- [ ] 1.3 Add behavior tests to `tests/adapters/test_plan_convergence_controls.py`
  over `convergence_fixture.py` for: adjacent-only findings do not earn a round;
  a pre-existing finding is carry-forward; an introduced defect earns a round;
  an unknown classification is resolved before a round; two exhausted rounds
  return split, re-scope, or carry-forward with no operator question; an exact
  split is selected before correction; a broad first review does not dispatch a
  correction; unresolved cost is `unpriced`,
  genuine zero usage is `$0`, and missing usage is `not-reported`; a stale idle
  marker with a live child waits, and a missing child reconciles once.
- [ ] 1.4 Update `tests/adapters/test_autonomous_plan_continuation.py`: replace
  the assertion that `MAX_DISPATCHES`, the two corrective rounds, and the two
  repair/audit rounds are all advisory with separate assertions that
  `MAX_DISPATCHES` and repair/audit rounds are advisory and that review
  corrective rounds use the two-round convergence budget. Update the canonical
  stop-list assertion so a corrective-round cap is not listed as a generic cap
  stop.
- [ ] 1.5 Update `tests/adapters/test_plan_handoff_gate.py`: keep
  `test_cap_override_permits_a_third_repair_round` for repair/audit rounds; add
  assertions that a third corrective round is refused without split, re-scope,
  or carry-forward, that an operator attestation is recorded and not re-derived,
  and that the auditor/hygiene surfaces reuse hash-bound ledger evidence. Do not
  add a test that a review-round cap permits a third corrective round.
- [ ] 1.6 Update `tests/adapters/test_plan_orchestrator_contract.py` with the
  maximum-surface heuristic in `core/plan-authoring.md` and
  `adapters/opencode/commands/opsx-plan.md`: named-surface count, pages/
  endpoints, acceptance scenarios, task count, expected diff size, the mandatory
  API + CLI + page + freshness + error-taxonomy split, and the requirement that
  the command record and report the assessment.
- [ ] 1.7 Reconcile the in-flight adaptive-splitting contract if it is present:
  in `tests/adapters/test_adaptive_plan_splitting.py`, keep every split
  authority, obligation, freeze, materialization, and idempotence assertion,
  and replace the "continue permits one bounded corrective round" assertion with
  "continue is replaced by autonomous split or re-scope; there is no
  operator-authorization path". Do not duplicate,
  rename, or re-implement `tests/adapters/split_fixture.py`, and do not edit
  `openspec/changes/add-adaptive-plan-splitting/` in this change.
- [ ] 1.8 Confirm the new test package is discovered: `tests/adapters/` already
  has `__init__.py`; run `python3 -m unittest
  tests.adapters.test_plan_convergence_controls` from the repository root and
  fix discovery or imports before proceeding.

## 2. Orchestrator convergence policy

- [ ] 2.1 In `adapters/opencode/agents/opsx-orchestrator.md`, replace the
  post-two-round "continue with one bounded corrective round" text with the
  two-round default: split from an exact `split_authorizations` entry or the
  plan's scope-preserving split rules, re-scope/narrow and carry the remainder
  forward, then continue. State that a third-round `cap_override` is refused,
  that there is no operator-authorization path, and that no terminal decision
  request is returned.
- [ ] 2.2 Add a `## Review convergence and finding classification` section to
  `adapters/opencode/agents/opsx-orchestrator.md` defining
  `introduced_by_diff`, `adjacent_scope`, and `pre_existing`, the precedence
  rule, the `unknown` adjudication rule, and the disposition of each class.
  State that only `introduced_by_diff` earns a corrective round.
- [ ] 2.3 Add the broad-first-review checkpoint to
  `adapters/opencode/agents/opsx-orchestrator.md`: the named-surface trigger,
  the three-or-more-uncorrelated-loci trigger, the three-or-more unprovable
  acceptance scenarios trigger, and the rule that the split-or-re-scope
  decision happens before any corrective dispatch. Link it to the exact
  `split_authorizations` entry and the plan's scope-preserving split rules. If
  the baseline does not provide `split_authorizations` or
  `opsx-split-materializer`, use the plan rules with the standard change-writer;
  if no split rule covers the remainder, narrow and carry forward. Do not ask
  the operator and do not invent a materializer or a plan edit.
- [ ] 2.4 Add a `## Operator attestation` section to
  `adapters/opencode/agents/opsx-orchestrator.md` with the record fields
  `subject`, `statement`, `scope`, `source`, and `recorded_at`; state that a
  quiescence, authority, gate, evidence, or scope attestation is authoritative
  for its subject and is not re-derived, that it cannot make a false repository
  claim true or widen scope, and that a conflict with a hard fact stops.
- [ ] 2.5 Add a `## Per-stage cost and burn` section to
  `adapters/opencode/agents/opsx-orchestrator.md`: per-stage `tokens_in`,
  `tokens_out`, `cache_read_tokens`, `cost_status`, `estimated_cost`, and
  `usage_source`; run totals; `priced_subtotal` and `unpriced_stage_count`; the
  rule that unresolved or unavailable cost is `unpriced`, missing usage is
  `not-reported`, and a bare `$0` is forbidden when any stage is unpriced.
- [ ] 2.6 Add the stale-idle-marker rule to
  `adapters/opencode/agents/opsx-orchestrator.md` under status handling: wait
  when the ledger is in flight and a live child has newer progress; record
  `stale_idle_marker` with the marker source and live evidence; do not
  re-dispatch, cancel, duplicate, or start the next stage; reconcile once only
  when no live child and no terminal result remain.
- [ ] 2.7 Add a bounded-bookkeeping rule to
  `adapters/opencode/agents/opsx-orchestrator.md` and the `## Run ledger`
  section: at most one ledger-writer dispatch per stage, one batched write for
  checkpoint plus record-only events, no separate `coder` session per record,
  no writer when a stage has no change or evidence, and the minimal evidence
  packet fields.
- [ ] 2.8 Update `## Advisory caps` in
  `adapters/opencode/agents/opsx-orchestrator.md`: keep `MAX_DISPATCHES` and
  repair/audit rounds advisory; state that a corrective round is not a generic
  cap; state that a dispatch or repair override never authorizes a corrective
  round.
- [ ] 2.9 Update `## Stage selection` step 7 in
  `adapters/opencode/agents/opsx-orchestrator.md`: after the first review,
  evaluate the broad-review checkpoint before correction; after two corrective
  rounds, evaluate split or re-scope before any further correction; record the
  decision and its basis.
- [ ] 2.10 Update `## Dispatch contract` and `## Worker invocation fields` in
  `adapters/opencode/agents/opsx-orchestrator.md`: pass `SUCCESS_PARAMETERS`,
  `SLICE_SURFACES`, and `PRIOR_CLASSIFICATIONS` to `opsx-review`; pass
  `OPERATOR_ATTESTATIONS` and `EXISTING_EVIDENCE` to
  `opsx-integration-auditor` and `opsx-repo-hygiene`; pass the convergence
  cycle and budget to `opsx-implementer`.
- [ ] 2.11 Update `## Stopping conditions (canonical stop list)` in
  `adapters/opencode/agents/opsx-orchestrator.md`: remove the generic
  corrective-round cap from the "never stop" list, add "a corrective budget
  exhausted with no split authorization uses the plan split rules or narrows and
  carries forward, then continues; it is not a raw-count stop and does not ask
  the operator", and keep dispatch and repair caps on the never-stop list.
- [ ] 2.12 Update `## Final report` in
  `adapters/opencode/agents/opsx-orchestrator.md` to include the convergence
  decision and basis, finding classification counts, operator attestations
  verbatim, per-stage token and cost status, run token totals, priced subtotal,
  unpriced stage count, stale-marker observations, and the autonomous
  split/re-scope/carry-forward decision. Forbid a bare `$0` total when any stage is unpriced.

## 3. Reviewer contract

- [ ] 3.1 In `adapters/opencode/agents/opsx-review.md`, replace `## Cycle
  discipline` with "first review and every re-review are full-scope": read the
  accepted success parameters, artifacts, full diff, and prior findings;
  enumerate every current-slice blocking finding; report prior findings as
  closed, still open, or superseded; record a late current-slice finding with
  unchanged evidence as a reviewer process defect.
- [ ] 3.2 Add `SUCCESS_PARAMETERS`, `SLICE_SURFACES`, and
  `PRIOR_CLASSIFICATIONS` to the `opsx-review` input contract, and instruct the
  reviewer to judge only the accepted success parameters and the derived
  coverage matrix.
- [ ] 3.3 Add `introduced_by_diff` and `adjacent_scope` to every `findings`
  entry in `adapters/opencode/agents/opsx-review.md`; define the classification
  precedence; state that only `introduced_by_diff` enters `fix_prompt` and
  `findings`; state that `unknown` does not earn a round and is adjudicated by
  the orchestrator.
- [ ] 3.4 Add an `out_of_scope[]` array to the reviewer output contract in
  `adapters/opencode/agents/opsx-review.md` for `adjacent_scope` and
  `pre_existing` findings, with locus, observed, required, evidence, impact,
  proposed slice for adjacent items, disposition, `introduced_by_diff`, and
  `adjacent_scope`. State that a `pass` may carry `out_of_scope[]` entries and
  that they do not change the verdict.
- [ ] 3.5 Add the evidence-format rule to
  `adapters/opencode/agents/opsx-review.md`: a log shape, transcript style,
  preferred artifact, field order, or command spelling is not blocking when the
  behavior is proved; an equivalent proof is accepted; a false claim and an
  accepted-behavior defect still block.
- [ ] 3.6 Add `scope: "current-slice" | "adjacent"` to every `coverage[]`
  entry in `adapters/opencode/agents/opsx-review.md`; state that a
  `not-covered` current-slice case concerning accepted behavior blocks, a
  `not-covered` adjacent case does not, and a next-phase compatibility case is
  adjacent unless the current success parameters require it now.
- [ ] 3.7 Keep the post-archive `carry_forward[]` contract in
  `adapters/opencode/agents/opsx-review.md` unchanged, and state that
  `out_of_scope[]` is not a substitute for a carry-forward item whose target is
  an archived change.

## 4. Change writer contract

- [ ] 4.1 In `adapters/opencode/agents/opsx-change-writer.md`, add a pre-writing
  surface assessment: named surfaces, pages/endpoints/routes, acceptance
  scenarios, task count, and expected production diff. Require a split proposal
  before writing when the change exceeds the maximum or spans API + CLI + page +
  freshness + error taxonomy.
- [ ] 4.2 Add the acceptance-surface enumeration to
  `adapters/opencode/agents/opsx-change-writer.md`: error taxonomy, scope and
  registry cases, cache invariants, parity checks, and guards. Require the
  enumeration to appear in the design, spec deltas, and tasks before
  implementation.
- [ ] 4.3 State in `adapters/opencode/agents/opsx-change-writer.md` that the
  writer authors one concern, does not create the proposed child changes, does
  not edit the plan, and does not let a review correction silently widen the
  enumerated surface.
- [ ] 4.4 Extend the change-writer output contract in
  `adapters/opencode/agents/opsx-change-writer.md` with
  `surface_assessment` (`named_surfaces`, `pages_endpoints`, `scenarios`,
  `tasks`, `expected_diff_lines`) and `split_proposal` (`required`, `parent`,
  `children`, `surface_map`, or `none`). Keep the existing `carry_forward`
  fields.

## 5. Implementer contract

- [ ] 5.1 In `core/phase-agents/implementer.md` plan-orchestrator mode, add the
  risk order: scope/registry and authorization, error taxonomy, cache
  invariants and concurrency, freshness and persistence, page/UI, polish. State
  that a change without scope or cache starts at its highest-risk invariant.
- [ ] 5.2 In `core/phase-agents/implementer.md`, require the exact focused
  tests the change defines, forbid broad refactors and unrelated renames, forbid
  a new test layer for behavior already proved lower, and state the
  `blocked`-only-for-a-real-contract-conflict rule. Do not change the
  controller-round workflow.
- [ ] 5.3 Regenerate every adapter and plugin implementer output with
  `python3 scripts/generate-phase-agents.py`; do not hand-edit
  `adapters/opencode/agents/opsx-implementer.md` or any other generated file.
- [ ] 5.4 Run `python3 scripts/generate-phase-agents.py --check` and confirm no
  drift.

## 6. Auditor and hygiene ceremony

- [ ] 6.1 In `adapters/opencode/agents/opsx-integration-auditor.md`, add a
  proportional-evidence section: accept a recorded operator attestation for the
  state or authority it names; reuse ledger evidence with a matching hash; do
  not dispatch a fresh quiescence probe when a live child and clean ledger
  establish state; return minimal proof fields, not duplicate transcripts.
- [ ] 6.2 State in `adapters/opencode/agents/opsx-integration-auditor.md` that
  attestation never waives `source-manifest-at-head`, `plan-at-head`, or
  `archive-commit-implementation-paths`, and that a missing hash is collected,
  not assumed.
- [ ] 6.3 Extend the auditor output contract in
  `adapters/opencode/agents/opsx-integration-auditor.md` with
  `attestations[]` (subject, statement, scope, source) and
  `evidence_reused[]` (path, sha256, ledger source). Do not remove any existing
  check, blocker, or next-action field.
- [ ] 6.4 In `adapters/opencode/agents/opsx-repo-hygiene.md`, accept a recorded
  operator authority attestation without asking again, reuse hash-bound ledger
  evidence, and keep the report minimal. Keep Phase A/Phase B, explicit
  pathspecs, original preservation, and the refuse-to-invent-scope rule
  unchanged.
- [ ] 6.5 Add `attestations[]` to the hygiene output contract in
  `adapters/opencode/agents/opsx-repo-hygiene.md` and state that an attestation
  authorizes only the named commit or action, never a wider pathspec.

## 7. Plan-authoring guidance

- [ ] 7.1 In `core/plan-authoring.md`, add the maximum-surface table and the
  named-surface definition under `## Loop-Aware Scoping`: 3 named surfaces, 3
  pages/endpoints, 5 acceptance scenarios, 12 tasks, 400 expected production
  changed lines. State the mandatory split for API + CLI + page + freshness +
  error taxonomy and for three or more named surfaces.
- [ ] 7.2 In `core/plan-authoring.md`, make the split checkpoint the default
  response to an oversized change, not another review override; require an
  exact `split_authorizations` entry when one exists, otherwise an autonomous
  split under the plan rules or narrowing/carry-forward, and forbid leaving the
  oversized change for corrective rounds.
- [ ] 7.3 In `core/plan-authoring.md`, require the surface assessment in the
  change section or authoring response and require the error taxonomy,
  scope/registry cases, cache invariants, parity checks, and guards to be
  enumerated before writing.
- [ ] 7.4 In `adapters/opencode/commands/opsx-plan.md`, add an entry rule and a
  self-verification step for the surface assessment and mandatory split, and
  require the final response to report every change that was split, narrowed, or
  proposed for re-scope.
- [ ] 7.5 Sync `adapters/codex-cli/plugin/opsx-controller/plan-authoring.md`
  byte-for-byte from `core/plan-authoring.md` after 7.1–7.3; verify with `cmp
  -s core/plan-authoring.md
  adapters/codex-cli/plugin/opsx-controller/plan-authoring.md`.

## 8. Documentation

- [ ] 8.1 Update `AGENTS.md` plan-level agent and sizing sections: describe the
  two-round corrective budget, finding classification, broad-first-review split
  checkpoint, operator attestation, bounded bookkeeping, cost/burn reporting,
  stale-idle handling, and maximum surface heuristic. Keep the existing split
  authority and human-gate rules intact.
- [ ] 8.2 Update `README.md` with the convergence policy and the statement that
  a third corrective round is not an advisory-cap override. Keep the Phase
  Transition Gate and archive-only stop language.
- [ ] 8.3 Update `docs/adapters.md` OpenCode section for the orchestrator,
  reviewer, change-writer, implementer, auditor, and hygiene contract changes.
- [ ] 8.4 Update `core/model-efficiency-workflow.md` cost-status and reporting
  sections: distinguish `estimated`, `unresolved`/`unpriced`, and
  `not-reported`; state that a run total with unpriced stages is not `$0`; add
  the per-stage token and priced-subtotal fields to the run report guidance.
- [ ] 8.5 Confirm no documentation still says all review corrective rounds are
  advisory: search `AGENTS.md`, `README.md`, `docs/adapters.md`,
  `core/plan-authoring.md`, and `adapters/opencode/agents/opsx-orchestrator.md`
  for the old "review corrective rounds are advisory" phrasing and correct it.

## 9. Installer and validation

- [ ] 9.1 Run `python3 -m unittest discover -t . -s tests` and `node
  tests/opencode/test-opsx-usage-emitter.js` from the repository root; both
  pass.
- [ ] 9.2 Run `python3 scripts/generate-phase-agents.py --check`; no drift.
- [ ] 9.3 Run `bash install.sh --global --only opencode --verify` into a
  temporary `HOME` under `/tmp/opencode`; confirm the updated orchestrator,
  reviewer, change-writer, implementer, auditor, hygiene agent, `/opsx-plan`
  command, and plan-authoring reference are deployed.
- [ ] 9.4 Confirm the temporary-`HOME` install reports verification success and
  does not modify this repository's worktree.
- [ ] 9.5 Run `openspec validate add-plan-convergence-controls --strict` and
  confirm it passes with no warnings.
- [ ] 9.6 After merge, the maintainer re-runs `bash install.sh --global
  --verify` on the real machine to deploy the updated agents and reference
  (manual).
