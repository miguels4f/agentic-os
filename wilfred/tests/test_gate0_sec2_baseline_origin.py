"""Baseline capture must protect tests, controls, configuration and data as planned."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import engine
from hyperwilfred_core.schemas import BuildMode, BuildObjective


def _planned_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    product = repo / "prod"
    tests = product / "tests"
    tests.mkdir(parents=True)
    (product / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (product / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (tests / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    (tests / "conftest.py").write_text("\n")
    (tests / "golden.txt").write_text("4\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    engine.plan(
        repo_root=repo,
        run_id="r",
        mode=BuildMode.IMPROVE,
        product_dir="prod",
        objective=BuildObjective(id="o", title="Protect baseline", objective="improve calc"),
    )
    return repo, product


@pytest.mark.parametrize(
    "path,content",
    [
        ("tests/test_calc.py", "def test_add():\n    assert True\n"),
        ("tests/conftest.py", "VALUE = 1\n"),
        ("pytest.ini", "[pytest]\ntestpaths = tests\naddopts = -q\n"),
        ("tests/golden.txt", "5\n"),
    ],
)
def test_capture_refuses_preexisting_witness_changed_since_plan(
    tmp_path: Path,
    path: str,
    content: str,
) -> None:
    repo, product = _planned_repo(tmp_path)
    (product / path).write_text(content)
    with pytest.raises(engine.EngineError, match="base commit|base_commit"):
        engine.regression_baseline(repo_root=repo, run_id="r", target=product)


def test_capture_refuses_product_only_change(tmp_path: Path) -> None:
    # the owner's decision of 2026-10-02 (S0.2i): capture only on the base-commit tree.
    repo, product = _planned_repo(tmp_path)
    (product / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")
    with pytest.raises(engine.EngineError, match="base commit|base_commit"):
        engine.regression_baseline(repo_root=repo, run_id="r", target=product)


def test_capture_on_the_planned_tree_is_accepted(tmp_path: Path) -> None:
    repo, product = _planned_repo(tmp_path)
    result = engine.regression_baseline(repo_root=repo, run_id="r", target=product)
    assert result["baseline"]["passing"] == 1

@pytest.mark.parametrize(
    "path,content",
    [
        ("conftest.py", "collect_ignore: list[str] = []\n"),
        ("tests/test_more.py", "def test_more() -> None:\n    assert True\n"),
        ("tests/extra.json", "{}\n"),
    ],
)
def test_capture_refuses_added_witness_since_plan(
    tmp_path: Path, path: str, content: str,
) -> None:
    repo, product = _planned_repo(tmp_path)
    (product / path).write_text(content)
    with pytest.raises(engine.EngineError, match="base commit|base_commit"):
        engine.regression_baseline(repo_root=repo, run_id="r", target=product)
