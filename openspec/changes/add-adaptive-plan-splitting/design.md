# Design: add-adaptive-plan-splitting

## Context

`opsx-orchestrator` is the model-level plan conductor. It holds no edit or shell
capability and delegates every write. Its current contract sizes work only
through the authoring reference's general loop-aware guidance, and its
corrective-round allowance is advisory: after two rounds, the orchestrator can
record a `cap_override` and continue. There is no explicit sizing decision
before implementation and no authorized way to replace a planned change with a
different set of exact nodes.

That is sufficient for a change whose remaining work is shrinking. It is the
wrong control for a change that was authored too large: the same reviewer
blocker class can reopen, the blocker count can stay flat, or the remaining work
can be orthogonal to the parent's stated concern. More corrective rounds do not
make the unit of work correct.

The deterministic `opsx-plan` / `opsx-run` path is unchanged. It compiles the
markdown plan to TOML and sequences the resulting DAG; it has no model-level
sizing decision and no conditional node concept. This change therefore treats a
split as a **model-level plan revision** consumed by `opsx-orchestrator`, not as
a new manifest format.

## Goals / Non-Goals

**Goals:**

- Make sizing an explicit, recorded step before implementation.
- Make split-versus-continue an explicit decision after two corrective rounds
  and before any third override.
- Give the plan author a way to preauthorize an exact, scope-preserving,
  named replacement without granting general autonomy a plan-edit authority.
- Preserve every parent obligation through a checked mapping, remap
  dependencies, and require an aggregate exit before the plan can complete.
- Freeze and bind a clean selected plan revision before dispatching a child.
- Make selection, resume, and idempotence deterministic and fail closed.
- Keep the conductor read-only and keep exactly one writer active at a time.
- Add a testable transition model and contract regressions without pretending
  a prompt-only runtime has been exercised end-to-end.

**Non-Goals:**

- A new TOML manifest key, a conditional DAG, or automatic compilation of prose
  such as "split this if it gets hard".
- Changing the acceptance tier, phase tier, `human_gates`, `pause_before`, trust
  or privacy posture, acceptance policy, or any archived change.
- Reopening, archiving, or spec-syncing the original parent as part of a split.
- Reusing the parent's checked tasks as child acceptance.
- Granting `opsx-repo-hygiene`, `opsx-implementer`, or any installed model a
  general plan-rewrite or split capability it does not declare.
- Proving live model compliance with a unit test. The fixture proves the
  normative rules; live behavior remains a prompt-contract regression.

## Decisions

### D1. Size before implementation, not after failure

Before the implement dispatch for a change, the orchestrator records a sizing
assessment: the change's obligations, the expected touched surfaces, the
expected review surface, and the expected number of corrective rounds. If the
assessment cannot plausibly fit one loop, or an exact split authorization exists
for that parent, the orchestrator makes a split-or-continue decision before
implementation.

Sizing is an assessment, not a numeric score. It exists to prevent a known
oversized unit from consuming the corrective allowance, not to replace the
reviewer's judgment.

### D2. After two corrective rounds, split-or-continue is explicit

After two corrective rounds and before any third round override, the
orchestrator records one decision:

- **split** — only when an exact preauthorized replacement exists for this
  parent and child set;
- **bounded continue** — exactly one more corrective round, with a named closure
  test and a re-evaluation checkpoint;
- **proposed re-scope/authority decision** — when a split is indicated but no
  exact authorization exists.

A split is preferred when a blocker class that was previously corrected has
reopened, the blocker set is not shrinking across rounds, or the remaining work
is orthogonal to the parent's stated concern. A bounded continue is not a
licence for a second override for the same blocker class: if the closure test
fails, the next decision is split or proposed re-scope, not another override.

No decision is ever "stop because the round count was reached". Independent
work that is already authorized by the plan may continue while the blocked
change awaits a re-scope/authority decision.

### D3. Preauthorization is exact, model-level, and opt-in

`split_authorizations` is an optional plan-frontmatter list. Each entry names:

```yaml
split_authorizations:
  - parent: <exact planned parent slug>
    children:
      - <exact child slug>
      - <exact child slug>
    child_dependencies:
      <child slug>: [<exact prerequisite slugs>]
    obligation_map:
      <child slug>: [<parent obligation ids>]
    aggregate_exit: "<how the parent's success parameters are proved after all children archive>"
    rehome:
      <child slug>: [<exact donor paths>]   # optional
```

The child list must contain two or more unique, verb-led slugs. Every child
dependency is an exact plan slug or a declared dependency; every parent
obligation (requirement, finding, success parameter, or deferral) appears in
exactly one child's map; the union of child obligations equals the parent's
obligations with no additions. The aggregate exit names the commands or
observable conditions that prove the parent's original success parameters after
all children have archived.

This field is read only by the model-level orchestrator. It is not a compiled
manifest key, it is not read by `opsx-plan compile`, and it does not make the
plan conditional. When an entry is selected, the tracked plan is revised so the
parent heading is replaced by the chosen child headings; the parent and all
unchosen nodes are absent. No prose is interpreted as an authorization.

### D4. Only an orchestrator-owned, unaccepted parent may be replaced

A split is eligible only when:

- the parent is in the plan and is not in the resolved human-gate set;
- the parent is unaccepted: it is not archived and its accepted implementation
  has not been frozen by an archive;
- no child is in the resolved human-gate set, already active as a different
  change, already archived, or already a node in the plan;
- the parent and children stay in the parent's phase, and every child inherits
  the same effective acceptance tier;
- the plan's `acceptance`, phase gates, `human_gates`, `pause_before` intent,
  trust/privacy posture, and acceptance policy are unchanged; the split adds,
  removes, or moves no gate;
- no archived change, archived delta, or synced spec is edited.

The orchestrator's general autonomy, a reviewer's `fix` verdict, a cap override,
and a supervisor's role are all insufficient authority. Only an exact
`split_authorizations` entry is authority. A named supervisor may select one
only through an explicitly provisioned capability that declares that action;
absent that declaration it fails closed and reports the proposed decision.

### D5. The replacement remaps rather than duplicates obligations

Every predecessor of the parent becomes a prerequisite of each entry child that
needs it; no child depends on the parent. Every existing change that depended on
the parent depends on **all** children, and the aggregate exit proves the
parent's original success parameters after the last child archives. Cross-child
dependencies are only the exact ones declared in `child_dependencies`.

An unmapped parent obligation, an obligation mapped to two children, a child
obligation not drawn from the parent, a dropped dependency, an unknown
dependency slug, a child that depends on the parent, or a successor that does
not point at the full child set is an authoring or selection defect: the
orchestrator does not select the entry and returns the defect as a proposed
decision.

Checkboxes are not obligations for this purpose. A parent's checked tasks are
evidence of prior progress in the donor, never acceptance of a child. Child
tasks are authored fresh; a rehomed source file does not complete a child task
until that child's own review evidence accepts it.

### D6. Freeze, rebind, bind, then dispatch

A dirty parent candidate is never moved before it can be restored. The
materializer first writes a freeze manifest containing the donor identity
(change directory, active revision, worktree status), the source and evidence
hashes, the blob or copy lineage, and a restorable location under the run state.
Only then does it rehome the exact paths in the authorization's `rehome` map.
A dirty candidate with no freeze record, or a path absent from both the
authorization and the donor, blocks the materialization.

The materialization handoff is one named writer, `opsx-split-materializer`. It:

1. revalidates the exact authorization, plan hash, parent eligibility, child
   collisions, obligation map, and dependency remap;
2. freezes the donor and records the hashes/lineage;
3. revises the tracked plan to contain only the chosen nodes and updated
   aggregate exit;
4. rehomes only exact authorized donor paths, with no product-behavior change;
5. stages explicit pathspecs and makes one local commit (no push, no archive,
   no spec sync);
6. verifies `opsx-plan compile` on the selected plan, `git diff --check`, the
   plan revision hash, and the node/edge set against the approved map;
7. returns the freeze manifest, commit, hashes, checks, and residual candidate
   state.

The orchestrator dispatches no child work until:
- the selected plan revision is committed at `HEAD`;
- the plan contains only the chosen nodes;
- the compiled graph and dependency map are verified and bound to the ledger;
- the parent has not been archived or spec-synced;
- the freeze/lineage record is complete.

If the materializer is unavailable and no named supervisor declares the
capability, the orchestrator does not substitute `opsx-repo-hygiene`,
`opsx-implementer`, or an installed model's generic edit ability. It records
`split-blocked-no-authorized-materializer` and returns the proposed decision.

### D7. Ledger selection and resume are idempotent

The ledger holds one split record per parent and selection:

- `selected` — exact authorization and plan hash captured;
- `frozen` — donor manifest, source/evidence hashes, lineage recorded;
- `materialized` — plan revision and rehomed paths written, not yet bound;
- `bound` — commit, plan revision hash, node/edge set, and compile result
  recorded; children may start.

There is at most one non-terminal split per parent; a second selection while one
is pending is refused. Resume reads the ledger first: a `bound` record with the
same plan hash is a no-op; a `selected`/`frozen`/`materialized` record is
revalidated against the captured hashes and resumes at the earliest incomplete
step; a record whose parent, children, or plan hash no longer matches is plan
drift and fails closed with a proposed decision. A child dispatch recorded before
`bound` is invalid and is reported, not replayed.

### D8. Tests prove the rules, not the model

The runtime is a prompt surface, so a unit test cannot prove that a live model
follows it. The change adds:

- static contract regressions over `core/plan-authoring.md`, the `/opsx-plan`
  command, `opsx-orchestrator`, and `opsx-split-materializer`;
- a pure `tests/adapters/split_fixture.py` transition model for eligibility,
  obligation/dependency preservation, freeze, binding, ledger idempotence, and
  convergence decisions;
- positive and negative fixtures for every fail-closed case.

The test docstrings and the final report distinguish the fixture's normative
coverage from live model behavior, which remains unverified until a real run.
