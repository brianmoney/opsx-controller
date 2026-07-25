# Plan: Complete Claude Code Plan Authoring Support

## Goal

Let Claude Code users author phased OpenSpec implementation-plan Markdown with
the same machine-readable convention as the OpenCode `/opsx-plan` command,
without implying that Claude Code can compile the plan to TOML when the shared
compiler is configured only for OpenCode.

## Scope

- Add a Claude Code `opsx-plan` skill that authors one plan document.
- Give that skill an explicit authoring implementation rather than relying on an
  unspecified built-in `build` agent.
- Package the feature consistently for adapter installs and the standalone
  Claude plugin.
- Document the OpenCode dependency of the existing `opsx-plan compile` command
  accurately.

## Non-Goals

- Do not change the shared `opsx-plan compile` implementation to invoke Claude.
- Do not add redundant manual review, verification, or archive skills; the
  existing `opsx-drive` controller already owns those phases.
- Do not add installer code that duplicates the existing generic skill-copy
  behavior.

## Implementation Steps

### 1. Define the Claude plan-authoring architecture

Create a dedicated `opsx-plan-author` Claude Code agent, or keep all authoring
instructions directly in the `opsx-plan` skill. Prefer a dedicated agent if the
prompt needs to stay long and reusable.

If an agent is added, it must be explicitly present in both of these package
layouts:

- `adapters/claude-code/agents/opsx-plan-author.md`
- `plugins/opsx-controller/agents/opsx-plan-author.md`

The skill must name that agent explicitly when delegating. Do not reference a
generic `build` agent: this repository does not provide one for Claude Code.

### 2. Add the `opsx-plan` skill to the Claude adapter

Create `adapters/claude-code/skills/opsx-plan/SKILL.md`.

Mirror the plan-document rules in
`adapters/opencode/commands/opsx-plan.md` and
`adapters/opencode/commands/opsx-author.md`:

- Accept one planning request and reject an empty request.
- Read `CLAUDE.md`, `AGENTS.md`, referenced source material, existing
  capabilities, and existing active or archived change ids.
- Default the output path to `docs/plans/<kebab-case-topic>-plan.md` unless the
  request supplies a path.
- Refuse to overwrite an existing document unless the request explicitly asks
  to replace or revise it.
- Produce the required frontmatter, phase/change structure, dependency syntax,
  capability ownership, success parameters, manual gates, and non-goals.
- Re-scan dependency paragraphs before reporting success.

The skill should advertise `/opsx-plan <planning request>`. Do not add a second
`/opsx-author` surface unless there is a demonstrated compatibility requirement.

### 3. Handle compilation honestly

The existing `opsx-plan compile` command invokes OpenCode and requires
`OPSX_CONTROLLER_MODEL`. A Claude-only installation cannot run it without an
OpenCode installation and configuration.

The new Claude skill must therefore:

- Run the compile self-check only when `opsx-plan` is available and its OpenCode
  prerequisites are configured.
- Otherwise report that the document was authored but not compiled, and state
  that an OpenCode-configured environment must run `opsx-plan compile` before
  plan execution.
- Never present successful Markdown authoring as successful TOML compilation.

Keep this limitation documented until a separately scoped change makes the
compiler client-selectable or adds a Claude-backed compiler.

### 4. Package the skill for both Claude Code distribution paths

Add the same `opsx-plan` skill to the standalone plugin:

- `plugins/opsx-controller/skills/opsx-plan/SKILL.md`

Keep plugin and adapter prompts in sync. Update
`plugins/opsx-controller/README.md` to list the new skill and its namespaced
invocation:

```text
/opsx-controller:opsx-plan <planning request>
```

### 5. Update host-project guidance

Update `adapters/claude-code/templates/project/CLAUDE.snippet.md` to describe:

- `/opsx-plan <planning request>` for plan-document authoring.
- `/opsx-drive <change-id>` for an accepted single-change controller run.
- The requirement to compile the authored document before running a plan, and
  the current OpenCode dependency of that compile operation.

Update the top-level README Claude Code adapter section and plugin section with
the new command and its compilation limitation.

### 6. Preserve installer behavior

Do not change `adapters/claude-code/install.sh` merely to add the skill. Its
existing `install_skills` loop copies every skill directory for both global and
project installations. Verify that a new `opsx-plan` directory is included in
both destinations.

## Validation

1. Run `bash adapters/claude-code/install.sh --global --verify` and confirm both
   `opsx-drive` and `opsx-plan` are installed under `~/.claude/skills/`.
2. Install into a temporary project and confirm both skills appear under
   `<project>/.claude/skills/` and the existing state-file ignore rule remains
   unchanged.
3. Run `claude --plugin-dir ./plugins/opsx-controller` and confirm the plugin
   exposes both `/opsx-controller:opsx-drive` and
   `/opsx-controller:opsx-plan`.
4. Use the authoring skill to create a small plan document, verify its required
   Markdown structure and dependency syntax, and confirm it does not overwrite
   an existing path without explicit permission.
5. In an environment with OpenCode, `opsx-plan`, and
   `OPSX_CONTROLLER_MODEL` configured, run `opsx-plan compile <doc> -o <file>`
   and `opsx-plan run <file> --dry-run`.
6. In a Claude-only environment, verify the skill clearly reports that compile
   self-checking is unavailable rather than claiming compilation succeeded.

## Follow-Up Change

If Claude-native compilation is required, create a separate OpenSpec change to
make the shared compiler provider-selectable, define its Claude headless
invocation and model configuration, add tests, and update the operator
documentation. Do not couple that orchestration change to plan-document
authoring.
