---
title: Plan-Authoring Guidance and Controller Surface Trim
doc_type: implementation-plan
status: proposed
updated: 2026-08-01
---

# Plan-Authoring Guidance and Controller Surface Trim

## Purpose

Two problems share one root cause: opsx-controller grew a parallel command
surface that duplicates what upstream OpenSpec 1.7.0 now ships natively, and
the plan-authoring knowledge that makes plans compile and scope well is
trapped inside invocation-scoped command bodies that plain-chat models never
see.

Frontier models asked in plain chat to plan multi-stage work in an installed
repo produce generic plans: no compile convention (backticked `Depends on:`
slugs, `(proposed` capability markers, `Phase N` semantics), no loop-aware
scoping, and frequent confusion between upstream `/opsx:*` commands
(`propose`, `apply`, `archive`) and opsx-controller's own `/opsx-*` commands
(`opsx-plan`, `opsx-drive`). Meanwhile `/opsx-author`, `/opsx-review`,
`/opsx-verify-auto`, `/opsx-archive-no-prompt`, and the nested-controller
`/opsx-drive` path duplicate or predate the direct-dispatch execution model
and upstream's per-change commands.

This plan trims opsx-controller to its unique value — plan-level
orchestration (compile, run, report), the strict-gate worker agents, and
`opsx-run` — defers per-change operations to upstream OpenSpec, and installs
one client-neutral plan-authoring reference where plain-chat models actually
look: the project guidance files and installed support directories.

## Capability Ownership

`plan-authoring-guidance` is a proposed capability for the client-neutral
plan-authoring convention and its distribution: the reference document, its
deployment into adapter support directories, the guidance pointers in project
snippets, and the per-adapter plan-authoring surfaces that defer to it. It is
separate from `claude-code-plan-authoring` because it defines the shared
convention every client points at, not one client's invocation contract. It
is separate from `adapter-aware-plan-compilation` because it governs how
humans and models write the source markdown, not how the compiler converts
it.

## Phase 1: Retire Legacy Surfaces

### Change: `remove-legacy-command-surfaces`

**Purpose:** Delete the deprecated, duplicated, and superseded command and
skill surfaces so only the supported entrypoints remain.

**Depends on:** None. This may be developed independently because it only
removes files and installer lines.

**Capabilities:** `plan-driven-opencode-execution`,
`plan-driven-claude-code-execution`, `codex-cli-adapter`.

**Scope:** Delete `adapters/opencode/commands/opsx-author.md` (drifted
duplicate of `opsx-plan.md`), `opsx-archive-no-prompt.md` (already a disabled
stub), `opsx-verify-auto.md` (legacy shell-loop helper),
`opsx-review.md` (manual surface; the reviewer agent remains), and
`opsx-drive.md` plus `adapters/opencode/agents/opsx-controller.md` (the
nested-controller path superseded by direct dispatch and `opsx-run`). Delete
`adapters/claude-code/skills/opsx-drive/`,
`plugins/opsx-controller/skills/opsx-drive/`, and
`adapters/codex-cli/skills/opsx-drive/` including its install and plugin
packaging paths in `adapters/codex-cli/install.sh`. Add a stale-surface
cleanup step to all three installers (`rm -f` the deleted files from global
and project install roots) so re-installing trims previously deployed copies.
Delete the opsx-drive contract tests in `tests/orchestrator/test_opsx_plan.py`
that pin the deleted surfaces.

**Out of scope:** Removing the legacy drive mode from the orchestrator itself
(the next change does that), deleting the worker agents
(`opsx-implementer`, `opsx-reviewer`, `opsx-archiver`), deleting the vendored
upstream `.claude/skills/openspec-*` skills, or any documentation rewrite.

**Success parameters:** The deleted paths are absent from the repo;
`grep -rn "opsx-drive\|opsx-author\|opsx-verify-auto\|opsx-archive-no-prompt" adapters/ plugins/`
returns no live references; each installer runs clean against a temporary
project directory; `python3 -m unittest discover -t . -s tests` and
`node tests/opencode/test-opsx-usage-emitter.js` pass.

### Change: `remove-legacy-drive-mode`

**Purpose:** Remove the orchestrator's legacy single-command drive path so
direct dispatch is the only execution model and misconfigured manifests fail
closed with guidance.

**Depends on:** `remove-legacy-command-surfaces`, because both update
`tests/orchestrator/test_opsx_plan.py` and this change removes fixtures for
the deleted surfaces.

**Capabilities:** `plan-driven-opencode-execution`,
`plan-driven-claude-code-execution`, `adapter-aware-plan-compilation`,
`codex-cli-adapter`.

**Scope:** In `orchestrator/opsx-plan.py`, remove `drive_change()`, the
`drive` stage branch in the run loop, and the `invoke` and `max_attempts`
config keys; when any of `implement_invoke`, `review_invoke`, or
`archive_invoke` is missing, fail closed with an error naming all three keys
instead of falling back to legacy drive mode. In `lib/orchestrator/base.py`,
drop `invoke` from every `ADAPTER_DEFAULTS` entry (this retires the codex-cli
`$opsx-drive` default, so codex manifests fail closed with the new guidance).
Update `lib/orchestrator/compiler.py` schema guidance and
`orchestrator/samples/sample-plan.toml` to match the reduced key set. Update
the `invoke`/`max_attempts` fixtures in `tests/orchestrator/test_opsx_plan.py`,
`tests/orchestrator/test_logs.py`, and `tests/orchestrator/test_telemetry.py`,
remove legacy-drive-mode behavior tests, and add a test for the fail-closed
non-direct-mode error.

**Out of scope:** Building codex-cli direct dispatch (codex plan-run support
is dropped, not replaced), changing the strict review gate, changing
`opsx-run` behavior, or touching the worker agents.

**Success parameters:**
`python3 orchestrator/opsx-plan.py status orchestrator/samples/sample-plan.toml`
loads cleanly; a manifest missing any direct stage invoke fails with an error
naming all three required keys; both test suites pass;
`grep -n "drive_change\|ADAPTER_DEFAULTS.*invoke" orchestrator/opsx-plan.py lib/orchestrator/base.py`
returns no live code.

## Phase 2: Plan-Authoring Guidance

### Change: `add-plan-authoring-reference`

**Purpose:** Create the single client-neutral source of truth for authoring
compilable, well-scoped plan documents, and deploy it where plain-chat
models can find it.

**Depends on:** `remove-legacy-command-surfaces`, because both edit the same
adapter install.sh files and this builds on the cleanup step.

**Capabilities:** `plan-authoring-guidance` (proposed; see Capability
Ownership), `shared-orchestrator-installation`.

**Scope:** Author `core/plan-authoring.md` containing: the before-writing
checklist (`AGENTS.md`, source material, `openspec/config.yaml` schema,
`openspec/specs/`, active and archived change ids); the document structure
contract (frontmatter, `## Phase N:` headings, `### Change:` slug headings,
field order, closing sections including `## Suggested Manual Gates`); the
eleven-rule machine-read compile convention with the four dependency forms
(moved from the command bodies, which later slim to point here); an explicit
namespace disambiguation between upstream `/opsx:*` per-change commands and
opsx-controller `/opsx-*` plan-level commands, with the rule that authoring
command forms are checked against the repo's actual registration (upstream
1.7.0 generates tool-correct spellings per client — `/opsx-propose` for
OpenCode, `/opsx:propose` for Claude Code, `$openspec-propose` for Codex —
and this repo runs the stock core profile);
current-OpenSpec authoring facts including the `skip_specs: true` declaration
for docs-only and refactor changes and nested capability folders under
`openspec/specs/`; loop-aware
scoping heuristics (one concern per change sized for a single
implement-review loop, executable success parameters, scope discipline for
the create stage, real-only dependency edges, gates on judgment not
difficulty, runnable-horizon rule, security and data-integrity sequencing);
self-verification including the compile self-check; and a pointer to the
`opsx-plan-manifest` skill for the TOML half. Extend all three installers to
deploy the reference into their support directories
(`~/.config/opencode/opsx-controller/`, `~/.claude/opsx-controller/`,
`~/.codex/opsx-controller/`, and the project-level equivalents), with updated
install output and verify messages. Add installer tests asserting the
reference deploys for all three adapters, global and project.

**Out of scope:** Rewriting the snippets, command bodies, or skills that
point at the reference (the next change does that), a new `skills/` npx
package, and any orchestrator behavior change.

**Success parameters:** `core/plan-authoring.md` exists and covers every
section listed above; a fresh install into a temporary project deploys the
reference for each adapter; the new installer tests pass alongside both
existing suites.

### Change: `rewrite-plan-guidance-surfaces`

**Purpose:** Point every plan-adjacent surface at the shared reference and
state the division of labor between upstream OpenSpec and opsx-controller in
the guidance models actually read.

**Depends on:** `add-plan-authoring-reference`, because the snippets,
commands, and skills all point at the reference it installs.

**Capabilities:** `plan-authoring-guidance`, `claude-code-plan-authoring`,
`codex-cli-adapter`.

**Scope:** Rewrite `adapters/opencode/templates/project/AGENTS.snippet.md`,
`adapters/claude-code/templates/project/CLAUDE.snippet.md`, and
`adapters/codex-cli/templates/project/AGENTS.snippet.md` around an explicit
division of labor: author plans by reading the installed `plan-authoring.md`
(or invoking `/opsx-plan`); compile and run with the `opsx-plan` CLI; use
upstream OpenSpec commands and skills for per-change propose, apply, archive,
and verify; use `opsx-run` for a manual single-change loop (OpenCode and
Claude Code only). The codex snippet additionally states plan-run is
unsupported on that adapter. Slim `adapters/opencode/commands/opsx-plan.md`
and `adapters/claude-code/agents/opsx-plan-author.md` to their invocation
contracts, deferring the convention to the installed reference (project path,
else global path). Add `adapters/codex-cli/skills/opsx-plan/SKILL.md`
following the codex skill pattern (reads the reference project-first, else
global; reports honestly when no compile adapter is configured), deploy it in
`adapters/codex-cli/install.sh` and the plugin packaging path, and add the
`opsx-plan` skill to `plugins/opsx-controller/` for Claude plugin parity.

**Out of scope:** Changing the compile convention itself, changing
`/opsx-plan` argument handling, and any documentation outside the three
snippets and the named adapter files.

**Success parameters:** All three snippets contain the plan-authoring
pointer and the division of labor; the opencode command and claude agent
contain no duplicated convention text (the eleven rules live only in
`core/plan-authoring.md`); the codex skill installs and follows the same
reference-first pattern as the deleted drive skill did; both test suites
pass.

## Phase 3: Documentation

### Change: `update-docs-for-trimmed-controller`

**Purpose:** Make every doc describe the trimmed execution model and the new
guidance surfaces, so no document teaches a deleted workflow.

**Depends on:** `remove-legacy-drive-mode` and
`rewrite-plan-guidance-surfaces`, because the documentation must describe the
final execution model and guidance surfaces.

**Capabilities:** `plan-operator-cli`, `plan-authoring-guidance`.

**Scope:** Update `README.md` (remove drive-era references, add a
Documentation-table row for the plan-authoring reference),
`orchestrator/README.md` (remove legacy-drive and adapter-invocation sections
that describe `invoke`, state the fail-closed direct-dispatch requirement,
state codex plan-run is unsupported, point plan-doc authoring at
`core/plan-authoring.md`), `docs/adapters.md` (deployed-file lists and codex
capability statement), `docs/opsx-plan-operator-workflow.md` (schema tables
without `invoke`/`max_attempts`), `docs/claude-code-parity.md`,
`core/README.md` and the core contract docs where they reference the legacy
drive path, `core/model-efficiency-workflow.md`,
`skills/opsx-plan-manifest/references/schema.md` (drop the removed keys),
`plugins/opsx-controller/README.md`, the `skills/opsx-controller` npx package
(rewrite around plan-level orchestration plus upstream deferral), and the
repo-root `AGENTS.md` (project description and any surface mentions).

**Out of scope:** Rewriting archived plan documents, changelog curation, and
any behavior change.

**Success parameters:**
`grep -rn "opsx-drive\|opsx-author\|opsx-verify-auto\|opsx-archive-no-prompt" README.md docs/ core/ skills/ plugins/ AGENTS.md`
returns no live workflow references (archived plans and historical notes
excepted); every remaining doc that mentions plan authoring points at
`core/plan-authoring.md`; `openspec validate --all` passes; both test suites
pass.

## Recommended Sequence

1. Implement `remove-legacy-command-surfaces` first; it clears the deleted
   surfaces and their contract tests so later changes edit shared files
   without conflict.
2. Implement `remove-legacy-drive-mode` and `add-plan-authoring-reference`
   next; they are independent of each other and may proceed in parallel.
3. Implement `rewrite-plan-guidance-surfaces` once the reference exists to
   point at.
4. Finish with `update-docs-for-trimmed-controller` when the execution model
   and surfaces are final.

## Overall Completion Criteria

The series is complete when opsx-controller's installed surface is exactly:
the `opsx-plan`/`opsx-run` CLI, the three worker agents per adapter, one
plan-authoring command or skill per adapter deferring to
`core/plan-authoring.md`, and guidance snippets that route per-change work to
upstream OpenSpec. A frontier model in plain chat in an installed repo can
find the plan-authoring convention from the project guidance file alone, the
orchestrator has a single execution model that fails closed on
misconfiguration, no document teaches a deleted workflow, and both test
suites plus `openspec validate --all` are green.

## Explicit Non-Goals

This plan does not build codex-cli direct dispatch, rework the
implementer/reviewer/archiver worker agent internals, change the strict
review gate semantics, remove or modify the vendored upstream
`.claude/skills/openspec-*` skills, create a new `skills/` npx package for
plan authoring, migrate or rewrite archived plans and manifests, or change
`opsx-run` behavior.

## Suggested Manual Gates

The compiler will automatically add `pause_before = true` to
`add-plan-authoring-reference` because it introduces the proposed
`plan-authoring-guidance` capability; keep that gate so the reference
document gets human review before downstream surfaces point at it. Consider
an additional manual gate on `remove-legacy-command-surfaces` since it is the
first user-facing deletion, though the changes it makes are git-recoverable
and were pre-approved during planning.
