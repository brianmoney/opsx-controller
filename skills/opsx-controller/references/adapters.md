# Adapter Guidance

## Adapter capability matrix

`opsx-plan` runs a plan's changes through either the legacy nested-controller
path (`invoke`, one `/opsx-drive` subprocess per change) or the direct
implement-review-archive path (one bounded worker subprocess per stage,
plan-owned round control, stage logs, and telemetry). Direct dispatch is
gated purely on configuration — a plan takes it whenever `implement_invoke`,
`review_invoke`, and `archive_invoke` are all set, regardless of adapter.

| Adapter | Nested-controller (`invoke`) | Direct dispatch defaults | Usage/model source |
|---|---|---|---|
| `opencode` | Supported | Supported (`ADAPTER_DEFAULTS`) | OpenCode plugin sidecar (`opencode_plugin`), plus worker JSON and log metadata |
| `claude-code` | Supported | Supported (`ADAPTER_DEFAULTS`) | Claude Code result envelope (`claude_result_json`), plus worker JSON and log metadata |
| `codex-cli` | Supported | Reachable by configuration, but has no `ADAPTER_DEFAULTS` invokes and is unvalidated — an operator must hand-write all three stage invokes in `[plan]` | Worker JSON and log metadata only (no dedicated envelope/sidecar source) |

Worker JSON parsed from the stage's own one-line JSON result always takes
precedence over any adapter-specific source. See
`docs/opsx-plan-operator-workflow.md` for the full usage-source precedence
chain.

## OpenCode

Source repo installer:

```bash
bash adapters/opencode/install.sh --global
```

Or per project:

```bash
bash adapters/opencode/install.sh --project /path/to/project
```

## Claude Code

Source repo installer:

```bash
bash adapters/claude-code/install.sh --global
```

Or per project:

```bash
bash adapters/claude-code/install.sh --project /path/to/project
```

## Other Clients

If a client supports custom prompts, commands, skills, or subagents, map the
same controller contract onto three phases:

- implement
- review
- archive

Preserve the durable state contract, strict review gate, and explicit archive
scope behavior.
