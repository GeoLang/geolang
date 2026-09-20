"""Which third-party packages a tool module needs before it can be loaded.

The tools import their heavy dependencies inside their function bodies, so
importing a module says nothing about whether calling it will raise
ModuleNotFoundError. Reading the source answers that without running it.
"""
from __future__ import annotations

import ast
import sys
from importlib.machinery import PathFinder
from importlib.util import find_spec
from pathlib import Path

from src.core.qgis_session import QGIS_SYSTEM_PATHS

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIRECTORY = REPO_ROOT / "src" / "agents" / "tools"

# import name on the left, the name to install on the right, listed only where
# the two differ
DISTRIBUTION_NAMES = {
    "sklearn": "scikit-learn",
    "osgeo": "GDAL",
    "PIL": "Pillow",
    "yaml": "PyYAML",
}

IMPORT_ERROR_NAMES = {"ImportError", "ModuleNotFoundError"}
CATCH_ALL_NAME = "Exception"


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    """The exception names one handler catches, empty for a bare except."""
    if handler.type is None:
        return set()
    caught = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    return {node.id for node in caught if isinstance(node, ast.Name)}


def _function_body_wrappers(tree: ast.AST) -> set[ast.Try]:
    """The try statements that turn a whole tool body into an error string.

    A tool wraps everything it does in one try and returns the message from the
    handler, so that try says nothing about whether an import is optional.
    """
    wrappers = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        last = node.body[-1]
        if isinstance(last, ast.Try):
            wrappers.add(last)
    return wrappers


def _guards_a_missing_package(node: ast.Try, wrappers: set[ast.Try]) -> bool:
    for handler in node.handlers:
        names = _handler_names(handler)
        if names & IMPORT_ERROR_NAMES:
            return True
        catches_everything = not names or CATCH_ALL_NAME in names
        if catches_everything and node not in wrappers:
            return True
    return False


def _collect(
    node: ast.AST, names: set[str], wrappers: set[ast.Try], seen: set[str]
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Try) and _guards_a_missing_package(child, wrappers):
            continue
        if isinstance(child, ast.Import):
            names.update(alias.name.split(".")[0] for alias in child.names)
        elif isinstance(child, ast.ImportFrom):
            if child.level == 0 and child.module:
                names.add(child.module.split(".")[0])
            elif child.level == 1 and child.module:
                _collect_sibling(child.module, names, seen)
        else:
            _collect(child, names, wrappers, seen)


def _collect_sibling(module: str, names: set[str], seen: set[str]) -> None:
    """What a shared tool module needs, counted for whoever imports it."""
    if module in seen:
        return
    seen.add(module)
    path = TOOLS_DIRECTORY / f"{module}.py"
    if not path.is_file():
        return
    _collect_source(path.read_text(), names, seen)


def _collect_source(source: str, names: set[str], seen: set[str]) -> None:
    tree = ast.parse(source)
    _collect(tree, names, _function_body_wrappers(tree), seen)


def _is_repo_package(name: str) -> bool:
    return (REPO_ROOT / name).is_dir() or (REPO_ROOT / f"{name}.py").is_file()


def required_packages(source: str) -> set[str]:
    """Top-level packages the source imports and cannot run without.

    A relative import of a shared tool module is followed, so what `_sites` or
    `_isochrones` needs counts for every tool importing it. The standard
    library, this repo's own packages and imports a try guards against the
    package being absent are all left out.
    """
    names: set[str] = set()
    _collect_source(source, names, set())
    return {
        name
        for name in names
        if name not in sys.stdlib_module_names and not _is_repo_package(name)
    }


def _installed(name: str) -> bool:
    """On the default path, or the system paths qgis_session bridges at call time."""
    if find_spec(name) is not None:
        return True
    system_paths = [path for path in QGIS_SYSTEM_PATHS if Path(path).is_dir()]
    return PathFinder().find_spec(name, system_paths) is not None


def missing_packages(source: str) -> list[str]:
    """The required packages that are not installed, named as you install them."""
    missing = [name for name in required_packages(source) if not _installed(name)]
    return sorted(DISTRIBUTION_NAMES.get(name, name) for name in missing)
