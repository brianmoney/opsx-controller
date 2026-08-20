# bound-compile-prompt Tasks

## 1. Bound the compile prompt

- [ ] 1.1 In `lib/orchestrator/compiler.py`, add module constant `COMPILE_PROMPT_BUDGET_CHARS = 128_000` with a docstring explaining the budget, and an inline-argv threshold constant (e.g. `MAX_INLINE_ARG_CHARS = 100_000`).
- [ ] 1.2 Change `discover_template_pairs()` to scan only top-level `openspec/plans/*.md` pairs; remove the `openspec/plans/archived/` scan entirely and update the docstring.
- [ ] 1.3 Add `_select_repo_template_pair(repo, available_chars)` returning the smallest active md+toml pair whose combined size fits `available_chars`, or `None`.
- [ ] 1.4 Restructure `build_compile_prompt()` into priority-ordered assembly: (1) always include source markdown, schema guidance, compile instructions (log a warning if the source alone exceeds the budget); (2) include the canonical sample pair only if it fits the remaining budget, logging a note when omitted; (3) include one repository template pair via `_select_repo_template_pair()` only if it fits, logging a note when omitted.

## 2. Fix Claude Code invocation and add the pre-spawn guard

- [ ] 2.1 Add `prompt_transport` to `COMPILE_CLIENTS` (`"file"` for opencode, `"stdin"` for claude-code) and remove `{prompt}` from the claude-code `argv_template` so it becomes `["{executable}", "-p", "--model", "{model}"]`.
- [ ] 2.2 Update `run_compile_client()` to pass the prompt via `subprocess.run(input=prompt, ...)` for the stdin transport; keep the opencode workspace-local `--file` behavior unchanged.
- [ ] 2.3 Add the pre-spawn argv-size guard: after argv construction, raise `PlanError` naming the adapter and stating the prompt is too large for argv delivery when any single argv element exceeds `MAX_INLINE_ARG_CHARS`.

## 3. Expose the compile timeout knob

- [ ] 3.1 Add a `timeout_minutes: float = 10.0` parameter to `run_compile_client()` replacing the hardcoded `timeout=600`; update the timeout `PlanError` message to name `--timeout-minutes`.
- [ ] 3.2 Add `--timeout-minutes` (float, default `10.0`) to the `compile` subparser in `orchestrator/opsx-plan.py` and pass it through `cmd_compile` to `run_compile_client`.

## 4. Tests

- [ ] 4.1 Add a reproduction test: synthetic repo with enough large archived plan pairs to exceed the old unbounded prompt size; assert the built prompt stays within the budget (plus fixed sections) and contains no archived content.
- [ ] 4.2 Add a test that archived pairs are excluded by `discover_template_pairs()` and never appear in the prompt even with budget remaining.
- [ ] 4.3 Add a budget-priority test: sizes crafted so examples cannot fit; assert source markdown and schema guidance remain present and omitted examples are dropped.
- [ ] 4.4 Add a Claude stdin test: mock `subprocess.run`, compile with an oversized prompt via the claude-code adapter; assert `input=prompt`, no argv element contains the prompt, and argv stays small.
- [ ] 4.5 Add a pre-spawn guard test: force an oversized inline-argv case; assert `PlanError` is raised and `subprocess.run` is never called.
- [ ] 4.6 Update the existing Claude argv tests (`test_build_argv_for_claude_code`, `test_build_argv_claude_never_includes_variant`) to the stdin contract, and update the timeout test for the new parameter and message; confirm the existing opencode `--file` tests pass unchanged.
- [ ] 4.7 Add an end-to-end test: in the large-archive synthetic repo, run `cmd_compile` with a mocked client returning valid TOML; assert the output passes `load_plan()` validation and is written.

## 5. Docs and verification

- [ ] 5.1 Update `orchestrator/README.md` "The compile stage" section: bounded prompt inclusion rules (source + schema + canonical sample + at most one small active repo template; archived excluded; budget), Claude stdin transport, and the `--timeout-minutes` flag with its default.
- [ ] 5.2 Run `python3 -m unittest discover -t . -s tests` and `node tests/opencode/test-opsx-usage-emitter.js` from the repo root; both suites pass.
- [ ] 5.3 Run `openspec validate bound-compile-prompt --strict`.
- [ ] 5.4 Verify against Knowledge Forge read-only from the checkout: `python3 orchestrator/opsx-plan.py --repo /home/brian/knowledge-forge compile docs/plans/gmail-dsh-cutover-plan.md -o /tmp/opsx-plan-selfcheck.toml --force` and the same with `--adapter claude-code`; confirm a bounded prompt size log and a successful compile or deterministic pre-spawn error (never a hang or `Errno 7`).
