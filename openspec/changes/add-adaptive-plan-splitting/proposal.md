# Proposal: add-adaptive-plan-splitting

## Why

The model-level `opsx-orchestrator` now drives orchestrator-owned changes to
completion without asking for internal repository, evidence, commit, or intake
decisions. Its corrective-round caps are advisory, so a change that keeps
reopening the same blocker can consume round after round. That is the wrong
response to a change that was simply sized too large.

Three defects block a reliable answer:

1. **Sizing happens too late.** The orchestrator can discover that a change does
   not fit one loop only after implementation and two failed corrective rounds.
   The plan authoring reference says to size each change for one loop, but the
   model-level runtime has no explicit sizing step before implementation and no
   recorded basis for a split.
2. **A third corrective round is the only escape.** The advisory-cap rule lets
   the orchestrator override the two-round allowance, but it does not require
   the orchestrator to ask whether the work should be split instead. A repeated
   blocker class, a blocker set that is not shrinking, or orthogonal work is
   treated as more of the same change rather than as evidence that the change is
   the wrong unit.
3. **There is no authorized replacement path.** Renaming or replacing a planned
   change by hand is a plan edit, and general autonomy does not authorize a plan
   edit. Without a preauthorized, exact replacement, the only safe outcome is a
   proposed re-scope decision, which leaves the run at a decision boundary even
   when the plan already named a better decomposition.

The same problem exists in the other direction: an operator or plan author may
know that one planned change is a candidate for splitting but has no way to
preauthorize the exact child slugs, their dependency remap, and the aggregate
exit. A vague "split if needed" instruction cannot be applied safely because it
does not say which nodes may exist, what each child owns, or how the parent's
obligations close.

## What Changes

- Add **sizing before implementation** to the model-level orchestrator. Before
  the implement dispatch, it records a sizing assessment (scope, obligations,
  likely review surface, and expected corrective rounds). An oversized change,
  or a change with an exact preauthorized split, gets an explicit
  split-or-continue decision before implementation starts.
- Add an explicit **split-or-continue decision after two corrective rounds** and
  before any third-round override. Prefer a split when a blocker class has
  reopened, the blocker set is not shrinking, or the remaining work is
  orthogonal. A continue decision permits exactly one bounded correction with a
  named closure test and a re-evaluation checkpoint. With no authorized fallback,
  return a concrete proposed re-scope/authority decision rather than stopping on
  the raw round count. Independent, already-authorized work may proceed.
- Add optional plan-frontmatter **`split_authorizations`** entries naming an
  exact parent, exact child slugs, each child's dependencies, an exact
  obligation map, the aggregate exit, and any exact donor-path rehome map. This
  is model-level plan data: it adds no compiled-manifest key, the deterministic
  `opsx-plan` compiler does not read it, and no conditional node or prose
  auto-compilation is introduced. When selected, the tracked plan is revised to
  contain only the chosen nodes.
- Add a **named split materialization/rebinding handoff**
  (`opsx-split-materializer`) that freezes a restorable donor with source and
  evidence hashes and lineage, writes the selected plan revision, rehomes only
  the authorized unaccepted candidate paths, binds a local commit, and verifies
  the compiled graph before any child is dispatched. The orchestrator stays
  read-only and only one writer mutates the worktree at a time. If that
  materializer is unavailable and no named supervisor declares the capability,
  the orchestrator returns the proposed decision instead of improvising with
  generic repository hygiene or an installed model's undeclared abilities.
- Define **ledger selection, resume, and idempotence** semantics: one active
  selection per parent; state progression `selected → frozen → materialized →
  bound`; resume from the earliest incomplete state after revalidating hashes;
  no re-materialization of a bound revision with the same plan hash; fail closed
  on collisions, unlisted slugs, unmet prerequisites, and plan drift.
- Add authoring guidance in `core/plan-authoring.md` and the OpenCode
  `/opsx-plan` command for sizing and exact split preauthorization, plus contract
  regressions and a pure transition fixture for positive and negative cases.
  The fixture proves the normative rules, not live model behavior.

## Impact

- **Agents/surfaces:** `adapters/opencode/agents/opsx-orchestrator.md`, new
  `adapters/opencode/agents/opsx-split-materializer.md`, plus documentation in
  `AGENTS.md`, `README.md`, and `docs/adapters.md`. Existing change-writer,
  implementer, reviewer, archiver, auditor, and hygiene contracts are unchanged;
  the materializer is the single authorized writer for a split.
- **Plan authoring:** `core/plan-authoring.md` and
  `adapters/opencode/commands/opsx-plan.md` document `split_authorizations`.
- **Specs:** new capability `adaptive-plan-splitting`; added requirement in
  `plan-authoring-guidance`.
- **Tests:** new contract and transition tests under `tests/adapters/`; no live
  model dispatch is claimed as tested.
- **Compatibility:** the deterministic `opsx-plan` / `opsx-run` state machine,
  compiled TOML manifest, acceptance tier, human gates, phase gates, trust
  boundary, and archived changes are unchanged. `split_authorizations` adds no
  manifest key and needs no compile change. The existing `human_gates` stop list
  keeps precedence: a human-gated parent or child is never split.
