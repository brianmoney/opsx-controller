# Field lessons for operator-directed frontier supervision

Concrete failure -> reason -> better-action examples gathered from frontier
supervision of a long-running, multi-agent OpenSpec effort. Use them to make the
call when a run stalls, refuses, or reports a suspicious success. Read only the
section that matches your situation, or run the checklist at the end before
accepting any stage.

## Contents

1. [Orchestration and execution discipline](#orchestration-and-execution-discipline)
2. [Evidence and attempt accounting](#evidence-and-attempt-accounting)
3. [Verification that proves behavior](#verification-that-proves-behavior)
4. [Product integrity for supervised services](#product-integrity-for-supervised-services)
5. [Effects, idempotency, and uncertainty](#effects-idempotency-and-uncertainty)
6. [Operator handoff and durable recovery](#operator-handoff-and-durable-recovery)
7. [Pre-review and default-path checklist](#pre-review-and-default-path-checklist)
8. [Handoff and compaction template](#handoff-and-compaction-template)

---

## Orchestration and execution discipline

| Symptom / failure | Why it happens | Better action |
| --- | --- | --- |
| Frontier session starts editing files and running commands itself. | Execution feels faster than briefing an agent. | Keep reasoning, gate decisions and dispositions in the frontier; delegate operations, inspection, commands, implementation, docs, verification, archive and real waiting to the currently configured built-in agents. |
| A shell worker, ad-hoc server, or launch script replaces a configured agent. | The configured agent was awkward for one task. | Use the agents actually configured; do not substitute OpenCode/Claude shell workers and do not override an agent's model or configuration. App/API runtime settings are separate from worker/harness model settings. |
| A restricted registered-service role is treated as an operator. | The skill loaded and "feels" powerful. | Restricted service roles gain nothing from this skill. Keep retired loops/services retired; allow public supported inspection, never a private controller's authority store. |
| Worker instruction is an unstructured paragraph. | Rushing to dispatch. | Use Objective / Current state / Constraints / Execution steps / Completion criteria, in plain grammatical English. Include exact branch/revision, scope, and completion evidence. |
| Two writers touch one worktree, or the writer reviews itself. | Throughput pressure. | One writer per shared worktree; review by a different agent on the actual diff/artifact. Parallelize only independent read-only research. |
| Many tiny peripheral green commits, but the plan never advances. | Activity mistaken for progress. | Name the critical path, detach retained consumers while legacy modules exist, then delete retired modules and their exclusive tests; preserve general behavior/tests. Split a coupled deletion along the dependency edge instead of re-demanding one giant step. |
| A status or dry-run command is assumed read-only. | "Read-only" inferred from the command name. | Some status/dry-run surfaces still write projections or activate ownership. Inspect the actual installed semantics, use an authorized read-only surface or wrapper, and never inspect a private controller's ledger to work around it. |

## Evidence and attempt accounting

| Symptom / failure | Why it happens | Better action |
| --- | --- | --- |
| A PASS is reused for a later, larger scope. | Convenience. | A PASS covers only the reviewed scope; a later extension cannot inherit it. Bind evidence to revision/tree, effective config, actual path, outcome IDs. |
| Attempt counts drift or reset. | A corrective label was treated as a new round. | Reserve counts before dispatch, counting started work, errors, timeouts, cancels. A same-attempt missing-JSON clarification is not a new full round; a corrective label does not reset a counter. |
| Extra zero-warning gates or budgets appear. | "Rigor" applied without authorization. | Do not invent a default zero-warning gate, extra gates, or budgets; do not reintroduce retired cost controls. Change a limit only under actual, current authorization, and stop re-prompting once the user lifts a cap. |
| A strict fail with only note/warning findings is rejected. | Severity not read literally. | Report reviewer severities intact. Accept note/warning-only strict fail under the current skip policy; reserve fix-now for concrete required impact, else follow-up or record-only. |
| Reviewer says PASS but a required check failed. | Deference to the verdict. | Required-check failures, mandatory omissions, and unsupported claims block acceptance even when the reviewer said PASS. |
| Source rounds are spent on cosmetic prose. | Losing sight of what blocks. | Fix cosmetic issues in passing or record them; do not spend source rounds or broad reruns on them. |
| Proof levels are conflated. | "It passed tests" treated as "it works live." | Separate fixtures, the supported default entrypoint, a deployed service, a live provider, approved ownership/cutover, and an actual soak. Read each gate's real evidence requirement; do not invent vendor/live prerequisites for a fixture-scoped gate or promote a connectivity-only canary. |

## Verification that proves behavior

| Symptom / failure | Why it happens | Better action |
| --- | --- | --- |
| Default CLI probed in a warmed process. | An earlier doctor/readiness call left state behind. | Probe in a genuinely fresh process: installed entry-point metadata is not an initialized in-process registry, and shared bootstrap must not rely on a prior doctor/readiness/qualify call. |
| A failure blamed on the wrong environment. | Explicit root flags assumed to prove import origin. | Use the absolute candidate interpreter and inspect the real module/venv paths before blaming or reinstalling another environment. |
| A permissive fake SDK accepts everything. | Fakes swallow `**kwargs`, hiding signature errors. | Pin and read the currently installed method signatures and wire behavior; send and reply may accept different fields. Verify real default construction, registered-file credentials with the ambient environment absent, and per-account resolver isolation without global env mutation. |
| Structured errors vanish from user-facing output. | Only the happy path was serialized. | Keep SDK error, attempt stage and IDs durably in the qualification output. Intent existence does not prove HTTP was attempted; a missing send reference does not prove it was not sent; a TypeError before signature binding differs from one after the network call. |
| Cancellation tests fail under background execution. | SIGINT inheritance differs from the foreground case. | Run required long tests in the foreground with enough command timeout; diagnose the real process/signal topology instead of raising timeouts. "429/retry" text in a read result is not provider-throttling evidence. |
| Archive guard breaks on lifecycle transitions. | Today's active path was hardcoded or tests disabled. | Support active versus archived state and genuine later requirement supersession; keep a retired spec/catalog structurally valid. Do not disable tests or hardcode the active path, and do not rerun broad green suites for a doc-only register edit. |

## Product integrity for supervised services

| Symptom / failure | Why it happens | Better action |
| --- | --- | --- |
| "Complete" because interfaces exist and flags are set. | Composition mistaken for a working path. | Walk the real path: input -> authorization -> actual API/tools -> durable effects and readback -> real answer/result -> outbox -> provider acceptance. A default caller with hardcoded missing prerequisites is not complete. |
| Identity/vocabulary validation treated as authority. | Checking a label instead of a grant. | Re-check current scope/grant before every disclosure, mutation, and resume. An absent launcher trust marker is not a model-facing agent's permission to self-authorize. |
| A refusal is bypassed via direct SDK, helper, SQL, another principal, or unset env. | Wanting the result now. | Do not bypass a real refusal; give the operator concise, supported steps. Do not invent operator-only classifications by analogy to an unrelated command — assess the actual action. |
| Instruction is truncated or re-parsed with regex/prefix rules. | Convenience classifier. | Preserve the full authenticated instruction; use API-backed structured outcome interpretation for ordinary language. Persist stable outcomes before effects and bind each to a compatible target-specific receipt/readback; one receipt or a model claim must not satisfy missing actions. |
| Source context is the whole catalog; caps/budgets are muddled. | Over-broad context injection. | Use the configured agent's approved skills, not an entire catalog. Enforce output caps and cumulative prompt/output budgets as distinct semantics. |
| Status boilerplate delivered instead of an answer. | Effects and answer conflated. | Deliver the grounded answer plus actions/artifacts/citations. Model prose cannot verify effects, but do not discard the answer. Assert that a claimed-but-unperformed action blocks completion. |
| Connected tools only work via a test-only callback. | Test scaffolding treated as production wiring. | Require initialized, session-aware, discovered, invoked production clients via canonical config and scope. Trust mutation/readback metadata, verify target state independently, never accept a remote success Boolean; unavailable capabilities stay honest. |

## Effects, idempotency, and uncertainty

| Symptom / failure | Why it happens | Better action |
| --- | --- | --- |
| An unknown outcome is assumed successful or replayed. | Anxiety about a hang. | Keep applied-but-unverified outcomes uncertain. Only a not-applied result or qualified idempotence permits replay. Stable effect identities must not rely solely on regenerated model call IDs. |
| A restart resends a slightly different body. | Payload rebuilt from memory. | Persist the exact canonical submission payload before the first call so restart reconstructs a byte-identical body/recipients/subject/correlation/key. Do not join all evidence refs or replace lost descriptions with fallback text. |
| Idempotency expiry is evaded or a failed payload is edited under its key. | Wanting the send to go through. | Never refresh the original attempt time to evade expiry; never change a failed payload under its old key. |
| A lost answer is "fixed" by resending an echo. | Treating the answer as the instruction. | Hold it: do not resend an instruction echo or rerun known effects. A correction/follow-up uses honest lineage and preserves old uncertainty and audit. Operator withdrawal suppresses future attempts, not proof of unsent/nonacceptance/completion. |

## Operator handoff and durable recovery

| Symptom / failure | Why it happens | Better action |
| --- | --- | --- |
| Config path guessed, or a stray file placed under a secrets dir. | Rushing. | Use the exact canonical config path and reference names, never secret values. Preserve user files; don't ask again for an already-supplied key. Registered env files vs an activated projection differ — activate via supported sync/doctor-repair, not by editing generated JSON. |
| Actions repeated after a partial sync. | Assuming the whole step failed. | A partial sync can activate the needed setting before a later failure. Inspect actual state before repeating. |
| Timestamps re-owned to fix a `utime` failure. | Working around `copy2`'s write-then-utime behavior. | Group write permission does not grant permission to preserve another owner's timestamps. Do not chown/chmod/replace ownership to hide it, and never swallow a real content denial. |
| A failed poll exits zero, so `&&` continues. | Convenience scripting. | Return truthful CLI status and a safe, account-specific reason; failed polls must exit non-zero. Validate exact flags, target selector semantics, and evidence-ID uniqueness (write-once default IDs can be consumed even on a blocked run). |
| Handoff hits an old backlog or an absent file is assumed present. | Reusing a stale target. | Target the exact canary, use a real source note, and confirm the requested file is absent before execution. |
| A fetched sent copy is used as proof of inbound handling/DKIM. | Header looked present. | Header survival is not proof of inbound handling, DKIM, or the full chain. Record operator-reported versus locally corroborated observations and real timestamps without inventing revision/time/provenance. |
| Checkpoint grows unbounded or histories are re-read constantly. | Appending everything. | Keep a compact current checkpoint outside tracked code; keep append-only history separately and link evidence. Do not re-read huge histories after every call. |
| "Soak" with a fixture clock or a promise to wait. | No real elapsed time. | An actual soak needs real elapsed time, stable shipped revision/settings, and timestamped health/latency/backlog/no-loss/no-duplicate observations. Delegate real authorized waiting; otherwise report the precise missing data/authority. |
| A commit is staged without reviewing its content. | Forward momentum. | Before committing, inspect status, diff and recent history, stage only the intended source and evidence, and exclude credentials, operator runtime/config, controller state and temporary logs. |

---

## Pre-review and default-path checklist

Run this before accepting any stage or reporting completion.

- [ ] Evidence is bound to an exact source revision/tree, effective config, the
      actual path exercised, and outcome IDs.
- [ ] The PASS scope equals the reviewed scope; no inherited PASS from an
      earlier, smaller scope.
- [ ] Attempt counts were reserved before dispatch and include started errors,
      timeouts and cancels; no reset via corrective labels.
- [ ] Reviewer severities are reported intact; note/warning-only strict fail is
      accepted per current skip policy; fix-now only for concrete required
      impact.
- [ ] No required check failed, no mandatory item is omitted, and no claim is
      unsupported — regardless of the reviewer verdict.
- [ ] No invented default zero-warning gate, extra gate, or budget; no retired
      cost control reintroduced; limits changed only under current
      authorization.
- [ ] Proof level is named correctly: fixture / default entrypoint / deployed /
      live provider / ownership-cutover / soak.
- [ ] Default CLI and registry checks ran in a fresh process with the absolute
      candidate interpreter; no reliance on a prior doctor/readiness call.
- [ ] Real SDK signatures are pinned; no permissive fake is masking wire
      behavior; credentials verified with ambient env absent.
- [ ] The real data path was walked end-to-end; the delivered answer/result and
      its actions/artifacts/citations are present.
- [ ] Effects are bound to durable outcomes before side effects; applied-but-
      unverified outcomes remain uncertain; no replay without not-applied or
      idempotence proof.
- [ ] Any handoff targets the exact canary/artifact with a real source note; the
      checkpoint is compact and links append-only history.
- [ ] Any status/dry-run command used for inspection was verified read-only for
      the installed semantics, or replaced with an authorized read-only surface.
- [ ] Commits were preceded by a status/diff/history inspection that staged only
      the intended source and evidence and excluded credentials, operator
      runtime/config, controller state and temporary logs.

## Handoff and compaction template

```text
# Checkpoint: <short stage name>
Timestamp: <UTC, real>
Critical path: <what is on it, what is not>

Branch: <branch>
Revision: <tree/revision hash>
Dirty paths: <paths, or none>
Live writers: <agent(s) and worktree, or none>
Stage: <current stage>
Attempt counts: <started / errors / timeouts / cancels / reserved>

Decisions:
- <disposition + one-line reason + evidence ref>

Checks:
- <check> -> <observed result> (evidence ref)

Proof level: <fixture | default entrypoint | deployed | live provider | ownership-cutover | soak>

Source-vs-live blockers:
- <blocker> -> <missing evidence, exact next action, or needed operator step>

Next exact action: <one concrete step, and who does it>

History: <link to append-only history; do not inline it here>
```
