---
name: opsx-plan-manifest
description:
  Turn a markdown implementation plan into a runnable opsx-plan TOML manifest,
  or audit and repair an existing one. Use this whenever someone wants to make a
  phased plan executable, mentions a plan.toml or plan manifest, asks to
  "compile" or "convert" a plan doc, wants opsx-plan to drive their OpenSpec
  changes, or has a docs/*-plan.md they want the orchestrator to run. Also use
  it when a manifest is misbehaving -- changes stuck pending, a gate that never
  fires, or config keys that appear to do nothing -- since the most common cause
  is a manifest that no longer matches the repository.
license: MIT
metadata:
  author: brianmoney
  version: '1.0.0'
---

# Authoring an opsx-plan Manifest

## The core idea

A markdown plan and a TOML manifest describe different things, and conflating
them is where most bad manifests come from.

The markdown describes **intent across all time**: why the work matters, what
shipped already, what was considered and deferred, what the end state looks
like. It is a document for humans and it stays useful after the work is done.

The manifest describes **what is left to execute right now**, as a dependency
graph the orchestrator will walk unattended. It is not a table of contents for
the plan doc. A change that already shipped does not belong in it. A change that
was deferred to next quarter does not belong in the runnable set. A change
blocked on a decision nobody has made does not belong in the enabled set.

So the work is not transcription. It is reconciling the plan against the
repository as it exists today, and encoding the result as a graph. Get that
wrong and the orchestrator will happily spend an afternoon reimplementing
something that shipped last month.

`opsx-plan compile <plan.md> -o <plan.toml>` exists and does a mechanical
first pass by invoking the selected adapter (`--adapter opencode` or
`--adapter claude-code`). It reads only the markdown, so it cannot know
what already shipped or what is blocked. Hand-authoring is usually faster for
plans under roughly a dozen changes. If you do compile, treat its output as a
draft and run every step below against it.

## Workflow

### 1. Read the schema from source, not from examples

The authoritative key list is `load_plan()` in
`<controller>/lib/orchestrator/planref.py` (before the runtime split it was
`orchestrator/opsx-plan.py`). `build_schema_guidance()` — which emits the key
tables as markdown and is what `compile` feeds to the model — lives in
`<controller>/lib/orchestrator/compiler.py`.

This matters because the loader reads an explicit allowlist and **silently drops
every other key**. Example manifests and previously compiled plans in the wild
may carry keys that do nothing — `depends_on_phase` is one real instance found in
circulating manifests. Copy a key from an example and you may be writing a config
line that reads as configured-and-working in review while having no effect at all.

Locate the controller via `$KF_OPSX_CONTROLLER`, `$OPSX_CONTROLLER`, or
`~/opsx-controller`. `references/schema.md` has the current key tables and the
adapter defaults, but re-derive from source if the installed controller looks
newer than this skill.

### 2. Read the plan document

Extract the change ids, the phase grouping, and the stated dependencies. Note
anything the doc marks as done, deferred, blocked, or optional — that is the
raw material for step 4.

### 3. Reconcile against the repository

For each change id the plan mentions, establish which of these is true:

- `openspec/changes/<id>/` exists → active, drivable now
- `openspec/changes/archive/<date>-<id>/` exists → already shipped
- neither → does not exist yet, needs a `create_invoke` authoring stage

Do not trust the plan doc on this. Plans go stale, and work sometimes ships
outside the plan entirely — including changes implemented directly without ever
becoming an OpenSpec change, which leave no archive entry at all. Check the
filesystem, and check git history when a plan claims something is done but no
archive entry exists.

`scripts/audit_manifest.py` automates this check once a draft exists.

### 4. Decide each change's disposition

| Situation | Disposition | Why |
|---|---|---|
| Already shipped or archived | **Omit entirely** | Nothing to drive. A disabled entry for finished work is noise that implies the graph is waiting on it. |
| Deferred, nothing depends on it | **Omit**, note why in a comment | Keeps the graph honest about what is actually pending. |
| Deferred, but later work depends on it | `enabled = false` | The edge has to stay visible or the dependency graph lies. |
| Blocked on a human decision | `enabled = false` + comment naming the unblock condition | Whoever enables it later needs to know what has to be true first. |
| Ready to run | enabled (the default) | |

Put the reasoning in TOML comments. Six months from now the manifest is the
only artifact anyone reads, and "why is phase 3 disabled" is the first question
they will have. The comment costs one line and saves an archaeology session.

### 5. Fill in the `[plan]` table

Most keys have sane defaults. The ones that need real thought:

**`create_invoke`** — the authoring command, with `{change}` and `{plan_doc}`
substituted. The slash-command form differs between repos: `/opsx:ff` and
`/opsx-ff` are both in use depending on how the repo registers its skills. Check
the repo's actual skill or command registration rather than copying from an
example, because a wrong command form fails at the first create attempt.

**`fast_checks`** — the post-archive gate, run after every completed change.
Find the repo's own canonical quality gate rather than assembling one; most
repos have a single command that already bundles lint, tests, and validation
(check `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, or the CI workflow). Listing
its constituent parts separately just runs them twice.

Use a repo-relative interpreter or entrypoint path (`.venv/bin/<tool>`) rather
than a bare command name. The orchestrator runs these as subprocesses, and a
bare name silently depends on whatever the host `PATH` resolves — which may be a
different version, or nothing at all.

**`review_created`** — keep this `true` unless there is a specific reason not
to. It stops the loop after a change is authored and waits for
`opsx-plan accept`, so proposals and spec deltas get human eyes before any code
is written. Turning it off means the first thing anyone reviews is a finished
implementation of a possibly-wrong proposal.

**`timeout_minutes`** — must fit an implement→review round *plus* the fast
checks. Time the repo's gate before picking a number. The controller's chunk
timeout is what catches genuine hangs, so this does not need to be tight.

### 6. Place gates deliberately

`pause_before = true` stops the loop until someone runs `opsx-plan approve`. It
is the mechanism for "a human should look before this proceeds," and it is worth
being sparing: gate everything and the plan is just a manual process with extra
steps; gate nothing and the orchestrator makes irreversible-ish decisions alone.

Gate on genuine judgment, not on difficulty:

- the first change that establishes a new capability, storage contract, or
  public interface that later changes will build on
- a change that alters trust, privacy, or security posture — anything that
  first allows data to leave the machine, relaxes a permission, or adds a
  third-party dependency on a data path
- a change whose prerequisite is a decision outside the plan's control

Do not gate merely because a change is large or touches many files. That is what
review is for.

### 7. Verify

Three checks, covering different things. `$OPSX` below is whichever env var the
repo uses for the controller checkout — `KF_OPSX_CONTROLLER` in knowledge-forge,
`OPSX_CONTROLLER` elsewhere, or just the path.

```bash
# Graph: parses, ids unique, deps known, no cycles, correct ready/pending state
python3 $OPSX/orchestrator/opsx-plan.py status <manifest>

# Silent-drop keys and change-ids that don't match the repo
python3 <skill>/scripts/audit_manifest.py <manifest> --repo <repo-root>

# Full gate config and DAG as the orchestrator will actually walk it
python3 $OPSX/orchestrator/opsx-plan.py run --dry-run <manifest>
```

Read the `status` output rather than just checking it exits clean. Every change
should be in the state you intended: exactly the changes you expect showing
`ready`, disabled ones showing `skipped`, and dependent ones `pending`. A change
sitting `ready` that you meant to gate is a real bug and `status` will show it
plainly.

Before an actual unattended run, `opsx-plan.py doctor` checks the environment
(models configured, client reachable, tree clean) — worth running once, since
those failures otherwise surface several minutes into a run.

## Failure modes worth knowing

**Transcribing the plan verbatim.** The most common and most expensive mistake.
Produces a manifest that re-drives finished work. Step 3 exists to catch it.

**Trusting example manifests.** The canonical sample at
`orchestrator/samples/sample-plan.toml` is test-verified against the current
loader and exercises every surface key — derive from `load_plan()` for
anything not covered.

**Bare command names in `fast_checks`.** Works when you test it interactively,
fails or silently runs the wrong binary under the orchestrator.

**Gating everything.** A manifest where most changes have `pause_before = true`
is not an automated plan. Decide which gates carry real judgment and drop the
rest.

**Dependencies stated by phase.** `phase` is a display grouping only. Ordering
comes from `depends_on` and nothing else. Two changes in phase 2 run in
whatever order the topological sort produces unless an explicit edge says
otherwise.

**Leaving the manifest behind when the plan changes.** If the plan doc gets
revised — work reordered, a change split in two — the manifest needs the same
edit. Re-run the audit script after any plan revision.

## Reference files

- `references/schema.md` — `[plan]`, `[[changes]]`, and `[plan.git_delivery]`
  key tables, adapter defaults, dependency semantics, and how to re-derive them
  from source when the controller changes.
- `references/worked-example.md` — a real plan reduced from five changes to a
  two-change runnable graph, showing each disposition decision and the resulting
  TOML.
