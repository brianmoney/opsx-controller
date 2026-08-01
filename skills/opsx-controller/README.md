# opsx-controller skill package

This package is laid out so Vercel's `npx skill` can install it directly from
this repository.

Example:

```bash
SKILL_BASE_URL="https://github.com/brianmoney/opsx-controller/tree/main" \
  npx skill skills/opsx-controller
```

Contents:

- `SKILL.md`: main skill entrypoint covering plan-level orchestration and the
  per-change controller loop
- `references/`: self-contained controller contract, adapter notes, state schema,
  and phase protocol

For authoring compilable markdown implementation plans, see the shared reference
at `core/plan-authoring.md` in the source repository. This package is
guidance-focused. For automated installation into a specific client, use the
source repository's adapter installers.
