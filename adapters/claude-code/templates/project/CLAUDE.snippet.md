## OpenSpec Controller Workflow

Use `/opsx-plan <planning request>` to author one phased OpenSpec
implementation-plan markdown document.

- `/opsx-plan` writes a source markdown plan document, usually at
  `docs/plans/<kebab-case-topic>-plan.md` unless you request another path.
- The authored markdown must be compiled before plan execution.
- `opsx-plan compile` currently depends on an OpenCode-configured environment
  and `OPSX_CONTROLLER_MODEL`; Claude-only installs may author the markdown but
  cannot honestly claim TOML compilation succeeded.

Use `/opsx-drive <change-id>` as the default controller entrypoint when you
want the iterative implement-review-archive loop for one accepted OpenSpec
change.

- `/opsx-drive` supports exactly one change per run.
- Durable controller state lives under `.claude/opsx-controller/<change-id>.json`.
- The controller uses the fixed agents `opsx-implementer`, `opsx-reviewer`, and
  `opsx-archiver`.
- The review gate is strict: any critical, warning, or note finding blocks
  archive.
- Clean review auto-archives without a manual confirmation step.
- After editing `.claude/agents/`, restart Claude Code so the updated workflow
  is loaded.
