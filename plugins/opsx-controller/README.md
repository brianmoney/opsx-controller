# Claude Code Plugin

This plugin packages the Claude Code adapter in a shareable Claude plugin
layout.

Local development and testing:

```bash
claude --plugin-dir ./plugins/opsx-controller
```

Then invoke the plugin skills as:

```text
/opsx-controller:opsx-drive <change-id>
/opsx-controller:opsx-plan <planning request>
```

Plugin contents:

- `skills/opsx-drive/SKILL.md`: controller entrypoint
- `skills/opsx-plan/SKILL.md`: implementation-plan authoring entrypoint
- `agents/opsx-implementer.md`: implementation phase agent
- `agents/opsx-reviewer.md`: review phase agent
- `agents/opsx-archiver.md`: archive phase agent
- `agents/opsx-plan-author.md`: implementation-plan authoring agent

Compilation note:

- `/opsx-controller:opsx-plan` authors the markdown plan document in Claude
  Code.
- `opsx-plan compile` still depends on an OpenCode-configured environment and
  `OPSX_CONTROLLER_MODEL`.
- If those prerequisites are unavailable, the skill must report that the
  document was authored but not compiled.

This plugin is intentionally self-contained so it can be tested with
`--plugin-dir` and later published to a Claude plugin marketplace.
