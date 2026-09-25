"""Executable import and packaging contracts, including deferred imports."""

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "nstream"


def _graph():
    graph: dict[str, set[str]] = {}
    for path in SOURCE.rglob("*.py"):
        if path.name == "_bench.py":
            continue  # development-only entrypoint, explicitly excluded from the wheel
        module = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
        package = module.rsplit(".", 1)[0]
        if module.endswith(".__init__"):
            module = package
        edges = graph.setdefault(module, set())
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                edges.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    prefix = package.split(".")[: len(package.split(".")) - node.level + 1]
                    base = ".".join(prefix + ([base] if base else []))
                edges.add(base)
                edges.update(f"{base}.{alias.name}" for alias in node.names)
    return graph


def test_runtime_dependencies_are_stdlib_only():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert metadata["project"]["dependencies"] == []
    for module, edges in _graph().items():
        assert all(
            edge.split(".")[0] in sys.stdlib_module_names | {"nstream"} for edge in edges if edge
        ), module


def test_nothing_below_entrypoint_imports_cli():
    for module, edges in _graph().items():
        if module not in {"nstream.cli", "nstream.__main__"}:
            assert "nstream.cli" not in edges, module


def test_import_graph_has_no_cycles():
    graph = _graph()
    visited: set[str] = set()

    def visit(module, stack):
        assert module not in stack, " -> ".join([*stack, module])
        if module in visited:
            return
        for edge in graph[module]:
            if edge in graph:
                visit(edge, [*stack, module])
        visited.add(module)

    for module in graph:
        visit(module, [])
