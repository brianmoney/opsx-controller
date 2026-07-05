## Context

`opsx-plan` now drives OpenCode-backed plan execution by launching direct phase workers (`opsx-implementer`, `opsx-reviewer`, and `opsx-archiver`) with `opencode run --agent ...`. These runs are intended to be unattended, so any tool permission request that requires an operator response is a terminal failure for the active stage.

The current OpenCode worker prompts require reading `AGENTS.md`, but this repository does not have that file. In recent runs the reviewer responded by globbing upward into `/home/brian`, triggering an `external_directory` permission request. Because `opencode run` cannot answer that prompt, the agent exited before emitting its required final JSON object. The orchestrator then reported the transcript as generic invalid JSON, hiding the real reason.

## Goals / Non-Goals

**Goals:**

- Keep OpenCode direct worker runs non-interactive when optional repo guidance files are absent.
- Make the intended guidance lookup explicit: repo-root `AGENTS.md` is optional, and missing files do not justify parent-directory searches.
- Preserve allowed reads for installed global OpenCode prompts under `~/.config/opencode`.
- Report permission-denied worker transcripts with an actionable `permission denied before JSON output` style reason.
- Cover the prompt, permission, and parser behavior with focused unit tests.

**Non-Goals:**

- Do not change the worker JSON protocol or add new worker result fields.
- Do not relax the final one-line JSON requirement for successful worker completion.
- Do not add retry behavior for permission-denied worker exits; the fix should prevent the prompt and improve diagnostics when one still occurs.
- Do not change Claude Code or Codex adapter behavior.

## Decisions

1. Treat missing repo guidance as an explicit non-fatal path in OpenCode worker prompts.

   The agent files should say to read repo-root `AGENTS.md` if it exists, continue when it does not, and not search parent or external directories for it. This gives the model a deterministic fallback and removes ambiguity that leads to broad `Glob "**/AGENTS.md"` calls above the workspace.

   Alternative considered: create an empty `AGENTS.md` in this repository. Rejected because it only fixes this repo and leaves installed workers fragile for any host project without that file.

2. Deny broad external directories by default in worker permissions.

   The OpenCode worker agent frontmatter should keep specific `~/.config/opencode/**` allows, but broad `external_directory` should be `deny` rather than `ask`. Headless plan workers should fail closed without entering an unanswerable interactive permission path.

   Alternative considered: allow all external directories. Rejected because phase workers do not need broad home-directory access, and allowing it weakens the safety boundary the adapter already declares.

3. Detect known permission rejection transcript markers in `parse_stage_json()`.

   The parser should still scan for a valid final JSON object first. If none exists, it should inspect sanitized transcript lines for OpenCode permission-denial markers such as `permission requested`, `auto-rejecting`, or `The user rejected permission`, and return a permission-specific parse reason.

   Alternative considered: make `run_logged_command()` classify non-zero process exits. Rejected for this change because the current runner does not capture return code in its outcome contract, and a non-zero exit alone still would not explain whether the failure was permission-related.

## Risks / Trade-offs

- Prompt wording may still be ignored by a model in rare cases -> Broad external-directory `deny` ensures the run fails closed rather than waiting on a prompt, and parser diagnostics make the failure actionable.
- Denying broad external directories could block a future valid worker need -> The current workers only require repo-local files and installed global prompts; future external needs should add narrow explicit allow rules and tests.
- Parser marker matching could misclassify unrelated transcript text -> Only apply the permission-specific reason when no JSON object is found, so valid worker JSON remains authoritative.

## Migration Plan

- Update repository adapter source files under `adapters/opencode/agents/`.
- Re-run the OpenCode installer so `~/.config/opencode/agents/` receives the hardened agent files.
- Restart any long-running OpenCode sessions so agent config is reloaded.
- Reset and rerun affected failed plan changes after preserving or reviewing existing dirty worktree changes.

## Open Questions

None.
