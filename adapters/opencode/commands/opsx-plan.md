---
description: Author a phased OpenSpec implementation plan document that `opsx-plan compile` can convert into a runnable plan manifest
agent: build
---

Author exactly one phased implementation plan markdown document for this
repository. The document is both a human planning artifact and machine input:
`opsx-plan compile` converts it into a `plan.toml` manifest via model-assisted compilation with local validation
that drives automated change creation (`/opsx-ff`) and implementation. On
OpenCode-backed plan runs, `opsx-plan` now dispatches implement/review/archive
workers directly; `/opsx-drive <change-id>` remains the manual single-change
controller path. Follow the machine-read convention below exactly — a
mis-stated dependency line becomes a wrong edge in an unattended automation
DAG.

Resolved inputs:

- Planning request: `$ARGUMENTS`

Entry rules:

- If the planning request is empty, stop and report that
  `/opsx-author <what to plan, with source material references>` is required.
- Resolve the output path: use a path named in the request; otherwise write to
  `docs/plans/<kebab-case-topic>-plan.md`.
- If the output file already exists, stop and report it, unless the request
  explicitly says to replace or revise it.

Before writing, read:

1. `AGENTS.md` if it exists.
2. Any source material referenced in the request (review findings, design
   notes, attached files).
3. The existing capability list under `openspec/specs/` so capability
   references are real, and proposed capabilities are genuinely new.
4. Existing change ids under `openspec/changes/` and
   `openspec/changes/archive/` so new slugs do not collide.

Document structure requirements:

- YAML frontmatter with at least `title:`, `doc_type: implementation-plan`,
  `status: proposed`, and `updated:` date.
- `# <Title>` heading, then a Purpose section.
- A `## Capability Ownership` section whenever new capability directories are
  proposed, listing each with its rationale.
- Phases as `## Phase N: <Name>` headings where N is an integer (0-based or
  1-based, consistently).
- Each change as a `### Change: ` heading followed by the slug in backticks.
- Each change body contains, in order: `**Purpose:**`, `**Depends on:**`,
  `**Capability:**` (or `**Capabilities:**`), `**Scope:**`,
  `**Out of scope:**`, and `**Success parameters:**`.
- End the document with Recommended Sequence, Overall Completion Criteria,
  and Explicit Non-Goals sections.

Machine-read convention (interpreted by `opsx-plan compile` — follow exactly):

1. A dependency on specific changes is written as backticked exact slugs in
   the `**Depends on:**` paragraph. Every backticked slug there becomes a DAG
   edge.
2. A dependency on an entire phase is written as the words `Phase N`. It
   compiles to edges on all changes of that phase, or all preceding changes
   when the phase is the change's own.
3. No dependencies: begin the paragraph with `None.`
4. Mentioning another change WITHOUT depending on it: the paragraph must
   begin with `None.` or contain independence wording ("independent",
   "in parallel", "may proceed"). Otherwise the mention compiles into a
   false edge.
5. The `**Depends on:**` paragraph extends to the first blank line. Never
   place a backticked change slug or a `Phase N` reference inside it unless
   it is a true dependency.
6. A deferred change includes the word "deferred" in its `**Depends on:**`
   paragraph; it compiles to `enabled = false`.
7. Any dependency wording outside rules 1-6 compiles to no edges.
   Use that only when the dependency is genuinely non-mechanical
   (e.g. "completion of any active change touching the same
   requirements") and the operator must decide.
8. A new capability is marked
   `**Capability:** \`name\` (proposed; see Capability Ownership).` — the
   first change per proposed capability compiles to a `pause_before`
   approval gate.
9. Slugs are unique kebab-case OpenSpec change ids, verb-led (e.g.
   `add-`, `enforce-`, `extract-`, `replace-`), and collide with no existing
   or archived change.
10. Phase exit gates needing human judgment cannot be inferred by the
    compiler. State them in prose AND list them in a final
    `## Suggested Manual Gates` section naming the change ids where the
    operator should add `pause_before = true` to the compiled manifest.
11. The compiler does not support or emit `# REVIEW` markers. Review-fix
    cycles are managed by the orchestrator's implement/review/archive loop,
    not by inline markers in source or compiled output.

Reference example of the four dependency forms:

```markdown
### Change: `add-atomic-writes`

**Purpose:** Make runtime writes crash-safe.

**Depends on:** None. This may be developed in parallel with Phase 1.

**Capability:** `runtime-state` (proposed; see Capability Ownership).

### Change: `validate-locking-contract`

**Purpose:** Verify filesystem locking semantics.

**Depends on:** `add-atomic-writes`.

**Capability:** `runtime-state` (proposed; see Capability Ownership).

### Change: `extract-dispatch-coordinator`

**Purpose:** Move dispatch out of the intake module.

**Depends on:** Phase 1, because the extracted path should target the
hardened executor contract.

**Capability:** `gmail-intake-pipeline`.

### Change: `add-migration-registry`

**Purpose:** Upgrade older runtime records after schema evolution.

**Depends on:** Phase 1. Implementation is deferred until the first schema
version bump is proposed.

**Capability:** `runtime-state` (proposed; see Capability Ownership).
```

Plan-quality rules:

- Decompose into small, independently verifiable, independently revertable
  changes; one concern per change.
- Sequence security and data-integrity corrections before enabling more
  automation.
- Separate behavior-preserving extraction from behavior changes; never
  combine specification cleanup with product behavior changes.
- Give every change explicit Scope, Out of scope, and executable Success
  parameters.
- Prefer dependency edges that are real ordering constraints, not stylistic
  preferences; an over-constrained DAG serializes work that could proceed in
  parallel.

Self-verification before the final response:

1. Re-scan every `**Depends on:**` paragraph against rules 1-7. Confirm no
   unintended backticked slug or `Phase N` reference appears in any of them.
2. If the orchestrator is available (an `opsx-plan` executable on PATH, or a
   script path documented in `AGENTS.md`), run
   `opsx-plan compile <doc> -o /tmp/opsx-author-selfcheck.toml --force` and
   verify the compile succeeds and produces the expected changes; fix any
   missing dependencies or malformed entries and rerun until clean.
3. If the compiler is not available, state that in the final response so the
   operator runs it manually.

Final response requirements:

- Do not repeat the document body.
- Report: the output path, phase and change counts, proposed capabilities,
  the compile self-check result (or that it was unavailable), and the
  suggested manual `pause_before` gates.
- Remind the operator to review the compiled DAG with
  `opsx-plan run <plan> --dry-run` before any unattended run.
