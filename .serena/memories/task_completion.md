# Completion checks
- For orchestrator or adapter changes, run both `python3 -m unittest discover -t . -s tests` and `node tests/opencode/test-opsx-usage-emitter.js` from the repository root.
- For OpenSpec artifact changes, run `openspec validate <change> --strict` or `openspec validate --all` as appropriate.
- New Python test packages require `__init__.py` so unittest discovery includes them.
- After code touching installed-runtime sources, maintainer must reinstall the relevant adapter with `--global --verify` after merge.