# Claude Code Parity for `compile` and the Orchestrator Executables

**Status:** resolved (add-adapter-aware-plan-compilation)
**Scope:** `opsx-plan compile` and installation of the `opsx-plan` / `opsx-run`
executables

## Summary

The controller loop is client-neutral. The two surfaces a *new user* touches
first are now client-neutral as well:

1. `opsx-plan compile` supports `--adapter opencode` (default) and
   `--adapter claude-code`, shelling out to the corresponding binary with
   adapter-specific model validation before spawn.
2. All three global installers (`adapters/opencode/install.sh`,
   `adapters/claude-code/install.sh`, `adapters/codex-cli/install.sh`) deploy
   `opsx-plan` and `opsx-run` to `~/.local/bin/` via the shared
   `scripts/install-orchestrator.sh` helper.

## What is *not* broken

Worth stating explicitly, to keep the scope honest:

- The implement / review / archive loop is fully client-neutral, with complete
  `claude-code` entries in `ADAPTER_DEFAULTS`.
- Direct stage dispatch (`invoke_direct_stage`) is adapter-driven and already
  works for Claude Code, including `--output-format json` and the
  markdown-prefixed result parsing added earlier.
- Per-adapter model resolution (`lib/models/resolver.py`) already exists and is
  used everywhere except `compile`.
- Telemetry, reporting, and the dashboard are adapter-agnostic.

## Implementation synopsis

### A. Shared orchestrator installation

Smallest change, largest share of the pain removed. Also fixes Codex CLI for
free.

1. Extract `install_orchestrator()` into a shared, client-neutral script — e.g.
   `scripts/install-orchestrator.sh` — with the same behavior and idempotence.
2. Call it from all three adapter installers.
3. Keep `--verify` semantics and the existing summary lines per installer.
4. Confirm `_check_stale_install()` still detects a stale deployed copy when the
   install is performed by a non-OpenCode installer.

No orchestrator changes are required for this step. It can ship independently.

### B. Adapter-aware `compile`

1. **Decide how the adapter is selected.** This is the one real design question:
   `compile` runs *before* a plan manifest exists, so there is no active plan to
   read an adapter from. Options, roughly in order of preference:
   - an explicit `--adapter` flag on `compile`, defaulting to a configured
     value;
   - a `default_adapter` in `~/.config/opsx-controller/models.toml`;
   - inference from which adapters are installed, which fails ambiguously when
     more than one is and should probably be rejected.
2. **Introduce a per-adapter compile invocation** alongside `ADAPTER_DEFAULTS`,
   giving each adapter its command shape (`opencode run --model <m> <prompt>`,
   `claude -p --model <m> <prompt>`, and a Codex equivalent or an explicit
   "unsupported" error).
3. **Generalize `check_controller_model()`** to resolve the `controller` role
   against the selected adapter rather than pinning `opencode`, and update its
   fail-closed message to name that adapter.
4. **Replace `run_opencode_for_compile()`** with an adapter dispatcher.
   Non-zero exit, spawn failure, and timeout handling stay identical; only the
   argv construction and error text vary.
5. **Generalize `extract_toml()`.** See the risk below — this is the step most
   likely to need real work rather than a rename.
6. **Adjust `build_schema_guidance()`** so the emitted adapter defaults match
   the selected adapter instead of always emitting OpenCode's.

### C. Doctor and diagnostics

Add a check that the client required for `compile` is present for the selected
adapter, so the failure is reported at `opsx-plan doctor` time rather than
mid-command.

### D. Tests

- Unit coverage for the compile dispatcher per adapter (argv construction,
  spawn failure, non-zero exit, timeout).
- Extraction tests against realistic Claude Code output, not just OpenCode's.
- An installer test, or at minimum a verification path, asserting that each
  adapter's installer produces working `opsx-plan` / `opsx-run`.

### E. Documentation

- Remove the OpenCode prerequisite callout from the README quick start.
- Update `docs/adapters.md` — particularly
  [Choosing an adapter](adapters.md#choosing-an-adapter), which currently
  encodes the OpenCode requirement.
- Update `AGENTS.md` maintainer deploy notes if the installer entry points move.

## Principal technical risk

`extract_toml()` fails closed on any content surrounding the TOML payload:
multiple fenced blocks, or any prose before or after a single block, raises
rather than guessing. That strictness is correct and should not be relaxed
globally.

Claude Code is more inclined than OpenCode to wrap output in explanatory prose,
and the repository already carries a fix for markdown-prefixed *stage* results
(`fa26f00`) — evidence that this is a real behavioral difference, not a
hypothetical one. Expect adapter-aware compile to need one or both of:

- a stricter compile prompt for the Claude adapter ("output only TOML" is
  already instruction 1, but may need reinforcement or a stop-sequence style
  constraint), and
- a per-adapter extraction path that tolerates a known prefix shape while still
  failing closed on genuine ambiguity.

Budget for this specifically. It is the difference between a rename and a
behavior change, and it should be validated against real Claude output before
the work is called done.

## Recommended sequencing for launch

1. **A (shared installer)** — independent, low risk, fixes Codex CLI as well.
2. **B (adapter-aware compile)** — the substantive work; gated on the extraction
   risk above.
3. **C, D, E** — alongside B.

If only one lands before launch, land A. It converts "install a competing agent"
into "install a competing agent *to compile*," which is a materially smaller
objection and removes the `command not found` wall entirely.

## Out of scope

- Changing the controller contract, phase protocol, or state schema.
- Codex CLI feature parity beyond installation. `ADAPTER_DEFAULTS` has no
  `implement` / `review` / `archive` entries for `codex-cli`; that gap is real
  but separate from this document.
- Removing OpenSpec as a prerequisite of the target repository.
