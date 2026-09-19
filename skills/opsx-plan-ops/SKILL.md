---
name: opsx-plan-ops
description:
  Operate, monitor, triage, and recover `opsx-plan` runs against plan
  manifests (TOML dependency DAGs of OpenSpec changes). Use this skill whenever
  a plan run bombs, stalls, or fails — messages like 'review output invalid',
  'expected a final JSON object line', provider 5xx in worker logs, 'modified
  requirement header not found', a blocked/failed change in `opsx-plan status`,
  or the user asking to run, reset, resume, or watch a plan. Also use before
  launching any background `opsx-plan run`, because launch discipline and the
  clean-tree gate are where most self-inflicted failures come from.
license: MIT
metadata:
  author: brianmoney
  version: '1.0.0'
---

# opsx-plan-ops — operations playbook

`opsx-plan` drives OpenSpec changes through implement → review → archive using
model workers. The controller is deterministic; the workers are not. Nearly
every plan failure is one of a small set of known shapes — triage by symptom
first, root-cause second, and fix at the correct layer (controller repo, model
config, plan content, or operator process).

## Mental model

- A **plan** (a TOML manifest, conventionally `openspec/plans/<name>.toml`)
  lists changes in phases with `depends_on`, plus gates:
  `require_clean_tracked`, `fast_checks`, `pause_before`,
  `escalate_after_review_fails`, `invalid_output_retries`.
- Each change loops **rounds** of `implement` → `review`; a zero-finding
  review promotes to `archive`. `pause_before = true` changes wait for
  `opsx-plan approve <change-id>`.
- State lives in `.opsx-plan/` under the target repo: `<plan>.state.json`
  (controller state), `workers/<plan>/<change>.json` (per-change worker
  state), `logs/<change>.<stage>.r<round>.<attempt>.log` (one file per worker
  invocation).
- **Worker output contract**: every worker's final message must be exactly one
  line containing one JSON object — no prose before or after. The controller
  parses the log for that line; a worker that ends in prose fails the stage
  with `output invalid: expected a final JSON object line`.
  `invalid_output_retries` (default 2) re-runs the stage with a
  `RETRY_CORRECTION` hint before failing the change.
- **Unattended runs** are owned by the `opsx-plan autopilot` wrapper, a
  systemd user unit — not by a foreground shell. Autopilot loops
  `opsx-plan run`, auto-resets transient failures, gates `pause_before`
  changes behind a veto window, and escalates everything else. See
  `docs/opsx-plan-operator-workflow.md` → "Autopilot (unattended runs)".

## Pre-flight (before every `opsx-plan run`)

1. `git status --short` — `require_clean_tracked = true` (the default) refuses
   a dirty tracked tree. Workers commit `wip: <change-id>` commits as they go,
   but a killed run often leaves finished work uncommitted. Commit it (same
   `wip:` style) before launching; ask the owner before any git mutation.
2. `opsx-plan status` — know which changes are pending/failed/blocked and
   which phase a failed change died in.
3. `opsx-plan doctor` — preflight checks: models resolve, client on PATH,
   tree clean, plan loads. Cheaper than discovering these mid-run.
4. Confirm the worker models and variants are sane:
   `opsx-plan models show --adapter <adapter>`. Variants are model-specific
   effort labels; an invalid variant is **silently dropped to default** (see
   triage table).

## Launch discipline

For the full step-by-step operator procedure, see
`docs/opsx-autopilot-runbook.md`.

`opsx-plan run` is a foreground, serial process: if it is a background job of
an agent shell, a shell timeout or cleanup kills the whole process group —
including the controller mid-stage. For anything past a quick
`--max-changes 1` smoke run, do **not** hand-roll `setsid nohup`. Launch
unattended work through the **autopilot wrapper**, which supervises
`opsx-plan run` in a loop from a systemd user unit.

```bash
# One-time: enable the unit. A drop-in (plan.conf) sets WorkingDirectory and
# OPSX_PLAN; the template ships installed-but-disabled.
systemctl --user enable opsx-autopilot
systemctl --user start  opsx-autopilot

# Watch it
systemctl --user status opsx-autopilot
journalctl --user -u opsx-autopilot -f
```

What autopilot does in your stead (full detail in
`docs/opsx-plan-operator-workflow.md` → "Autopilot (unattended runs)"):

- **Transient failures** (`subagent_output_invalid`, timeouts) get a bounded
  auto-reset — default 2 per failure signature, at least 300s apart.
  Signatures persist in `.opsx-plan/autopilot-state.json`, so they survive
  `opsx-plan reset`.
- **`pause_before` gates**: autopilot notifies, waits a veto window (default
  30 min), then auto-approves. Veto by creating `.opsx-plan/veto/<change-id>`
  during the window; approving manually short-circuits the wait.
- **Permanent classes** (billing/quota, permission rejections,
  `finding_recurrence_exceeded`, `max_rounds_reached`, `no_progress`, archive
  failures, unknown) escalate immediately — never auto-retried.

Escalation appends a digest to `.opsx-plan/escalations.jsonl` (change id,
class, `last_result`, reason, finding loci, attempt count, stage log path,
suggested action), sends an ntfy push, and leaves the unit **down** until you
fix and restart it (change-level classes exit 0; `environment` exits 2 and is
retried until `StartLimitBurst` trips). Every decision is logged to
`.opsx-plan/autopilot-events.jsonl`. Monitor with `opsx-plan status` and the
journal; worker logs still stream into `.opsx-plan/logs/`, and
`opsx-plan logs --follow` selects the in-progress one.

## Triage: symptom → cause → action

| Symptom | Likely cause | Where to look | Fix |
|---|---|---|---|
| `output invalid: expected a final JSON object line` | Worker ended with prose (contract miss) | `opsx-plan logs --change <id> --stage <stage>` — the worker's last message is a summary, not JSON | Normally absorbed by `invalid_output_retries`. Recurring? Harden the worker agent prompt in the controller repo and reinstall the adapter |
| `Error: UnknownError` / "Unexpected server error" in a worker log | Transient provider 5xx | The stage log is tiny and contains only the error | Retriable — `opsx-plan reset <change>` then run. Sustained failures mean provider outage or a bad model id |
| Review quality is oddly shallow, or `variant` looks ignored | Silent variant downgrade: the configured variant is not a valid label for the pinned model, so the client falls back to default | `opsx-plan models show` prints resolved variants with their source; the client's own session records show the *actual* variant used | Set `<role>_variant` in `~/.config/opsx-controller/models.toml` to a valid label for the pinned model, reinstall the adapter, re-run |
| Archive fails: `modified requirement header not found` / `archive_spec_update_failed` | Delta spec MODIFIED header doesn't match the canonical spec verbatim — a requirement's name is its identity; the implementer renamed it while extending the body | `grep -n "^### Requirement"` in `openspec/changes/<change>/specs/<cap>/spec.md` vs `openspec/specs/<cap>/spec.md` | Fix the **delta**, never the canonical spec, to resolve the mismatch. This is a semantic decision — use the trusted-model pattern below |
| Run refuses to start / stops immediately | Dirty tracked tree, or `pause_before` gate awaiting approval | `git status --short`; `opsx-plan status` | Commit wip work; `opsx-plan approve <change-id>` for gated changes |
| Run died mid-stage with no error | Controller process killed (shell timeout, laptop sleep, OOM) | `ps` shows no `opsx-plan run`; last log ends mid-stage | Under autopilot the unit restarts it (`Restart=on-failure`); a manual run dead-ends. Completed stage work in the log is not reused — the stage re-runs; a no-op implement with all tasks done is cheap |
| Change loops review-fail rounds with the same finding | Recurring defect; escalation may engage after `escalate_after_review_fails` rounds | `PRIOR_FINDING_LOCI` in the worker input header of each review log | Check `finding_recurrence_limit` semantics; consider `opsx-plan reset` and manual intervention on the finding's locus |

Autopilot's failure classifier mirrors this table: the transient rows (worker
output contract misses and provider errors) get a bounded auto-reset;
everything else escalates rather than being retried.

## Trusted model + deterministic context (for semantic fixes)

When the blocker is a judgment call rather than a mechanical bug (spec
identity, requirement renames, contract interpretation), do not let the
incident-handling agent improvise it inline, and do not hand it to a cheap
worker. Dispatch one trusted model with a fully bounded context:

1. Name the exact files to read (delta vs canonical, not "the specs").
2. State the failure message and the governing semantics (e.g. "a
   requirement's name is its identity; MODIFIED must match verbatim").
3. Enumerate the acceptable options and the selection criterion (minimal
   churn, identity preserved).
4. Constrain scope: which file(s) may be edited, which validations must run,
   no commits.
5. Require a one-paragraph decision report as the final message.

Invoke it as a one-shot with your client's non-interactive mode and an
explicit strong model. Verify the diff afterward — trust, but read the git
status.

## Recovery playbook

1. Fix the root cause at its layer (see table).
2. Commit any uncommitted worker output — the run that follows a reset
   refuses a dirty tracked tree (`require_clean_tracked`).
3. `opsx-plan reset <change-id>` — returns the change to pending so it
   re-enters the implement phase. A reset change with all tasks complete
   re-runs implement as a fast no-op, then review, then archive. This is
   normal, not a loop.
4. Restart autopilot and watch the first stage transition before walking away:
   `systemctl --user start opsx-autopilot`. (An unattended run that escalated
   left the unit down on purpose — autopilot never retries a permanent class;
   see the "Launch discipline" section.)
5. After `done`, the plan stops at the next `pause_before` change — that is
   the checkpoint to review diffs before approving.

## Forensics: adapter session records

When the worker log isn't enough (did the model stop, crash, or get
truncated?), the adapter client usually keeps its own session records. One
worked example, for the opencode adapter:

- `~/.local/share/opencode/opencode.db` (sqlite; use Python's `sqlite3` if the
  CLI is absent):
  - `session` table: per-session model + **actual** `variant`, token totals,
    cost, agent name. Find sessions by time/agent.
  - `message` table (`data` JSON): per-message `finish` reason (`stop`,
    `tool-calls`, `length`), `error`, and token breakdown. `finish: stop`
    with a prose final message = clean contract miss; `length` = truncation;
    an `error` field = provider failure.
- `~/.local/share/opencode/log/opencode.log` corroborates with
  `stream`/`loop` lines per session id.
- Valid variant labels per model:
  `~/.cache/opencode/models.json` → `<provider>.models.<id>.reasoning_options`.

Other adapters keep equivalent records in their own session stores — the
questions to answer are always the same: which model and variant actually
ran, how did the last message finish, and was there a provider-side error.

## Boundaries

- Controller bugs, worker prompt fixes, and retry/escalation semantics are
  fixed **in the opsx-controller source repo**, then deployed with the
  adapter's installer (`bash adapters/<adapter>/install.sh --global
  --verify`), which also redeploys `opsx-plan`/`opsx-run`. Never patch the
  installed copies directly — and note that after any `orchestrator/` change
  the reinstall is *required*, not optional: a stale installed runtime fails
  outright rather than serving old behavior (`opsx-plan doctor` diagnoses
  this).
- Worker models and variants are configured in
  `~/.config/opsx-controller/models.toml` (`<role>` and `<role>_variant`
  keys), not in plan files.
- Ask the owner before any git mutation (commits, resets) in the target repo
  — the wip-commit convention is `wip: <change-id>`.
