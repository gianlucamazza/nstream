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


# ADR 0037: domain modules decide; frontends (cli, headless, settings, series' TUI flow)
# render and prompt. The domain must not import the fzf picker or TUI labels.
_DOMAIN = {
    "api", "addons", "availability", "bridge", "cast_delivery", "cast_flow", "cast_vet",
    "caster", "debrid", "discovery", "engine", "mirror", "quality", "remux", "serve",
    "stream_select", "subs", "subalign", "tracks",
}  # fmt: skip
_TUI = {"picker", "labels"}


def test_domain_does_not_import_the_tui():
    found = set()
    for module, edges in _graph().items():
        name = module.removeprefix("nstream.")
        if name not in _DOMAIN:
            continue
        for tui in _TUI:
            if f"nstream.{tui}" in edges:
                found.add((name, tui))
    # The pre-0037 debt (caster/subs/stream_select menus) is paid: no exceptions remain.
    assert found == set(), "domain→TUI import (ADR 0037): inject the menu instead"


# Bottom tier: imported by everything, so it may depend only on itself (and the stdlib).
_BOTTOM = {"util", "log", "notices", "languages", "srt", "providers"}


def test_bottom_tier_imports_only_the_bottom_tier():
    graph = _graph()
    for name in _BOTTOM:
        own = {e.split(".")[1] for e in graph[f"nstream.{name}"] if e.startswith("nstream.")}
        assert own <= _BOTTOM, (name, own - _BOTTOM)
