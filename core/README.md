# opsx-controller core

Client-neutral OpenSpec controller contract for driving one accepted change
through implement, review, and archive rounds with durable state.

This directory documents the workflow semantics that adapters should preserve.

- `controller-contract.md`: lifecycle, phase order, and stop conditions
- `state-schema.md`: durable state expectations and resume behavior
- `phase-protocol.md`: input and output contracts for implement, review, and
  archive phases

Current adapters:

- `adapters/opencode/`: OpenCode commands, agents, installer, and templates
- `adapters/claude-code/`: Claude Code skill, phase agents, installer, and templates
- `plugins/opsx-controller/`: Claude Code plugin package for namespaced distribution
- `skills/opsx-controller/`: Vercel `npx skill` package for discovery and guided use
