# Adapter Guidance

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
