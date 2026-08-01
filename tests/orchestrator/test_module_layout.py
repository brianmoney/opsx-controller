"""Verify lib/orchestrator/*.py inter-module dependency graph.

- Is acyclic (no circular imports).
- No ``from lib.orchestrator.<module> import <name>`` between orchestrator
  modules — every cross-module reference must go through the module object.
"""

from __future__ import annotations

import ast
import unittest
from collections import defaultdict, deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_PKG = REPO_ROOT / "lib" / "orchestrator"


def _module_name(filepath: Path) -> str:
    return filepath.stem  # e.g. "base", "groundtruth"


def _collect_import_edges(filepath: Path) -> list[tuple[str, str]]:
    """Return ``(from_module, to_module)`` edges found in *filepath*.

    Handles both ``import lib.orchestrator.foo`` and
    ``from lib.orchestrator import foo`` forms.
    """
    edges: list[tuple[str, str]] = []
    from_mod = _module_name(filepath)
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # ``import lib.orchestrator.foo``
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name.startswith("lib.orchestrator."):
                    to_mod = name.rsplit(".", 1)[-1]
                    edges.append((from_mod, to_mod))
        # ``from lib.orchestrator import foo, bar``
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("lib.orchestrator."):
                to_mod = node.module.rsplit(".", 1)[-1]
                edges.append((from_mod, to_mod))
            elif node.module == "lib.orchestrator":
                # Whole-package import — each name is a module reference
                for alias in node.names:
                    edges.append((from_mod, alias.name))
    return edges


def _collect_name_imports(filepath: Path) -> list[tuple[str, str, str]]:
    """Return ``(from_module, imported_module, imported_name)`` triples for
    any ``from lib.orchestrator.<module> import <name>`` discovered in
    *filepath*."""
    name_imports: list[tuple[str, str, str]] = []
    from_mod = _module_name(filepath)
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("lib.orchestrator."):
                to_mod = node.module.rsplit(".", 1)[-1]
                for alias in node.names:
                    name_imports.append((from_mod, to_mod, alias.name))
    return name_imports


def _has_cycle(edges: list[tuple[str, str]]) -> bool:
    """Topological-sort check — returns True iff a cycle exists."""
    in_degree: dict[str, int] = defaultdict(int)
    adj: dict[str, list[str]] = defaultdict(list)
    nodes: set[str] = set()
    for src, dst in edges:
        adj[src].append(dst)
        in_degree[dst] += 1
        nodes.add(src)
        nodes.add(dst)
    # Ensure nodes with zero in-degree are included
    for n in list(nodes):
        _ = in_degree[n]  # defaultdict, so missing keys default to 0
    q = deque(n for n in nodes if in_degree[n] == 0)
    visited = 0
    while q:
        n = q.popleft()
        visited += 1
        for neighbor in adj[n]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                q.append(neighbor)
    return visited != len(nodes)


class ModuleLayoutTests(unittest.TestCase):
    def test_acyclic_inter_module_dependencies(self) -> None:
        edges: list[tuple[str, str]] = []
        for pyfile in sorted(ORCHESTRATOR_PKG.glob("*.py")):
            if pyfile.name.startswith("__"):
                continue
            edges.extend(_collect_import_edges(pyfile))

        fq_edges: list[tuple[str, str]] = []
        for src, dst in edges:
            if src == dst:
                continue
            fq_edges.append((src, dst))

        if _has_cycle(fq_edges):
            from_nodes = {s for s, _ in fq_edges}
            to_nodes = {d for _, d in fq_edges}
            all_nodes = sorted(from_nodes | to_nodes)
            adj: dict[str, list[str]] = {n: [] for n in all_nodes}
            for s, d in fq_edges:
                adj[s].append(d)
            self.fail(
                f"Inter-module dependency graph is cyclic.\n"
                f"Edges ({len(fq_edges)}):\n"
                + "\n".join(f"  {s} -> {d}" for s, d in sorted(fq_edges))
            )

    def test_no_name_imports_between_modules(self) -> None:
        """Fail on any ``from lib.orchestrator.<mod> import <name>``."""
        violations: list[str] = []
        for pyfile in sorted(ORCHESTRATOR_PKG.glob("*.py")):
            if pyfile.name.startswith("__"):
                continue
            for from_mod, to_mod, name in _collect_name_imports(pyfile):
                violations.append(
                    f"  {from_mod}.py: from lib.orchestrator.{to_mod} import {name}"
                )
        if violations:
            self.fail(
                f"Name-import violations detected "
                f"(cross-module references must go through the module object):\n"
                + "\n".join(violations)
            )

    def test_expected_dependency_direction(self) -> None:
        """Confirm the graph has the expected shape documented in the design."""
        edges: list[tuple[str, str]] = []
        for pyfile in sorted(ORCHESTRATOR_PKG.glob("*.py")):
            if pyfile.name.startswith("__"):
                continue
            edges.extend(_collect_import_edges(pyfile))

        # Build adjacency for each module
        deps: dict[str, set[str]] = defaultdict(set)
        for src, dst in edges:
            if src == dst:
                continue
            deps[src].add(dst)

        # Phase 1 edges (already established)
        self.assertIn("dashboard", deps, "dashboard module missing from graph")
        self.assertIn("report", deps.get("dashboard", set()),
                      "dashboard → report edge missing")
        self.assertIn("planref", deps.get("report", set()),
                      "report → planref edge missing")
        self.assertIn("base", deps.get("planref", set()),
                      "planref → base edge missing")
        self.assertIn("base", deps.get("cost", set()),
                      "cost → base edge missing")

        # Phase 2 edges
        self.assertIn("base", deps.get("groundtruth", set()),
                      "groundtruth → base edge missing")
        self.assertIn("groundtruth", deps.get("state", set()),
                      "state → groundtruth edge missing")
        self.assertIn("state", deps.get("telemetry", set()),
                      "telemetry → state edge missing")
        self.assertIn("groundtruth", deps.get("delivery", set()),
                      "delivery → groundtruth edge missing")
        self.assertIn("groundtruth", deps.get("doctor", set()),
                      "doctor → groundtruth edge missing")
        self.assertIn("telemetry", deps.get("doctor", set()),
                      "doctor → telemetry edge missing")
        self.assertIn("base", deps.get("compiler", set()),
                      "compiler → base edge missing")
        self.assertIn("state", deps.get("logs", set()),
                      "logs → state edge missing")

        # No module should depend on entrypoint
        for mod, mod_deps in deps.items():
            self.assertNotIn("opsx_plan", mod_deps,
                             f"{mod} depends on entrypoint (opsx_plan)")


if __name__ == "__main__":
    unittest.main()
