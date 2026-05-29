# Claude Code Plugin

This plugin packages the Claude Code adapter in a shareable Claude plugin
layout.

Local development and testing:

```bash
claude --plugin-dir ./plugins/opsx-controller
```

Then invoke the controller skill as:

```text
/opsx-controller:opsx-drive <change-id>
```

Plugin contents:

- `skills/opsx-drive/SKILL.md`: controller entrypoint
- `agents/opsx-implementer.md`: implementation phase agent
- `agents/opsx-reviewer.md`: review phase agent
- `agents/opsx-archiver.md`: archive phase agent

This plugin is intentionally self-contained so it can be tested with
`--plugin-dir` and later published to a Claude plugin marketplace.
