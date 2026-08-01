# TODO

## Compiler feeds archived plan pairs into the compile prompt

`discover_template_pairs()` includes `openspec/plans/archived/` md+toml pairs
as "Repository template plans" in the compile prompt. Archived manifests
preserve historical command forms that drift as the tooling evolves — four
archived TOMLs use the retired `/opsx-ff` create command while only the
canonical sample uses `/opsx-propose`. The compile model copies the majority
pattern instead of checking the repo's actual command registration
(`.opencode/commands/`), producing a `create_invoke` that bombs at the first
create attempt (observed 2026-08-01 compiling
`plan-authoring-and-surface-trim-plan.md`).

Candidate fixes: exclude archived pairs from template discovery (live pairs +
canonical sample should suffice), or keep them with an explicit prompt warning
that archived examples may reference retired commands and `create_invoke` must
be checked against the repo's registered commands/skills.

### Places needing change

- `lib/orchestrator/compiler.py:108-126` — `discover_template_pairs()`
- `lib/orchestrator/compiler.py:304-320` — `build_compile_prompt()` template
  section
- `tests/orchestrator/test_compiler.py` — prompt-construction tests

## Rename controller_model / controller role

The `controller` role in `models.toml` is misleading — it's not about the
orchestrator/controller process, it's about the model that authors plan
artifacts (compile prompt, create_invoke, legacy nested-controller dispatch).

Candidates: `author`, `creator`, `composer`, `generator`.

### Places needing change

- `models.example.toml:26,33` — role key + examples
- `lib/models/types.py:9` — `ROLES` tuple
- `lib/models/resolver.py:82-85` — `ResolvedModel` lookups
- `lib/orchestrator/compiler.py:66-105` — `check_controller_model()` + its callers
- `orchestrator/opsx-plan.py:455-457` — `{controller_model}` substitution comment
- `orchestrator/opsx-plan.py:1922-1923` — `{controller_model}` in `create_invoke` template
- `orchestrator/opsx-plan.py:491` — `OPSX_CONTROLLER_MODEL` env export via `apply_model_env`
- `docs/` — multiple files reference `controller` as a role
- `openspec/plans/archived/operator-workflow-upgrades-plan.toml:10` — `{controller_model}` in archived plan
- `tests/` — ~50 references to `OPSX_CONTROLLER_MODEL`, `controller` role lookups

### Open questions

- Does `{controller_model}` in existing user plans (invoke/create_invoke fields)
  need backward compat, or is a breaking rename acceptable?
- Which name: `author`, `creator`, `composer`, `generator`, or something else?

## User pricing override (`~/.config/opsx-controller/pricing.toml`)

Allow users to override/add pricing entries via a personal catalog file that
merges with the repo catalog (user entries supersede on `(provider, model_id)`
collision). Missing file is a no-op.

### Places needing change

- `lib/pricing/loader.py:82-98` — `PricingCatalog.__init__`: add optional
  `user_catalog_path` param; after loading repo catalog, load user file and
  merge entries by `(provider, model_id)` key (user overwrites repo for same
  key; user-only entries are appended).
- `lib/orchestrator/cost.py:21-37` — `_get_catalog()`: resolve
  `~/.config/opsx-controller/pricing.toml` and pass it to `PricingCatalog()`.
- `tests/lib/pricing/test_loader.py` — merge-behavior tests: user entry
  supersedes matching repo entry, user-only entry added, missing/empty user
  file is a no-op, user file with only metadata still works.
- `tests/orchestrator/test_cost.py` — end-to-end test with a temp user
  override file wired through `_get_catalog` / `_set_catalog`.
