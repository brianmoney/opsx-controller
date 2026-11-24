# add-adaptive-plan-splitting Tasks

## 1. Authoring guidance and command surface

- [x] 1.1 In `core/plan-authoring.md`, add a sizing section that directs
  authors to size each change for one loop before implementation and states
  that the model-level orchestrator records a sizing assessment before the
  implement dispatch.
- [x] 1.2 In `core/plan-authoring.md`, document `split_authorizations` in the
  frontmatter example and define its exact fields: parent, children,
  child_dependencies, obligation_map, aggregate_exit, and optional rehome.
- [x] 1.3 In `core/plan-authoring.md`, state that the field is model-level only,
  adds no manifest key, is not read by `opsx-plan compile`, creates no
  conditional nodes, and requires the selected tracked plan to contain only the
  chosen nodes.
- [x] 1.4 In `core/plan-authoring.md`, document the two-round split-or-continue
  decision, the preferred-split conditions, the one-bounded-correction rule,
  and the proposed re-scope decision when no exact authorization exists.
- [x] 1.5 In `adapters/opencode/commands/opsx-plan.md`, require the author to
  size changes before implementation and to declare or omit exact split
  authorizations, verify slugs/collisions/obligation mapping, and report the
  declared replacements.

## 2. Orchestrator contract

- [x] 2.1 In `adapters/opencode/agents/opsx-orchestrator.md`, add a
  pre-implementation sizing assessment to the stage sequence and require the
  result in the ledger.
- [x] 2.2 Add a split-or-continue decision after two corrective rounds and
  before any third override, with the preferred-split conditions and the
  one-bounded-correction closure test.
- [x] 2.3 Add a preauthorized replacement section defining exact authority,
  eligibility, forbidden changes, obligation/dependency remap, aggregate exit,
  and the rule that general autonomy is not authority.
- [x] 2.4 Add the freeze-then-rebind materialization handoff, the named
  `opsx-split-materializer` dispatch, the no-generic-writer fallback, and the
  clean-revision/graph-binding precondition for dispatching any child.
- [x] 2.5 Add ledger selection states, resume/idempotence rules, and fail-closed
  handling for collisions, unlisted slugs, unmet prerequisites, and plan drift.
- [x] 2.6 Add split decisions and proposed re-scope decisions to the final
  report and canonical stop/continue rules without adding a raw-count stop.

## 3. Named materialization writer

- [x] 3.1 Add `adapters/opencode/agents/opsx-split-materializer.md` with a
  read/write/commit contract limited to one exact authorized split.
- [x] 3.2 Require exact authorization revalidation, parent eligibility, child
  collision checks, obligation/dependency/aggregate-exit checks, and fail-closed
  behaviour.
- [x] 3.3 Require the restorable donor freeze (identity, source/evidence hashes,
  blob or copy lineage) before any dirty-candidate rehome.
- [x] 3.4 Require the selected plan revision, exact rehome paths, explicit
  pathspec commit, compile/graph verification, and machine-readable output.
- [x] 3.5 Forbid archive, spec sync, parent acceptance reuse, product-behavior
  change, unrelated edits, push, rebase, and sub-agent dispatch.

## 4. Specs and tests

- [x] 4.1 Add the `adaptive-plan-splitting` capability spec delta with sizing,
  exact authorization, eligibility, obligation/dependency preservation,
  freeze/bind, ledger/idempotence, and convergence requirements.
- [x] 4.2 Add the `plan-authoring-guidance` requirement for sizing and exact
  split preauthorization.
- [x] 4.3 Add `tests/adapters/split_fixture.py` with a pure transition model for
  eligibility, obligation/dependency preservation, freeze, binding, ledger
  idempotence, and convergence decisions.
- [x] 4.4 Add positive and negative fixture cases for every fail-closed rule,
  including unauthorized, human-gated, archived, colliding, unlisted, orphaned,
  duplicated, dropped-edge, tier/gate change, rehome-without-freeze, early
  child dispatch, drift, and resume cases.
- [x] 4.5 Add static contract regressions over the authoring reference, the
  `/opsx-plan` command, the orchestrator, and the materializer.
- [x] 4.6 Distinguish fixture-proved normative rules from untested live model
  behavior in the test docstring and final report.

## 5. Repository documentation

- [x] 5.1 Add `opsx-split-materializer` and the preauthorized split contract to
  the plan-level agent set in `AGENTS.md`.
- [x] 5.2 Add the sizing/convergence/split path to `README.md` and the OpenCode
  adapter surface list in `docs/adapters.md`.
- [x] 5.3 State that split authority is not implicit in general autonomy or in
  a generic writer/installed model, and that an unprovisioned materializer
  returns a proposed decision.

## 6. Validation

- [x] 6.1 Run `python3 -m unittest discover -t . -s tests`.
- [x] 6.2 Run `node tests/opencode/test-opsx-usage-emitter.js`.
- [x] 6.3 Run `python3 scripts/generate-phase-agents.py --check`.
- [x] 6.4 Run `git diff --check`.
- [x] 6.5 Run `openspec validate add-adaptive-plan-splitting --strict`.
- [x] 6.6 Report exact files, checks, and the scope limit that no live model
  dispatch was exercised.
