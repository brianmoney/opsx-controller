---
description: Legacy strict verifier helper retained for automation compatibility
agent: build
---

Legacy helper note:
- Prefer `/opsx-drive <change-id>` for the supported controller workflow.
- Keep this helper only for deprecated shell-loop compatibility and other
  explicit machine-readable verification escape hatches.

Run the global `/opsx-verify` command and convert its result into a strict
machine-readable response for automation loops.

The deprecated `openspec-loop.sh` wrapper greps for a discrete verifier status.
Your final response is parser-facing output, not a human-facing report.

Verify output to classify:
!`if [ -n "$1" ]; then opencode run --agent build "/opsx-verify $1"; else printf 'VERIFY_FAIL: missing change id; rerun /opsx-verify-auto <change-id>.\n'; fi`

Classify the verify output using these rules:
- Return `VERIFY_PASS` only if the verify output contains no CRITICAL issues and
  no WARNING issues.
- Return `VERIFY_FAIL` if the verify output contains any CRITICAL issues, any
  WARNING issues, or if the verify output is missing, unusable, incomplete, or
  ambiguous.
- Return `VERIFY_FAIL` if no change id was provided; do not invoke interactive
  `/opsx-verify` without an explicit change id.
- Ignore SUGGESTION items unless they are needed to explain how to resolve a
  CRITICAL or WARNING issue.
- For `VERIFY_FAIL`, convert the CRITICAL and WARNING issues into a compact fix
  prompt with concrete files and lines when available. Tell the implementing
  agent to update code, tests, and relevant OpenSpec artifacts as needed, then
  rerun `/opsx-verify-auto`.

Final response requirements:
- Respond with exactly one line.
- Allowed outputs:
  `VERIFY_PASS`
  `VERIFY_FAIL: <fix prompt>`
- The first character must be `V`.
- Emit the status line exactly once.
- Do not print the verification report, checks run, evidence, residual risk,
  readiness, headings, bullets, code fences, or any extra commentary.
- If classification is uncertain, return `VERIFY_FAIL: Rerun /opsx-verify for this change, capture the missing CRITICAL/WARNING details, fix the blocking issue, and rerun /opsx-verify-auto.`
