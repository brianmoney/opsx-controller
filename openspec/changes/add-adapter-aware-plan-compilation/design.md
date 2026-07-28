## Context

`opsx-plan compile` predates adapter-neutral direct execution. It hard-codes
the OpenCode controller-model resolution, command argv, schema defaults, log
text, and extraction errors. Separately, only the OpenCode global installer
copies the client-neutral Python executable and runtime packages to
`~/.local/`.

Compile runs before a plan manifest exists, so it cannot obtain an adapter from
the plan it will generate. The existing model resolver already supports each
adapter, and doctor already knows how to check a selected adapter client on
PATH. The change must reuse those seams without changing controller state,
phase execution, or the model configuration format.

## Goals / Non-Goals

**Goals:**
- Let an operator explicitly compile through OpenCode or Claude Code using a
  controller model resolved for that client.
- Preserve existing OpenCode command behavior for callers that omit an adapter.
- Install the common executables and runtime through every global adapter
  installer.
- Fail before client invocation for unsupported clients, absent binaries,
  unresolved controller models, ambiguous model output, or invalid manifests.
- Let `doctor` preflight the same adapter selection when no plan exists.

**Non-Goals:**
- Add Codex CLI compile support or complete Codex direct-stage parity.
- Infer an adapter from installed tools or add a persisted `default_adapter`
  setting to `models.toml`.
- Relax the general TOML extraction contract or alter phase protocol, state
  schema, telemetry, or `opsx-run` single-change behavior.
- Change project-scoped adapter installation into a user-level executable
  installation mechanism.

## Decisions

### Select compilation with an explicit flag

`opsx-plan compile` gains `--adapter`, accepting known adapter keys and
defaulting to `opencode`. It is threaded as an explicit argument through model
resolution, prompt construction, invocation, extraction, and user-visible
diagnostics. This retains current commands while making the Claude path
deterministic:

```bash
opsx-plan compile --adapter claude-code plan.md -o plan.toml
```

`opsx-plan doctor` gains the same optional flag only for plan-less operation;
a loaded plan's `adapter` remains authoritative. This preserves existing plan
preflight semantics and provides a way to check a future compile client.

Alternatives considered:
- A `default_adapter` in model configuration would hide selection in a file
  whose current purpose is role-to-model resolution and introduces a migration
  and precedence contract.
- Installed-client inference is nondeterministic when more than one client is
  available and would make the generated manifest surprising.
- Making `--adapter` required breaks existing OpenCode automation without a
  corresponding safety benefit.

### Separate compile-client metadata from stage defaults

Add a small registry dedicated to compile clients. Its entries supply the
client label, executable, argv shape, and whether compilation is supported.
`ADAPTER_DEFAULTS` remains the source of manifest and stage invocation
defaults. The dispatcher constructs argv from the compile registry and keeps
the existing 600-second timeout, working directory, output capture, and
failure handling common.

OpenCode uses `opencode run --model <model> <prompt>`. Claude uses its
non-interactive print invocation `claude -p --model <model> <prompt>`. Codex
is recognized by the flag parser but has no compile command at launch, so it
raises a scope-specific error before model resolution or process spawn.

This avoids inventing incomplete stage defaults for Codex while maintaining a
single selected-adapter flow for known clients.

### Generate a manifest for the selected adapter

`build_schema_guidance` and `build_compile_prompt` accept the selected adapter
and render its `[plan]` defaults. The prompt requires the generated `adapter`
field to equal that selection. The normal temporary-file parse and `load_plan`
validation remains the final authority; therefore a model cannot silently
produce a manifest for a different client.

### Preserve strict output validation with a Claude-specific boundary

OpenCode continues to accept only bare TOML or one clean TOML fence. Claude's
prompt is reinforced to emit raw TOML. If captured Claude CLI output requires
handling a stable envelope, the implementation may remove exactly one
documented prefix or result envelope before applying the same strict parser.
It must still reject multiple fences, content after the payload, multiple
candidate TOML documents, and arbitrary explanatory prose.

No broad "find the first `[plan]`" extraction is permitted, because it would
turn malformed or ambiguous model output into a plan selected by guesswork.

### Share only global runtime installation

Move the existing OpenCode `install_orchestrator` body into
`scripts/install-orchestrator.sh`, invoked from every global installer. The
helper owns idempotent replacement of the runtime libraries and executable
copies. Each adapter retains its own model loading, artifacts, summary text,
and CLI verification. Project installers do not invoke the helper because the
runtime destination is user-global.

The installed locations do not change, so `_check_stale_install` retains its
content-hash implementation and works independently of which installer last
deployed the executable.

## Risks / Trade-offs

- [Claude output includes prose or a changing result envelope] → Capture real
  representative output before finalizing extraction; accept only a single
  verified form and cover it with fixtures.
- [Claude CLI command shape differs across installed versions] → Keep argv in
  one registry entry, document the supported headless form, and test argv
  construction without requiring a live client.
- [Installer helper changes cause shell-path or root-resolution regressions] →
  Pass the repository root explicitly, test with a temporary `HOME`, and keep
  installed paths unchanged.
- [Default OpenCode obscures the new Claude path] → Put `--adapter
  claude-code` in Claude quick starts and make compile logs print the selected
  adapter and client.
- [Documentation continues claiming a global OpenCode dependency] → Update all
  identified quick-start, adapter, operator, plugin, and Claude-authoring
  references in the same change.

## Migration Plan

1. Add the shared installer helper and call it from all global installers.
2. Implement adapter-aware compile and doctor selection with OpenCode as the
   compatibility default.
3. Add dispatcher, extraction, doctor, and installer tests.
4. Update documentation and Claude authoring guidance.
5. Run repository test suites and strict OpenSpec validation.

No data migration is required. Rollback consists of restoring the previous
single-client compile path; installed executables remain compatible because
their paths and runtime layout do not change.

## Open Questions

- Confirm the exact Claude CLI transcript from a supported installed version.
  If it already emits plain stdout for `-p`, no Claude-specific extraction
  exception is needed; otherwise the captured stable envelope becomes the only
  additional accepted form.
