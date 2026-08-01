## Why

The implement-review loop has no way to notice that it is re-reporting the
same defect. Two controls exist today and neither catches it:

- `no_progress_limit` measures whether the implementer *did something*
  (files touched, tasks completed), not whether it fixed *the thing*. A
  change can edit files every round, report progress every round, and never
  close the finding that is blocking it.
- `max_rounds` is a budget ceiling, not a stall detector. It fires only after
  the budget is fully spent.

Evidence from the `email-handoff-routing-safety` plan run
(`.opsx-plan/logs/*.review.r*.log`, rounds 1-8 of two changes):

| change | rounds | observed |
|---|---|---|
| `align-agent-retry-escalation-semantics` | r7 → r8 | critical finding repeated **byte-for-byte identical** |
| `enforce-trusted-email-handoff-outcomes` | r3 → r4 | critical finding repeated **byte-for-byte identical** |
| `enforce-trusted-email-handoff-outcomes` | r1,r2,r3,r6,r7,r8 | `sources/gmail/intake.py:_apply_coordination_outcome` cited by a critical finding in **six of eight rounds** |
| `align-agent-retry-escalation-semantics` | r4,r5,r7,r8 | `scheduling/heartbeat.py:_dispatch_pending_handoff` cited in four rounds |

In `align-agent-retry-escalation-semantics` round 8 the implementer reported
progress and touched files, so `no_progress_streak` never incremented — yet
the review finding it produced was character-identical to round 7's. That
round was pure waste and nothing in the system could see it.

Escalation does not solve this. `escalate_after_review_fails = 3` fired at
round 4 in both changes above; critical counts then went `4,4,4 → 5,5,3` and
`2 → 1,1,1`. Promoting the model did not break either stall, because the
trigger (rounds elapsed) is not evidence of stuckness.

Recurrence of a finding locus *is* evidence of stuckness, and it is
detectable — but only if findings survive as structured data. Today they do
not: `fix_prompt` is free prose, `latest_fix_prompt` is overwritten each
round, and per-round history stores `finding_counts` only. The logs are the
sole record, and they are only parseable by hand.

## What

- Extend the reviewer contract so each review returns a machine-readable
  `findings` array (severity, repo-relative locus list, statement) alongside
  the existing prose `fix_prompt`, which is unchanged.
- Persist per-round findings in controller state so recurrence is computable
  from state rather than from stage logs.
- Derive finding identity **in the orchestrator, not in the reviewer.** The
  run data shows the reviewer re-describes one defect with different prose
  and a different lead symbol across rounds even while
  `LATEST_FIX_PROMPT` is in its input, so a reviewer-assigned id would drift
  exactly as the prose drifted. Identity is computed by normalizing each
  locus against the repository's tracked files.
- Add a plan key `finding_recurrence_limit` that halts a change when a single
  normalized locus has been cited by a blocking finding in that many distinct
  rounds. Default `0` (disabled), matching the introduction of
  `escalate_after_review_fails`.
- Supply the prior round's finding loci to the next review dispatch so the
  reviewer can name a carried-forward defect consistently.

## Non-goals

- **This change does not alter escalation.** `escalate_after_review_fails`
  keeps its current round-count trigger. Retargeting escalation onto
  recurrence is a plausible follow-up, but the run data above shows
  escalation made both stalls no better, so recurrence halts for operator
  triage rather than silently promoting the model into a stall.
- **This change does not fix the inert review gate.** `skip_warning` and
  `skip_suggestion` are separately broken: every branch of the gate in
  `apply_review_result` requires `verdict == "pass"`, while every reviewer
  agent is instructed to return `pass` only when all three counts are zero,
  so the keys can never take effect. That is its own change. This proposal
  is written to compose with it — "blocking finding" is defined in terms of
  whichever severities gate under the active configuration.

## Risk

Cumulative (not consecutive) locus counting is the detection rule, because
consecutive counting misses the real pattern: in
`align-agent-retry-escalation-semantics` the recurring locus appears in
rounds 4,5 and again in 7,8 — never three rounds in a row — so a consecutive
streak counter with a limit of 3 would never fire.

The cost is that a locus legitimately touched across several converging
rounds can trip the ceiling early. In
`enforce-trusted-email-handoff-outcomes` a limit of 3 would halt at round 3,
while critical counts were still falling (12 → 8 → 4). In that specific run
the halt would have been correct in hindsight — the same symbol was still
broken at round 8 — but that is not guaranteed in general.

This is mitigated by defaulting to disabled, by halting for operator triage
rather than failing the plan silently, and by naming the offending locus and
its rounds in the halt reason so a false positive costs one glance rather
than a lost run.

## Impact

- `orchestrator/opsx-plan.py` — `load_plan` (new key), `apply_review_result`
  (finding persistence, identity computation, ceiling check),
  `render_single_change_manifest`, `build_schema_guidance`,
  `NO_RETRY_RESULTS`, review dispatch input.
- `adapters/opencode/agents/opsx-reviewer.md`,
  `adapters/claude-code/agents/opsx-reviewer.md`,
  `adapters/codex-cli/agents/opsx-reviewer.toml`,
  `plugins/opsx-controller/agents/opsx-reviewer.md` — reviewer output
  contract.
- `orchestrator/samples/sample-plan.toml` — new key in the canonical sample.
- `tests/orchestrator/test_opsx_plan.py` — recurrence, normalization, and
  backward-compatibility coverage.
