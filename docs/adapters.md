# Adapter Reference

`opsx-controller` ships one adapter per coding client. Each adapter maps the
same three phases — implement, review, archive — onto that client's packaging
model, so the core controller semantics stay identical. Plan compilation and
plan-run support vary by adapter, and direct dispatch is the only plan-run
execution model.

- [Choosing an adapter](#choosing-an-adapter)
- [Model configuration](#model-configuration)
- [OpenCode adapter](#opencode-adapter)
- [Claude Code adapter](#claude-code-adapter)
- [Codex CLI adapter](#codex-cli-adapter)
- [dsh adapter](#dsh-adapter)
- [Packaging: Claude plugin, Codex plugin, Vercel skill](#packaging)
- [Deprecation notes](#deprecation-notes)

## Choosing an adapter

All four adapters install the same controller surfaces. They differ in what
gets installed and in the plan compilation and plan-run capabilities that vary
by adapter:

| | OpenCode | Claude Code | Codex CLI | dsh |
|---|---|---|---|---|
| implement / review / archive loop (plan-level) | yes | yes | no (plan-run unsupported) | yes |
| `opsx-plan` + `opsx-run` on `PATH` | installed to `~/.local/bin` | installed to `~/.local/bin` | installed to `~/.local/bin` | installed to `~/.local/bin` |
| single-change `opsx-run` | yes | no (OpenCode-pinned) | no (OpenCode-pinned) | no (OpenCode-pinned) |
| `opsx-plan compile` (markdown plan → TOML) | yes (OpenCode controller model) | yes (Claude Code controller model) | needs OpenCode or Claude Code | needs OpenCode or Claude Code |
| plan authoring skill (`/opsx-plan`) | yes | yes | yes | — |

All four adapters install `opsx-plan` and `opsx-run` to `~/.local/bin/`
via the shared installer helper. The Codex CLI and dsh adapters do not support
plan compilation (`opsx-plan compile`), but their global installers still
deploy the orchestrator executables — use `opsx-plan` from `PATH` as you would
with any other adapter.
Single-change `opsx-run` is OpenCode-pinned: `run-one` has no `--adapter` flag
and always uses the OpenCode adapter. Claude Code and dsh operators run a single
change via a one-change plan manifest with the matching `adapter` and
`opsx-plan run` instead. Codex CLI plan-run is unsupported — the adapter defines
no default stage invokes, so a codex-cli plan that relies on adapter defaults
fails closed at load time naming all three required keys.

`opsx-plan compile` supports OpenCode (the default) and Claude Code (via
`--adapter claude-code`). Each requires a `controller` model resolved for
the corresponding adapter — the compiler rejects invalid model syntax
before writing any output. Plan compilation is not supported through the
Codex CLI or dsh adapters; use `--adapter opencode` or `--adapter claude-code`
instead. A markdown plan authored under any adapter can be compiled as
long as an appropriate `controller` model is resolved.

## Model configuration

Configure models before your first install. Roles are `controller`,
`implementer`, `reviewer`, and `archiver` (all required), with an optional
fifth role `implementer_escalation` exported as `OPSX_IMPLEMENTER_ESCALATION_MODEL`.
Leaving the escalation role unresolved does not block runs unless the plan
sets `escalate_after_review_fails > 0`, which fails closed at load time with
guidance to resolve it.

```bash
python3 orchestrator/opsx-plan.py models init   # seeds ~/.config/opsx-controller/models.toml
$EDITOR ~/.config/opsx-controller/models.toml
python3 orchestrator/opsx-plan.py models show --adapter opencode
```

See `models.example.toml` at the repo root for the file shape. `models init`
needs no prior install — run it against the repo checkout directly.

If no `models.toml` exists, installers and runs fall back to ambient `OPSX_*_MODEL`
environment variables (for example from `.env`, kept as the legacy path — see
`.env.example`). Installers and plan runs fail closed with guidance if no model
resolves.

## Phase-agent sources and generation

The implementer, reviewer, and archiver instruction bodies are authored once,
client-neutrally, under `core/phase-agents/{implementer,reviewer,archiver}.md`.
Each distribution keeps a thin template (only its frontmatter/TOML keys and
client-specific step text) under
`<distribution>/agent-templates/opsx-<phase>.<ext>.tmpl`, with a single
`{{phase_body}}` placeholder.

`scripts/generate-phase-agents.py` renders every committed agent and
command-surface file from those inputs. The agent outputs are:

- `adapters/{opencode,claude-code,codex-cli,dsh}/agents/opsx-<phase>.*`
- `adapters/codex-cli/plugin/agents/opsx-<phase>.toml`
- `plugins/opsx-controller/agents/opsx-<phase>.md`

Regenerate after editing a canonical body or a template:

```bash
python3 scripts/generate-phase-agents.py          # write outputs
python3 scripts/generate-phase-agents.py --check  # fail on drift
```

Generated files carry a header naming the generator and canonical source; edit
the sources under `core/phase-agents/` and the templates, not the outputs.

The `/opsx-ship` command body is likewise authored once at
`core/commands/opsx-ship.md` and rendered into four surfaces from thin
`<distribution>/command-templates/opsx-ship.md.tmpl` templates that carry only
client-specific frontmatter/preamble and a single `{{phase_body}}` placeholder:

- `adapters/opencode/commands/opsx-ship.md`
- `adapters/claude-code/skills/opsx-ship/SKILL.md`
- `adapters/codex-cli/skills/opsx-ship/SKILL.md`
- `plugins/opsx-controller/skills/opsx-ship/SKILL.md`

`adapters/codex-cli/plugin/skills/opsx-ship/SKILL.md` is the bundle copy that
`adapters/codex-cli/install.sh --plugin` regenerates from the Codex skill, and
it must stay byte-identical to it. The same `--check` gate detects drift in
every generated ship surface.

## OpenCode adapter

What it contains:

- `adapters/opencode/commands/opsx-plan.md`: plan-authoring slash command
- `adapters/opencode/commands/opsx-ship.md`: `/opsx-ship` conductor command for
  one change
- `adapters/opencode/agents/opsx-ship.md`: `/opsx-ship` conductor agent
  (`mode: all`, `edit: deny`, `bash: deny`, `task: allow`, plus a `tools:`
  denylist for Serena's file, memory, and configuration mutation tools). It has
  no shell at all, so every shell command and `openspec` query is delegated to
  a sub-agent; the conductor never runs a command whose arguments it cannot
  validate. Serena remains enabled, and its read-only tools stay available. The
  denylist is defense-in-depth: an MCP mutation tool left exposed elsewhere is
  reported as a hardening note, not treated as a run-blocking defect.
- `adapters/opencode/agents/opsx-orchestrator.md`: plan-level conductor agent
  (`mode: all`, `edit: deny`, `bash: deny`, `task: allow`). It reads a markdown
  plan directly, drives changes in dependency order, and dispatches the change
  writer, implementer, a fresh reviewer, and the archiver. It then runs the
  mandatory Phase Transition Gate before selecting the next change. It holds no
  durable state of its own; the run ledger is maintained by delegated writers.
  It drives every change outside the resolved human-gate set autonomously —
  authoring, corrective rounds, evidence disposition, local commits, and intake
  — records each decision with its basis, and does not ask for an internal
  repository, evidence, commit, or intake decision. The run's stop list is the
  optional `human_gates` plan-frontmatter list of exact change slugs, resolved
  as explicit `HUMAN_GATES` input first, then frontmatter, then empty; an
  explicit value replaces the frontmatter list, and an unknown slug fails
  preflight. `human_gates` is a model-level plan field, distinct from the
  compiled-manifest `pause_before` gate consumed by `opsx-plan` / `opsx-run`;
  neither substitutes for the other.
- `adapters/opencode/agents/opsx-integration-auditor.md`: fresh read-only
  handoff auditor (`mode: all`, `edit: deny`, `task: deny`, `question: deny`,
  inspection-only shell). It proves or disproves `handoff_ready` from the
  committed repository state and returns the machine-readable report
  (`status`, `handoff_ready`, `change`, `head`, `checks[]`, `blockers[]`,
  `next_action`). It never edits, stages, commits, pushes, or archives.
  `local-only-delivery` replaces `branch-upstream` (an existing upstream is
  informational); `next-change-readiness` fails only on a planned-slug collision
  or an unmet dependency, never on an unauthored proposal. A collision with a
  plan-reachable occupant is a `resolve-slug-collision` repair owned by the
  orchestrator; an out-of-plan occupant is an `operator-decision` stop because
  no worker may archive, rename, or rewrite it. Evidence manifests are read from
  `HEAD`, every indexed entry needs a non-empty `path` and `sha256`, and a
  worktree-only or malformed manifest is blocked rather than passed. An evidence
  gap becomes a bounded `repair-evidence` repair or an `unqualified`
  `carry_forward` record, with `stop` reserved for human-only boundaries.
- `adapters/opencode/agents/opsx-repo-hygiene.md`: scoped handoff writer
  (`edit: allow`, `bash: allow`, `task: deny`, `question: deny`). It repairs in
  phases: Phase A commits the safe baseline (the exact accepted implementation
  pathspecs the auditor named, the plan, the `.opsx-run/` ignore entry, and any
  perf/evidence script retarget) even while an evidence blocker is still open;
  Phase B repairs evidence within a bounded authority that preserves historical
  originals byte-for-byte and never claims a missing artifact is verified. It
  refuses to invent scope, never changes product behavior, and reports an
  `amended_commit` only for its own commit while that commit is still `HEAD`. A
  code defect requires a reviewed change. It performs repository repairs only:
  a plan-reachable `resolve-slug-collision` is orchestrator-owned, so it never
  renames, moves, archives, or deletes an active change directory to clear a
  plan-reachable slug; an out-of-plan slug collision is an `operator-decision`
  stop that it does not act on.
- `adapters/opencode/agents/opsx-split-materializer.md`: the single authorized
  writer for one exact plan-preauthorized split. It revalidates the exact
  `split_authorizations` entry, freezes a restorable donor with source/evidence
  hashes and lineage, revises the tracked plan to contain only the chosen child
  nodes, rehomes only the authorized unaccepted candidate paths, binds a local
  commit with explicit pathspecs, and verifies the compiled node/edge set. It
  never archives or spec-syncs the parent, changes the acceptance/phase tier,
  gates, human boundary, trust posture, or archived work, reuses parent
  checkmarks as child acceptance, pushes, or spawns sub-agents. The orchestrator
  dispatches no child until the selected plan revision is clean, committed at
  `HEAD`, and the graph is verified and bound. A split with no exact
  authorization or no available materializer returns a proposed
  re-scope/authority decision, never an improvised generic-writer fallback.
- `adapters/opencode/agents/opsx-change-writer.md`: authors or amends exactly
  one change's OpenSpec artifacts and runs the strict validation gate. It does
  not implement source, archive, or commit.
- `adapters/opencode/agents/opsx-review.md`: generic read-only reviewer with
  acceptance tiers (`poc` / `phase` / `release`), findings dispositions, and a
  required `coverage[]` matrix (`covered` / `not-applicable` / `not-covered`
  with evidence). It is not tied to any one plan or repository.
- `adapters/opencode/agents/opsx-implementer.md`: implementation agent. It
  supports the controller-round contract and a plan-orchestrator mode selected
  from its input fields.
- `adapters/opencode/agents/opsx-reviewer.md`: strict controller-round reviewer
  agent (distinct from the plan-level `opsx-review`)
- `adapters/opencode/agents/opsx-archiver.md`: non-interactive archiver agent
- `adapters/opencode/support/opsx-controller-state-README.md`: state contract
- deployed `plan-authoring.md` reference (copied from `core/plan-authoring.md`)
  at `~/.config/opencode/opsx-controller/plan-authoring.md` (global) or
  `<project>/.opencode/opsx-controller/plan-authoring.md` (project)
- `adapters/opencode/templates/project/`: host-project setup snippets
- `adapters/opencode/install.sh`: OpenCode installer

### Phase Transition Gate

`opsx-orchestrator` owns a mandatory handoff gate between changes. Archiving
records a change as durable; it does not make the repository ready for the next
change. The gate runs after archive and before any work for the next change.

The run itself is autonomous: every change outside the resolved human-gate set
is driven end-to-end without asking for an internal repository, evidence,
commit, or intake decision. `MAX_DISPATCHES`, review corrective rounds, and
repair/audit rounds are planning allowances rather than terminal stops, and a
shortfall in the three-dispatch handoff reserve is recorded, not terminal.

1. A **fresh** `opsx-integration-auditor` verifies a reproducible `HEAD`: a
   clean tracked tree, the accepted source and plan at `HEAD`, the archive
   lineage carrying the implementation paths, `.opsx-run/` ignored, evidence
   manifests read from `HEAD` (every indexed entry needs a non-empty `path` and
   `sha256`; worktree-only or malformed manifests never pass), perf/evidence
   scripts that do not recreate an archived change directory, no planned-slug
   collision and archived dependencies for the next change (an unauthored next
   proposal is expected, not a blocker), `local-only-delivery` instead of a
   pushed branch, and a fresh checkout that can run the focused checks plus
   `openspec validate --all --strict`.
2. When it returns `handoff_ready: false` with machine-actionable blockers, the
   list is split by `repair.owner`: `opsx-repo-hygiene` repairs exactly the
   repository blockers with explicit pathspecs, while a plan-reachable
   `resolve-slug-collision` blocker is resolved at plan level by the
   orchestrator — it does not start or author the colliding change and records
   which artifact is authoritative — with no repository mutation. An out-of-plan
   slug occupant is an `operator-decision` stop, not a repair, because no worker
   may archive, rename, or rewrite an external change. The hygiene
   repairs run in two phases: Phase A commits the safe baseline (accepted
   source, the plan, the `.opsx-run/` ignore entry, a script-path retarget)
   even while an evidence question is still open, then Phase B adds or
   supersedes evidence within a bounded authority that preserves historical
   originals byte-for-byte. It never stages user-owned files, unrelated
   source, secrets, or generated state, and never edits product code to
   satisfy a check. `amended_commit` is reported only for its own commit
   while that commit is still `HEAD`.
3. A fresh audit confirms the repair. Two repair/audit rounds are a planning
   allowance, not a terminal cap: a further round is permitted with a recorded
   `cap_override`, and the run stops only when no scoped repair remains or the
   blocker needs a human-only decision.

The auditor's `handoff_ready`, `checks[]`, and `blockers[]` are copied into the
run ledger and the final report unchanged, and `handoff_ready` is part of the
orchestrator's definition of done. The orchestrator reserves three dispatches
per change for the gate (`HANDOFF_RESERVE`, default `3`), excluded from any
`MAX_DISPATCHES` cap, so review cycles cannot consume the handoff budget.

Delivery is local to the run: the archiver and the hygiene writer make local
commits and local amendments, and no worker pushes, creates a remote branch,
opens a pull request, merges, or requires a remote upstream. A configured
upstream, when one exists, is reported as informational and changes no verdict.
The run's stop list is the plan's optional `human_gates` list of exact change
slugs (explicit `HUMAN_GATES` input first, then frontmatter, then empty); an
unknown slug fails preflight, and a gated change is never started. `human_gates`
is a model-level plan field, distinct from the compiled-manifest `pause_before`
gate; neither substitutes for the other.

A post-archive finding is a carry-forward item on the current change's ledger —
with archived origin, locus, evidence, impact, `blocks-later-change`, and
disposition — never a reopened archived change and never an unplanned change
inserted mid-plan. Remaining carry-forwards are reported when the plan's
changes finish.

An **archive-only** commit — the archive move plus synced specs with zero
implementation paths, while accepted source remains outside `HEAD` — is
`handoff_ready: false`, not completion, and the existing archive-only refusal
remains in force. An operator waiver to archive without committing source is
recorded verbatim and stops the run; the next change starts only after the
operator grants authority to commit the implementation and a fresh audit passes.

The archiver itself fails closed on this shape: `core/phase-agents/archiver.md`
now requires an explicit `IMPLEMENTATION_PATHS` scope whenever tracked or
untracked source differs from the archive parent, and returns blocked JSON with
`ambiguous archive commit scope`, the pending source paths, and empty
`implementation_paths` rather than reporting `status=archived`.

These three surfaces (`opsx-integration-auditor`, `opsx-repo-hygiene`, and the
gate section of `opsx-orchestrator`) are OpenCode-only today. Port them to
`core/phase-agents` and the other adapters once their contracts stabilize.

Requirements:

- OpenCode
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo-specific guidance in the host project's `AGENTS.md`

Install:

```bash
bash adapters/opencode/install.sh --global
bash adapters/opencode/install.sh --project /path/to/project
```

This adapter's installer also installs the orchestrator itself:

- `opsx-plan` and `opsx-run` to `~/.local/bin/`
- runtime libraries to `~/.local/lib/opsx-controller/`

Project install behavior:

- copies commands into `<project>/.opencode/commands/`
- copies agents into `<project>/.opencode/agents/`
- installs the controller state contract at
  `<project>/.opencode/opsx-controller/README.md`
- ensures `<project>/.opencode/.gitignore` ignores `opsx-controller/*.json`
- creates `<project>/.opencode/opencode.json` with the watcher-ignore snippet
  only when the project does not already have any OpenCode config

If the project already has `opencode.json`, `opencode.jsonc`, or
`.opencode/opencode.json`, merge
`adapters/opencode/templates/project/opencode.json.snippet.json` manually.

Model changes take effect on the next `opsx-plan run` — no installer re-run
needed for direct dispatch.

To advertise the controller path in the host repo's instructions, merge
`adapters/opencode/templates/project/AGENTS.snippet.md` into its `AGENTS.md`.

## Claude Code adapter

What it contains:

- `adapters/claude-code/skills/opsx-plan/SKILL.md`: implementation-plan
  authoring skill
- `adapters/claude-code/skills/opsx-ship/SKILL.md`: `/opsx-ship` conductor skill
  whose `allowed-tools` are limited to `Task`, `Read`, `Glob`, `Grep`, and
  `TodoWrite` — no `Edit`/`Write`/`MultiEdit` and no `Bash` at all, because
  Claude Code prefix `Bash(...)` rules cannot exclude redirection, pipelines, or
  chaining — so every unit of work, including shell inspection, is delegated
- `adapters/claude-code/agents/opsx-implementer.md`: implementation phase agent
- `adapters/claude-code/agents/opsx-reviewer.md`: strict review phase agent
- `adapters/claude-code/agents/opsx-archiver.md`: archive phase agent
- `adapters/claude-code/agents/opsx-plan-author.md`: implementation-plan
  authoring agent
- `adapters/claude-code/support/opsx-controller-state-README.md`: state contract
- deployed `plan-authoring.md` reference (copied from `core/plan-authoring.md`)
  at `~/.claude/opsx-controller/plan-authoring.md` (global) or
  `<project>/.claude/opsx-controller/plan-authoring.md` (project)
- `adapters/claude-code/templates/project/`: host-project setup snippets
- `adapters/claude-code/install.sh`: Claude Code installer

Requirements:

- Claude Code
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo guidance in `CLAUDE.md`, `AGENTS.md`, or both

Install:

```bash
bash adapters/claude-code/install.sh --global
bash adapters/claude-code/install.sh --project /path/to/project
```

Add `--verify` to either form to check the deployment after installing.

Project install behavior:

- copies skills into `<project>/.claude/skills/`
- copies agents into `<project>/.claude/agents/`
- installs the controller state contract at
  `<project>/.claude/opsx-controller/README.md`
- ensures `<project>/.claude/.gitignore` ignores `opsx-controller/*.json`

Usage from the host project root:

```text
/opsx-plan <planning request>
```

`/opsx-plan` authors the markdown implementation plan. Compiling it to TOML
works with `opsx-plan compile --adapter claude-code` (requires a `controller`
model resolved for `claude-code`). See [Choosing an adapter](#choosing-an-adapter).

To advertise the controller path in the host repo's instructions, merge
`adapters/claude-code/templates/project/CLAUDE.snippet.md` into its `CLAUDE.md`.

## Codex CLI adapter

What it contains:

- `adapters/codex-cli/skills/opsx-plan/SKILL.md`: plan-authoring skill with
  required YAML frontmatter
- `adapters/codex-cli/skills/opsx-ship/SKILL.md`: the `/opsx-ship` conductor
  skill, which fails closed as unsupported on Codex CLI. Codex spawns agents
  under the parent session's permission profile and its agent roles cannot
  override the sandbox, so it cannot dispatch a separately writable sub-agent
  beneath a read-only conductor; the skill reports that capability gap instead
  of doing the work inline
- `adapters/codex-cli/agents/opsx-implementer.toml`: implementation phase agent
- `adapters/codex-cli/agents/opsx-reviewer.toml`: strict review phase agent
- `adapters/codex-cli/agents/opsx-archiver.toml`: archive phase agent
- `adapters/codex-cli/support/opsx-controller-state-README.md`: state contract
- deployed `plan-authoring.md` reference (copied from `core/plan-authoring.md`)
  at `~/.codex/opsx-controller/plan-authoring.md` (global) or
  `<project>/.codex/opsx-controller/plan-authoring.md` (project)
- `adapters/codex-cli/templates/project/`: host-project setup snippets
- `adapters/codex-cli/install.sh`: Codex CLI installer
- `adapters/codex-cli/plugin/`: marketplace plugin bundle

Requirements:

- OpenAI Codex CLI
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo guidance in `AGENTS.md`
- `/opsx-ship` is unsupported on Codex CLI. Codex cannot simultaneously run a
  read-only conductor and dispatch a separately writable sub-agent: spawned
  agents inherit the parent session's permission profile and the supported agent
  role keys cannot override `sandbox_mode`, `permission_profile`, or
  `approval_policy`. Multi-agent depth is a top-level `[agents]` key
  (`[agents] max_depth`) and cannot be nested under a `[profiles.*]` entry. The
  skill fails closed with a diagnostic naming this capability gap; run
  `/opsx-ship` on OpenCode or Claude Code instead. `opsx-plan`/`opsx-run`
  behavior is unchanged.

Install:

```bash
bash adapters/codex-cli/install.sh --global
bash adapters/codex-cli/install.sh --project /path/to/project
```

Plan-run is unsupported on this adapter. `codex-cli` defines no default
`implement_invoke` / `review_invoke` / `archive_invoke`, so a `codex-cli` plan
that relies on adapter defaults fails closed at load time with a `PlanError`
naming all three required keys. Compile the markdown through `--adapter
opencode` or `--adapter claude-code`, then drive the loop on an adapter that
supports plan-run.

Project install behavior:

- copies skill into `<project>/.agents/skills/opsx-plan/`
- copies agents into `<project>/.codex/agents/`
- installs the controller state contract at
  `<project>/.codex/opsx-controller/README.md`
- ensures `<project>/.codex/.gitignore` ignores `opsx-controller/*.json`

State path differs from other adapters: durable state files live at
`.opsx-controller/<change-id>.json` (project root) because the Codex sandbox
protects the `.codex/` directory from agent writes.

To advertise the controller path in the host repo's instructions, merge
`adapters/codex-cli/templates/project/AGENTS.snippet.md` into its `AGENTS.md`.

## dsh adapter

The DeepSeek Harness (`dsh`, https://github.com/deepseek-ai/deepseek-harness)
adapter drives the same implement/review/archive loop with a headless dsh
profile. dsh has no `--agent` flag, so role specialization is carried by an
installed shim instead of client-side agent registration.

What it contains:

- `adapters/dsh/bin/opsx-dsh-worker`: the worker shim — the single
  translation point between the orchestrator's invoke contract and the dsh
  CLI
- `adapters/dsh/agents/opsx-implementer.md`: implementation phase role
  instructions
- `adapters/dsh/agents/opsx-reviewer.md`: strict review phase role
  instructions
- `adapters/dsh/agents/opsx-archiver.md`: non-interactive archive phase role
  instructions
- `adapters/dsh/support/opsx-controller-state-README.md`: state contract
- deployed `plan-authoring.md` reference (copied from `core/plan-authoring.md`)
  at `~/.config/opsx-controller/dsh/plan-authoring.md` (global) or
  `<project>/.opsx-controller/dsh/plan-authoring.md` (project)
- `adapters/dsh/templates/project/AGENTS.snippet.md`: host-project setup
  snippet
- `adapters/dsh/install.sh`: dsh installer

Shim contract:

- Dispatch is `opsx-dsh-worker --role implementer|reviewer|archiver <input>`
  with the controller's worker input block as the final positional argument.
- The shim composes the role instruction file with the input block into one
  prompt and execs `dsh --profile headless [--patch <model-patch>] <prompt>`,
  replacing its own process image so the controller's timeout and
  process-group signal handling apply directly to dsh.
- Role instructions resolve project-first from `.opsx-controller/dsh/agents/`
  relative to the working directory, then global from
  `~/.config/opsx-controller/dsh/agents/`.

Binary resolution (in this order):

1. `DSH_BINARY` — an executable path or a name resolved on `PATH`
2. `dsh` on `PATH`
3. Pinned npx fallback `npx --yes @deepseek-ai/dsh@0.1.0-rc.7`

When none resolves, the shim fails closed naming all three sources. The pin is
a single constant with a re-validation note; dsh is developer preview with
explicit breaking-change warnings, and the shim is the one place a CLI change
touches.

Model override via generated patch:

- `OPSX_<ROLE>_MODEL` is split into provider/model, the provider is mapped
  through the built-in `deepseek` → `deepseek-official` map overlaid by the
  `OPSX_DSH_PROVIDER_MAP` JSON environment variable, and a top-level-array
  loader patch (`- id: agent-default-model` with a `config:` map of the
  mapped provider and model) is written under `$DSH_HOME/patches/` and
  passed via `--patch`. A provider-less `OPSX_<ROLE>_MODEL` fails closed —
  the validated dsh release requires a provider in the patch config.
- The patch shape was validated against dsh `0.1.1-rc.2`; keep the
  `PINNED_DSH_PACKAGE` and `BUILTIN_PROVIDER_MAP` constants in
  `opsx-dsh-worker` in sync with any release you move the pin to.
- No `OPSX_<ROLE>_MODEL` → no `--patch` → dsh's shipped default model
  applies. Secrets are never written into patches or prompts.
- Before writing a new patch the shim sweeps stale `opsx-*-model-*.yml`
  files older than one hour from `$DSH_HOME/patches/`; fresh patches and
  operator-owned files are left untouched.

Reasoning variants:

- `OPSX_<ROLE>_VARIANT` (set by the orchestrator from the resolved
  `<role>_variant`) uses canonical `low`, `medium`, `high`, and `max` labels;
  dsh maps them to `off`, `low`, `high`, and `max` respectively. An unknown
  non-empty label prints a role/value diagnostic and is dropped so dsh's
  default effort applies.
- dsh accepts the effort only through the `agent-default-model` settings
  section of `$DSH_HOME/settings.yaml` (not the patch config), so the shim
  merges the resolved variant into exactly that key, preserving every other
  setting. A stage with no variant removes the key so a previous stage's
  effort cannot leak.

Controlled runtime environment:

- `DSH_HOME` resolves as ambient `DSH_HOME` → `OPSX_DSH_HOME` → a default
  under the user's state directory.
- `DSH_PERMISSION_MODE=workspace-write`, `DSH_TOOLS_MODE=code`, and
  `DSH_TELEMETRY_DISABLED=1` are defaulted without overriding operator-set
  values.
- A stable startup `AGENTS.md` is written into `DSH_HOME` only when absent.
  dsh reads the project `AGENTS.md` from the working directory natively.

Requirements:

- Node.js with TypeScript type-stripping
  (`process.features.typescript`); DFSG distro Node builds boot dsh but every
  tool call fails. The installer warns when the check is falsy.
- either a `dsh` binary or `npx` on `PATH` (the pinned npx fallback dispatches
  dsh without a real binary)
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo guidance in the host project's `AGENTS.md`

Install:

```bash
bash adapters/dsh/install.sh --global
bash adapters/dsh/install.sh --project /path/to/project
```

Add `--verify` to either form to check the deployment after installing.

Project install behavior:

- copies role instructions and support files into
  `<project>/.opsx-controller/dsh/`
- project-installed role files shadow the global ones (the shim resolves the
  project directory first)

State path: durable per-change state files live at
`.opsx-controller/<change-id>.json` (project root) because dsh has no
protected project config directory. That file is the authoritative per-change
state the controller writes and the dsh worker reads via `STATE_FILE`; on
resume the controller validates it and stops with a diagnostic when it is
malformed or belongs to a different change. Plan-level bookkeeping stays under
`.opsx-plan/<plan>.state.json` as an internal mechanism, separate from the
per-change file.

Usage from the host project root: run the plan loop with `opsx-plan run` on a
manifest with `adapter = "dsh"`; there are no slash commands, and
`opsx-plan compile` and single-change `opsx-run` are not supported (use
`--adapter opencode` or `--adapter claude-code` to compile). To advertise the
controller path in the host repo's instructions, merge
`adapters/dsh/templates/project/AGENTS.snippet.md` into its `AGENTS.md`.

## Packaging

### Claude Code plugin

A shareable Claude plugin lives at `plugins/opsx-controller/`.

```bash
claude --plugin-dir ./plugins/opsx-controller
```

```text
/opsx-controller:opsx-plan <planning request>
```

Why use the plugin package:

- namespaced Claude skill for sharing across projects
- self-contained `skills/` and `agents/` layout
- ready to evolve toward marketplace distribution

As with the Claude Code adapter, `/opsx-controller:opsx-plan` authors the
markdown plan document; `opsx-plan compile` supports both `--adapter opencode`
and `--adapter claude-code`. The plugin reports when compilation was
unavailable rather than implying success.

### Codex plugin

A self-contained plugin bundle at `adapters/codex-cli/plugin/` is ready for
Codex marketplace distribution, including the controller skill, phase agents,
and a manifest at `.codex-plugin/plugin.json`.

```bash
bash adapters/codex-cli/install.sh --plugin
```

### Vercel skill package

A Vercel skill package lives at `skills/opsx-controller/`.

```bash
SKILL_BASE_URL="https://github.com/brianmoney/opsx-controller/tree/main" \
  npx skill skills/opsx-controller
```

Current scope:

- provides a discoverable skill wrapper around the shared workflow contract
- installs with Vercel's `npx skill` flow
- bundles self-contained reference docs for the core workflow and adapter usage

It is a guidance package, not a full cross-client automated installer.

## Deprecation notes

> **Historical.** The legacy `/opsx-drive` command and its nested-controller
> single-change path (once shipped per-adapter as `/opsx-drive`,
> `/opsx-controller:opsx-drive`, or `$opsx-drive`) have been **removed**. They
> are recorded here only so an operator who remembers the old surface knows
> what happened to it: no adapter ships it any longer, and direct dispatch has
> been the only execution path since the stage invokes were introduced.

Use `opsx-run <change-id>` (equivalently `opsx-plan run-one <change-id>`) for
single-change execution: it is OpenCode-pinned (`run-one` has no `--adapter`
flag) and drives the implement/review/archive loop with the same retry,
no-progress, and archive-verification gates. Claude Code and dsh users run a
single change via a one-change plan manifest with the matching `adapter` and
`opsx-plan run`; Codex CLI plan-run is unsupported.

## Adding another adapter

The repository is organized so additional client adapters can be added without
changing the core controller semantics. Map that client's packaging model onto
the same three phases — implement, review, archive — and keep the durable state
contract, strict review gate, and explicit archive scope behavior intact.
