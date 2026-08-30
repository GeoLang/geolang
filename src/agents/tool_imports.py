"""Which third-party packages a tool module needs before it can be loaded.

The tools import their heavy dependencies inside their function bodies, so
importing a module says nothing about whether calling it will raise
ModuleNotFoundError. Reading the source answers that without running it.
"""
from __future__ import annotations

import ast
import sys
from importlib.util import find_spec
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

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


def _collect(node: ast.AST, names: set[str], wrappers: set[ast.Try]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Try) and _guards_a_missing_package(child, wrappers):
            continue
        if isinstance(child, ast.Import):
            names.update(alias.name.split(".")[0] for alias in child.names)
        elif isinstance(child, ast.ImportFrom):
            if child.level == 0 and child.module:
                names.add(child.module.split(".")[0])
        else:
            _collect(child, names, wrappers)


def _is_repo_package(name: str) -> bool:
    return (REPO_ROOT / name).is_dir() or (REPO_ROOT / f"{name}.py").is_file()


def required_packages(source: str) -> set[str]:
    """Top-level packages the source imports and cannot run without.

    Relative imports, the standard library, this repo's own packages and
    imports a try guards against the package being absent are all left out.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    _collect(tree, names, _function_body_wrappers(tree))
    return {
        name
        for name in names
        if name not in sys.stdlib_module_names and not _is_repo_package(name)
    }


def missing_packages(source: str) -> list[str]:
    """The required packages that are not installed, named as you install them."""
    missing = [name for name in required_packages(source) if find_spec(name) is None]
    return sorted(DISTRIBUTION_NAMES.get(name, name) for name in missing)
