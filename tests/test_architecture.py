"""Architectural invariant: the domain core has no outward dependencies.

The design is ports-and-adapters. ``iris_signal_harvester.domain`` is the "DOMAIN CORE -
no I/O dependencies" box, which is what lets the pipeline be tested without a network or
a database. This test turns that documented claim into an enforced one by parsing the
source of every module in the package (no import side effects) and walking the import
graph, directly and transitively through first-party modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

import iris_signal_harvester

ROOT_PKG = "iris_signal_harvester"
SRC = Path(iris_signal_harvester.__file__).resolve().parent

# First-party layers the domain core must never reach, directly or transitively.
FORBIDDEN_LAYERS = ("sink", "http", "adapters", "reporting")
# Third-party I/O libraries the domain core must not import either.
FORBIDDEN_THIRD_PARTY = ("httpx", "psycopg", "psycopg2", "requests", "aiohttp", "urllib3")


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _all_modules() -> dict[str, Path]:
    return {_module_name(p): p for p in SRC.rglob("*.py")}


def _imports(module: str, path: Path) -> set[str]:
    """Absolute dotted names imported by ``path`` (``from a import b`` yields ``a.b`` too)."""
    is_package = path.name == "__init__.py"
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                if node.level > 1:
                    base_parts = base_parts[: -(node.level - 1)]
                base = ".".join(base_parts)
                base = f"{base}.{node.module}" if node.module else base
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def _layer(name: str) -> str | None:
    parts = name.split(".")
    if parts[0] == ROOT_PKG and len(parts) > 1:
        return parts[1]
    return None


def _domain_modules() -> list[str]:
    return sorted(m for m in _all_modules() if m == f"{ROOT_PKG}.domain" or _layer(m) == "domain")


def test_domain_package_is_discovered() -> None:
    # Guards the guard: an empty module list would make every assertion below vacuous.
    modules = _domain_modules()
    assert f"{ROOT_PKG}.domain.normalise" in modules
    assert f"{ROOT_PKG}.domain.sanitize" in modules
    assert f"{ROOT_PKG}.domain.geo.resolver" in modules


def test_domain_does_not_import_outer_layers_directly() -> None:
    modules = _all_modules()
    offenders: list[str] = []
    for name in _domain_modules():
        for imported in sorted(_imports(name, modules[name])):
            if _layer(imported) in FORBIDDEN_LAYERS:
                offenders.append(f"{name} imports {imported}")
            if imported.split(".")[0] in FORBIDDEN_THIRD_PARTY:
                offenders.append(f"{name} imports {imported}")
    assert not offenders, "domain core depends outward:\n" + "\n".join(offenders)


def test_domain_does_not_reach_outer_layers_transitively() -> None:
    modules = _all_modules()
    graph = {
        name: {i for i in _imports(name, path) if i in modules} for name, path in modules.items()
    }
    seen: set[str] = set()
    stack = list(_domain_modules())
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.get(current, ()))
    reached = sorted(m for m in seen if _layer(m) in FORBIDDEN_LAYERS)
    assert not reached, f"domain core transitively reaches outer layers: {reached}"


def test_import_scanner_detects_a_violation(tmp_path: Path) -> None:
    # The scanner must actually see absolute, relative and from-import forms.
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from iris_signal_harvester.sink.sanitise import x\n"
        "from ..sink import postgis\n"
        "import iris_signal_harvester.http.client\n",
        encoding="utf-8",
    )
    found = _imports(f"{ROOT_PKG}.domain.probe", probe)
    assert {_layer(i) for i in found} >= {"sink", "http"}
    assert f"{ROOT_PKG}.sink.postgis" in found
