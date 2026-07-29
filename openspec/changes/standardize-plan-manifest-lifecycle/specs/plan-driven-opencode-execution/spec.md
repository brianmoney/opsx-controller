## MODIFIED Requirements

### Requirement: Single-change runner executes without a plan manifest

The OpenCode adapter SHALL provide an `opsx-run <change-id>` command surface that starts or resumes the direct implement, review, and archive worker loop for exactly one existing accepted OpenSpec change without requiring a plan TOML manifest.

The runner SHALL synthesize the minimal one-change orchestration configuration needed by the existing direct OpenCode execution path and SHALL persist durable state under `.opsx-plan/`.

The runner SHALL additionally serialize that synthesized configuration to a derived manifest under `.opsx-plan/plans/` before dispatching any worker, so that reporting surfaces can resolve the run. The derived manifest is an output of the run, not an input to it: the runner SHALL NOT require, read, or consult any operator-supplied manifest, and SHALL NOT fail a run because no authored manifest exists.

The runner SHALL write the derived manifest only after its input guards have passed, so a run that is refused for a missing, unauthored, or dirty-worktree reason leaves no manifest behind.

#### Scenario: Operator runs one existing change
- **WHEN** an operator invokes `opsx-run vault-gardening-suggestions` from a repository with an authored `openspec/changes/vault-gardening-suggestions/` change
- **THEN** the runner dispatches the OpenCode implementer, reviewer, and archiver workers through the direct OpenCode loop without reading a plan manifest

#### Scenario: Equivalent script subcommand is available
- **WHEN** an operator invokes the orchestrator script through `opsx-plan run-one vault-gardening-suggestions`
- **THEN** the orchestrator uses the same single-change execution behavior as `opsx-run vault-gardening-suggestions`

#### Scenario: Run emits a derived manifest for reporting
- **WHEN** an operator invokes `opsx-run vault-gardening-suggestions` and the run's input guards pass
- **THEN** the runner writes `.opsx-plan/plans/run-vault-gardening-suggestions.toml` describing the configuration it is about to execute, before dispatching the first worker

#### Scenario: Refused run leaves no derived manifest
- **WHEN** an operator invokes `opsx-run vault-gardening-suggestions` in a repository whose tracked worktree is dirty and tracked-clean enforcement is enabled
- **THEN** the runner exits with its existing error and `.opsx-plan/plans/run-vault-gardening-suggestions.toml` is not created

#### Scenario: Completion names the reporting commands
- **WHEN** a single-change run finishes
- **THEN** the runner prints the `opsx-plan report` and `opsx-plan dashboard` invocations that target the run it just completed

### Requirement: Compile prompts include source and adapter reference context

The compile command SHALL provide the selected client with a self-contained
prompt that includes the source markdown plan, the expected TOML manifest
shape, dependency and phase interpretation rules, current selected-adapter
defaults, a canonical markdown/TOML sample plan pair, and representative
repository plan references when available in the repository.

The canonical sample pair SHALL be included in every compile prompt and SHALL be listed before any repository plan references, so that prompt example quality does not depend on the contents of the repository being compiled against.

Repository plan discovery SHALL consider pairs directly under `openspec/plans/` and pairs under `openspec/plans/archived/`, preferring non-archived pairs. Discovery SHALL NOT descend into any other nested directory, and derived manifests under `.opsx-plan/plans/` SHALL NOT be offered as references.

When a repository contains no plan pairs of its own, the prompt SHALL omit the repository reference section entirely rather than stating that no pairs were found.

If the canonical sample pair cannot be located, the compile command SHALL proceed without it rather than failing, and SHALL NOT substitute a claim that no examples exist.

The prompt SHALL instruct the model to emit only the compiled TOML manifest and not to include prose outside the TOML payload.

#### Scenario: Prompt contains the canonical sample and schema guidance
- **WHEN** `opsx-plan compile` builds a prompt for a source markdown plan
- **THEN** the prompt includes the source plan content, manifest field guidance for `[plan]` and `[[changes]]`, dependency-resolution guidance, and the canonical sample plan markdown paired with its compiled TOML

#### Scenario: Canonical sample is present regardless of repository contents
- **WHEN** `opsx-plan compile` builds a prompt in a repository that contains no plan pairs at all
- **THEN** the canonical sample pair is still included, and the prompt contains no statement that example plans were unavailable

#### Scenario: Repository pairs supplement the canonical sample
- **WHEN** `opsx-plan compile` builds a prompt in a repository containing pairs under `openspec/plans/` and under `openspec/plans/archived/`
- **THEN** the canonical sample is listed first, followed by the non-archived repository pairs, followed by the archived pairs

#### Scenario: Archived-only repository still contributes real-world context
- **WHEN** `opsx-plan compile` builds a prompt in a repository whose only plan pairs live under `openspec/plans/archived/`
- **THEN** those archived pairs are included as repository references after the canonical sample

#### Scenario: Missing canonical sample degrades rather than fails
- **WHEN** `opsx-plan compile` builds a prompt and the canonical sample pair cannot be resolved from the installed runtime or the repository checkout
- **THEN** the command builds the prompt without the canonical section and the compile proceeds

#### Scenario: Prompt forbids prose output
- **WHEN** the prompt is sent to the selected compile client
- **THEN** it instructs the model to return TOML only so the result can be validated and written without manual cleanup
