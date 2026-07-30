# Adapter Reference

`opsx-controller` ships one adapter per coding client. Each adapter maps the
same three phases — implement, review, archive — onto that client's packaging
model, so the core controller semantics stay identical.

- [Choosing an adapter](#choosing-an-adapter)
- [Model configuration](#model-configuration)
- [OpenCode adapter](#opencode-adapter)
- [Claude Code adapter](#claude-code-adapter)
- [Codex CLI adapter](#codex-cli-adapter)
- [Packaging: Claude plugin, Codex plugin, Vercel skill](#packaging)
- [Deprecation notes](#deprecation-notes)

## Choosing an adapter

All three adapters drive the same loop. They differ in what gets installed and
in the plan compilation and authoring capabilities that vary by adapter:

| | OpenCode | Claude Code | Codex CLI |
|---|---|---|---|
| implement / review / archive loop | yes | yes | yes |
| `opsx-plan` + `opsx-run` on `PATH` | installed to `~/.local/bin` | installed to `~/.local/bin` | installed to `~/.local/bin` |
| `opsx-plan compile` (markdown plan → TOML) | yes (OpenCode controller model) | yes (Claude Code controller model) | needs OpenCode or Claude Code |
| plan authoring skill (`/opsx-plan`) | yes | yes | — |

All three adapters install `opsx-plan` and `opsx-run` to `~/.local/bin/`
via the shared installer helper. The Codex CLI adapter does not support
plan compilation (`opsx-plan compile`) but its global installer still
deploys the orchestrator executables — use `opsx-plan` or
`opsx-run` from `PATH` as you would with any other adapter.

`opsx-plan compile` supports OpenCode (the default) and Claude Code (via
`--adapter claude-code`). Each requires a `controller` model resolved for
the corresponding adapter — the compiler rejects invalid model syntax
before writing any output. Plan compilation is not supported through the
Codex CLI adapter; use `--adapter opencode` or `--adapter claude-code`
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

## OpenCode adapter

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
  `/opsx-apply`, `/opsx-verify`, `/opsx-archive`

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

The installer resolves each agent's `model` value through the resolver and
writes concrete `provider/model` values into the installed Markdown agent
files. That baked value is only used by the deprecated `/opsx-drive`
nested-controller path — direct dispatch, the default execution path, reads
`models.toml` fresh at every plan load and needs no reinstall. Re-run the
installer only if you still depend on `/opsx-drive`.

To advertise the controller path in the host repo's instructions, merge
`adapters/opencode/templates/project/AGENTS.snippet.md` into its `AGENTS.md`.

## Claude Code adapter

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

- `adapters/codex-cli/skills/opsx-drive/SKILL.md`: controller skill with
  required YAML frontmatter
- `adapters/codex-cli/skills/opsx-drive/agents/openai.yaml`: optional Codex UI
  metadata
- `adapters/codex-cli/agents/opsx-implementer.toml`: implementation phase agent
- `adapters/codex-cli/agents/opsx-reviewer.toml`: strict review phase agent
- `adapters/codex-cli/agents/opsx-archiver.toml`: archive phase agent
- `adapters/codex-cli/support/opsx-controller-state-README.md`: state contract
- `adapters/codex-cli/templates/project/`: host-project setup snippets
- `adapters/codex-cli/install.sh`: Codex CLI installer
- `adapters/codex-cli/plugin/`: marketplace plugin bundle

Requirements:

- OpenAI Codex CLI
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo guidance in `AGENTS.md`
- Codex CLI configured with `agents.max_depth >= 1` for subagent dispatch

Install:

```bash
bash adapters/codex-cli/install.sh --global
bash adapters/codex-cli/install.sh --project /path/to/project
```

Project install behavior:

- copies skill into `<project>/.agents/skills/opsx-drive/`
- copies agents into `<project>/.codex/agents/`
- installs the controller state contract at
  `<project>/.codex/opsx-controller/README.md`
- ensures `<project>/.codex/.gitignore` ignores `opsx-controller/*.json`

State path differs from other adapters: durable state files live at
`.opsx-controller/<change-id>.json` (project root) because the Codex sandbox
protects the `.codex/` directory from agent writes.

To advertise the controller path in the host repo's instructions, merge
`adapters/codex-cli/templates/project/AGENTS.snippet.md` into its `AGENTS.md`.

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

`/opsx-drive` (the nested-controller single-change path, available per-adapter
as `/opsx-drive`, `/opsx-controller:opsx-drive`, or `$opsx-drive`) is
**deprecated**. Direct dispatch has been the default execution path for both
the `opencode` and `claude-code` adapters since their stage invokes were added,
and `/opsx-drive` is now the only remaining consumer of install-time model
baking.

Use `opsx-run <change-id>` (equivalently `opsx-plan run-one <change-id>`)
instead: it drives the same implement/review/archive loop with the same retry,
no-progress, and archive-verification gates, and requires no plan manifest.
`/opsx-drive` continues to work during the deprecation period — `opsx-plan`
logs a warning when a resolved plan still takes the nested-controller path —
but it will be removed in a later change.

## Adding another adapter

The repository is organized so additional client adapters can be added without
changing the core controller semantics. Map that client's packaging model onto
the same three phases — implement, review, archive — and keep the durable state
contract, strict review gate, and explicit archive scope behavior intact.
