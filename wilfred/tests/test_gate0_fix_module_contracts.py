"""Static module contracts of control review: public dependencies, no import cycle.

Control review verification must depend only on documented public names of its sibling
modules, the general regression gate must not import the control module at all (its caller
supplies the control authority), and the control-helper path literal has one definition.
"""

import ast
import importlib
import inspect
from pathlib import Path

from hyperwilfred_core import control_evolution, regression

PACKAGE = Path(control_evolution.__file__).resolve().parent


def _tree(module) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def test_regression_never_imports_the_control_module() -> None:
    mentions = []
    for node in ast.walk(_tree(regression)):
        if isinstance(node, ast.Import):
            mentions += [a.name for a in node.names if "control_evolution" in a.name]
        elif isinstance(node, ast.ImportFrom):
            mentions += [f"{node.module}:{a.name}" for a in node.names
                         if "control_evolution" in f"{node.module}.{a.name}"]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A dynamic import by name would still be an import of the control module.
            mentions += [node.value] if "control_evolution" in node.value else []
        elif isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            mentions += [name] if "control_evolution" in name else []
    assert mentions == []


def test_control_module_uses_only_public_exported_names_of_sibling_modules() -> None:
    tree = _tree(control_evolution)
    aliases: dict[str, str] = {}
    used: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "hyperwilfred_core":
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"hyperwilfred_core.{alias.name}"
        elif (isinstance(node, ast.ImportFrom) and node.module
              and node.module.startswith("hyperwilfred_core.")):
            used += [(node.module, alias.name) for alias in node.names]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("hyperwilfred_core."):
                    aliases[alias.asname or alias.name] = alias.name
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in aliases):
            used.append((aliases[node.value.id], node.attr))
    assert {"hyperwilfred_core.approvals", "hyperwilfred_core.regression"} <= {
        module for module, _name in used}
    private = sorted({f"{module}.{name}" for module, name in used if name.startswith("_")})
    assert private == []
    unexported = sorted(
        f"{module}.{name}" for module, name in set(used)
        if name not in getattr(importlib.import_module(module), "__all__", ()))
    assert unexported == []


def test_control_helper_path_literal_is_defined_once() -> None:
    owners = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value == control_evolution.CONTROL_PATH:
                owners.append(path.relative_to(PACKAGE).as_posix())
    assert owners == ["control_evolution.py"]


def test_compare_receives_control_authority_instead_of_locating_it() -> None:
    parameters = inspect.signature(regression.compare).parameters
    assert "control_authority" in parameters
    assert parameters["control_authority"].default is None
    for keyword in ("approved_transitions", "base_commit"):
        assert keyword in parameters
    for located in ("repo_root", "run_id", "baseline_sha256", "review_public_key_path"):
        assert located not in parameters
