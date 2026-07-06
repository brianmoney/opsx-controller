## Context

`opsx-plan` already runs TOML manifests and its documentation has started to refer to `opsx-plan compile`, but the orchestrator CLI currently exposes only `run`, `status`, `approve`, `accept`, `reset`, and `run-one`. Plan authors can write structured markdown implementation plans, and examples under `openspec/plans/` show the intended markdown-to-TOML relationship, but converting those documents into manifests is still manual.

The implementation surface is `orchestrator/opsx-plan.py`. The existing `load_plan()` function is the authoritative manifest parser and dependency validator, so the compile path should reuse it after generation instead of introducing a second TOML interpretation path.

## Goals / Non-Goals

**Goals:**
- Add `opsx-plan compile <plan.md> -o <plan.toml>` as a first-class CLI subcommand.
- Invoke OpenCode with the model resolved from `OPSX_CONTROLLER_MODEL` to perform the markdown-to-TOML transform.
- Provide the model enough context to produce a valid manifest: source markdown, TOML field expectations, dependency rules, current parser constraints, and representative markdown/TOML examples.
- Validate the generated TOML locally before reporting success.
- Keep tests deterministic by mocking subprocess/model invocation.

**Non-Goals:**
- Replacing the model-assisted transform with a full custom markdown parser.
- Running or accepting the compiled plan as part of `compile`.
- Creating OpenSpec changes from the compiled manifest.
- Supporting non-OpenCode compile adapters in this change.
- Changing direct implement/review/archive execution behavior.

## Decisions

1. Compilation will be model-assisted but locally validated.
Rationale: the source markdown is intentionally human-authored and prose-rich, while the TOML manifest is strict enough to validate with existing code. A model can do the semantic extraction, and Python can fail closed on invalid TOML, missing changes, bad dependency references, or empty manifests.
Alternatives considered: implement a deterministic markdown compiler. Rejected for this change because phase dependency wording, prose fields, and future plan style variations would create a larger parser project than the immediate operator need.

2. `OPSX_CONTROLLER_MODEL` is required for compile.
Rationale: the controller model is already the orchestrator-level model knob. Reusing it keeps compile behavior aligned with other controller-level planning work and avoids introducing a new environment variable.
Alternatives considered: rely on OpenCode's default model. Rejected because the user explicitly needs this path to invoke OpenCode using `OPSX_CONTROLLER_MODEL`, and explicit model selection makes compile runs reproducible.

3. The compiler will build an explicit reference bundle instead of relying on ambient repository access.
Rationale: the prompt should be self-contained enough for the model to produce the TOML without rediscovering context. The bundle should include the source markdown, a concise schema summary derived from `load_plan()` behavior, dependency-resolution rules from the authoring convention, current adapter defaults, and representative template pairs such as `openspec/plans/model-efficiency-telemetry-plan.md` plus its `.toml` manifest when available.
Alternatives considered: pass only an `@plan.md` file reference to OpenCode. Rejected because it omits the manifest shape and examples that prevent malformed or under-specified output.

4. Generated TOML will be written only after successful validation.
Rationale: operators should not receive a manifest that `opsx-plan run` cannot load. The compile path can capture model output in memory, extract a raw TOML payload or fenced TOML block, parse it with `tomllib`, and call `load_plan()` against a temporary file before atomically replacing the requested output path.
Alternatives considered: write model output directly and ask the operator to validate manually. Rejected because the orchestrator already has enough local validation hooks to fail earlier and preserve the previous output.

5. Output overwrite behavior will be explicit.
Rationale: compiled manifests may be edited by operators after generation. `compile` should refuse to overwrite an existing output unless `--force` is supplied, while still supporting `-o/--output` as the normal output path.
Alternatives considered: always overwrite. Rejected because accidental loss of operator-reviewed DAG gates is high impact.

## Risks / Trade-offs

- [Model output can contain prose or invalid TOML] -> Mitigation: instruct the model to emit TOML only, extract fenced TOML when present, and validate with `tomllib` plus `load_plan()` before writing.
- [Prompt context can become stale as `load_plan()` changes] -> Mitigation: generate the schema summary near the compile code and test that compile prompts mention the same critical fields consumed by `load_plan()`.
- [Template examples can be missing in downstream installations] -> Mitigation: discover repository template plans when available, include a small built-in manifest schema summary regardless, and fail only when the source markdown or OpenCode invocation is unavailable.
- [Subprocess tests could accidentally require live OpenCode] -> Mitigation: isolate command construction and model invocation behind small functions that unit tests can mock.

## Migration Plan

1. Add compile-specific helpers for model resolution, reference-bundle assembly, OpenCode invocation, TOML extraction, and validation.
2. Add the `compile` argparse subcommand with `source`, `-o/--output`, and `--force` options.
3. Wire compile to write through a temporary file and atomic replacement after validation.
4. Update README or plan workflow documentation with the new command and failure modes.
5. Add unit tests that mock OpenCode output and cover success, invalid output, missing model, existing output without `--force`, and reference injection.

## Open Questions

- Should a later change add a deterministic `--no-model` parser mode for strict plans that follow the machine-read convention exactly?
- Should compile record provenance comments in the TOML, or keep output purely as the model-produced manifest after validation?
