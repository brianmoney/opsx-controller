---
description: Legacy non-interactive archive helper retained for automation compatibility
agent: build
---

Legacy helper note:
- Prefer `/opsx-drive <change-id>` for the supported controller workflow.
- Keep this helper only for deprecated shell-loop compatibility and other
  explicit non-interactive archive escape hatches.

Archive a completed OpenSpec change non-interactively for automation loops.

Resolved automation input:
!`change="${1:-}"; choice="${ARCHIVE_PROMPT_CHOICE:-1}"; printf 'CHANGE=%s\nCHOICE=%s\n' "$change" "$choice"`

Use the resolved `CHANGE` and `CHOICE` values above.

Choice semantics:
- `1`: if unsynced delta specs exist, sync first; if delta specs are already
  synced, archive now.
- `2`: if unsynced delta specs exist, archive without syncing; if delta specs
  are already synced, sync anyway, then archive.
- `3`: cancel and return `ARCHIVE_FAIL: canceled by choice 3`.

Non-interactive rules:
- Never ask a question.
- Never call an ask-user tool.
- If the change name is missing, invalid, or ambiguous, return
  `ARCHIVE_FAIL: missing or invalid change name`.
- If any artifact is not `done`, return
  `ARCHIVE_FAIL: incomplete artifacts for <change>`.
- If any tasks remain unchecked, return
  `ARCHIVE_FAIL: incomplete tasks for <change>`.
- If any sync step is ambiguous or cannot be applied cleanly without
  clarification, return
  `ARCHIVE_FAIL: unable to sync <capability> without clarification`.
- If the archive target already exists, return
  `ARCHIVE_FAIL: archive target already exists: <path>`.

Required workflow:
1. Check `openspec status --change "<change>" --json` and confirm all artifacts
   are `done`.
2. Read the change task file and fail closed if any `- [ ]` tasks remain.
3. Inspect delta specs under `openspec/changes/<change>/specs/`.
4. If delta specs exist, compare each delta spec with
   `openspec/specs/<capability>/spec.md` and determine whether sync work is
   still needed.
5. Apply the delta specs directly to the main specs when `CHOICE` requires
   sync. Use the same intelligent merge behavior as `/opsx-sync`: preserve
   unchanged content, add new requirements or scenarios, modify only the
   targeted requirement blocks, and create new main spec files when the
   capability does not exist yet.
6. Create `openspec/changes/archive/` if needed.
7. Move `openspec/changes/<change>` to
   `openspec/changes/archive/YYYY-MM-DD-<change>`.

Final response requirements:
- Respond with exactly one line.
- Allowed outputs:
  - `ARCHIVE_PASS: <archive-path> specs=<synced|sync-skipped|no-delta|already-synced|synced-anyway>`
  - `ARCHIVE_FAIL: <reason>`
- Do not print headings, bullets, explanations, code fences, or any extra text.
