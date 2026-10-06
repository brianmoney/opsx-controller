# phase-agent-generation Specification

## Purpose
TBD - created by archiving change add-phase-agent-generation. Update Purpose after archive.

## Requirements

### Requirement: One canonical instruction body per phase

The repository SHALL maintain exactly one authored, client-neutral instruction
body per phase at `core/phase-agents/implementer.md`,
`core/phase-agents/reviewer.md`, and `core/phase-agents/archiver.md`. Each body
SHALL contain the shared workflow, evidence-based completion and accepted
deferral policy, manual-task rule, phase classification rules, and the
machine-readable final-response contract for that phase.

The adapter and plugin instruction files SHALL NOT be authored independently.
A shared policy change SHALL require editing only the canonical body for that
phase, and every distribution SHALL receive the change through generation.

#### Scenario: Policy text exists once

- **WHEN** a shared implementer or reviewer policy clause is changed
- **THEN** only `core/phase-agents/<phase>.md` needs editing for the change to
  reach every adapter and plugin distribution

#### Scenario: Machine-readable contracts are preserved

- **WHEN** canonical bodies are extracted from the current agent sources
- **THEN** the implementer, reviewer, and archiver final-response JSON fields
  remain available to worker parsing exactly as specified by the phase protocol

### Requirement: Adapter templates carry only client-specific surface

Each distribution SHALL provide a template containing its client metadata,
wrapper format, and client-specific step text, with a single placeholder that
receives the canonical body at generation time.

Templates SHALL preserve: OpenCode frontmatter, permissions, and
`{env:OPSX_*_MODEL}`/`{env:OPSX_*_VARIANT}` tokens; Claude Code `tools`,
`model: inherit`, and `effort`; Codex CLI `sandbox_mode`, model, and
`model_reasoning_effort` with the body in `developer_instructions`; and the dsh
preamble with no frontmatter. Installed agent file names and destinations SHALL
remain unchanged.

#### Scenario: Client metadata survives generation

- **WHEN** the generator renders each adapter's agent files
- **THEN** the client-specific metadata is byte-preserved from the template and
  unresolved `{env:}` tokens are present for install-time substitution

#### Scenario: Client-specific steps stay out of the canonical body

- **WHEN** a client-only step such as the CLAUDE.md read requirement or the dsh
  role preamble is needed
- **THEN** it lives in that distribution's template and not in
  `core/phase-agents/`

### Requirement: Deterministic generation with generated-file markers

A stdlib generator at `scripts/generate-phase-agents.py` SHALL render every
distributed phase-agent file from the canonical bodies and adapter templates.
Generation SHALL be deterministic: identical inputs produce byte-identical
outputs, with no timestamps and stable ordering.

Every generated output SHALL begin with a header naming the generator and the
canonical source and stating that the file is generated rather than
hand-edited.

#### Scenario: Regeneration is byte-identical

- **WHEN** the generator runs twice against unchanged sources
- **THEN** both runs produce identical bytes for every output

#### Scenario: Generated files are marked

- **WHEN** an editor opens a distributed agent file
- **THEN** the first lines identify it as generated from a canonical
  `core/phase-agents/` source

### Requirement: Drift between sources and generated files fails checks

The generator SHALL support a check mode that exits non-zero and reports every
output that differs from its committed copy. The repository's test or CI
pipeline SHALL fail when generated files drift from the canonical bodies and
templates, covering all adapter agent files and plugin distribution copies,
including the archiver.

#### Scenario: Hand-editing a generated file fails the check

- **WHEN** a distributed agent file is edited without regenerating
- **THEN** the drift check reports that file and exits non-zero

#### Scenario: Every distribution is covered

- **WHEN** the drift check runs
- **THEN** it covers OpenCode, Claude Code, Codex CLI, and dsh agent files plus
  the Codex plugin and Claude plugin copies, including the archiver

### Requirement: Authored and generated files are documented

The repository documentation SHALL state which phase-agent files are authored
canonical sources, which are generated outputs, and the command that regenerates
them.

#### Scenario: A contributor finds the regeneration command

- **WHEN** a contributor looks for how to change implementer, reviewer, or
  archiver instructions
- **THEN** the documentation points to the canonical body and the generator
  command rather than to individual adapter files
