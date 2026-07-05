## 1. Worker Prompt Hardening

- [ ] 1.1 Update `adapters/opencode/agents/opsx-controller.md` to treat repo-root `AGENTS.md` as optional and forbid parent/external searches for missing guidance.
- [ ] 1.2 Update `adapters/opencode/agents/opsx-implementer.md` with the same optional guidance and no external search instructions.
- [ ] 1.3 Update `adapters/opencode/agents/opsx-reviewer.md` with the same optional guidance and no external search instructions.
- [ ] 1.4 Update `adapters/opencode/agents/opsx-archiver.md` with the same optional guidance and no external search instructions.

## 2. Worker Permission Hardening

- [ ] 2.1 Change broad `external_directory` defaults in OpenCode worker agent frontmatter from `ask` to `deny`.
- [ ] 2.2 Preserve explicit `~/.config/opencode` allow rules after the broad deny rule so installed global prompt reads remain available.
- [ ] 2.3 Add or update tests that assert OpenCode worker agents deny broad external directories while allowing `~/.config/opencode` reads.

## 3. Permission-Denial Diagnostics

- [ ] 3.1 Extend `parse_stage_json()` to detect permission-rejection transcript markers when no final JSON object is present.
- [ ] 3.2 Return an actionable parse reason that identifies permission denial before JSON output.
- [ ] 3.3 Ensure valid final JSON output remains authoritative even when earlier transcript lines contain noise.
- [ ] 3.4 Add parser regression tests for auto-rejected `external_directory` transcripts and noisy logs with final JSON.

## 4. Verification And Recovery Notes

- [ ] 4.1 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [ ] 4.2 Run `openspec validate harden-opencode-headless-workers --strict`.
- [ ] 4.3 Document or summarize the post-change operational step to reinstall OpenCode agents and restart long-running OpenCode sessions.
