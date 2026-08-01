"""Orchestrator runtime package.

Implementation modules backing the `opsx-plan` CLI entrypoint
(`orchestrator/opsx-plan.py`). Importing any submodule here must not parse
arguments, spawn a process, or touch `.opsx-plan/` — the entrypoint alone
owns those side effects.
"""
