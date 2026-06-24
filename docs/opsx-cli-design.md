# opsx as a CLI — design

Status: proposal (no build yet)
Author: design discussion, 2026-06
Supersedes: the current "controller agent + per-adapter command/agent prose" model

## 1. Why

Today the OpenSpec controller workflow is encoded as **deterministic logic
written in LLM prose**, duplicated across layers and adapters. That is the root
cause of every failure class we have hit operating it:

| Failure we hit | Root cause (all the same shape) |
| --- | --- |
| drive looked "done" but wasn't / false negatives | completion judged from controller-state JSON + commit convention, which drift |
| drive ran on the wrong model and bailed | model routing lives in command-vs-agent prose; `opencode run` didn't apply it |
| reviewer reconciles `opsx-review.md` + `opsx-verify.md` | verification spread across overlapping prose docs |
| implementer "is parent to" apply | every phase is an *agent* (actor) **and** a *command* (prose) with the same job under two names |
| `.codex` vs `.opencode` vs `.opsx-controller` path confusion | the layout contract is restated in prose per adapter and diverges |
| interrupted drive left partial state that blocked re-runs | round/state-machine logic is in the LLM, not in code that can clean up |

The same workflow exists **three times** (`adapters/opencode`,
`adapters/claude-code`, `adapters/codex-cli`), each re-expressing the same state
machine in that client's prose dialect. And within one adapter it exists
**twice**: the base OpenSpec step-commands (`opsx-apply`, `opsx-verify`,
`opsx-review`, `opsx-archive`) and the controller agents that wrap them
(`opsx-implementer`, `opsx-reviewer`, `opsx-archiver`).

`orchestrator/opsx-plan.py` already proved the antidote at the plan level: a
deterministic Python layer that does ordering/dispatch/verification and calls
the LLM only for irreducible judgment. This document extends that pattern
**down into the controller** so the round-level state machine is code too.

## 2. Principle

> Deterministic work is code. The LLM is invoked only where judgment is
> irreducible — and there are exactly two such points: **apply** (write the
> implementation) and **verify** (judge whether it matches the spec).

> **opsx orchestrates OpenSpec commands; it never reimplements them.** This is
> the entire point. The deterministic facts come from the `openspec` CLI
> (`status`, `instructions apply`, `validate --strict`, `archive`), and the two
> judgment steps are delegated to OpenSpec's own `/opsx-apply` and `/opsx-verify`
> commands. opsx adds the loop, the gate, and the state around those commands —
> not a parallel copy of them.

Everything else — resolving the change, sequencing phases, counting rounds,
detecting progress, committing, selecting models, reading/writing state — is
`opsx` CLI logic with **one** source of truth. The judgment itself stays in the
OpenSpec commands, so improving `/opsx-verify` improves every workflow that uses
it, with nothing to keep in sync.

## 3. Command surface

```
opsx new <change>          scaffold a change                    (LLM: optional)
opsx ff <change|desc>      author all artifacts in one pass     (LLM: write)
opsx apply <change>        implement one round                  (LLM: write code)
opsx verify <change>       atomic PASS/FAIL verdict             (LLM: judge)
opsx archive <change>      openspec archive + commit            (deterministic)
opsx drive <change>        the round loop                       (deterministic)
opsx status [<change>]     report from OpenSpec ground truth    (deterministic)
```

`opsx drive` is the controller: it runs `apply` and `verify` in a loop and calls
`archive` when verify passes. It is the only command `opsx-plan` needs to invoke
per change (replacing today's `opencode run "/opsx-drive …"`).

## 4. Deterministic core (no LLM)

The CLI owns, in code, exactly what prose keeps getting wrong:

- **Layout & ground truth.** The single definition of where changes, the
  archive, and state live. Completion is OpenSpec's archive move and nothing
  else (already adopted in `opsx-plan.py:verify_change_done`).
- **The drive state machine.** Phase order (apply → verify → archive), round
  budget, and the no-progress stop, identical to the patient loop now in
  `opsx-plan.py` but at the per-change/round grain.
- **Progress detection.** `tasks.md` checkbox counts + worktree fingerprint
  (already in `opsx-plan.py:progress_fingerprint`); no parsing of the drive's
  private state.
- **Archive.** `openspec archive` + a conventional commit — no model needed.
- **Model selection.** A config table maps each LLM verb to a model
  (`apply → deepseek/deepseek-v4-pro`, `verify → github-copilot/gpt-5.4`), in
  one place, not scattered across command and agent frontmatter.
- **State.** One JSON state file per change, written by the CLI, used for
  resumption. The LLM never owns state.

## 5. The two judgment steps — delegated to OpenSpec commands

`apply` and `verify` are **not new prompts**. They are the existing OpenSpec
commands `/opsx-apply` and `/opsx-verify`. The CLI's job at each step is:

1. run the relevant `openspec` CLI calls for deterministic facts, then
2. invoke the core OpenSpec command headlessly for the judgment, and
3. parse a single structured (JSON) line back.

This is exactly the pattern the current phase agents already follow — the
implementer runs `openspec status`/`instructions apply` then executes
`/opsx-apply`; the reviewer runs those plus `openspec validate --strict` then
executes `/opsx-verify`. The CLI replaces the *agent prose that wraps* each
command, not the command itself.

### apply  → delegates to `/opsx-apply`

```
input  (CLI -> /opsx-apply): { change, round, fix_prompt?, context_summary? }
output (-> CLI):  one JSON line
  {"status":"implemented","change":"<id>","round":<n>,
   "tasks_completed":<int>,"tasks_total":<int>,
   "files_touched":["..."],"summary":"one sentence","blocked":false}
```

### verify  → delegates to `/opsx-verify` (the "atomic verify, nothing else")

```
input  (CLI -> /opsx-verify): { change, round }
output (-> CLI):  one JSON line  (today's opsx-reviewer contract)
  {"status":"reviewed","change":"<id>","round":<n>,
   "verdict":"pass|fail",
   "finding_counts":{"critical":0,"warning":0,"note":0},
   "summary":"one sentence","fix_prompt":"empty when pass",
   "next_phase":"archive|implement"}
```

`/opsx-verify` is the single atomic verifier — a strict zero-finding gate that
returns a verdict plus, on failure, the fix prompt the next `apply` round
consumes. The redundant wrappers around it go away: `opsx-verify-auto` (a
re-parsing shim) is unnecessary once the CLI reads the verdict directly, and the
reviewer no longer needs `/opsx-review` because the archive-vs-fix **decision**
is `opsx drive`'s, not a separate prose command. `/opsx-verify` and `/opsx-apply`
themselves remain — they are core OpenSpec commands and are used, not replaced.

## 6. What collapses

Only the **controller/agent prose** collapses. The core OpenSpec commands
(`/opsx-apply`, `/opsx-verify`, `/opsx-archive`) stay — the CLI invokes them.

| Today (agent prose that wraps a command) | Under opsx CLI | Core OpenSpec command it still calls |
| --- | --- | --- |
| `opsx-controller.md` reimplements a state machine in prose | `opsx drive` (code) | sequences the ones below |
| `opsx-implementer` agent wrapping `/opsx-apply` | `opsx apply` (invokes the command) | **`/opsx-apply`** (kept) |
| `opsx-reviewer` agent wrapping `/opsx-review` + `/opsx-verify` (+ `opsx-verify-auto` shim) | `opsx verify` (invokes the command) | **`/opsx-verify`** (kept; `/opsx-review` no longer needed; `opsx-verify-auto` shim retired) |
| `opsx-archiver` agent wrapping `/opsx-archive` | `opsx archive` (mostly deterministic: `openspec archive` + commit) | **`/opsx-archive`** / `openspec archive` |
| same agent prose × 3 adapters | one CLI; adapters shrink to "how to invoke a model headlessly" | unchanged |

`verify-prompt.md` is a user-owned shortcut (hand `/opsx-verify` findings to an
implementing agent), not part of the controller flow — it stays as-is.

## 7. Migration (incremental, no big bang)

1. **Done already:** `opsx-plan` verifies from OpenSpec ground truth and drives a
   patient loop; the verify layer is consolidated onto the single atomic
   `opsx-reviewer` contract (this change).
2. **Extract `opsx archive`** first — it is fully deterministic and the
   lowest-risk slice; have the controller call it instead of the archiver agent.
3. **Extract `opsx verify`** — wrap the existing `opsx-reviewer` contract as a
   CLI subcommand that shells the model and parses the JSON line.
4. **Extract `opsx apply`** similarly.
5. **Extract `opsx drive`** — port the round state machine from
   `opsx-controller.md` into code that calls `apply`/`verify`/`archive`.
6. **Retire prose** per adapter as each verb moves into the CLI; an adapter ends
   up as just a model-invocation shim + auth.

Each step is independently shippable and leaves the system working.

## 8. Open questions

- **Adapter boundary:** the CLI still needs a per-client way to run a model
  headlessly and capture one JSON line. Define the smallest such interface
  (`opsx-model run --model <m> --prompt <p> -> stdout`).
- **`new`/`ff` authoring:** keep as an LLM verb in the CLI, or leave to the
  client's native command for now? (Lower priority; not on the drive hot path.)
- **Where the CLI lives:** extend `orchestrator/` (alongside `opsx-plan.py`) or
  a new top-level `opsx/` package.
- **Model config format:** a single `opsx.toml`/section mapping verb -> model,
  read by both the CLI and `opsx-plan`.
