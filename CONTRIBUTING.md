# Contributing

## Running the tests

Two independent suites, both stdlib/runtime only — no repo `.venv` or
`node_modules` required.

Python (3.11+, stdlib-only — the orchestrator relies on `tomllib`):

```bash
python3 -m unittest discover -t . -s tests
```

Run it from the repository root. Discovery picks up any `test_*.py` under
`tests/`, so a new test module is included automatically — add an
`__init__.py` when you create a new test package, or discovery will skip the
directory without saying so.

To run one module or one test while iterating:

```bash
python3 -m unittest tests.orchestrator.test_opsx_plan
python3 -m unittest tests.orchestrator.test_opsx_plan.ArchiveCommitEvidenceGateTests
```

JavaScript (Node, no dependencies):

```bash
node tests/opencode/test-opsx-usage-emitter.js
```

Both run in CI on every push and pull request (`.github/workflows/test.yml`).

## Why stdlib-only

`orchestrator/opsx-plan.py` is deliberately dependency-free. It's the thing
that drives your actual OpenSpec changes through implement/review/archive —
keeping it stdlib-only means no supply-chain surface and no version drift
between environments. Contributions to the orchestrator should preserve this.

## The OpenSpec-driven workflow

Changes to this repo's own behavior are expected to go through the same
OpenSpec change workflow the tool implements: propose a change under
`openspec/changes/<id>/` (proposal, design, tasks, delta specs), implement
it, review it, and archive it. Start with `core/controller-contract.md`,
`core/state-schema.md`, and `core/phase-protocol.md` for the shared contract
every adapter implements.

Ordinary bug fixes and small, self-contained PRs don't need to go through
the full OpenSpec ceremony — use judgment.
