"""The tests-directory relocation rule is written once and every baseline shape resolves as before.

The versioned and legacy baseline shapes are resolved by separate helpers; both, and the
compare-time relocation, derive the single witness head through one shared helper.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from hyperwilfred_core import approvals, regression

SHA_A, SHA_B, SHA_OLD = "a" * 64, "b" * 64, "c" * 64
IDS = ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]
SHAPE_HELPERS = ("baseline_scope", "_versioned_scope", "_legacy_scope", "_relocated_scope")


def _product(root: Path) -> Path:
    (root / "tests").mkdir(parents=True)
    for name in ("test_a.py", "test_b.py"):
        (root / "tests" / name).write_text("def test_one(): pass\n")
    return root


def _versioned(test_roots: list[str], hashes: dict[str, str], **extra: object) -> dict:
    return {"schema_version": regression.BASELINE_VERSION, "target": ".",
            "test_roots": test_roots, "test_config_sha256": approvals.digest(test_roots),
            "passing_ids": list(IDS), "passing": len(IDS), "test_file_hashes": hashes, **extra}


def _legacy(captured: Path, hashes: dict[str, str]) -> dict:
    return {"target": str(captured), "passing_ids": list(IDS), "passing": len(IDS),
            "test_file_hashes": hashes}


def test_legacy_tests_directory_shape_resolves_from_product_root(tmp_path: Path) -> None:
    product = _product(tmp_path / "product")
    baseline = _legacy(tmp_path / "capture" / "tests", {"test_a.py": SHA_A, "test_b.py": SHA_B})
    roots, ids, hashes = regression.baseline_scope(product, baseline)
    assert roots == (product.resolve() / "tests",)
    assert ids == set(IDS)
    assert hashes == {"tests/test_a.py": SHA_A, "tests/test_b.py": SHA_B}


def test_legacy_tests_directory_shape_resolves_at_the_tests_directory(tmp_path: Path) -> None:
    tests = _product(tmp_path / "product") / "tests"
    baseline = _legacy(tmp_path / "capture" / "tests", {"test_a.py": SHA_A, "test_b.py": SHA_B})
    roots, ids, hashes = regression.baseline_scope(tests, baseline)
    assert roots == (tests.resolve(),)
    assert ids == set(IDS)
    assert hashes == {"test_a.py": SHA_A, "test_b.py": SHA_B}


def test_legacy_repo_root_shape_discards_archived_hashes(tmp_path: Path) -> None:
    product = _product(tmp_path / "product")
    baseline = _legacy(tmp_path / "capture" / "repo", {
        "tests/test_a.py": SHA_A, "tests/test_b.py": SHA_B,
        ".hw/run/archive/tests/test_old.py": SHA_OLD, "test_stray.py": SHA_OLD})
    roots, ids, hashes = regression.baseline_scope(product, baseline)
    assert roots == (product.resolve() / "tests",)
    assert ids == set(IDS)
    assert hashes == {"tests/test_a.py": SHA_A, "tests/test_b.py": SHA_B}


def test_legacy_shape_with_two_heads_is_still_refused(tmp_path: Path) -> None:
    product = _product(tmp_path / "product")
    baseline = _legacy(tmp_path / "capture" / "repo", {"tests/test_a.py": SHA_A})
    baseline["passing_ids"] = [IDS[0], "other/test_c.py::test_three"]
    with pytest.raises(regression.RegressionError,
                       match="legacy baseline test scope is ambiguous or unsafe"):
        regression.baseline_scope(product, baseline)


def test_versioned_relocated_shape_resolves_to_the_tests_directory(tmp_path: Path) -> None:
    product = _product(tmp_path / "product")
    baseline = _versioned(["."], {"test_a.py": SHA_A, "test_b.py": SHA_B})
    roots, ids, hashes = regression.baseline_scope(product, baseline)
    assert roots == ((product / "tests").resolve(),)
    assert ids == set(IDS)
    assert hashes == {"tests/test_a.py": SHA_A, "tests/test_b.py": SHA_B}


def test_versioned_portable_shape_is_not_relocated(tmp_path: Path) -> None:
    product = _product(tmp_path / "product")
    baseline = _versioned(["tests"], {"tests/test_a.py": SHA_A, "tests/test_b.py": SHA_B})
    roots, ids, hashes = regression.baseline_scope(product, baseline)
    assert roots == ((product / "tests").resolve(),)
    assert ids == set(IDS)
    assert hashes == {"tests/test_a.py": SHA_A, "tests/test_b.py": SHA_B}


def test_compare_follows_a_relocated_witness_to_its_capture_boundary(tmp_path: Path) -> None:
    product = _product(tmp_path / "product")
    baseline = _versioned(["."], {"test_a.py": SHA_A, "test_b.py": SHA_B},
                          control_file_hashes={})
    moved = regression._relocated_scope(product.resolve(), baseline)
    assert moved is not None
    assert moved[0] == product.resolve() / "tests"
    assert moved[1]["passing_ids"] == ["test_a.py::test_one", "test_b.py::test_two"]


@pytest.mark.parametrize("change", ["symlink", "nested_hash", "no_controls", "unprefixed",
                                    "missing_head", "portable_roots"])
def test_compare_does_not_follow_an_unsafe_relocation(tmp_path: Path, change: str) -> None:
    product = _product(tmp_path / "product")
    baseline = _versioned(["."], {"test_a.py": SHA_A, "test_b.py": SHA_B},
                          control_file_hashes={})
    if change == "symlink":
        (product / "tests").rename(product / "real")
        (product / "tests").symlink_to(product / "real", target_is_directory=True)
    elif change == "nested_hash":
        baseline["test_file_hashes"] = {"tests/test_a.py": SHA_A}
    elif change == "no_controls":
        del baseline["control_file_hashes"]
    elif change == "unprefixed":
        baseline["passing_ids"] = [*IDS, "test_c.py::test_three"]
    elif change == "missing_head":
        (product / "tests").rename(product / "moved")
    else:
        baseline["test_roots"] = ["tests"]
    assert regression._relocated_scope(product.resolve(), baseline) is None


def _functions() -> dict[str, ast.FunctionDef]:
    module = ast.parse(inspect.getsource(regression))
    return {node.name: node for node in module.body if isinstance(node, ast.FunctionDef)}


def _called_names(node: ast.AST) -> set[str]:
    return {call.func.id for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}


def test_relocation_head_is_derived_in_one_helper() -> None:
    functions = _functions()
    for name in SHAPE_HELPERS:
        splits = [call for call in ast.walk(functions[name]) if isinstance(call, ast.Call)
                  and isinstance(call.func, ast.Attribute) and call.func.attr == "split"
                  and call.args and isinstance(call.args[0], ast.Constant)
                  and call.args[0].value == "/"]
        assert not splits, f"{name} derives node-id heads itself"
    callers = {name for name, node in functions.items()
               if "_relocation_head" in _called_names(node)}
    assert callers == {"_versioned_scope", "_legacy_scope", "_relocated_scope"}


def test_baseline_scope_dispatches_to_one_helper_per_shape() -> None:
    calls = _called_names(_functions()["baseline_scope"])
    assert {"_versioned_scope", "_legacy_scope"} <= calls
    assert "_relocation_head" not in calls
