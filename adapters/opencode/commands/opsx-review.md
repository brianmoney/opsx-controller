---
description: Review an OpenSpec change and decide whether it is ready to archive or needs a fix prompt
---

Review an OpenSpec change and decide whether it is ready to archive or needs more implementation work.

This command should thoroughly inspect the change artifacts and implementation, then choose one outcome:
- Recommend archiving the change
- Generate a concise prompt for the implementing agent to finish the remaining work

**Input**: Optionally specify a change name after `/opsx-review` (for example, `/opsx-review add-auth`). If omitted, check if it can be inferred from conversation context. If vague or ambiguous you MUST prompt for available changes.

**Steps**

1. **If no change name provided, prompt for selection**

   Run `openspec list --json` to get available changes. Use the **AskUserQuestion tool** to let the user select.

   Show active changes only. Include schema if available. Prefer changes with tasks or visible implementation progress.

   **IMPORTANT**: Do NOT guess or auto-select a change when multiple candidates exist.

2. **Check status to understand the workflow**

   ```bash
   openspec status --change "<name>" --json
   ```

   Parse the JSON to understand:
   - `schemaName`
   - `artifacts` and their status
   - Whether the change appears artifact-complete

3. **Load the change context**

   ```bash
   openspec instructions apply --change "<name>" --json
   ```

   Read all files from `contextFiles`. If delta specs exist under `openspec/changes/<name>/specs/` and were not included, read them too.

4. **Summarize the intended change**

   Build a short summary from the artifacts:
   - What the change was meant to do
   - What requirements or capabilities it covers
   - What tasks were supposed to be completed

5. **Review the implementation**

   Run:

   ```bash
   openspec validate "<name>" --strict
   ```

   Treat validation failures as **CRITICAL** unless they are clearly unrelated to the active change.

   Check:
   - Task completion vs actual code
   - Requirement implementation evidence in the codebase
   - Important scenario coverage and tests
   - Alignment with any design decisions
   - How closely the implementation matches the artifact instructions

   Use file references for important findings.

6. **Classify findings**

   Use:
   - **CRITICAL** for missing or materially incorrect work
   - **WARNING** for partial coverage, missing tests, or notable divergence
   - **NOTE** for minor cleanup or stale artifacts

7. **Decide the outcome**

   The review gate is strict: any **CRITICAL**, **WARNING**, or **NOTE** finding is blocking.

   **Recommend archive** only if the change intent is satisfied, strict OpenSpec validation passes, and there are no CRITICAL, WARNING, or NOTE findings.

   **Generate a concise implementation prompt** if there are any findings. The prompt should tell the implementing agent exactly what remains, cite the most relevant files, and state what must be true before archive.

8. **Output format**

   ```markdown
   ## Change Review: <change-name>

   ### Change Summary
   <concise summary>

   ### Implementation Assessment
   - **Overall:** Strong | Partial | Off-track
   - **Tasks:** X/Y complete
   - **Requirements:** M/N evidenced in code
   - **Tests:** Adequate | Partial | Missing for key paths

   ### Alignment With Instructions
   - <concise assessment>

   ### Findings
   - **CRITICAL:** <finding>
   - **WARNING:** <finding>
   - **NOTE:** <finding>

   ### Decision
   - **Archive Recommendation:** Ready to archive
   ```

   Or:

   ```markdown
   ### Decision
   - **Archive Recommendation:** Not ready

   ### Prompt For Implementing Agent
   Implement the remaining work for OpenSpec change `<change-name>`. Finish: (1) <gap>, (2) <gap>, (3) <gap>. Check `<file>:<line>` and `<file>:<line>`. The change is ready only when the implementation matches the artifacts and key scenarios are covered.
   ```

**Guardrails**

- Always end with one clear decision
- Be thorough in the review, but keep the final summary concise
- Do not recommend archive if any CRITICAL, WARNING, or NOTE finding remains
- Do not produce a long rewrite plan; produce a short, actionable implementation prompt instead
- Suggest `/opsx-apply <name>` when fixes are needed
- Suggest `/opsx-archive <name>` when the change is ready
