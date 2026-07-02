# opsx-plan: plan-level orchestrator

`orchestrator/opsx-plan.py` iterates a TOML plan manifest of OpenSpec changes
(a dependency DAG) and, for each ready change, runs a staged lifecycle:

1. **create** — if `openspec/changes/<id>` does not exist, invoke your change
   authoring command (e.g. `/opsx-ff ... create a change for {change}`) and
   verify the result independently
2. **implement / review / archive** — for OpenCode-backed runs, dispatch the
   fixed `opsx-implementer`, `opsx-reviewer`, and `opsx-archiver` workers as
   separate one-shot subprocesses, with `opsx-plan` owning phase state,
   retries, recovery, and verification
3. **legacy drive** — non-OpenCode adapters may still invoke a single
   controller command and verify completion from ground truth instead of
   trusting that run's output

The orchestrator is deliberately a deterministic script, not an agent. All
LLM judgment stays inside `/opsx-ff` and the configured implement/review/archive
workers. This layer only does ordering, dispatch, verification, retry policy,
and durable bookkeeping.

## Requirements

- Python 3.11+ (stdlib only; uses `tomllib`)
- A host project that already uses OpenSpec, with an opsx-controller adapter
  installed and verified to work for single changes
- Each change either already exists as an accepted OpenSpec change, or a
  `create_invoke` command is configured so the orchestrator can author it
- A headless invocation that works for your client (see Adapter invocation)

## The compile stage

The OpenCode adapter ships an `/opsx-plan` command
(`adapters/opencode/commands/opsx-plan.md`) that instructs your frontier
model to author plan docs in this convention and self-check them against
the compiler, so generated docs compile cleanly on the first try.

`compile` deterministically derives `plan.toml` from a phased plan doc that
follows the authoring convention: `## Phase N:` headings, `### Change:
\`slug\`` headings, a `**Depends on:**` paragraph, and `**Capability:**` /
`**Capabilities:**` lines. Resolution rules:

- backticked known change ids in `Depends on:` become edges; `Phase N`
  references expand to that phase's changes (preceding-only when
  self-referential, e.g. "all preceding Phase 3 changes")
- text starting with "None" or containing independence wording
  ("independent", "in parallel", "may proceed") produces no edges even when
  other changes are mentioned, with the mention noted in a comment
- "deferred" wording sets `enabled = false`
- the first change of each capability marked `(proposed` gets
  `pause_before = true`
- anything unresolvable is emitted with `depends_on = []` and a loud
  `# REVIEW:` comment — the compiler never guesses

The generated manifest is round-trip validated (unique ids, known deps, no
cycles) and a review summary is printed. Two things the compiler cannot do,
by design: detect a dependency the doc forgot to state, and place judgment
gates such as phase exit reviews — add those `pause_before = true` entries
yourself. Always review the DAG (`run --dry-run`) before an unattended run.

If you author plan docs with a frontier model, telling it to follow this
convention (backticked slugs in `Depends on:`, explicit `(proposed` capability
markers) makes its output directly compilable.

## The create stage

Creation is just-in-time: a change is authored only when its dependencies are
done, because later changes write spec deltas against spec state produced by
earlier ones. `{change}` and `{plan_doc}` are substituted into
`create_invoke`; per-change `create_invoke` overrides are supported for
changes that need a custom prompt.

A change counts as created only when independent evidence agrees:
`openspec/changes/<id>` contains `proposal.md` and `tasks.md`, the
`created_check` command (default `openspec validate <id> --strict`) exits 0,
and creation modified no tracked files. Creation gets its own attempt budget
(`create_max_attempts`).

With `review_created = true` (the default), orchestrator-created changes stop
at `awaiting_acceptance` so you can review the proposal and spec deltas, then
continue with:

```bash
python3 .../opsx-plan.py accept plan.toml <change-id>
```

Changes you created by hand are presumed reviewed and skip this gate. Use
`run --create-only` to batch create+verify the currently actionable frontier
without dispatching implementation stages. Set `review_created = false` for
fully unattended create→implement (recommended only after `/opsx-ff` output has earned that trust
on a few supervised runs).

## Usage


From the host project root:

```bash
# 0. generate plan.toml from your phased plan doc, then REVIEW the DAG
python3 /path/to/opsx-controller/orchestrator/opsx-plan.py compile \
  docs/phased-implementation-plan.md -o plan.toml

# preview order, gates, and current status without running anything
python3 /path/to/opsx-controller/orchestrator/opsx-plan.py run plan.toml --dry-run

# run the plan (serial; Ctrl-C is safe — state persists, resume by re-running)
python3 /path/to/opsx-controller/orchestrator/opsx-plan.py run plan.toml

# approve a pause_before gate, then re-run
python3 .../opsx-plan.py approve plan.toml add-atomic-runtime-state-writes

# inspect / recover
python3 .../opsx-plan.py status plan.toml
python3 .../opsx-plan.py reset plan.toml <change-id>
```

Useful run flags: `--max-changes N`, `--budget-minutes N`,
`--only <id> [<id>...]`, `--repo <path>`.

Orchestrator state lives at `.opsx-plan/<plan-name>.state.json` in the host
project. For OpenCode-backed direct runs, that file is the authoritative
durable source for each change's phase, round, latest fix prompt, review
result, archive result, tracked change files, and last-stage log metadata.
Per-stage logs live at `.opsx-plan/logs/<change>.<stage>.r<round>.*.log`, and
compatibility worker-state snapshots used as phase inputs live under
`.opsx-plan/workers/`. Add `.opsx-plan/` to the host project's `.gitignore`.

## Plan manifest

See `plan.example.toml`. Per-change fields: `id` (required), `depends_on`,
`phase` (informational), `pause_before` (requires explicit `approve` before
dispatch — use for human gates like new-capability approvals or phase exit
reviews), `enabled` (set `false` for deferred changes), and per-change
`timeout_minutes` / `max_attempts` overrides.

Review the `depends_on` graph by hand before an unattended run. The
orchestrator validates ids and rejects cycles, but it cannot detect a
*missing* edge.

## How completion is verified

A change is marked `done` only when all of the following agree:

1. a fresh archive worker run returned machine-readable success for the current
   change
2. `openspec/changes/<id>` is absent from the worktree
3. a dated `openspec/changes/archive/YYYY-MM-DD-<id>` directory exists
4. an `archive(<id>):` commit is reachable from `HEAD` and matches the stored
   archive result
5. all `fast_checks` commands exit 0 (e.g. `openspec validate --all`,
   your fast test suite)

The worker process exit code is never treated as success. For OpenCode-backed
plan runs, repository archive evidence without matching plan-owned archive
worker evidence is reconciled as failed, not as completed.

On startup the orchestrator reconciles recorded state against the repo:
recorded-done changes whose evidence has disappeared are downgraded to failed;
a stale `running` status from a killed run is recovered to pending; and a
direct OpenCode change resumes from the persisted plan-owned phase instead of a
nested controller state file.

## Retry and failure policy

- For OpenCode-backed runs, `opsx-plan` owns the implement-review-archive loop
  directly. It persists the active phase, round, latest fix prompt,
  no-progress streak, review verdict, archive result, and tracked log path in
  the plan state file and resumes from that state on the next run.
- Review failures loop back to implement inside `opsx-plan`; the script
  advances rounds itself and stops when `max_rounds` or `no_progress_limit` is
  reached.
- Manual `/opsx-drive <change-id>` remains available for single-change control,
  but `opsx-plan` no longer nests `/opsx-drive` for OpenCode execution.
- A change that archives successfully but then fails `fast_checks` is marked
  failed without retry (re-archiving an already archived change cannot fix the
  repo).
- A failed change blocks its dependents; independent branches keep running.
- `require_clean_tracked` (default true) refuses to start a new change while
  tracked files are dirty, so failures cannot bleed across changes.
  Untracked leftovers are allowed, matching the archiver contract.

## Adapter invocation

Defaults (override with `invoke` / `state_file` / `implement_invoke` /
`review_invoke` / `archive_invoke` in `[plan]`):

| adapter | invocation | state file |
|---|---|---|
| `opencode` | `opencode run --agent opsx-implementer`, `opencode run --agent opsx-reviewer`, `opencode run --agent opsx-archiver` for plan runs; `opencode run "/opsx-drive {change}"` remains the manual single-change controller entrypoint | `.opencode/opsx-controller/{change}.json` for manual `/opsx-drive`; `.opsx-plan/<plan>.state.json` is authoritative for direct plan execution |
| `claude-code` | `claude -p "/opsx-drive {change}"` | `.claude/opsx-controller/{change}.json` |
| `codex-cli` | `codex exec "$opsx-drive {change}"` | `.opsx-controller/{change}.json` |

Verify your client's headless syntax and permission configuration before an
unattended run — for example, Claude Code's `-p` mode needs tool permissions
pre-granted (settings or `--permission-mode`), and Codex needs
`agents.max_depth >= 1`. Test with `--max-changes 1` first.

## Execution model

Serial by design: two mutating plan-stage runs in one worktree is a known failure
mode, and the archive commit per change keeps each step independently
revertable. If you later want parallel independent branches, run them in
separate `git worktree` checkouts with a merge step gated on `fast_checks` —
that belongs above this script, not inside it.
