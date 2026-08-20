# adapter-aware-plan-compilation Specification

## Purpose

Define adapter selection and client-specific behavior for compiling Markdown
implementation plans into validated TOML manifests.

## Requirements

### Requirement: Operators select a compile adapter explicitly

The orchestrator SHALL accept `opsx-plan compile --adapter <adapter>` for each
known adapter key. When `--adapter` is omitted, it SHALL use `opencode`.

The selected adapter SHALL determine controller-model resolution, client
invocation, generated manifest defaults, prompt instructions, extraction
diagnostics, and compile log messages. The compiler SHALL require the generated
`[plan].adapter` value to match the selected adapter through its existing
manifest validation path.

Generated manifest guidance SHALL describe only the direct-dispatch
configuration keys `implement_invoke`, `review_invoke`, and `archive_invoke`.
It SHALL NOT document or generate the retired `invoke` or `max_attempts` keys.

#### Scenario: Claude Code compiles a plan

- **WHEN** an operator runs `opsx-plan compile --adapter claude-code plan.md -o plan.toml` with a resolvable Claude controller model
- **THEN** the orchestrator invokes Claude Code, validates a manifest whose adapter is `claude-code`, and writes the output atomically

#### Scenario: Existing OpenCode command remains compatible

- **WHEN** an operator runs `opsx-plan compile plan.md -o plan.toml` without `--adapter`
- **THEN** the orchestrator selects `opencode` and retains the existing OpenCode compile behavior

#### Scenario: Compile guidance excludes legacy drive keys

- **WHEN** the compiler builds schema guidance or a sample manifest for a plan
- **THEN** the guidance contains the three direct stage invoke keys and contains neither `invoke` nor `max_attempts`

### Requirement: Supported compile clients use adapter-specific controller models

For each supported compile adapter, the orchestrator SHALL resolve the
`controller` role using that adapter before spawning its client. It SHALL fail
before spawn when the role is unresolved or violates the selected adapter's
model-identifier syntax.

OpenCode compilation SHALL invoke `opencode run --model <model> <prompt>`.
When the `controller` role resolves a reasoning variant (via a
`controller_variant` key or `OPSX_CONTROLLER_VARIANT`), OpenCode compilation
SHALL additionally pass `--variant <variant>` on the same invocation. When no
controller variant is resolved, compilation SHALL omit the `--variant` flag
entirely so the client's built-in default applies.

Claude Code compilation SHALL invoke its non-interactive print command with
the resolved model, delivering the compile prompt through standard input
rather than as an inline argv argument, so prompt size is never limited by
the operating-system argument-list limit. The Claude Code CLI has no
reasoning-variant flag, so a resolved controller variant SHALL be ignored for
Claude Code compilation rather than rejected or passed through. Spawn
failures, non-zero exits, and timeouts SHALL name the selected client. The
compile client timeout SHALL default to 600 seconds and SHALL be configurable
through `opsx-plan compile --timeout-minutes`; a timeout failure SHALL name
that option in its diagnostic.

#### Scenario: Claude model does not leak from OpenCode configuration

- **WHEN** OpenCode and Claude Code have different configured controller models and an operator compiles with `--adapter claude-code`
- **THEN** the Claude client argv receives only the Claude Code controller model

#### Scenario: Claude prompt travels on stdin

- **WHEN** an operator compiles with `--adapter claude-code` and a prompt of any size
- **THEN** the prompt is passed to the Claude process through standard input and no argv element contains the prompt text

#### Scenario: Missing selected controller model fails closed

- **WHEN** the controller role is unresolved for the selected compile adapter
- **THEN** compilation exits before client invocation with remediation naming that adapter

#### Scenario: Resolved controller variant is passed to OpenCode

- **WHEN** the controller role resolves both a model and a variant for `opencode` and an operator compiles a plan
- **THEN** the OpenCode client argv contains `--variant` with the resolved variant value

#### Scenario: Unresolved controller variant is omitted

- **WHEN** the controller role resolves a model but no variant for `opencode` and an operator compiles a plan
- **THEN** the OpenCode client argv contains no `--variant` flag and compilation proceeds with the client default effort

#### Scenario: Claude Code compilation ignores a resolved variant

- **WHEN** the controller role resolves both a model and a variant for `claude-code` and an operator compiles with `--adapter claude-code`
- **THEN** compilation succeeds and no variant flag or argument is passed to the Claude client

#### Scenario: Compile timeout is configurable

- **WHEN** an operator runs `opsx-plan compile --timeout-minutes 20 plan.md`
- **THEN** the compile client is allowed 1200 seconds before a timeout failure, and a timeout diagnostic names the `--timeout-minutes` option

#### Scenario: Compile timeout default is unchanged

- **WHEN** an operator compiles without `--timeout-minutes`
- **THEN** the compile client timeout remains 600 seconds

### Requirement: Unsupported compile adapters fail before invocation

The compiler SHALL recognize `codex-cli` as an adapter selection but SHALL
reject it before model resolution or client spawn with an error that states
Codex CLI compilation is unsupported in the current release.

#### Scenario: Codex compile is rejected explicitly

- **WHEN** an operator runs `opsx-plan compile --adapter codex-cli plan.md -o plan.toml`
- **THEN** no Codex process is spawned and the command reports that Codex compilation is out of scope

### Requirement: TOML extraction remains fail closed per client

The compiler SHALL accept bare TOML or exactly one clean TOML fence for
OpenCode output. Claude Code output SHALL use the same rule unless the
implementation documents and tests one stable Claude-specific output envelope.

Any accepted Claude-specific envelope handling SHALL produce exactly one TOML
candidate before applying the normal extraction rule. The compiler SHALL reject
empty output, multiple fenced blocks, multiple candidate payloads, prose before
or after the payload, and malformed TOML.

#### Scenario: Claude output with an approved stable envelope is accepted

- **WHEN** Claude Code returns the one documented and tested envelope containing one TOML payload
- **THEN** the compiler extracts that payload and continues with normal TOML and manifest validation

#### Scenario: Ambiguous model output is rejected

- **WHEN** a supported compile client returns prose surrounding a TOML fence or multiple TOML fences
- **THEN** compilation fails without writing or replacing the output manifest
