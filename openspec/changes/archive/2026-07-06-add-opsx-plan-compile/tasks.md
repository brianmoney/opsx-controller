## 1. Compile Command Plumbing

- [x] 1.1 Add an `opsx-plan compile` argparse subcommand with `source`, `-o/--output`, and `--force` options.
- [x] 1.2 Add compile command validation for missing source files, non-markdown source paths, existing output without `--force`, and missing or empty `OPSX_CONTROLLER_MODEL`.
- [x] 1.3 Add helper functions for resolving compile paths relative to the current repo and preserving clear error messages through `PlanError`.

## 2. Prompt And Reference Bundle

- [x] 2.1 Implement a compile prompt builder that embeds the source markdown plan content.
- [x] 2.2 Include manifest schema guidance covering `[plan]`, `[[changes]]`, dependency edges, gates, adapter defaults, and fields consumed by `load_plan()`.
- [x] 2.3 Discover and inject available repository template plan pairs, including `openspec/plans/*.md` with matching `.toml` examples when present.
- [x] 2.4 Instruct the model to emit only TOML and to preserve dependency semantics, manual gates, phase numbers, and `plan_doc` references.

## 3. OpenCode Invocation And Output Handling

- [x] 3.1 Invoke OpenCode for compile using the model from `OPSX_CONTROLLER_MODEL` and pass the prompt without requiring an interactive session.
- [x] 3.2 Capture OpenCode stdout/stderr and return a clear failure when the process exits non-zero or cannot be spawned.
- [x] 3.3 Extract a TOML payload from raw model output or a fenced `toml` block while rejecting empty or ambiguous output.

## 4. Manifest Validation And Write Safety

- [x] 4.1 Parse generated TOML with `tomllib` and validate it through the existing `load_plan()` path before reporting success.
- [x] 4.2 Write valid output through a temporary file and atomic replacement so invalid output never replaces an existing manifest.
- [x] 4.3 Print a concise success message with the output path and enough summary details for operators to run `opsx-plan status` next.

## 5. Documentation And Tests

- [x] 5.1 Update repository documentation to describe `opsx-plan compile`, `OPSX_CONTROLLER_MODEL`, `--force`, validation behavior, and an example compile command.
- [x] 5.2 Add unit tests for prompt construction, template injection, missing model failure, existing-output protection, successful compile write, invalid TOML rejection, and invalid dependency rejection.
- [x] 5.3 Add CLI parser coverage proving `compile` appears in help and routes to the compile command handler.
- [x] 5.4 Run `openspec validate add-opsx-plan-compile --strict` and the relevant orchestrator test subset.
