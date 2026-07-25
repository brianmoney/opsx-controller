# opsx-controller

Portable OpenSpec controller workflow with a shared core contract and client-
specific adapters.

The goal is to make one accepted OpenSpec change easy to drive through a strict
implement, review, and archive loop while keeping the workflow flexible enough
to package for different coding clients.

## Layout

- `core/`: client-neutral controller contract, state schema, and phase protocol
- `orchestrator/`: `opsx-plan` deterministic plan-level orchestrator
- `docs/`: operator workflow and benchmarking guides
- `adapters/opencode/`: OpenCode commands, agents, installer, support files,
  and templates
- `adapters/claude-code/`: Claude Code skill, phase agents, installer, support
  files, and templates
- `adapters/codex-cli/`: Codex CLI skill, phase agents, installer, support
  files, templates, and plugin manifest
- `plugins/opsx-controller/`: Claude Code plugin package for `--plugin-dir` and
  marketplace packaging
- `skills/opsx-controller/`: Vercel `npx skill` package for discovery and
  guided use

## Core Workflow

The shared workflow contract is client-neutral:

- supports exactly one OpenSpec change per run
- persists durable per-change state
- loops implement -> review -> implement until review is clean
- treats any critical, warning, or note finding as blocking
- auto-archives only after a fresh zero-finding review
- fails closed when archive scope or phase output is ambiguous

Start with:

- `core/controller-contract.md`
- `core/state-schema.md`
- `core/phase-protocol.md`

## Plan-Level Orchestrator

`opsx-plan` drives multi-change OpenSpec plans from compile through archive,
with preflight checks, budget controls, manual gates, log inspection,
cost-tracking telemetry, and branch/PR delivery. For single changes, `opsx-run`
skips the plan manifest.

- [**Operator Workflow Guide**](docs/opsx-plan-operator-workflow.md) — the
  full operator-facing workflow: activation, `doctor`, budgets, gates, logs,
  notifications, and branch/PR delivery.
- [**Model Efficiency Workflow**](core/model-efficiency-workflow.md) — how to
  benchmark model choices using telemetry, reports, and dashboards.
- [`orchestrator/README.md`](orchestrator/README.md) — technical reference:
  manifest schema, execution model, retry policy, and adapter invocation.

## OpenCode Adapter

What it contains:

- `adapters/opencode/commands/opsx-drive.md`: main slash command entrypoint
- `adapters/opencode/agents/opsx-controller.md`: controller/orchestrator
- `adapters/opencode/agents/opsx-implementer.md`: implementation round agent
- `adapters/opencode/agents/opsx-reviewer.md`: strict reviewer agent
- `adapters/opencode/agents/opsx-archiver.md`: non-interactive archiver agent
- `adapters/opencode/commands/opsx-review.md`: review prompt used by the
  controller's strict review phase
- `adapters/opencode/commands/opsx-archive-no-prompt.md`: deprecated archive
  helper stub that fails closed and points users to `/opsx-drive`
- `adapters/opencode/commands/opsx-verify-auto.md`: legacy verifier helper
- `adapters/opencode/support/opsx-controller-state-README.md`: state contract
- `adapters/opencode/templates/project/`: host-project setup snippets
- `adapters/opencode/install.sh`: OpenCode installer

Requirements:

- OpenCode
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo-specific guidance in the host project's `AGENTS.md`
- global OpenSpec phase prompts already installed as OpenCode slash commands:
  - `/opsx-apply`
  - `/opsx-verify`
  - `/opsx-archive`

Model configuration, before the first install:

```bash
python3 orchestrator/opsx-plan.py models init   # seeds ~/.config/opsx-controller/models.toml
$EDITOR ~/.config/opsx-controller/models.toml    # set [adapters.opencode] roles
python3 orchestrator/opsx-plan.py models show --adapter opencode
```

Roles are `controller`, `implementer`, `reviewer`, and `archiver`, resolved
per adapter and exported as `OPSX_CONTROLLER_MODEL`, `OPSX_IMPLEMENTER_MODEL`,
`OPSX_REVIEWER_MODEL`, and `OPSX_ARCHIVER_MODEL`. See `models.example.toml`
at the repo root for the file shape. `opsx-plan models init` needs no prior
install — run it against the repo checkout directly. Once installed,
`opsx-plan models ...` works the same way without the `python3
orchestrator/opsx-plan.py` prefix. If no `models.toml` exists, installers and
runs fall back to ambient `OPSX_*_MODEL` environment variables (for example
from `.env`, kept as the legacy path — see `.env.example`).

Install globally:

```bash
bash adapters/opencode/install.sh --global
```

Install into one project:

```bash
bash adapters/opencode/install.sh --project /path/to/project
```

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

The OpenCode adapter installer resolves each agent's `model` value through
the resolver and writes concrete `provider/model` values into the installed
Markdown agent files. That baked value is only used by the deprecated
`/opsx-drive` nested-controller path (see [Deprecation
Notes](#deprecation-notes)) — direct dispatch, the default execution path,
reads `models.toml` fresh at every plan load and needs no reinstall. Re-run
the installer only if you still depend on `/opsx-drive`.

Usage from the host project root:

```text
/opsx-drive <change-id>
```

`/opsx-drive` is **deprecated**. Use `opsx-run <change-id>` instead — it
drives the same loop without the nested-controller path. See
[Deprecation Notes](#deprecation-notes).

If you want the host repo instructions to advertise the controller path, merge
`adapters/opencode/templates/project/AGENTS.snippet.md` into that project's
`AGENTS.md`.

## Claude Code Adapter

What it contains:

- `adapters/claude-code/skills/opsx-drive/SKILL.md`: main Claude Code slash
  command entrypoint
- `adapters/claude-code/skills/opsx-plan/SKILL.md`: implementation-plan
  authoring skill
- `adapters/claude-code/agents/opsx-implementer.md`: implementation phase agent
- `adapters/claude-code/agents/opsx-reviewer.md`: strict review phase agent
- `adapters/claude-code/agents/opsx-archiver.md`: archive phase agent
- `adapters/claude-code/agents/opsx-plan-author.md`: implementation-plan
  authoring agent
- `adapters/claude-code/support/opsx-controller-state-README.md`: state contract
- `adapters/claude-code/templates/project/`: host-project setup snippets
- `adapters/claude-code/install.sh`: Claude Code installer

Requirements:

- Claude Code
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo guidance in `CLAUDE.md`, `AGENTS.md`, or both

Install globally:

```bash
bash adapters/claude-code/install.sh --global
```

Install into one project:

```bash
bash adapters/claude-code/install.sh --project /path/to/project
```

Project install behavior:

- copies skills into `<project>/.claude/skills/`
- copies agents into `<project>/.claude/agents/`
- installs the controller state contract at
  `<project>/.claude/opsx-controller/README.md`
- ensures `<project>/.claude/.gitignore` ignores `opsx-controller/*.json`

Usage from the host project root:

```text
/opsx-plan <planning request>
/opsx-drive <change-id>
```

`/opsx-drive` is **deprecated**. Use `opsx-run <change-id>` instead — see
[Deprecation Notes](#deprecation-notes).

Compilation note:

- `/opsx-plan` authors the markdown implementation plan in Claude Code.
- `opsx-plan compile` still requires an OpenCode-configured environment plus
  a `controller` model resolved for the `opencode` adapter (`opsx-plan models
  show --adapter opencode` to check).
- A Claude-only installation can author the markdown but cannot claim TOML
  compilation succeeded until that OpenCode-backed compile step runs.

If you want the host repo instructions to advertise the controller path, merge
`adapters/claude-code/templates/project/CLAUDE.snippet.md` into that project's
`CLAUDE.md`.

## Codex CLI Adapter

What it contains:

- `adapters/codex-cli/skills/opsx-drive/SKILL.md`: controller skill with required YAML frontmatter
- `adapters/codex-cli/skills/opsx-drive/agents/openai.yaml`: optional Codex UI metadata
- `adapters/codex-cli/agents/opsx-implementer.toml`: implementation phase agent
- `adapters/codex-cli/agents/opsx-reviewer.toml`: strict review phase agent
- `adapters/codex-cli/agents/opsx-archiver.toml`: archive phase agent
- `adapters/codex-cli/support/opsx-controller-state-README.md`: state contract
- `adapters/codex-cli/templates/project/`: host-project setup snippets
- `adapters/codex-cli/install.sh`: Codex CLI installer
- `adapters/codex-cli/plugin/.codex-plugin/plugin.json`: marketplace manifest
- `adapters/codex-cli/plugin/skills/opsx-drive/`: plugin-scoped controller skill
- `adapters/codex-cli/plugin/agents/`: plugin-scoped phase agents

Requirements:

- OpenAI Codex CLI
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo guidance in `AGENTS.md`
- Codex CLI configured with `agents.max_depth >= 1` for subagent dispatch

Install globally:

```bash
bash adapters/codex-cli/install.sh --global
```

Install into one project:

```bash
bash adapters/codex-cli/install.sh --project /path/to/project
```

Project install behavior:

- copies skill into `<project>/.agents/skills/opsx-drive/`
- copies agents into `<project>/.codex/agents/`
- installs the controller state contract at
  `<project>/.codex/opsx-controller/README.md`
- ensures `<project>/.codex/.gitignore` ignores `opsx-controller/*.json`

Usage from the host project root:

```text
$opsx-drive <change-id>
```

`/opsx-drive` is **deprecated**. Use `opsx-run <change-id>` instead — see
[Deprecation Notes](#deprecation-notes).

State path differs from other adapters: durable state files live at
`.opsx-controller/<change-id>.json` (project root) because Codex sandbox
protects the `.codex/` directory from agent writes.

If you want the host repo instructions to advertise the controller path, merge
`adapters/codex-cli/templates/project/AGENTS.snippet.md` into that project's
`AGENTS.md`.

### Codex Plugin

A self-contained plugin bundle at `adapters/codex-cli/plugin/` is ready for
Codex marketplace distribution.

Create the plugin bundle locally:

```bash
bash adapters/codex-cli/install.sh --plugin
```

The plugin includes the controller skill, phase agents, and a marketplace
manifest (`.codex-plugin/plugin.json`).

## Claude Code Plugin

This repo also includes a shareable Claude plugin at `plugins/opsx-controller/`.

Local development and testing:

```bash
claude --plugin-dir ./plugins/opsx-controller
```

Usage:

```text
/opsx-controller:opsx-plan <planning request>
/opsx-controller:opsx-drive <change-id>
```

`/opsx-controller:opsx-drive` is **deprecated**. Use
`opsx-run <change-id>` instead — see [Deprecation Notes](#deprecation-notes).

Why use the plugin package:

- namespaced Claude skill for sharing across projects
- self-contained `skills/` and `agents/` layout
- ready to evolve toward marketplace distribution

Compilation note:

- `/opsx-controller:opsx-plan` authors the markdown plan document.
- `opsx-plan compile` still depends on OpenCode plus a `controller` model
  resolved for the `opencode` adapter, so the plugin must report when
  compilation was unavailable instead of implying success.

## Vercel Skill Package

This repo also includes a Vercel skill package at `skills/opsx-controller/`.

Current scope:

- provides a discoverable skill wrapper around the shared workflow contract
- installs with Vercel's `npx skill` flow
- bundles self-contained reference docs for the core workflow and adapter usage

Example:

```bash
SKILL_BASE_URL="https://github.com/brianmoney/opsx-controller/tree/main" \
  npx skill skills/opsx-controller
```

It is a guidance package, not a full cross-client automated installer.

## Deprecation Notes

`/opsx-drive` (the nested-controller single-change path, available per-adapter
as `/opsx-drive`, `/opsx-controller:opsx-drive`, or `$opsx-drive`) is
**deprecated**. Direct dispatch has been the default execution path for both
the `opencode` and `claude-code` adapters since their stage invokes were
added, and `/opsx-drive` is now the only remaining consumer of install-time
model baking.

Use `opsx-run <change-id>` (equivalently `opsx-plan run-one <change-id>`)
instead: it drives the same implement/review/archive loop with the same
retry, no-progress, and archive-verification gates, and requires no plan
manifest. `/opsx-drive` continues to work during the deprecation period —
`opsx-plan` logs a warning when a resolved plan still takes the
nested-controller path — but it will be removed in a later change.

## Portability Notes

This repository is now organized so additional client adapters can be added
without changing the core controller semantics.

To support another coding client, map that client's packaging model onto the
same three phases:

- implement
- review
- archive

Keep the durable state contract, strict review gate, and explicit archive scope
behavior intact.
