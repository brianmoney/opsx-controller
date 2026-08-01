# Claude Code Plugin

This plugin packages the Claude Code adapter in a shareable Claude plugin
layout.

Local development and testing:

```bash
claude --plugin-dir ./plugins/opsx-controller
```

Then invoke the plugin skills as:

```text
/opsx-controller:opsx-plan <planning request>
```

Plugin contents:

- `skills/opsx-plan/SKILL.md`: implementation-plan authoring entrypoint
- `agents/opsx-implementer.md`: implementation phase agent
- `agents/opsx-reviewer.md`: review phase agent
- `agents/opsx-archiver.md`: archive phase agent
- `agents/opsx-plan-author.md`: implementation-plan authoring agent

This plugin packages the Claude Code adapter's plan-level authoring surface
and phase agents. Per-change operations (`implement`, `review`, `archive`)
are handled through the orchestrator's direct dispatch path; the plugin's
agents are invoked by `opsx-plan` via its configured stage invokes, not
directly by the user.

Compilation note:

- `/opsx-controller:opsx-plan` authors the markdown plan document in Claude
  Code.
- `opsx-plan compile --adapter claude-code` compiles the markdown into a TOML
  manifest using Claude Code.  This requires a `controller` model resolved for
  the `claude-code` adapter.
- If those prerequisites are unavailable, the skill must report that the
  document was authored but not compiled.

This plugin is intentionally self-contained so it can be tested with
`--plugin-dir` and later published to a Claude plugin marketplace.
