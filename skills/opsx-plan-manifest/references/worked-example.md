# Worked example: five planned changes → two runnable

A real case from the knowledge-forge repo, July 2026. It is a good example
because none of the interesting decisions come from the markdown — every one
comes from reconciling the plan against the repository.

## The input

`docs/plans/perms-and-search-plan.md` described five dependency-ordered changes:

```
1  fix-opencode-attached-vault-permissions
2  add-kf-context-roots-read-write
3  add-kf-context-lexical-search
4  add-kf-context-vector-search
5  add-kf-context-mcp-wrapper
```

A verbatim transcription would produce five `[[changes]]` entries in a chain.
That manifest would have been wrong in four of the five entries.

## Reconciling

**Change 1 — already shipped, with no archive entry.** `openspec/changes/` had
no directory for it, and neither did `archive/`. But the code was in place:
`templates/local/opencode.json.tmpl` rendered `{{EXTERNAL_DIRECTORY_JSON}}` and
`outputs.py` built the interactive vault roots. `git log` on that file showed
commit `bb938a4` implementing it directly, never as an OpenSpec change.

This is the case that catches people. "No archive entry" reads as "not done" if
you only check `openspec/`. Check the code and the history too when a plan
claims something shipped.

**Change 2 — deferred after inspecting the dependency.** The plan made search
depend on a `knowledge_forge.context` package providing root resolution and
authorized read/write. But root resolution already existed as
`resolve_domain_search_targets()`, and search needs read-only containment rather
than a write-authorization layer. The dependency was real in the document and
not real in the code. Nothing else depended on it, so it came out of the graph
entirely.

Worth stating plainly: this is a judgment call that changes the plan, not just
the manifest. It belongs in the plan doc as a recorded decision before it
belongs in the TOML.

**Change 3 — split.** As written it bundled a CLI contract, two retrieval
engines, ranking, incremental indexing, and a new runtime storage contract. Two
changes with a clean seam: `add-kf-grep-search` (no persistent state, one
capability) and `add-kf-indexed-search` (adds the storage contract and a second
capability).

**Change 4 — deferred but load-bearing.** Vector search is real future work and
change 5 depends on it, so the edge has to stay. `enabled = false`.

**Change 5 — blocked on something outside the plan.** An unrelated active
change, `add-kf-mcp-server`, shipped its own `note_search` tool with an
independent filesystem scanner — a second search implementation. Resolving that
overlap is a decision, not a task. `enabled = false` with the unblock condition
named in a comment.

## The result

Five planned changes became four entries, two of them enabled:

```toml
[plan]
name = "kf-search"
adapter = "opencode"
plan_doc = "docs/plans/perms-and-search-plan.md"
create_invoke = 'opencode run "/opsx:ff {change} --plan {plan_doc}"'
created_check = "openspec validate {change} --strict"
review_created = true
timeout_minutes = 60
fast_checks = [".venv/bin/kf devcheck"]

[[changes]]
id = "add-kf-grep-search"
phase = 1

[[changes]]
id = "add-kf-indexed-search"
phase = 2
depends_on = ["add-kf-grep-search"]
pause_before = true   # new runtime storage contract + cleanup retention class

[[changes]]
id = "add-kf-vector-search"
phase = 3
depends_on = ["add-kf-indexed-search"]
pause_before = true   # embedding-provider trust policy is a human decision
enabled = false       # deferred: out of the narrowed search-only scope

[[changes]]
id = "add-kf-search-mcp-wrapper"
phase = 4
depends_on = ["add-kf-grep-search", "add-kf-indexed-search"]
pause_before = true   # requires the add-kf-mcp-server hold to be resolved
enabled = false       # blocked: see openspec/changes/add-kf-mcp-server
```

Notes on the `[plan]` choices:

- `/opsx:ff`, not `/opsx-ff`. The controller's own `plan.example.toml` uses the
  hyphenated form; this repo registers the skill as `opsx:ff`. Checked the
  registration rather than copying the example.
- `fast_checks` is one command. `AGENTS.md` names `kf devcheck` as the canonical
  gate, and it already runs OpenSpec structural validation, semantic duplicate
  validation, package-boundary checks, ruff, and the fast suite. Listing those
  separately would run them twice.
- `.venv/bin/kf`, not `kf`. There was also a `kf` on the host `PATH` at
  `~/.local/bin/kf` — a different install. The repo-relative path removes the
  ambiguity.
- `pause_before` on 3b but not 3a. 3a is the larger change but carries no
  judgment call; 3b defines a storage contract and a cleanup retention class
  that later work inherits.

## Verifying

```
$ opsx-plan.py status docs/plans/perms-and-search-plan.toml
plan: kf-search
  P1 add-kf-grep-search         ready
  P2 add-kf-indexed-search      pending
  P3 add-kf-vector-search       skipped
  P4 add-kf-search-mcp-wrapper  skipped

$ audit_manifest.py docs/plans/perms-and-search-plan.toml --repo .
audit: clean -- ... 4 change(s), all reconciled against openspec/
```

Both matter. `status` proves the graph resolves and each change is in the
intended state — one `ready`, one `pending` behind it, two `skipped`. The audit
proves no key is silently ignored and no id names finished work.

Note that all four ids report `missing` from the audit's perspective and that is
correct here: none of the changes exist yet, and `create_invoke` is configured to
author them. The audit only fails on a missing id when there is no authoring
stage to produce it.
