## Why

Operators have plan markdown documents that are intended to become runnable `opsx-plan` TOML manifests, but `opsx-plan` currently has no `compile` subcommand despite documentation referring to one. This forces humans or ad hoc agents to translate plan docs manually, which creates DAG mistakes and slows the path from planning to implementation.

## What Changes

- Add an `opsx-plan compile <plan.md> -o <plan.toml>` command that converts a markdown implementation plan into an opsx-plan-compatible TOML manifest.
- Have compilation invoke OpenCode using `OPSX_CONTROLLER_MODEL` so the controller model performs the markdown-to-TOML transformation instead of adding a brittle bespoke markdown parser.
- Build a complete prompt/context package for the model, including the source markdown, expected TOML schema, machine-read dependency rules, current adapter defaults, and representative markdown/TOML template plans.
- Validate the generated TOML before writing or before reporting success, including loadability by the existing plan manifest parser and basic dependency integrity.
- Document the new compile workflow and add regression tests for command construction, reference injection, validation, and failure handling.

## Capabilities

### New Capabilities

### Modified Capabilities
- `plan-driven-opencode-execution`: Add a plan compilation surface that turns implementation-plan markdown into runnable plan TOML through a bounded OpenCode controller-model invocation.

## Impact

- `orchestrator/opsx-plan.py` CLI, prompt construction, subprocess invocation, TOML validation, and output-file handling
- `openspec/plans/` examples or references used as compile-time templates
- OpenCode model environment contract through `OPSX_CONTROLLER_MODEL`
- Documentation for `opsx-plan compile` usage and failure modes
- Orchestrator tests covering compile command behavior without requiring live model execution
