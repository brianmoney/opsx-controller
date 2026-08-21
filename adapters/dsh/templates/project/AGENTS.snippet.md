## OpenSpec Controller Workflow (dsh)

The shared plan-authoring reference is the single source of truth for
compilable plan documents. Read it first when you need the full machine-read
convention: `.opsx-controller/dsh/plan-authoring.md` (project-first) or
`~/.config/opsx-controller/dsh/plan-authoring.md` (global fallback).

### Upstream OpenSpec (per-change)

Upstream OpenSpec provides per-change operations that the controller invokes
through each adapter's client-specific commands. The dsh adapter has **no
slash commands**: dsh has no agent-selection flag, so roles are carried by the
installed `opsx-dsh-worker` shim instead of client-side command or agent
registration. Run per-change operations with the upstream OpenSpec CLI
directly, or drive the full loop through a plan manifest.

### opsx-controller (plan-level)

The controller provides plan-level orchestration:

- `opsx-plan` — compile, run, and report on multi-change implementation plans.
  The CLI entrypoint is installed at `~/.local/bin/opsx-plan`.
- `opsx-run` — manual single-change implement-review-archive loop. **Not
  supported on the dsh adapter** (dsh has no default stage invokes and no
  `--adapter` flag on `run-one`); run dsh changes via a plan manifest with
  `adapter = "dsh"` and `opsx-plan run`.

Durable controller state lives at `.opsx-controller/<change-id>.json` (dsh has
no protected project config dir). The controller dispatches the fixed worker
shim `opsx-dsh-worker` with `--role implementer`, `--role reviewer`, or
`--role archiver`. dsh reads this `AGENTS.md` from the working directory
natively and merges it with the stable startup file in `DSH_HOME`.
