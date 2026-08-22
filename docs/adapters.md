# Adapter Reference

`opsx-controller` ships one adapter per coding client. Each adapter maps the
same three phases — implement, review, archive — onto that client's packaging
model, so the core controller semantics stay identical.

- [Choosing an adapter](#choosing-an-adapter)
- [Model configuration](#model-configuration)
- [OpenCode adapter](#opencode-adapter)
- [Claude Code adapter](#claude-code-adapter)
- [Codex CLI adapter](#codex-cli-adapter)
- [dsh adapter](#dsh-adapter)
- [Packaging: Claude plugin, Codex plugin, Vercel skill](#packaging)
- [Deprecation notes](#deprecation-notes)

## Choosing an adapter

All four adapters drive the same loop. They differ in what gets installed and
in the plan compilation and authoring capabilities that vary by adapter:

| | OpenCode | Claude Code | Codex CLI | dsh |
|---|---|---|---|---|
| implement / review / archive loop (plan-level) | yes | yes | opt-in (hand-written stage invokes) | yes |
| `opsx-plan` + `opsx-run` on `PATH` | installed to `~/.local/bin` | installed to `~/.local/bin` | installed to `~/.local/bin` | installed to `~/.local/bin` |
| single-change `opsx-run` | yes | no (OpenCode-pinned) | no (OpenCode-pinned) | no (OpenCode-pinned) |
| `opsx-plan compile` (markdown plan → TOML) | yes (OpenCode controller model) | yes (Claude Code controller model) | needs OpenCode or Claude Code | needs OpenCode or Claude Code |
| plan authoring skill (`/opsx-plan`) | yes | yes | — | — |

All four adapters install `opsx-plan` and `opsx-run` to `~/.local/bin/`
via the shared installer helper. The Codex CLI and dsh adapters do not support
plan compilation (`opsx-plan compile`) or single-change `opsx-run`, but their
global installers still deploy the orchestrator executables — use
`opsx-plan` from `PATH` as you would with any other adapter.
Single-change `opsx-run` is OpenCode-pinned: `run-one` has no `--adapter` flag
and always uses the OpenCode adapter. Claude Code, Codex CLI, and dsh operators
run single changes via a hand-written plan manifest with `adapter = "claude-code"`
and `opsx-plan run` instead.

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

## OpenCode adapter

What it contains:

- `adapters/opencode/commands/opsx-plan.md`: plan-authoring slash command
- `adapters/opencode/agents/opsx-implementer.md`: implementation round agent
- `adapters/opencode/agents/opsx-reviewer.md`: strict reviewer agent
- `adapters/opencode/agents/opsx-archiver.md`: non-interactive archiver agent
- `adapters/opencode/support/opsx-controller-state-README.md`: state contract
- `adapters/opencode/templates/project/`: host-project setup snippets
- `adapters/opencode/install.sh`: OpenCode installer

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

- `adapters/codex-cli/skills/opsx-plan/SKILL.md`: plan-authoring skill with
  required YAML frontmatter
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

`/opsx-drive` (the legacy nested-controller single-change path, available per-adapter
as `/opsx-drive`, `/opsx-controller:opsx-drive`, or `$opsx-drive`) is
**removed**. Direct dispatch has been the only execution path since the stage
invokes were introduced; the nested-controller path is no longer available.

Use `opsx-run <change-id>` (equivalently `opsx-plan run-one <change-id>`) for
single-change execution: it is OpenCode-pinned (`run-one` has no `--adapter`
flag) and drives the implement/review/archive loop with the same retry,
no-progress, and archive-verification gates. Claude Code, Codex CLI, and dsh
users run single changes via a plan manifest with `adapter = "claude-code"` and
`opsx-plan run`.

## Adding another adapter

The repository is organized so additional client adapters can be added without
changing the core controller semantics. Map that client's packaging model onto
the same three phases — implement, review, archive — and keep the durable state
contract, strict review gate, and explicit archive scope behavior intact.
