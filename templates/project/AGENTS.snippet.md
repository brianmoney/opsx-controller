## OpenSpec Controller Workflow

Use `/opsx-drive <change-id>` as the default controller entrypoint when you
want the iterative implement-review-archive loop for one accepted OpenSpec
change.

- `/opsx-drive` supports exactly one change per run.
- Durable controller state lives under
  `.opencode/opsx-controller/<change-id>.json`.
- The controller uses the fixed agents `opsx-controller`, `opsx-implementer`,
  `opsx-reviewer`, and `opsx-archiver`.
- The review gate is strict: any critical, warning, or note finding blocks
  archive.
- Clean review auto-archives without a manual confirmation step.
- Do not use legacy nested-session shell loops as the default workflow when
  `/opsx-drive` is installed.
- After editing `opencode.json` or files under `.opencode/`, restart OpenCode so
  the updated workflow is loaded.
