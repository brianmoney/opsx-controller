---
name: opsx-controller
description:
  Guided OpenSpec controller workflow for one accepted change. Use when a repo
  already uses OpenSpec and you want a strict implement-review-archive loop with
  durable state, blocking review findings, and explicit archive scope.
license: MIT
metadata:
  author: brianmoney
  version: '1.1.0'
---

# OpenSpec Controller Workflow

This skill packages the `opsx-controller` workflow in a self-contained format so
it can be installed with Vercel's `npx skill` flow.

## What To Read

- `references/controller-contract.md`
- `references/state-schema.md`
- `references/phase-protocol.md`
- `references/adapters.md`

## Core Workflow

1. Pick exactly one accepted OpenSpec change.
2. Read repository guidance and the relevant OpenSpec artifacts.
3. Implement the next required work.
4. Run a strict review.
5. If review reports any critical, warning, or note findings, fix them and loop.
6. Archive only after a fresh clean review.

## Adapter Guidance

This package is a guide and reference bundle.

- For OpenCode automation, use `adapters/opencode/install.sh` from the source repo.
- For Claude Code automation, use `adapters/claude-code/install.sh` from the source repo.
- For other coding clients, map the same controller contract onto client-native
  commands, skills, or agents.

Keep the durable state contract, strict review gate, and explicit archive scope
intact.
