## 1. Contract

- [x] 1.1 Define the OpenCode usage sidecar JSONL record shape.
- [x] 1.2 Define the `OPSX_*` environment variables used to scope a sidecar to one stage invocation.
- [x] 1.3 Define normalized usage, model identity, event type, and timestamp fields.
- [x] 1.4 Define final-vs-incremental selection semantics.
- [x] 1.5 Define malformed, missing, unreadable, mismatched, and timeout behavior.
- [x] 1.6 Define `usage.usage_source = "opencode_plugin"` and source precedence relative to worker JSON and log metadata.

## 2. Verification

- [x] 2.1 Run `openspec validate define-opencode-plugin-usage-contract --strict`.
