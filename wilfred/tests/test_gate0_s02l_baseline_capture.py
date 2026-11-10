"""Baseline capture: submodule entries, the executable bit, the runner cache, and the moment
the repository tree is read (S0.2l, after the S0.2k verification of the S0.2i fix)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from hyperwilfred_core import engine, regression
from hyperwilfred_core.schemas import BuildMode, BuildObjective

PRODUCT = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
OTHER = "def test_other():\n    assert True\n"
FILES = {
    "calc.py": PRODUCT,
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
    "tests/test_calc.py": TEST,
    "tests/test_other.py": OTHER,
    "tests/conftest.py": "\n",
}


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
                    "user.email=fixture@example.invalid", *arguments], check=True,
                   capture_output=True)


def _planned(tmp_path: Path, files: dict[str, str] | None = None, *,
             before_commit: Callable[[Path], None] | None = None) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    product = repo / "prod"
    for name, content in (files or FILES).items():
        (product / name).parent.mkdir(parents=True, exist_ok=True)
        (product / name).write_text(content)
    if before_commit is not None:
        before_commit(repo)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    engine.plan(
        repo_root=repo, run_id="r", mode=BuildMode.IMPROVE, product_dir="prod",
        objective=BuildObjective(id="o", title="Protect baseline", objective="improve calc"),
    )
    return repo, product


def _capture(repo: Path, product: Path) -> dict:
    return engine.regression_baseline(repo_root=repo, run_id="r", target=product)


def _nothing_captured(repo: Path) -> bool:
    paths = engine.RunPaths(repo, "r")
    return (not paths.artifact(regression.BASELINE_FILE).exists()
            and engine.load_state(paths).regression_baseline_sha256 is None)


# ---- a submodule entry of the base commit has no bytes to compare: capture is refused --------

NEEDS_VENDOR = {**FILES, "tests/test_calc.py": (
    "import pytest\n\npytest.importorskip('vendor_lib')\nfrom calc import add\n\n\n"
    "def test_add():\n    assert add(2, 2) == 4\n")}


def _submodule(at: str) -> Callable[[Path], None]:
    def add(repo: Path) -> None:
        inner = repo / at
        inner.mkdir(parents=True)
        (inner / "__init__.py").write_text("VALUE = 1\n")
        _git(inner, "init", "-q")
        _git(inner, "add", "-A")
        _git(inner, "commit", "-qm", "vendored")
    return add


@pytest.mark.parametrize("at", ["prod/vendor_lib", "vendor_lib"])
@pytest.mark.parametrize("checkout", ["complete", "deleted", "emptied"])
def test_capture_refuses_a_base_commit_with_a_submodule_entry(
    tmp_path: Path, at: str, checkout: str,
) -> None:
    repo, target = _planned(tmp_path, NEEDS_VENDOR, before_commit=_submodule(at))
    listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "HEAD"], check=True,
                            capture_output=True, text=True).stdout
    assert "160000 commit" in listed and at in listed
    if checkout != "complete":
        subprocess.run(["rm", "-rf", str(repo / at)], check=True)
    if checkout == "emptied":
        (repo / at).mkdir()
    with pytest.raises(engine.EngineError, match="submodule") as refusal:
        _capture(repo, target)
    assert at in str(refusal.value)
    assert _nothing_captured(repo)


# ---- the executable bit is part of the commit and of the tree record -------------------------

TOOL = {**FILES, "run.sh": "#!/bin/sh\nexit 0\n", "notes.txt": "x\n",
        "tests/test_calc.py": (
            "import os\nfrom pathlib import Path\n\nimport pytest\n\nfrom calc import add\n\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    not os.access(Path(__file__).parent.parent / 'run.sh', os.X_OK), reason='tool')"
            "\n\n\ndef test_add():\n    assert add(2, 2) == 4\n")}


def _executable(repo: Path) -> None:
    (repo / "prod" / "run.sh").chmod(0o755)


def test_capture_accepts_a_clean_tree_with_an_executable_file(tmp_path: Path) -> None:
    repo, target = _planned(tmp_path, TOOL, before_commit=_executable)
    baseline = _capture(repo, target)["baseline"]
    assert baseline["passing"] == 2 and baseline["not_passing_ids"] == []


@pytest.mark.parametrize("path,mode", [("run.sh", 0o644), ("notes.txt", 0o755)])
def test_capture_refuses_a_changed_executable_bit(tmp_path: Path, path: str, mode: int) -> None:
    repo, target = _planned(tmp_path, TOOL, before_commit=_executable)
    (target / path).chmod(mode)
    with pytest.raises(engine.EngineError, match="exactly as committed at base_commit") as refusal:
        _capture(repo, target)
    assert f"prod/{path}" in str(refusal.value)
    assert _nothing_captured(repo)


def test_executable_bit_outside_the_target_is_compared(tmp_path: Path) -> None:
    def tool(repo: Path) -> None:
        (repo / "tool.sh").write_text("#!/bin/sh\nexit 0\n")
        (repo / "tool.sh").chmod(0o755)

    repo, target = _planned(tmp_path, before_commit=tool)
    (repo / "tool.sh").chmod(0o644)
    with pytest.raises(engine.EngineError, match="exactly as committed at base_commit") as refusal:
        _capture(repo, target)
    assert "tool.sh" in str(refusal.value)


# ---- the runner cache is neither read nor written by the protected run -----------------------

LAST_FAILED = json.dumps({"tests/test_other.py::test_other": True})


def _plant_cache(target: Path) -> Path:
    cache = target / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "lastfailed").write_text(LAST_FAILED)
    return cache / "lastfailed"


def test_capture_does_not_write_a_runner_cache(tmp_path: Path) -> None:
    repo, target = _planned(tmp_path)
    _capture(repo, target)
    assert not (target / ".pytest_cache").exists()


def test_planted_runner_cache_is_not_read_or_rewritten(tmp_path: Path) -> None:
    repo, target = _planned(tmp_path)
    planted = _plant_cache(target)
    baseline = _capture(repo, target)["baseline"]
    assert baseline["passing_ids"] == ["tests/test_calc.py::test_add",
                                       "tests/test_other.py::test_other"]
    assert planted.read_text() == LAST_FAILED


def test_committed_last_failed_option_cannot_select_tests_from_a_planted_cache(
    tmp_path: Path,
) -> None:
    files = {**FILES, "pytest.ini": "[pytest]\ntestpaths = tests\naddopts = --lf\n"}
    repo, target = _planned(tmp_path, files)
    _plant_cache(target)
    with pytest.raises(engine.EngineError, match="pytest did not pass cleanly"):
        _capture(repo, target)
    assert _nothing_captured(repo)


# ---- the tree is read before the tests run ---------------------------------------------------

SELF_RESTORING = (
    "import sys\nfrom pathlib import Path\n\nif 'pytest' in sys.modules:\n    import pytest\n"
    f"    Path(__file__).write_text({PRODUCT!r})\n"
    "    pytest.skip('hidden', allow_module_level=True)\n\n\n"
    "def add(a, b):\n    return a - b\n")


def test_product_that_restores_itself_during_the_run_is_refused(tmp_path: Path) -> None:
    repo, target = _planned(tmp_path)
    (target / "calc.py").write_text(SELF_RESTORING)
    with pytest.raises(engine.EngineError, match="did not pass at capture"):
        _capture(repo, target)
    assert (target / "calc.py").read_text() == PRODUCT  # restored while the tests ran
    assert _nothing_captured(repo)


def test_regression_gate_does_not_write_a_runner_cache_either(tmp_path: Path) -> None:
    clean = {
        "calc.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
        "pytest.ini": "[pytest]\ntestpaths = tests\n",
        "tests/test_calc.py": ("from calc import add\n\n\ndef test_add() -> None:\n"
                               "    assert add(2, 2) == 4\n"),
    }
    repo, target = _planned(tmp_path, clean)
    _capture(repo, target)
    engine.evaluate(repo_root=repo, run_id="r", target=target, step_id="regression")
    assert not (target / ".pytest_cache").exists()
