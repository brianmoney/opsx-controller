## OpenSpec Controller Workflow

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
