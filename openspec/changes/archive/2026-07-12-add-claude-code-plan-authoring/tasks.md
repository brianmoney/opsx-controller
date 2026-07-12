## 1. Claude Authoring Surface

- [x] 1.1 Add an `opsx-plan` Claude Code skill that accepts exactly one
  non-empty planning request and delegates explicitly to `opsx-plan-author`.
- [x] 1.2 Add the `opsx-plan-author` agent with the required repository
  discovery, output-path, overwrite-protection, plan-structure, dependency,
  capability, and final-result rules.
- [x] 1.3 Require the agent to distinguish a successful OpenCode-backed compile
  self-check from Markdown-only authoring when compile prerequisites are absent.

## 2. Plugin Packaging And Documentation

- [x] 2.1 Add equivalent `opsx-plan` skill and `opsx-plan-author` agent
  artifacts to `plugins/opsx-controller/`, using namespaced delegation.
- [x] 2.2 Update the plugin README with the namespaced authoring command and
  the OpenCode compiler limitation.
- [x] 2.3 Update the Claude host-project snippet and top-level README with
  authoring usage, `/opsx-drive` usage, and the required compilation step.

## 3. Installer And Validation

- [x] 3.1 Fix `adapters/claude-code/install.sh` --verify flag parsing (the
  `shift` inside the `for` loop did not work; replaced with array-filtering
  pattern from the OpenCode adapter) and verify its generic skill-copy behavior
  installs `opsx-plan` for both global and project destinations without
  changing the existing state-file ignore rule.
- [x] 3.2 Run `bash adapters/claude-code/install.sh --global --verify` and
  confirm `opsx-drive` and `opsx-plan` are installed under `~/.claude/skills/`.
- [x] 3.3 Install into a temporary project and confirm both skills and the
  authoring agent are copied under `.claude/` while its gitignore rule remains
  unchanged.
- [x] 3.4 Run `claude --plugin-dir ./plugins/opsx-controller` and verify both
  namespaced skills are exposed.
- [x] 3.5 Author a small plan with the skill, validate its required Markdown
  structure and dependency syntax, and prove an existing output is not
  overwritten without explicit instruction.
- [x] 3.6 In an OpenCode-configured environment, compile the authored document
  and dry-run the result; in a Claude-only environment, verify the skill reports
  Markdown-only authoring without claiming compilation.
- [x] 3.7 Run `openspec validate add-claude-code-plan-authoring --strict`.
