# Adapter Guidance

## Adapter capability matrix

`opsx-plan` runs a plan's changes through the direct implement-review-archive
path: one bounded worker subprocess per stage, plan-owned round control, stage
logs, and telemetry. Direct dispatch is gated purely on configuration — a plan
takes it whenever `implement_invoke`, `review_invoke`, and `archive_invoke` are
all set, regardless of adapter. A plan missing any of the three stage invokes
fails at load time with a `PlanError`; there is no fallback execution path.

For manual single-change control outside a plan run, use
`opsx-run <change-id>` (equivalently `opsx-plan run-one <change-id>`); it is
pinned to the OpenCode adapter and drives the same implement-review-archive loop
with no manifest required. Claude Code and dsh run a single change through a
one-change plan manifest with the matching `adapter` and `opsx-plan run`, and
Codex CLI plan-run is unsupported.

| Adapter | Direct dispatch defaults | Usage/model source |
|---|---|---|
| `opencode` | Supported (`ADAPTER_DEFAULTS`) | OpenCode plugin sidecar (`opencode_plugin`), plus worker JSON and log metadata |
| `claude-code` | Supported (`ADAPTER_DEFAULTS`) | Claude Code result envelope (`claude_result_json`), plus worker JSON and log metadata |
| `codex-cli` | Unsupported. `codex-cli` ships no `ADAPTER_DEFAULTS` invokes, so a plan that relies on adapter defaults fails closed at load time with a `PlanError` naming all three required keys. | Worker JSON and log metadata only (no dedicated envelope/sidecar source) |
| `dsh` | Supported (`ADAPTER_DEFAULTS` dispatch `opsx-dsh-worker --role <stage>`) | Worker JSON and log metadata only (no dedicated envelope/sidecar source) |

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
