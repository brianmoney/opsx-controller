# Commands
- Run unit tests from repo root: `python3 -m unittest discover -t . -s tests`.
- Run OpenCode emitter test: `node tests/opencode/test-opsx-usage-emitter.js`.
- Validate one change: `openspec validate <change> --strict`; validate all: `openspec validate --all`.
- Compile and operate plans with installed `opsx-plan`: `opsx-plan compile <plan.md>`, `opsx-plan run --dry-run`, `opsx-plan doctor`, `opsx-plan report`.
- Direct source entrypoint: `python3 orchestrator/opsx-plan.py ...`.
- Global deployment after merged adapter/orchestrator/plugin/skill/script changes uses the applicable installer with `--global --verify`; do not run maintainer deployment for ordinary contribution work.