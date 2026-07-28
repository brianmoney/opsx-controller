## 1. Shared Installation

- [ ] 1.1 Extract the existing user-level orchestrator runtime copy into a client-neutral installer helper that accepts the repository root and preserves the current executable and library destinations.
- [ ] 1.2 Invoke the shared helper from each global OpenCode, Claude Code, and Codex CLI installer while preserving adapter-local artifacts, model resolution, summaries, and `--verify` behavior.
- [ ] 1.3 Add installer-focused coverage using a temporary home directory to verify all global installers deploy both executables and the required runtime libraries, and that stale-install detection remains installer-agnostic.

## 2. Adapter-Aware Compilation

- [ ] 2.1 Add the `compile --adapter` CLI option with an `opencode` default and a compile-client registry that represents OpenCode, Claude Code, and explicitly unsupported Codex CLI.
- [ ] 2.2 Generalize compile controller-model resolution and validation to the selected supported adapter, with client-specific actionable errors.
- [ ] 2.3 Replace the OpenCode-only compile runner with a shared dispatcher that constructs supported client argv, preserves process timeout/error semantics, and rejects Codex before spawning a process.
- [ ] 2.4 Thread the selected adapter through schema guidance and compile prompts so generated manifests contain matching adapter defaults; fail validation when model output selects a different adapter.
- [ ] 2.5 Preserve strict OpenCode TOML extraction and implement only a captured, documented, and tested Claude envelope rule if the supported Claude CLI output requires one.

## 3. Diagnostics And Tests

- [ ] 3.1 Add `doctor --adapter` for plan-less preflight while keeping a resolved plan's adapter authoritative.
- [ ] 3.2 Extend orchestrator tests for OpenCode and Claude model selection, argv construction, spawn failure, timeout, non-zero exits, prompt defaults, manifest adapter matching, and unsupported Codex compilation.
- [ ] 3.3 Add extraction fixtures for valid raw/fenced Claude output and any approved stable envelope, plus rejection coverage for prose, multiple blocks, and multiple candidates.
- [ ] 3.4 Add doctor tests confirming plan-less Claude selection, adapter-specific identifier validation, client PATH checks, and unchanged plan-adapter precedence.

## 4. Documentation And Validation

- [ ] 4.1 Update README, adapter reference, orchestrator README, and operator workflow documentation to remove the universal OpenCode prerequisite and document selected-adapter compilation.
- [ ] 4.2 Update Claude authoring skills, plugin documentation, and associated guidance so self-checks run `compile --adapter claude-code` and accurately report missing Claude prerequisites.
- [ ] 4.3 Update `AGENTS.md` maintainer deployment notes for shared executable installation through all adapter installers.
- [ ] 4.4 Run `python3 -m unittest discover -t . -s tests`, `node tests/opencode/test-opsx-usage-emitter.js`, and `openspec validate add-adapter-aware-plan-compilation --strict`.
