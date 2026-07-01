---
description: Deprecated legacy archive helper; use /opsx-drive instead
agent: build
---

`/opsx-archive-no-prompt` is deprecated and intentionally disabled.

The supported non-interactive archive path is `/opsx-drive <change-id>`, which
dispatches the `opsx-archiver` agent after a clean zero-finding review. That
path validates the OpenSpec change, determines explicit archive scope, syncs
delta specs when safe, moves the change, stages only trusted files, and creates
the required archive commit.

Do not use this helper for `opsx-plan` or controller automation. Its historical
`ARCHIVE_PASS` contract predated the current archive evidence requirements and
is no longer authoritative.

Final response requirements:
- Respond with exactly one line.
- Always return:
  `ARCHIVE_FAIL: /opsx-archive-no-prompt is deprecated; use /opsx-drive <change-id>`
- Do not print headings, bullets, explanations, code fences, or any extra text.
