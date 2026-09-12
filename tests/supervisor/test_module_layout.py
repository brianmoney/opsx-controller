"""Verify ``lib/supervisor/*.py`` inter-module dependency discipline.

Mirrors the orchestrator package's mechanical layout check:

- The inter-module import graph is acyclic (no circular imports).
- No ``from lib.supervisor.<module> import <name>`` between supervisor
  modules — every cross-module reference goes through the module object.
- No supervisor module imports another runtime package (``lib.orchestrator``,
  ``lib.metrics``, ``lib.pricing``, ``lib.models``).
- Every supervisor module imports without running the CLI, spawning a
  process, or touching ``.opsx-plan/``.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
import unittest
from collections import defaultdict, deque
from pathlib import Path
from unittest import mock

from lib.supervisor import clock as clock_module
from lib.supervisor import ledger as ledger_module

REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_PKG = REPO_ROOT / "lib" / "supervisor"

_OTHER_RUNTIME_PACKAGES = ("lib.orchestrator", "lib.metrics", "lib.pricing", "lib.models")


def _module_name(filepath: Path) -> str:
    return filepath.stem


def _collect_import_edges(filepath: Path) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    from_mod = _module_name(filepath)
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("lib.supervisor."):
                    edges.append((from_mod, alias.name.rsplit(".", 1)[-1]))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("lib.supervisor."):
                edges.append((from_mod, node.module.rsplit(".", 1)[-1]))
            elif node.module == "lib.supervisor":
                for alias in node.names:
                    edges.append((from_mod, alias.name))
    return edges


def _collect_name_imports(filepath: Path) -> list[tuple[str, str, str]]:
    name_imports: list[tuple[str, str, str]] = []
    from_mod = _module_name(filepath)
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("lib.supervisor."):
                to_mod = node.module.rsplit(".", 1)[-1]
                for alias in node.names:
                    name_imports.append((from_mod, to_mod, alias.name))
    return name_imports


def _collect_runtime_package_imports(filepath: Path) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            for pkg in _OTHER_RUNTIME_PACKAGES:
                if name == pkg or name.startswith(pkg + "."):
                    violations.append(f"{filepath.name}: imports {name}")
    return violations


def _has_cycle(edges: list[tuple[str, str]]) -> bool:
    in_degree: dict[str, int] = defaultdict(int)
    adj: dict[str, list[str]] = defaultdict(list)
    nodes: set[str] = set()
    for src, dst in edges:
        adj[src].append(dst)
        in_degree[dst] += 1
        nodes.add(src)
        nodes.add(dst)
    queue = deque(n for n in nodes if in_degree[n] == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
    return visited != len(nodes)


def _supervisor_modules() -> list[Path]:
    return sorted(p for p in SUPERVISOR_PKG.glob("*.py") if not p.name.startswith("__"))


class SupervisorModuleLayoutTests(unittest.TestCase):
    def test_acyclic_inter_module_dependencies(self) -> None:
        edges: list[tuple[str, str]] = []
        for pyfile in _supervisor_modules():
            edges.extend(_collect_import_edges(pyfile))
        edges = [(src, dst) for src, dst in edges if src != dst]
        self.assertFalse(
            _has_cycle(edges),
            "lib/supervisor inter-module dependency graph is cyclic:\n"
            + "\n".join(f"  {s} -> {d}" for s, d in sorted(edges)),
        )

    def test_no_name_imports_between_modules(self) -> None:
        violations: list[str] = []
        for pyfile in _supervisor_modules():
            for from_mod, to_mod, name in _collect_name_imports(pyfile):
                violations.append(
                    f"  {from_mod}.py: from lib.supervisor.{to_mod} import {name}"
                )
        self.assertEqual(
            violations,
            [],
            "cross-module references must go through the module object:\n"
            + "\n".join(violations),
        )

    def test_no_other_runtime_package_imports(self) -> None:
        violations: list[str] = []
        for pyfile in _supervisor_modules():
            violations.extend(_collect_runtime_package_imports(pyfile))
        self.assertEqual(
            violations,
            [],
            "lib/supervisor must depend only on the standard library",
        )

    def test_every_module_imports_without_side_effects(self) -> None:
        opsx_marker = REPO_ROOT / ".opsx-plan"
        existed = opsx_marker.exists()
        fresh_names: list[str] = []
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("spawn")):
            with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawn")):
                for pyfile in _supervisor_modules():
                    name = f"lib.supervisor.{pyfile.stem}"
                    # Import fresh rather than returning the cached module, so
                    # import-time side effects would actually fire.
                    saved = sys.modules.pop(name, None)
                    try:
                        module = importlib.import_module(name)
                        self.assertEqual(module.__name__, name)
                        fresh_names.append(name)
                    finally:
                        if saved is not None:
                            sys.modules[name] = saved
        self.assertEqual(len(fresh_names), len(_supervisor_modules()))
        if not existed:
            self.assertFalse(
                opsx_marker.exists(),
                "importing lib.supervisor must not create .opsx-plan/",
            )

    def test_cross_module_call_observes_rebound_definition(self) -> None:
        """A rebind on the owning module reaches code in another module."""
        marker = "2001-01-01T00:00:00+00:00"
        original = clock_module.utcnow
        try:
            clock_module.utcnow = lambda: marker  # type: ignore[assignment]
            self.assertEqual(ledger_module._utcnow(), marker)
        finally:
            clock_module.utcnow = original  # type: ignore[assignment]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
