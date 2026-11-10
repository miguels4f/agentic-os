"""Real evaluator execution does not create bytecode or hide product mutations."""

import json
from pathlib import Path

import pytest

from hyperwilfred_core import engine, regression
from hyperwilfred_core.evaluators.code.pytest_evaluator import pytest_environment
from hyperwilfred_core.schemas import BuildObjective


def test_pytest_environment_overrides_inherited_bytecode_setting(monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "")
    assert pytest_environment()["PYTHONDONTWRITEBYTECODE"] == "1"


@pytest.mark.parametrize("mutate_source", [False, True])
def test_real_evaluate_preserves_bytes_and_rejects_source_changes(
    git_repo: Path, mutate_source: bool,
) -> None:
    product = git_repo / "product"
    product.mkdir()
    source = product / "calc.py"
    source.write_text("def double(value: int) -> int:\n    return value * 2\n")
    test = "from calc import double\n\n\ndef test_double() -> None:\n    assert double(3) == 6\n"
    if mutate_source:
        test = "from pathlib import Path\n" + test
        test += "    Path(__file__).with_name('calc.py').write_text('CHANGED = True\\n')\n"
    (product / "test_calc.py").write_text(test)
    engine.plan(
        repo_root=git_repo, run_id="bytecode", product_dir="product",
        objective=BuildObjective(id="o", title="Bytecode", objective="Preserve checked bytes"),
    )
    if mutate_source:
        with pytest.raises(engine.GateFailure):
            engine.evaluate(repo_root=git_repo, run_id="bytecode", target=product)
        paths = engine.RunPaths(git_repo, "bytecode")
        report = json.loads(paths.artifact("verification.json").read_text())
        checks = {check["name"]: check for check in report["checks"]}
        assert checks["pytest"]["passed"]
        assert not checks["product-snapshot-stability"]["passed"]
        assert source.read_text() == "CHANGED = True\n"
    else:
        assert engine.evaluate(repo_root=git_repo, run_id="bytecode", target=product)["passed"]
        baseline = regression.capture_baseline(product)
        assert regression.compare(product, baseline).passed
    assert not list(product.rglob("*.pyc"))
    assert not list(product.rglob("__pycache__"))
