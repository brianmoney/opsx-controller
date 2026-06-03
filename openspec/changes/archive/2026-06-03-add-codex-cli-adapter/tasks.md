## 1. Create adapter directory structure

- [x] 1.1 Create `adapters/codex-cli/` with subdirectories: `skills/opsx-drive/`, `agents/`, `support/`, `templates/project/`, `plugin/.codex-plugin/`, `plugin/skills/opsx-drive/`, `plugin/agents/`

## 2. Create skill entrypoint (controller)

- [x] 2.1 Create `adapters/codex-cli/skills/opsx-drive/SKILL.md` — prompt-driven controller skill with full orchestration logic (parse change-id, manage state file at `.opsx-controller/`, seed context cache and tracked files, dispatch phase agents via spawn_agent/wait_agent, implement→review→archive loop, strict review gate, stop conditions, error handling)
- [x] 2.2 Create `adapters/codex-cli/skills/opsx-drive/agents/openai.yaml` — skill metadata with display name, description, and brand color

## 3. Create phase agent TOML definitions

- [x] 3.1 Create `adapters/codex-cli/agents/opsx-implementer.toml` — implement phase agent with `workspace-write` sandbox, `gpt-5.4` model, `high` effort, and full implementer instructions in `developer_instructions`
- [x] 3.2 Create `adapters/codex-cli/agents/opsx-reviewer.toml` — review phase agent with `read-only` sandbox, `gpt-5.4` model, `high` effort, strict classification rules, and single-line JSON output contract
- [x] 3.3 Create `adapters/codex-cli/agents/opsx-archiver.toml` — archive phase agent with `danger-full-access` sandbox, `gpt-5.4` model, `high` effort, explicit scope validation, delta spec sync, dir move, and commit logic

## 4. Create support and template files

- [x] 4.1 Create `adapters/codex-cli/support/opsx-controller-state-README.md` — state contract documentation adapted for Codex CLI paths (references `.opsx-controller/` and `.codex/agents/`)
- [x] 4.2 Create `adapters/codex-cli/templates/project/AGENTS.snippet.md` — snippet for project AGENTS.md with skill invocation guidance

## 5. Create install script

- [x] 5.1 Create `adapters/codex-cli/install.sh` supporting `--global` (copies to `$HOME/.agents/skills/` and `$HOME/.codex/agents/`), `--project <path>` (copies to `<path>/.agents/skills/` and `<path>/.codex/agents/`, creates `.codex/.gitignore`), and `--plugin` (creates plugin bundle directory)

## 6. Create plugin manifest

- [x] 6.1 Create `adapters/codex-cli/plugin/.codex-plugin/plugin.json` with name, version, description, author, skills path, and UI interface metadata

## 7. Create plugin bundle files

- [x] 7.1 Create `adapters/codex-cli/plugin/skills/opsx-drive/SKILL.md` — plugin-scoped copy of the controller skill
- [x] 7.2 Copy phase agent TOML files into `adapters/codex-cli/plugin/agents/`

## 8. Validation

- [x] 8.1 Verify all 8 adapter files exist and contain required content matching spec requirements
- [x] 8.2 Run `openspec validate add-codex-cli-adapter --strict` and ensure zero findings
- [x] 8.3 Verify install script handles `--global`, `--project`, and `--plugin` modes correctly
