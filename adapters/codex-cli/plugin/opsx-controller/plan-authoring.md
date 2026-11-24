# Plan-Authoring Reference

The single client-neutral reference for authoring compilable, loop-sized
OpenSpec implementation plans that `opsx-plan compile` can convert into a
runnable TOML manifest.

Intended audience: models in plain chat, operators editing markdown directly,
and adapter-specific plan-authoring surfaces that defer to this document.

## Before Writing

Read these first so every capability reference is real, no slug collides with
existing ids, and the document structure mirrors the repository:

1. **`AGENTS.md`** (or the client-appropriate project guidance file). It
   documents the repository's conventions, test commands, and quality gates.
2. **Source material** referenced in the request: review findings, design
   notes, attached files.
3. **`openspec/config.yaml`** — the active OpenSpec schema, so change
   artifacts follow the repository's configured layout.
4. **Existing capabilities** under `openspec/specs/`. Every named capability
   directory there is an existing capability. Proposed capabilities must be
   genuinely new, declared in the plan's `Capability Ownership` section, and
   marked `(proposed)` in the first change that introduces them.
5. **Active and archived change ids** under `openspec/changes/` and
   `openspec/changes/archive/`. New slugs must not collide. Changes already
   shipped or archived must not be placed back into the runnable set.

## Document Structure

A compilable plan document has this shape. The compiler (`opsx-plan compile`)
reads headings and paragraph semantics — deviations produce wrong DAG edges or
fail compilation.

### Frontmatter

```yaml
---
title: Short Descriptive Title
doc_type: implementation-plan
status: proposed
acceptance: poc
updated: YYYY-MM-DD
human_gates:            # optional; omit or leave empty when nothing is gated
  - <planned-change-slug>
split_authorizations:   # optional; exact preauthorized named replacements
  - parent: <planned-change-slug>
    children: [<child-slug>, <child-slug>]
    child_dependencies:
      <child-slug>: [<prerequisite-slug>]
    obligation_map:
      <child-slug>: [<parent-obligation-id>]
    aggregate_exit: <how the parent's success parameters are proved after all children archive>
    rehome:              # optional; exact donor paths for unaccepted candidate source
      <child-slug>: [<path>]
---
```

`acceptance` is the plan's blocking threshold, not a status claim. It is one of:

- `poc` — a bounded proof of concept. Only a broken core path, a safety or
  credential exposure, or a false claim blocks. Hardening and release-grade
  gaps are follow-ups.
- `phase` — phase-gated delivery. A red phase gate or unmet phase success
  parameter blocks.
- `release` — release qualification. Digest binding, clean-host/Compose
  qualification, rollback/restore, offline dependencies, the classified suite,
  and detached-process containment are all in scope.

Default to `poc` only when the plan really is a bounded proof of concept. Set
`phase` or `release` explicitly when those gates are in scope; do not leave a
release plan at the default. The model-orchestrated `opsx-orchestrator` resolves
the tier as: explicit invocation value, then this frontmatter field, then
`poc`. The field is plan metadata; it does not replace the plan's success
parameters or gates.

`human_gates` is an optional list of exact change slugs declared in the same
plan. A listed change is operator-gated for a model-level plan run: the
`opsx-orchestrator` stops before it, does not author, implement, review, or
archive it, and reports the resume instruction. A later change whose dependency
is a gated change is not started either. Changes that are not listed are
orchestrator-owned and are driven to completion without per-change questions.

`human_gates` is distinct from `pause_before`, and neither substitutes for the
other:

| Field | Declared in | Read by | Gates |
|-------|-------------|---------|-------|
| `human_gates` | plan frontmatter | the model-level `opsx-orchestrator` run | a run-level stop before the change |
| `pause_before` | compiled TOML manifest | the `opsx-plan` / `opsx-run` controller | an approval step in the compiled DAG |

An absent or empty `human_gates` field means no human gates: every planned
change is treated as orchestrator-owned. A value that names a slug with no
`### Change:` heading in the plan is an authoring defect, not a silently
ignored entry — the model-level run fails closed at preflight and reports the
unknown slug.

The field belongs to the plan document only: it adds no manifest key and needs
no compile change. An explicit `HUMAN_GATES` value supplied to a model-level
run replaces this list for that run; that is a runtime input, not an authoring
edit, and the plan frontmatter is left unchanged.

### Split Authorizations

`split_authorizations` is an optional list of **exact preauthorized named
replacements** for the model-level `opsx-orchestrator`. It is the only
preauthorized authority for replacing a planned change with a different set of
nodes; where it holds no entry for the parent, a split is permitted only where
the plan's own split rules or text permit one, under the same materializer. It
is not a conditional-DAG format, it adds no compiled-manifest key, and
`opsx-plan compile` does not read it. When an entry is selected, the tracked plan
is revised to contain only the chosen nodes; the parent and every unchosen node
are absent, so the deterministic compiler still receives an ordinary
unconditional plan.

Each entry has this shape:

```yaml
split_authorizations:
  - parent: exact-planned-parent-slug
    children:
      - exact-child-slug
      - exact-child-slug
    child_dependencies:
      exact-child-slug: [exact-prerequisite-slug]
    obligation_map:
      exact-child-slug: [parent-requirement-id]
    aggregate_exit: "The command or observable condition proving the parent's original success parameters after every child archives."
    rehome:                     # optional; only unaccepted donor source/evidence
      exact-child-slug: [path/to/candidate]
```

The entry is valid only when all of these hold:

- the parent is an exact `### Change:` heading in this plan;
- each child is a unique kebab-case, verb-led slug that is not an existing
  active or archived change and is not already a planned node;
- every child stays in the parent's phase and inherits the parent's effective
  acceptance tier;
- every parent obligation (requirement, finding, success parameter, or
  deferral) appears in exactly one child's `obligation_map`, and no child
  obligation is outside the parent's set;
- the dependency map preserves every real edge: a parent predecessor remains a
  prerequisite of each child that needs it, no child depends on the parent, and
  every existing successor depends on all children;
- `aggregate_exit` proves the parent's original success parameters after the last
  child archives.

A split authorization SHALL NOT list a parent or child that is in
`human_gates`, change `acceptance`, add or remove a phase or approval gate,
change the trust/privacy posture, or touch an archived change. The parent SHALL
NOT be archived or spec-synced as part of the replacement, and its checked tasks
are never reused as child acceptance.

General autonomy, a reviewer verdict, a `cap_override`, and a supervisor role are
not split authority. The orchestrator selects an entry only by exact match and
fails closed otherwise. When a split is indicated but no exact entry exists, it
returns a proposed re-scope/authority decision rather than editing the plan or
stopping on a raw round count. Authors must not describe a split as prose such
as "split this if it gets hard": conditional prose authorizes nothing.

### Sections (in order)

1. **`# <Title>`** — matches the frontmatter title.
2. **`## Purpose`** — why the plan exists and what it addresses.
3. **`## Capability Ownership`** — required whenever new capability
   directories are proposed. List each proposed capability and its rationale.
4. **`## Phase N: <Name>`** — one per phase. `N` is an integer, 0-based or
   1-based, used consistently throughout the document.
5. **`### Change: \`<slug>\``** — one per change inside its phase. The slug
   is a unique kebab-case OpenSpec change id, verb-led (e.g. `add-`,
   `enforce-`, `extract-`, `replace-`).

### Change Body Fields (in order)

Each change body contains these six fields in this exact order:

- **`**Purpose:**`** — one sentence on what the change achieves.
- **`**Depends on:**`** — the dependency paragraph (see Machine-Read
  Convention below). Begins with `None.` when the change has no dependencies.
  This paragraph extends to the first blank line.
- **`**Capability:**`** or **`**Capabilities:**`** — existing capability
  names or proposed capability names. A proposed capability uses the form
  `` `name` (proposed; see Capability Ownership). ``
- **`**Scope:**`** — what the change covers.
- **`**Out of scope:**`** — explicit non-goals for this change.
- **`**Success parameters:**`** — verifiable, executable criteria that
  confirm the change is complete.

### Closing Sections

After the last phase:

- **`## Recommended Sequence`** — the intended implementation order,
  accounting for dependencies.
- **`## Overall Completion Criteria`** — the cross-change conditions that
  mark the plan as done.
- **`## Explicit Non-Goals`** — work the plan deliberately excludes.
- **`## Suggested Manual Gates`** — changes where an operator should add
  `pause_before = true` to the compiled manifest. The compiler records but
  does not invent these gates; list them here so the operator knows where to
  apply them. This section feeds the compiled-manifest gate only; a
  model-level run's stop list is the `human_gates` frontmatter field.

## Manual Tasks in tasks.md

A task line whose text ends with the marker `(manual)` (case-insensitive,
trailing whitespace tolerated) is an **operator-only manual task**. The
controller, implementer, reviewer, and archiver all classify tasks with this
same marker, so an unchecked `(manual)` task never blocks an unattended run:
implement may leave it unchecked, review does not flag it, and archive still
succeeds — recording it as an operator checklist to complete after the change
is archived.

For a change that will run unattended under `opsx-plan`, keep side-effecting
live-runtime verification out of `tasks.md` entirely. Express operator
follow-up as prose in the proposal or design, or cover it with automated
tests. If a manual step must stay a task, mark the task line `(manual)`.
An unchecked automatable task fails the run before archive — the controller
re-enters implement, and if the task stays unchecked the run fails naming it.
An unmarked manual task in an unattended run is therefore an authoring
defect: it traps the run in a failure loop that only the `(manual)` marker
(or moving the step out of `tasks.md`) resolves.

## Machine-Read Compile Convention

These eleven rules are interpreted by `opsx-plan compile`. Follow them
exactly — a mis-stated dependency line becomes a wrong edge in an unattended
automation DAG.

1. **Exact-slug dependency.** A dependency on specific changes is written as
   backticked exact slugs in the `**Depends on:**` paragraph. Every
   backticked slug present there becomes a DAG edge.

2. **Phase-level dependency.** A dependency on an entire phase is written as
   the words `Phase N`. It compiles to edges on all changes of that phase,
   or all preceding changes when `Phase N` is the change's own phase.

3. **No dependencies.** Begin the `**Depends on:**` paragraph with `None.`
   when the change has no mechanical dependencies.

4. **Mention without dependency.** Mentioning another change without
   depending on it requires the paragraph to begin with `None.` or to
   contain independence wording — `independent`, `in parallel`, or
   `may proceed`. Otherwise the mention compiles into a false edge.

5. **Paragraph boundary.** The `**Depends on:**` paragraph extends to the
   first blank line. Never place a backticked change slug or a `Phase N`
   reference inside it unless it is a true dependency.

6. **Deferred change.** A deferred change includes the word `deferred`
   in its `**Depends on:**` paragraph. It compiles to `enabled = false`.

7. **Non-mechanical dependency.** Any dependency wording outside rules 1-6
   compiles to no edges. Use this only when the dependency is genuinely
   non-mechanical (e.g. "completion of any active change touching the same
   requirements") and the operator must decide.

8. **Proposed capability gate.** A new capability is marked with
   `` `name` (proposed; see Capability Ownership). `` The first change per
   proposed capability compiles to a `pause_before` approval gate.

   A gated change is human-only by default: its gate can only be released by
   a human operator. When the gate should instead be releasable by the
   supervised job's policy-bound authority, state that explicitly in prose
   on the change; the compiler emits `pause_before_human_only = false` for
   it. `pause_before_human_only = true` is only valid on a change that also
   sets `pause_before = true` — the combination without a gate is a
   plan-load error.

9. **Unique slugs.** Slugs are unique kebab-case OpenSpec change ids,
   verb-led (e.g. `add-`, `enforce-`, `extract-`, `replace-`), and collide
   with no existing or archived change id.

10. **Manual gates are human-specified.** Phase exit gates needing human
    judgment cannot be inferred by the compiler. State them in prose and
    list them in the `## Suggested Manual Gates` section, naming the change
    ids where the operator should add `pause_before = true`.

11. **No `# REVIEW` markers.** The compiler does not support or emit
    `# REVIEW` markers. Review-fix cycles are managed by the orchestrator's
    implement/review/archive loop, not by inline markers in source or
    compiled output.

## Four Dependency Forms

These are the four dependency forms the compiler recognises, shown in a
reference example:

```markdown
### Change: `add-atomic-writes`

**Purpose:** Make runtime writes crash-safe.

**Depends on:** None. This may be developed in parallel with Phase 1.

**Capability:** `runtime-state` (proposed; see Capability Ownership).

### Change: `validate-locking-contract`

**Purpose:** Verify filesystem locking semantics.

**Depends on:** `add-atomic-writes`.

**Capability:** `runtime-state` (proposed; see Capability Ownership).

### Change: `extract-dispatch-coordinator`

**Purpose:** Move dispatch out of the intake module.

**Depends on:** Phase 1, because the extracted path should target the
hardened executor contract.

**Capability:** `gmail-intake-pipeline`.

### Change: `add-migration-registry`

**Purpose:** Upgrade older runtime records after schema evolution.

**Depends on:** Phase 1. Implementation is deferred until the first schema
version bump is proposed.

**Capability:** `runtime-state` (proposed; see Capability Ownership).
```

| Form | How it is written | Compiler behaviour |
|------|-------------------|--------------------|
| Specific change | Backticked slug in `**Depends on:**` | DAG edge to that change |
| Entire phase | Words `Phase N` in `**Depends on:**` | Edges to all changes of that phase |
| No dependency | `None.` or independence wording (`independent`, `in parallel`, `may proceed`) | No edges |
| Deferred change | Word `deferred` in `**Depends on:**` | `enabled = false` |

## Command Namespaces

The upstream repo and opsx-controller expose different commands in different
namespaces. Confusing them produces plan documents that reference
non-existent invocations.

### Upstream OpenSpec (per-change)

Upstream OpenSpec provides per-change commands. The exact spelling is
generated tool-correctly per client by the core profile (OpenSpec 1.7.0+):

- **OpenCode:** `/opsx-propose`, `/opsx-apply`, `/opsx-archive`
- **Claude Code:** `/opsx:propose`, `/opsx:apply`, `/opsx:archive`
- **Codex CLI:** `$openspec-propose`, `$openspec-apply`, `$openspec-archive`
- **dsh:** none — dsh has no slash commands or agent-selection flag; the
  upstream per-change operations run via the OpenSpec CLI directly, or through
  the plan loop.

These operate on a single OpenSpec change — proposing requirements, applying
implementation, archiving completed work.

### opsx-controller (plan-level)

opsx-controller provides plan-level orchestration commands:

- **`opsx-plan`** — compile, run, report on multi-change plans. The CLI
  entrypoint, installed to `~/.local/bin/opsx-plan`.
- **`opsx-run`** — manual single-change controller loop (OpenCode only: the
  `run-one` path is pinned to the OpenCode adapter and has no `--adapter` flag;
  Claude Code and dsh run a single change through a one-change plan manifest
  instead). Codex CLI plan-run is unsupported — the adapter defines no stage
  invokes.
- **`opsx-watch-plan`** — live stage-log follower.

### Rule

When authoring a plan that references commands, verify the client
registration in the repository rather than copying a spelling from another
repo. A wrong command form fails at the first invocation.

## Current OpenSpec Facts

These facts affect how changes are proposed and structured. They hold at the
time this reference was written; verify against the repository's current
`openspec/config.yaml` and `openspec/specs/` layout.

### `skip_specs: true`

For a change that is docs-only, refactor-only, or introduces no behavioral
delta, declare `skip_specs: true` in the OpenSpec change proposal. This
tells OpenSpec there are no behavioral requirements to capture, avoiding
the need to invent one.

Use it when the change:
- Only edits documentation (`README.md`, inline comments, guides).
- Performs a pure internal refactor with zero observable behaviour change.
- Cleans up dead code or renames internal identifiers.

Do not use it when the change alters observable behaviour, even subtly —
that is a behavioural requirement the spec must record.

### Nested Capability Folders

Capabilities live under `openspec/specs/`. A capability may contain nested
sub-capability directories:

```
openspec/specs/
  plan-authoring-guidance/
    spec.md
  shared-orchestrator-installation/
    spec.md
```

When planning, check the existing directory tree — a capability mentioned
in the plan must either exist under `openspec/specs/<name>/` or be declared
as a proposed capability in the plan's `Capability Ownership` section.

## Loop-Aware Scoping

The orchestrator drives changes through implement-review-archive loops
(up to `max_rounds` rounds, default 5). Size each change to fit a single
loop. A change that requires more rounds than the ceiling stalls the plan.

### One Concern Per Change

Decompose requests into small, independently verifiable, independently
revertable changes. Each change addresses exactly one concern. A change
that combines a refactor, a feature, and a docs update will fail review
repeatedly because the reviewer cannot approve one aspect without approving
all three.

### Maximum Change Surface

Size each change against all five dimensions before it is written:

| Dimension | Maximum before split or narrowing |
|---|---|
| Named surfaces | 3 (one primary plus at most two supporting) |
| Pages / endpoints / routes | 3 total |
| Acceptance scenarios | 5 |
| Tasks | 12 |
| Expected production diff | 400 changed lines (additions + deletions), excluding tests, fixtures, and docs |

A **named surface** is a distinct externally observable contract: an API
endpoint group, a CLI command group, a page/UI route, a freshness or persistence
contract, an error taxonomy, or a cache layer. A page that adds an API endpoint
counts as both.

The mandatory split rule is independent of the numeric limits: a change that
spans **API + CLI + page + freshness + error taxonomy**, or three or more named
surfaces, SHALL be split before it is written. There is no review override for
this rule.

Exceeding a numeric threshold requires the author to split or narrow the change.
There is no operator-authorization exception inside the run, and no exception
waives the mandatory cross-surface split. The split checkpoint is the default
response to an oversized change, not another review override: never leave an
oversized change for corrective rounds to discover. The `/opsx-plan` authoring
command records the five-dimension assessment for every change and reports any
change that was split, narrowed, or proposed for re-scope.

### Size Before Implementation

Size each change for one implement-review-archive loop before implementation,
not after two failed corrective rounds. A sizing note identifies the change's
obligations, the surfaces and tests it is likely to touch, its expected review
surface, and whether it plausibly closes in one loop. The model-level
`opsx-orchestrator` records this assessment before its implement dispatch and
uses it, not the round count alone, to decide whether a preauthorized replacement
is the better unit.

When a change is too large to size confidently, either narrow it in the plan or
declare an exact `split_authorizations` entry. Do not rely on repeated
`cap_override` entries to carry an oversized change.

### Acceptance-Surface Enumeration

Before writing a change's proposal, design, tasks, and spec deltas, enumerate the
acceptance surface its behavior implies:

- the error taxonomy: every error class, envelope, and terminal state the change
  can produce;
- scope and registry cases: the missing, invalid, valid, disabled, aliased,
  duplicate, and unauthorized cases that apply;
- cache invariants: key composition, collision behavior, invalidation,
  single-flight, stale-while-revalidate, and no-write or no-subprocess rules;
- parity checks: each pair of surfaces that must agree, such as API/CLI/page;
- guards: authentication, authorization, tenant, and limit checks.

The enumeration is the change's accepted surface: its acceptance scenarios and
tasks cover only what it names. A behavior outside it is a new slice or an
explicit accepted plan change, never a silent review correction. Keep the
surface proportional to the change: no release-grade hardening on a `poc` change
and no speculative case the plan has not accepted.

The surface assessment is the five dimensions in **Maximum Change Surface**.
Record named surfaces, pages/endpoints/routes, acceptance scenarios, task count,
and expected production diff in the change's scope and success parameters, or in
the authoring response when the change body carries no sizing note. When the
enumeration exceeds the maximum, propose the split before writing.

### Split-or-Re-scope After Two Corrective Rounds

Two corrective rounds are the default budget for one change. After the second
corrective round's review still fails, and before any further corrective
dispatch, the decision set is exactly:

- **Split** — select the plan's exact `split_authorizations` entry when one
  exists. When no exact entry exists, a split is permitted only where the plan's
  own split rules or plan text permit an unpreauthorized split; the children are
  authored to authorization-equivalent exactness — a complete obligation map, a
  dependency remap, and an aggregate exit — and `opsx-split-materializer`
  executes the structural split.
- **Re-scope** — narrow the current change to its accepted surface and carry the
  remainder forward as a new slice with an explicit obligation mapping.
- **Carry-forward** — when neither split nor re-scope is permitted by the plan,
  record the remainder as a carry-forward item.

Prefer split when a previously corrected blocker class has reopened, the blocker
set is not shrinking across rounds, or the remaining work is orthogonal to the
parent's stated concern; otherwise re-scope or carry forward.

There is no third corrective round, no bounded-continue option, no operator
convergence question, and no terminal stop on the round count. A proposed
re-scope/authority decision — a split the plan does not permit — is a recorded
ledger artifact reported in the run, not a decision request that ends it.
Independent work that is already authorized by the plan may continue.

A split child does not receive a fresh two-round budget for work that already
consumed rounds in its parent. Consumption is inherited by blocker class: a
blocker class that consumed a corrective round in the parent is recorded as
consumed for the child that owns that obligation, and a class first introduced by
the child's own diff accrues under that child's budget. A child that exhausts its
inherited allowance enters the same split-or-re-scope decision as any other
change.

Controller specs set the floor and a plan may tighten but never loosen them.
Where plan text and a controller capability spec prescribe different behavior
for the same situation, the tighter rule governs: a plan may reserve a split for
its own checkpoint, shorten a corrective budget, narrow the surface limits, add a
gate, or raise an evidence bar, but it may not permit a bounded third corrective
round, widen a surface, remove a gate, or weaken a stop condition or a hard
handoff check. Plan silence leaves the controller rule unchanged.

### Executable Success Parameters

Every change's `**Success parameters:**` must list concrete, verifiable
criteria — commands that can be run, files that must exist, tests that must
pass. A success parameter of "code is clean" is unverifiable; a success
parameter of `pytest tests/` produces a pass/fail.

### Create-Stage Scope Discipline

When the plan uses `create_invoke` to author changes automatically, the
create stage writes proposals and spec deltas — it does not implement.
Scope the create-stage request to produce a valid OpenSpec change
(`openspec validate <change> --strict` passes). Implementation follows
in the implement-review-archive loop driven by the orchestrator.

### Real-Only Dependency Edges

Prefer dependency edges that represent genuine ordering constraints — one
change must complete before another can start. Stylistic preferences ("I
want this done first because it feels foundational") over-constrain the
DAG and serialize work that could proceed in parallel.

### Gates on Judgment, Not Difficulty

Use `pause_before = true` for changes that require human judgment before
proceeding:

- The first change establishing a new capability, storage contract, or
  public interface that later changes will build on.
- A change that alters trust, privacy, or security posture — anything that
  allows data to leave the machine, relaxes a permission, or adds a
  third-party dependency on a data path.
- A change whose prerequisite is a decision outside the plan's control.

Do not gate merely because a change is large or touches many files. That is
what the reviewer is for.

By default a gate is human-only (`pause_before_human_only` absent): only a
human operator can release it. To delegate gate release to the supervised
job's policy-bound authority, set `pause_before_human_only = false` on the
change (which requires `pause_before = true`). Setting
`pause_before_human_only = true` without `pause_before = true` is invalid and
fails at plan load.

### Runnable-Horizon Rule

Every change in the plan must either be executable now (its dependencies
are completed or non-existent) or have a stated unblock condition.
A change whose dependency is a deferred change that has no scheduled
unblock date is not plan-ready — either drop it or make the enabling
change plan-ready.

### Security and Data-Integrity Sequencing

Sequence security and data-integrity corrections before enabling more
automation or loosening constraints. A plan that relaxes a permission
before hardening the path that permission gates is ordered incorrectly.

### Separation of Concerns in the Same Change

Separate behaviour-preserving extraction from behaviour changes. Never
combine specification cleanup with product behaviour changes in one
change — the review surface becomes ambiguous and the revert unit is
wrong.

## Self-Verification

Before reporting the plan document as complete:

1. **Dependency scan.** Re-read every `**Depends on:**` paragraph against
   the eleven compile rules. Confirm no unintended backticked slug or
   `Phase N` reference appears in any of them.

2. **Split-authorization scan.** When `split_authorizations` is present,
   confirm every parent and child slug is exact and collision-free, every
   parent obligation appears exactly once in a child's `obligation_map`, the
   dependency remap preserves every predecessor and successor, and
   `aggregate_exit` proves the parent's original success parameters. Confirm the
   field is not treated as conditional prose or as a manifest key.

3. **Compile self-check.** When `opsx-plan` is available on PATH:
   ```bash
   opsx-plan compile <doc> -o /tmp/opsx-plan-selfcheck.toml --force
   ```
   Verify the compile succeeds and produces the expected changes. Fix any
   missing dependencies or malformed structure and re-run until clean.

   On Claude Code, use `--adapter claude-code`. If the controller model is
   not configured for the adapter, compile self-checking is unavailable —
   report that honestly.

4. **Manual compile.** When the compiler is not available, state that the
   markdown was authored but not compiled, and tell the operator to run
   the compile step manually before any unattended run.

## TOML Manifest Authoring

This document covers the markdown half. The TOML half — turning a plan
document into a runnable `plan.toml` manifest, including the `[plan]` and
`[[changes]]` key tables, dependency semantics, gate placement, and
verification — is covered by the **`opsx-plan-manifest`** skill, which is
shipped with the controller. Invoke it or read its `SKILL.md` for the
complete manifest-authoring workflow.

## Plan-Quality Rules

These are the heuristics every plan author should apply, independent of
compile enforcement:

- **Decompose.** One concern per change. Small, independently verifiable,
  independently revertable.
- **Sequence security first.** Fix security and data-integrity issues before
  enabling more automation or relaxing constraints.
- **Separate extraction from behaviour change.** A refactor that moves code
  without changing behaviour belongs in its own change. A feature that
  depends on that refactor follows.
- **Explicit scope and non-scope.** Every change states what it covers and
  what it deliberately excludes. This prevents reviewer scope-creep and
  keeps the implementer focused.
- **Real dependency edges.** An over-constrained DAG serializes work that
  could proceed in parallel. Only add an edge when one change genuinely
  cannot start until another finishes.
- **Verifiable success.** Every success parameter is a command someone can
  run, a file they can check, or a condition they can observe — never a
  subjective quality judgment.
