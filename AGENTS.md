# AGENTS.md

Repo-root guidance for automated agents (opsx implementer, reviewer, archiver)
and any coding assistant working in this repository.

## Project

`opsx-controller` is a portable OpenSpec controller workflow: a client-neutral
core contract (`core/`) plus client-specific adapters (`adapters/opencode/`,
`adapters/claude-code/`, `adapters/codex-cli/`, `adapters/dsh/`) and the plan
orchestrator (`orchestrator/opsx-plan.py`). Start from
`core/controller-contract.md`, `core/state-schema.md`, and
`core/phase-protocol.md`.

For implementation plans that feed `opsx-plan compile`, the single
client-neutral reference is `core/plan-authoring.md`. Plan-authoring agents
and operators should follow that document; adapter-specific surfaces should
point to it rather than restating authoring rules.

The execution model is direct dispatch only: `opsx-plan` sequences a compiled
plan and dispatches the implement, review, and archive workers through the
plan's stage invokes, and a plan missing any of `implement_invoke`,
`review_invoke`, or `archive_invoke` fails closed at load time. Plan-level
orchestration (`opsx-plan` / `opsx-run`) is the controller's scope; per-change
propose, apply, archive, and verify work belongs to upstream OpenSpec commands
and skills, not to a controller-owned per-change workflow.

## Delegation: sub-agents are the default

Operators run expensive models in this repo, so primary-agent tokens and
context are the scarce resource. **Delegate by default, not as a last
resort.** Do the cheap thing (hand off) unless a task genuinely needs the
primary model's context. Push work down with the client's Task/sub-agent
facility:

- **Code exploration and search** — finding files, tracing symbols, mapping
  call sites, answering "where/how does X work" — belongs to a cheap
  exploration sub-agent (e.g. `explore`/`general` under opencode). Ask for a
  concise synthesis, not raw file dumps, so only the answer enters the
  primary context.
- **Minor, well-scoped tasks** — single-file edits, mechanical refactors,
  running tests/formatters, boilerplate — belong to a coding sub-agent (e.g.
  `coder`).
- **Controller rounds** — the `opsx-implementer`, `opsx-reviewer`, and
  `opsx-archiver` sub-agents are the intended delegation boundary for one
  OpenSpec round; invoke them rather than performing their work inline.
- When a task splits into independent pieces, fan out several sub-agents in
  one message and aggregate their results instead of serializing them
  through the primary model.

Keep in the primary context only the decisions and synthesis that require
it.

## Validation

- Tests: `python3 -m unittest discover -t . -s tests` and `node
  tests/opencode/test-opsx-usage-emitter.js`, both from the repository root
  (run both suites for orchestrator or adapter changes; both are
  stdlib/runtime only, so no repo `.venv` or `node_modules` is required). A
  new test package needs an `__init__.py` or discovery skips it silently.
- OpenSpec: `openspec validate <change> --strict` for a single change, or
  `openspec validate --all`.

## Phase-agent authoring and generation

The implementer, reviewer, and archiver instruction bodies are authored once
at `core/phase-agents/<phase>.md`. Adapter frontmatter, TOML keys, and
client-specific steps live in `<distribution>/agent-templates/opsx-<phase>.*`
templates, each with a single `{{phase_body}}` placeholder. Never hand-edit a
rendered `adapters/*/agents/opsx-*` or `plugins/opsx-controller/agents/*` file:
edit the canonical body or template and run
`python3 scripts/generate-phase-agents.py`. Use
`python3 scripts/generate-phase-agents.py --check` to detect drift;
`tests/adapters/test_generated_phase_agents.py` fails when committed outputs
are stale.

## Plan-level agent set (OpenCode)

The hand-authored OpenCode agents `opsx-orchestrator`, `opsx-change-writer`,
`opsx-review`, `opsx-integration-auditor`, `opsx-repo-hygiene`, and
`opsx-split-materializer` complement the controller-round
`opsx-implementer`/`opsx-reviewer`/`opsx-archiver` set:

- `opsx-orchestrator` is the plan-level conductor. It holds no shell or edit
  capability and drives a markdown plan change-by-change, dispatching the change
  writer, the implementer, a fresh reviewer, the archiver, and then the
  Phase Transition Gate. It drives every change outside the resolved
  human-gate set end-to-end — authoring, implementation, corrective rounds,
  evidence disposition, local commits, and whether the next change's authoring
  may start — and records each decision with its basis. It holds the two-round
  corrective budget, the finding classification, and the split-or-re-scope
  decision described in *Convergence, review, and cost controls*. It does not ask
  the operator for an internal repository, evidence, commit, or intake decision.
- `opsx-change-writer` authors or amends exactly one change's OpenSpec
  artifacts. It sizes the change against the maximum-surface heuristic first,
  enumerates the acceptance surface (error taxonomy, scope/registry cases, cache
  invariants, parity checks, guards) before writing, and proposes a split instead
  of authoring an oversized change. It does not implement source, archive, or
  commit.
- `opsx-review` is the generic read-only reviewer with acceptance tiers,
  findings dispositions, and a required coverage matrix. The first review and
  every re-review are full-scope; it reports a coverage `scope`
  (`current-slice` or `adjacent`), classifies every finding
  `introduced_by_diff` / `adjacent_scope` / `pre_existing`, and judges only the
  change's accepted success parameters. It carries no plan-specific paths or
  requirements.
- `opsx-integration-auditor` is the fresh, read-only handoff auditor. It proves
  or disproves `handoff_ready` from the committed repository state and never
  edits, stages, commits, pushes, or archives. Its only product is the
  machine-readable audit report. It checks `local-only-delivery` rather than a
  pushed branch, treats an unauthored next change as expected (only a
  planned-slug collision or an unmet dependency fails `next-change-readiness`),
  and reports an evidence gap as a bounded `repair-evidence` repair or an
  `unqualified` `carry_forward` record instead of an operator decision. It
  accepts a recorded operator attestation for the state or authority question it
  names, reuses hash-bound ledger evidence instead of re-collecting it, and never
  waives a hard content check it can read from `HEAD`. A planned-slug collision
  is a plan-level `resolve-slug-collision` repair when the occupant is a plan
  change or declared dependency; an out-of-plan occupant is an
  `operator-decision` stop because no worker may archive, rename, or rewrite it.
- `opsx-repo-hygiene` is the scoped handoff writer. It commits exactly the
  accepted implementation pathspecs the auditor named, ignores run-local state,
  and repairs handoff-blocking scripts or evidence. It accepts recorded operator
  authority for the commit it names and reuses hash-bound ledger evidence, while
  still refusing a wider pathspec. It repairs in phases: a safe
  baseline commit (accepted source, the plan, the `.opsx-run/` ignore entry, a
  script-path retarget) that proceeds even while an evidence question is still
  open, then a bounded evidence phase that preserves historical originals
  byte-for-byte. It refuses to invent scope, never changes product behavior, and
  amends only a commit it created in the current round while that commit is
  still `HEAD`.
- `opsx-split-materializer` is the single authorized writer for a plan split,
  whether it comes from an exact `split_authorizations` entry or from a
  plan-permitted unpreauthorized split the standard change-writer authored to
  authorization-equivalent exactness. It freezes a restorable donor (identity,
  source and evidence hashes, lineage), revises the tracked plan to contain only
  the chosen child nodes, rehomes only the authorized unaccepted candidate paths,
  binds a local commit, and verifies the compiled node/edge set. It does not
  archive or spec-sync the parent, change the acceptance or phase tier, gates,
  human boundary, trust posture, or archived work, reuse parent checkmarks as
  child acceptance, or push. It is not a generic writer: a plan split with
  neither an exact authorization nor a cited plan permission, or with no
  available materializer, fails closed for that split, and the orchestrator
  records the proposed re-scope/authority decision and narrows or carries the
  remainder forward.

`opsx-implementer` carries both the original controller-round contract and a
plan-orchestrator mode; the mode is selected from the input fields. Keep the
controller-round contract intact when editing that canonical body. In plan mode
it implements in risk order — scope/registry validation, error taxonomy, cache
invariants, freshness, then page/UI and polish — and verifies each step with the
change's exact focused tests.

All six are OpenCode-only today. Port them to `core/phase-agents` and the
other adapters once the shared contracts stabilize; do not hand-edit their
installed copies under `~/.config/opencode/agents/`.

Autonomy is scoped, not blanket. The orchestrator's stop list for a plan run is
the optional `human_gates` plan-frontmatter list of exact change slugs, resolved
as an explicit `HUMAN_GATES` input first, then the frontmatter list, then empty;
an explicit value replaces the frontmatter list rather than merging with it, and
an unknown slug is a fail-closed authoring defect. The orchestrator stops before
a gated change, reports the resume instruction, and does not start a later
change whose dependency is gated. `human_gates` is a **model-level plan field**
read by `opsx-orchestrator`, distinct from the **compiled-manifest
`pause_before` gate** consumed by the deterministic `opsx-plan` / `opsx-run`
controller: a plan may use both, neither substitutes for the other, and
`human_gates` adds no manifest key and needs no compile change.

Plan sizing and split authority are also explicit. Before implementation the
orchestrator records a sizing assessment against the maximum-surface heuristic in
`core/plan-authoring.md` — three named surfaces, three pages/endpoints/routes,
five acceptance scenarios, twelve tasks, and a 400-line expected production diff
— and a change spanning API + CLI + page + freshness + error taxonomy, or three
or more named surfaces, is split before it is written. After two corrective
rounds it makes a split-or-re-scope decision; there is no third-round override.
Split authority is not implicit in general autonomy, a cap override, a reviewer
verdict, or a supervisor role. A split is permitted only from an exact
`split_authorizations` frontmatter entry naming the parent, exact child slugs,
child dependencies, an obligation map that gives every parent obligation to
exactly one child, and an aggregate exit, or from the plan's own split rules
under the `opsx-split-materializer` writer. `split_authorizations` is model-level
plan data read by `opsx-orchestrator`, not a compiled-manifest key or
conditional DAG; selecting it revises the tracked plan so it contains only the
chosen nodes. With neither an exact authorization nor a cited plan permission,
or without an authorized materializer, the orchestrator records a concrete
proposed re-scope/authority decision in the ledger and narrows or carries the
remainder forward instead of stopping on a raw round count. Independent,
already-authorized work may continue.

### Convergence, review, and cost controls

Corrective rounds default to **two per change**. A corrective round is one
corrective implementation dispatch followed by the review that verifies it; the
initial review is not one. The first two are an internal orchestrator decision
when the prior review's findings are eligible. After the second failing review
the orchestrator records one split-or-re-scope-or-carry-forward decision and
continues: it SHALL NOT grant a third corrective round, treat one as a
`cap_override`, or ask the operator for convergence authorization. A child of a
split inherits its parent's corrective-round consumption by blocker class, so a
split cannot reset the budget.

Every finding is classified before another round: `introduced_by_diff` is the
only class that earns one, `adjacent_scope` becomes a proposed new slice or a
follow-up, and `pre_existing` is a carry-forward item. `unknown` is adjudicated
from evidence first and never earns a round on its own. The first review is
full-scope and doubles as a sizing checkpoint: a broad blocking set, or a change
spanning three or more named surfaces, triggers the split-or-re-scope decision
before any corrective implementation. Every re-review is full-scope too, and a
current-slice finding that round 1 could have established from the same evidence
is a reviewer process defect, not a reason for another round.

Bookkeeping is bounded: at most one ledger-writer dispatch per stage, batching
all record-only events, and a record-only update never justifies its own `coder`
session. Operator attestation about quiescence, authority, or a gate is
authoritative for the subject it names and is not re-derived — but it cannot make
a false repository claim true, waive a hard content check, or widen accepted
scope. A stale idle marker does not interrupt a live, in-flight child: record
`stale_idle_marker` with the evidence and keep waiting.

Each stage records `tokens_in`, `tokens_out`, `cache_read_tokens`, and a
`cost_status` of `estimated`, `unresolved`/`unpriced`, `unavailable`, or
`not-reported`. The run report carries per-stage tokens and cost status, the
three run token totals, a `priced_subtotal`, and an `unpriced_stage_count`; a
bare `$0` is forbidden while any stage is unpriced or `not-reported`.

**Precedence.** Controller capability specs set the floor for a plan run. An
accepted plan MAY tighten that floor — reserving a split for its own declared
checkpoint, shortening a budget, narrowing a surface limit, adding a gate, or
raising an evidence bar — but SHALL NOT loosen it: no third corrective round, no
widened surface or scope, no removed gate, no lowered acceptance tier, no
weakened stop condition or hard handoff check. Plan silence leaves the controller
rule in force; a loosening provision is void for that run and recorded as plan
drift.

Delivery is local. Commits and amendments are made through `opsx-archiver` and
`opsx-repo-hygiene` on the local branch: no push, no remote branch, no pull
request, no merge, and no no-push waiver. A configured upstream, if one happens
to exist, is informational and changes nothing.

`MAX_DISPATCHES` and the repair/audit rounds are planning allowances, not
terminal stops. Before exceeding one, the orchestrator records a `cap_override`
ledger entry naming the cap and its source, the count already consumed, the
concrete remaining work, why continuing is safer than stopping, and the
checkpoint at which it re-evaluates; then it continues. The three-dispatch
handoff reserve is likewise an allowance: a shortfall is recorded, not terminal.
The review corrective-round budget is not an advisory cap: a third corrective
round is refused even when `MAX_DISPATCHES` has headroom, and no dispatch,
repair, or audit override authorizes one.

A finding discovered after archive is a carry-forward item, never a reopened
change. Record the originating archived change, the current change, the locus
(`path[:symbol]`), observed and required behavior, evidence, impact,
`blocks-later-change`, and disposition. Do not reopen the archived change, edit
its archived delta, or insert an unplanned follow-up change mid-plan; report the
remaining follow-ups once the plan's orchestrator-owned changes finish.

### Phase Transition Gate

A change is not complete when it is archived; it is complete when the next
change can start reproducibly from `HEAD`. After archive and before selecting
the next change, `opsx-orchestrator` dispatches a fresh
`opsx-integration-auditor`. It advances only on `handoff_ready: true`.
A blocked audit goes to `opsx-repo-hygiene` only for the repository repairs it
names, that is, the blockers whose `repair.owner` is `opsx-repo-hygiene`, and
never for a plan-reachable `resolve-slug-collision` blocker, which the
orchestrator resolves at plan level by not starting or authoring the colliding
change and recording which artifact is authoritative, with no repository
mutation. An out-of-plan slug occupant is not a repair: it is an
`operator-decision` stop, because no worker may archive, rename, move, or rewrite
an external change. A fresh audit follows either repair path. Two repair/audit rounds are a
planning allowance, not a terminal cap: a further round is permitted with a
recorded `cap_override`, and the run stops only when no scoped repair remains or
the blocker needs a human-only decision.

The audit proves reproducibility: clean tracked tree, accepted source and plan at
`HEAD`, the archive lineage carrying the implementation paths, `.opsx-run/`
ignored, evidence manifests read from `HEAD`, and perf/evidence scripts that
do not recreate an archived change directory. It also requires
`local-only-delivery` (a committed local `HEAD`; an existing upstream is
informational) and a fresh checkout that can run the focused checks plus
`openspec validate --all --strict`. Evidence manifests are read from `HEAD`,
never from the worktree; every indexed entry needs a non-empty `path` and
`sha256`, and a worktree-only or malformed manifest is blocked rather than
passed. An unauthored next change is not an intake blocker — its proposal is
authored after handoff releases it — and only a planned-slug collision or an
unmet dependency fails `next-change-readiness`.

`handoff_ready` is part of the orchestrator's run ledger, definition of done, and
final report. The orchestrator SHALL NOT report "no open blockers" while it is
false or unknown.

An archive-only commit — an archive move plus synced specs with zero
implementation paths, while accepted source remains outside `HEAD` — is
`handoff_ready: false`, not completion; the existing archive-only refusal
remains in force. An operator waiver to archive without committing source is
recorded verbatim and is a **stop**, not a handoff: the next change starts only
after the operator grants new authority to commit and a fresh audit passes.

## Maintainer Notes: deploy after every change

This section applies to the maintainer's own machine, after a change has
merged to `main`. It is not part of what a PR needs to satisfy, and a
contributor working in their own checkout should not run these installers.

The commands, agents, and orchestrator that actually run **on the
maintainer's machine** are the **installed** copies under
`~/.config/opencode/` and `~/.local/bin/`, not the files in this repo.
Editing the repo does **not** change the maintainer's runtime behavior until
they re-install. A merged change can look "done" in git while stale code
keeps running locally.

**After merging any change that touches `adapters/`, `orchestrator/`,
`plugins/`, `skills/`, or `scripts/`, the maintainer re-runs the
installer(s) to deploy it:**

```bash
bash install.sh --global --verify
```

The repo-root `install.sh` is the single reinstall step: it installs every
adapter (opencode, claude-code, codex-cli, dsh) and the shared orchestrator
in one invocation. Adapter-specific re-runs remain available for a narrower
deploy, e.g. `bash install.sh --global --only claude-code --verify` or by
running an individual adapter installer directly (e.g.
`bash adapters/opencode/install.sh --global --verify`).

Any global installer now deploys the shared `opsx-plan` and `opsx-run`
executables (via `scripts/install-orchestrator.sh`), so a maintainer only
needs to rerun one installer — the installed runtime location is the same
regardless of which adapter deployed it. The same deploy covers the shipped
systemd **user** unit template `opsx-autopilot.service` (plus its drop-in
convention) as installed-but-disabled data. Enabling that unit
(`systemctl --user enable opsx-autopilot`) is a **separate operator step**:
the installer only copies files and never runs `systemctl` or enables a unit.

The orchestrator's implementation is split between the entrypoint
(`orchestrator/opsx-plan.py`) and the installed `lib/orchestrator/` runtime
package (see `orchestrator/README.md#source-layout`). Because of this, the
reinstall after an `orchestrator/` or `lib/orchestrator/` change is
**required, not merely recommended**: a stale installed runtime is missing
whole modules, not just running outdated logic, and commands like
`opsx-plan report` or `opsx-plan dashboard` fail outright — with a
diagnostic naming the missing package, per `opsx-plan doctor` — rather than
silently serving old behavior.

## Sandbox / filesystem discipline

Workers run headless under a sandbox that auto-rejects `external_directory`
access. Never search parent or external directories (e.g. do not `Glob`
`**/AGENTS.md` or read outside the repo root) — a rejected permission prompt
kills the worker before it can emit its result. Read exact, repo-relative or
`$HOME`-expanded paths and continue past any that do not exist.
