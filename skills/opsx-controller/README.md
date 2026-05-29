# opsx-controller skill package

This package is laid out so Vercel's `npx skill` can install it directly from
this repository.

Example:

```bash
SKILL_BASE_URL="https://github.com/brianmoney/opsx-controller/tree/main" \
  npx skill skills/opsx-controller
```

Contents:

- `SKILL.md`: main skill entrypoint
- `references/`: self-contained controller contract and adapter notes

This package is guidance-focused. For automated installation into a specific
client, use the source repository's adapter installers.
