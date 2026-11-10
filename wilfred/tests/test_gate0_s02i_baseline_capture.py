"""Baseline capture fails closed when it cannot bind the captured witness to the base commit."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import engine, regression
from hyperwilfred_core.schemas import BuildMode, BuildObjective

PRODUCT = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
GUTTED = "def test_add():\n    assert True\n"
FILES = {
    "calc.py": PRODUCT,
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
    "tests/test_calc.py": TEST,
    "tests/conftest.py": "\n",
    "tests/golden.txt": "4\n",
}


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
                    "user.email=fixture@example.invalid", *arguments], check=True)


def _planned(tmp_path: Path, files: dict[str, str] | None = None, *,
             git: bool = True) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    product = repo / "prod"
    for name, content in (files or FILES).items():
        (product / name).parent.mkdir(parents=True, exist_ok=True)
        (product / name).write_text(content)
    if git:
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


def test_capture_refuses_when_the_repository_is_hidden(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path)
    assert engine.load_state(engine.RunPaths(repo, "r")).base_commit
    (product / "tests" / "test_calc.py").write_text(GUTTED)
    (repo / ".git").rename(repo / ".git-hidden")
    with pytest.raises(engine.EngineError, match="base_commit"):
        _capture(repo, product)
    assert _nothing_captured(repo)


def test_capture_refuses_when_the_git_directory_is_emptied(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path)
    (product / "tests" / "test_calc.py").write_text(GUTTED)
    (repo / ".git").rename(repo / ".git-hidden")
    (repo / ".git").mkdir()
    with pytest.raises(engine.EngineError, match="base_commit"):
        _capture(repo, product)
    assert _nothing_captured(repo)


def test_capture_refuses_a_hidden_repository_even_with_untouched_tests(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path)
    (repo / ".git").rename(repo / ".git-hidden")
    with pytest.raises(engine.EngineError, match="base_commit"):
        _capture(repo, product)
    assert _nothing_captured(repo)


def test_capture_without_a_base_commit_outside_git_still_works(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path, git=False)
    assert engine.load_state(engine.RunPaths(repo, "r")).base_commit is None
    assert _capture(repo, product)["baseline"]["passing"] == 1


# ---- the comparison uses the captured hashes, never a second read of the tree ----------------

WITNESSES = [
    ("tests/test_calc.py", GUTTED, "test files"),
    ("tests/conftest.py", "VALUE = 1\n", "pytest control files"),
    ("tests/golden.txt", "5\n", "test data files"),
    ("pytest.ini", "[pytest]\ntestpaths = tests\naddopts = -q\n", "pytest configuration"),
]


@pytest.mark.parametrize("path,content,label", WITNESSES)
def test_witness_restored_after_the_run_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, content: str, label: str,
) -> None:
    repo, product = _planned(tmp_path)
    original = (product / path).read_text()
    (product / path).write_text(content)
    observe = regression.baseline_candidate

    def restoring(target: Path):
        observed = observe(target)
        (product / path).write_text(original)  # base bytes are back before the comparison
        return observed

    monkeypatch.setattr(regression, "baseline_candidate", restoring)
    with pytest.raises(engine.EngineError, match=f"{label} differ.* from base_commit"):
        _capture(repo, product)
    assert _nothing_captured(repo)


def _base(repo: Path) -> str:
    base = engine.load_state(engine.RunPaths(repo, "r")).base_commit
    assert base
    return base


def test_comparison_judges_the_baseline_it_is_given(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path)
    baseline = regression.capture_baseline(product)
    regression.require_base_commit_witness(product, _base(repo), baseline)
    forged = dict(baseline, test_file_hashes={"tests/test_calc.py": "0" * 64})
    with pytest.raises(regression.RegressionError, match="test files differ from base_commit"):
        regression.require_base_commit_witness(product, _base(repo), forged)
    moved = dict(baseline, test_roots=["."])
    with pytest.raises(regression.RegressionError, match="scope disagrees with base_commit"):
        regression.require_base_commit_witness(product, _base(repo), moved)


@pytest.mark.parametrize("missing", ["test_roots", "test_file_hashes", "control_file_hashes",
                                     "data_file_hashes", "pytest_config_file_sha256"])
def test_comparison_refuses_a_baseline_without_its_witness(tmp_path: Path, missing: str) -> None:
    repo, product = _planned(tmp_path)
    baseline = regression.capture_baseline(product)
    del baseline[missing]
    with pytest.raises(regression.RegressionError, match="base_commit"):
        regression.require_base_commit_witness(product, _base(repo), baseline)


# ---- a test that does not pass at capture is recorded, and named when capture is refused ----

OTHER = "def test_other():\n    assert True\n"
TWO_TESTS = {**FILES, "tests/test_other.py": OTHER}
BROKEN = "def add(a, b):\n    return a - b\n"
MODULE_SKIP = ("import sys\n\nif 'pytest' in sys.modules:\n    import pytest\n"
               "    pytest.skip('hidden', allow_module_level=True)\n\n\n" + BROKEN)
CALL_SKIP = "import pytest\n\n\ndef add(a, b):\n    pytest.skip('hidden')\n"
CALL_XFAIL = "import pytest\n\n\ndef add(a, b):\n    pytest.xfail('hidden')\n"
SKIPPED_AT_BASE = {**FILES, "tests/test_slow.py": (
    "import pytest\n\n\n@pytest.mark.skip(reason='needs a device')\n"
    "def test_slow():\n    assert False\n")}
MODULE_SKIPPED_AT_BASE = {**FILES, "tests/test_optional.py": (
    "import pytest\n\npytest.importorskip('a_module_that_is_not_installed')\n\n\n"
    "def test_optional():\n    assert True\n")}
REFUSED = "did not pass at capture"


@pytest.mark.parametrize("product,missing", [
    (MODULE_SKIP, "tests/test_calc.py"),
    (CALL_SKIP, "tests/test_calc.py::test_add"),
    (CALL_XFAIL, "tests/test_calc.py::test_add"),
])
def test_product_edit_cannot_take_a_test_out_of_the_baseline(
    tmp_path: Path, product: str, missing: str,
) -> None:
    repo, target = _planned(tmp_path, TWO_TESTS)
    (target / "calc.py").write_text(product)
    with pytest.raises(engine.EngineError, match=REFUSED) as refusal:
        _capture(repo, target)
    assert missing in str(refusal.value) and "calc.py" in str(refusal.value)
    assert _nothing_captured(repo)


@pytest.mark.parametrize("files,recorded", [
    (SKIPPED_AT_BASE, "tests/test_slow.py::test_slow"),
    (MODULE_SKIPPED_AT_BASE, "tests/test_optional.py"),
])
def test_skip_committed_at_base_is_captured_and_recorded(
    tmp_path: Path, files: dict[str, str], recorded: str,
) -> None:
    # Since S0.2o a test that does not pass must also be declared at the base commit.
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / ".hw-known-failures.json").write_text(
        '{"version": 1, "not_passing_at_capture": [{"id": "prod/' + recorded + '", '
        '"reason": "skipped on purpose"}]}\n')
    repo, target = _planned(tmp_path, files)
    (target / ".pytest_cache").mkdir()
    (target / ".pytest_cache" / "README.md").write_text("runner cache\n")
    (target / "tests" / ".DS_Store").write_text("desktop metadata\n")
    baseline = _capture(repo, target)["baseline"]
    assert baseline["passing_ids"] == ["tests/test_calc.py::test_add"]
    assert baseline["not_passing_ids"] == [recorded]
    assert [item["id"] for item in baseline["declared_not_passing"]] == [recorded]


@pytest.mark.parametrize("change", ["modified", "added", "removed", "linked", "directory"])
def test_skip_at_base_is_refused_once_the_target_differs_from_base(
    tmp_path: Path, change: str,
) -> None:
    files = {**SKIPPED_AT_BASE, "notes.txt": "kept\n"}
    repo, target = _planned(tmp_path, files)
    if change == "modified":
        (target / "calc.py").write_text(PRODUCT + "# later\n")
    elif change == "added":
        (target / "settings_local.py").write_text("DEVICE = None\n")
    elif change == "removed":
        (target / "notes.txt").unlink()
    elif change == "linked":
        (target / "notes.txt").unlink()
        (target / "notes.txt").symlink_to(target / "calc.py")
    else:
        (target / "plugins").mkdir()
    with pytest.raises(engine.EngineError, match=REFUSED):
        _capture(repo, target)
    assert _nothing_captured(repo)


def test_product_restored_after_the_run_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, target = _planned(tmp_path, TWO_TESTS)
    (target / "calc.py").write_text(MODULE_SKIP)
    observe = regression.baseline_candidate

    def restoring(observed_target: Path):
        observed = observe(observed_target)
        (target / "calc.py").write_text(PRODUCT)  # base bytes are back before the comparison
        return observed

    monkeypatch.setattr(regression, "baseline_candidate", restoring)
    with pytest.raises(engine.EngineError, match=REFUSED):
        _capture(repo, target)
    assert _nothing_captured(repo)


def test_skip_outside_git_is_recorded_without_a_comparison(tmp_path: Path) -> None:
    repo, target = _planned(tmp_path, SKIPPED_AT_BASE, git=False)
    baseline = _capture(repo, target)["baseline"]
    assert baseline["not_passing_ids"] == ["tests/test_slow.py::test_slow"]


@pytest.mark.parametrize("dirty", [False, True])
def test_declared_known_failure_is_tolerated_only_on_the_base_tree(
    tmp_path: Path, dirty: bool,
) -> None:
    failing = "def test_env():\n    assert False\n"
    files = {**FILES, "tests/test_env.py": failing}
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".hw-known-failures.json").write_text(
        '{"version": 1, "entries": [{"id": "prod/tests/test_env.py::test_env", '
        '"reason": "needs a machine-local file"}]}\n')
    repo, target = _planned(tmp_path, files)
    if dirty:
        (target / "calc.py").write_text(PRODUCT + "# later\n")
        with pytest.raises(engine.EngineError, match=REFUSED):
            _capture(repo, target)
        assert _nothing_captured(repo)
        return
    baseline = _capture(repo, target)["baseline"]
    assert baseline["not_passing_ids"] == ["tests/test_env.py::test_env"]
    assert baseline["tolerated_known_failures"]


@pytest.mark.parametrize("missing", ["not_passing_ids", "tree_sha256", "tree_root"])
def test_comparison_refuses_a_baseline_without_its_outcome_record(
    tmp_path: Path, missing: str,
) -> None:
    repo, target = _planned(tmp_path)
    baseline = regression.capture_baseline(target)
    del baseline[missing]
    with pytest.raises(regression.RegressionError, match="base_commit"):
        regression.require_base_commit_witness(target, _base(repo), baseline)


# ---- capture happens on the base-commit tree only ---------------------------------------------

NOT_BASE = "repository exactly as committed at base_commit"
CASES_PRODUCT = PRODUCT + "\n\nCASES = [(2, 2, 4), (1, 5, 6)]\n"
CASES_TEST = ("import pytest\nfrom calc import CASES, add\n\n\n"
              "@pytest.mark.parametrize('left,right,total', CASES)\n"
              "def test_add(left, right, total):\n    assert add(left, right) == total\n")
SHRUNK = "def add(a, b):\n    return 4\n\n\nCASES = [(2, 2, 4)]\n"


def test_product_edit_cannot_remove_a_collected_case(tmp_path: Path) -> None:
    files = {**FILES, "calc.py": CASES_PRODUCT, "tests/test_calc.py": CASES_TEST}
    repo, target = _planned(tmp_path, files)
    (target / "calc.py").write_text(SHRUNK)
    with pytest.raises(engine.EngineError, match=NOT_BASE) as refusal:
        _capture(repo, target)
    assert "calc.py" in str(refusal.value)
    assert _nothing_captured(repo)


@pytest.mark.parametrize("change", ["modified", "added", "removed", "directory"])
def test_capture_is_refused_on_a_target_that_differs_from_base(
    tmp_path: Path, change: str,
) -> None:
    repo, target = _planned(tmp_path, {**TWO_TESTS, "notes.txt": "kept\n"})
    if change == "modified":
        (target / "calc.py").write_text(PRODUCT + "# later\n")
    elif change == "added":
        (target / "settings_local.py").write_text("DEVICE = None\n")
    elif change == "removed":
        (target / "notes.txt").unlink()
    else:
        (target / "plugins").mkdir()
    with pytest.raises(engine.EngineError, match=NOT_BASE):
        _capture(repo, target)
    assert _nothing_captured(repo)


def test_capture_on_the_base_tree_ignores_exempt_entries(tmp_path: Path) -> None:
    repo, target = _planned(tmp_path, TWO_TESTS)
    (target / ".pytest_cache").mkdir()
    (target / ".pytest_cache" / "README.md").write_text("runner cache\n")
    (target / ".DS_Store").write_text("desktop metadata\n")
    baseline = _capture(repo, target)["baseline"]
    assert baseline["passing"] == 2 and baseline["not_passing_ids"] == []


# ---- the comparison covers the whole repository, not only the declared target -----------------

SHARED = {
    **FILES,
    "../shared/helper.py": "READY = True\n",
    "tests/conftest.py": ("import sys\nfrom pathlib import Path\n\n"
                          "sys.path.insert(0, str(Path(__file__).resolve().parents[2]"
                          " / 'shared'))\n"),
    "tests/test_calc.py": ("import helper\nimport pytest\nfrom calc import add\n\n"
                           "if not helper.READY:\n"
                           "    pytest.skip('helper not ready', allow_module_level=True)\n\n\n"
                           "def test_add():\n    assert add(2, 2) == 4\n"),
    "tests/test_other.py": OTHER,
}


def test_file_outside_the_target_cannot_take_a_test_out_of_the_baseline(tmp_path: Path) -> None:
    repo, target = _planned(tmp_path, SHARED)
    (repo / "shared" / "helper.py").write_text("READY = False\n")
    with pytest.raises(engine.EngineError, match=NOT_BASE) as refusal:
        _capture(repo, target)
    assert "shared/helper.py" in str(refusal.value) and "tests/test_calc.py" in str(refusal.value)
    assert _nothing_captured(repo)


@pytest.mark.parametrize("change", ["modified", "added", "removed", "directory", "linked"])
def test_capture_is_refused_when_the_repository_differs_outside_the_target(
    tmp_path: Path, change: str,
) -> None:
    repo, target = _planned(tmp_path, SHARED)
    if change == "modified":
        (repo / "shared" / "helper.py").write_text("READY = True  # later\n")
    elif change == "added":
        (repo / "sitecustomize.py").write_text("\n")
    elif change == "removed":
        (repo / "shared" / "helper.py").unlink()
        (target / "tests" / "test_calc.py").write_text(TEST)
    elif change == "directory":
        (repo / "plugins").mkdir()
    else:
        (repo / "linked.py").symlink_to(repo / "shared" / "helper.py")
    with pytest.raises(engine.EngineError, match="base_commit"):
        _capture(repo, target)
    assert _nothing_captured(repo)


def test_tracked_link_and_exempt_entries_do_not_block_a_clean_capture(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "shared").mkdir(parents=True)
    (repo / "alias.py").symlink_to("shared/helper.py")
    repo, target = _planned(tmp_path, SHARED)
    (repo / ".pytest_cache").mkdir()
    (repo / ".DS_Store").write_text("desktop metadata\n")
    (target / ".pytest_cache").mkdir()
    baseline = _capture(repo, target)["baseline"]
    assert baseline["passing"] == 2 and baseline["tree_root"] == ".."


def test_retargeted_link_outside_the_target_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "shared").mkdir(parents=True)
    (repo / "alias.py").symlink_to("shared/helper.py")
    repo, target = _planned(tmp_path, SHARED)
    (repo / "alias.py").unlink()
    (repo / "alias.py").symlink_to("prod/calc.py")
    with pytest.raises(engine.EngineError, match=NOT_BASE):
        _capture(repo, target)


def test_comparison_refuses_a_tree_recorded_from_another_root(tmp_path: Path) -> None:
    repo, target = _planned(tmp_path, SHARED)
    baseline = regression.capture_baseline(target)
    regression.require_base_commit_witness(target, _base(repo), baseline)
    narrowed = dict(baseline, tree_root=".")
    with pytest.raises(regression.RegressionError, match="base_commit"):
        regression.require_base_commit_witness(target, _base(repo), narrowed)
