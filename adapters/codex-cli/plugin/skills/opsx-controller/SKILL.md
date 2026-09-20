---
name: opsx-controller
description:
  Guided plan-level orchestration for OpenSpec repositories. Use when a repo
  already uses OpenSpec and you want `opsx-plan` to compile a markdown plan into
  a dependency DAG and drive each change through a strict, durable
  implement-review-archive loop. Per-change propose, apply, archive, and verify
  work stays with upstream OpenSpec.
license: MIT
metadata:
  author: brianmoney
  version: '1.2.0'
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

`opsx-controller` owns plan-level orchestration; per-change work belongs to
upstream OpenSpec.

**Plan-level orchestration** (`opsx-plan` / `opsx-run`):

1. Write a compilable markdown implementation plan following the shared
   client-neutral reference `core/plan-authoring.md`.
2. `opsx-plan compile` converts it into a runnable TOML dependency DAG.
3. `opsx-plan run` sequences changes through direct implement → review →
   archive dispatch, with durable per-change state and a strict review gate.
   A plan missing any of `implement_invoke`, `review_invoke`, or
   `archive_invoke` fails closed at load time — direct dispatch is the only
   execution model.

`opsx-run <change-id>` is the supported manual single-change loop; it is pinned
to the OpenCode adapter.

**Per-change work (upstream OpenSpec):** for a single change, use the upstream
OpenSpec commands and skills for propose, apply, archive, and verify. The
controller does not replace them and does not own a per-change workflow.

## Adapter Guidance

This package is a guide and reference bundle.

- For OpenCode automation, use `adapters/opencode/install.sh` from the source repo.
- For Claude Code automation, use `adapters/claude-code/install.sh` from the source repo.
- For other coding clients, map the same controller contract onto client-native
  commands, skills, or agents.

Keep the durable state contract, strict review gate, and explicit archive scope
intact.
