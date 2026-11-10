"""Baseline capture refuses every test that did not pass unless it is declared at base_commit
(S0.2o, after the S0.2n/T1b verification: a skip that depends on metadata git does not store,
such as group/other permission bits or an ACL, weakened the baseline without touching a byte).

A test that did not pass at capture (skipped, expected to fail, failing, or in a test file
that was not collected) protects nothing. Capture now refuses it unless it is listed, with a
human reason, under ``not_passing_at_capture`` in ``.hw-known-failures.json`` as committed at
the run's base commit (a declared known failure counts as well)."""

from __future__ import annotations

import json
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from hyperwilfred_core import engine, known_failures, regression
from hyperwilfred_core.schemas import BuildMode, BuildObjective

PRODUCT = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
OTHER = "def test_other():\n    assert True\n"
FILES = {
    "calc.py": PRODUCT,
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
    "tests/test_calc.py": TEST,
    "tests/test_other.py": OTHER,
}
DECLARATION = known_failures.DECLARATION_FILE
UNDECLARED = "not declared"

SKIPPED = ("import pytest\n\n\n@pytest.mark.skip(reason='needs a device')\n"
           "def test_device():\n    assert True\n")
XFAILED = ("import pytest\n\n\n@pytest.mark.xfail(reason='known bug', strict=False)\n"
           "def test_bug():\n    assert False\n")
MODULE_SKIPPED = ("import pytest\n\npytest.importorskip('a_module_that_is_not_installed')\n\n\n"
                  "def test_optional():\n    assert True\n")
NO_TESTS = "def helper():\n    return 1\n"
FAILING = "def test_env():\n    assert False\n"

KINDS = {
    "skip": ("tests/test_device.py", SKIPPED, "tests/test_device.py::test_device"),
    "xfail": ("tests/test_bug.py", XFAILED, "tests/test_bug.py::test_bug"),
    "module-skip": ("tests/test_optional.py", MODULE_SKIPPED, "tests/test_optional.py"),
    "uncollected": ("tests/test_helpers.py", NO_TESTS, "tests/test_helpers.py"),
}


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
         "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *arguments],
        check=True, capture_output=True, text=True).stdout


def _declaration(not_passing: list[dict] | None = None, entries: list[dict] | None = None,
                 ) -> str:
    body: dict = {"version": 1, "entries": entries or []}
    if not_passing is not None:
        body["not_passing_at_capture"] = not_passing
    return json.dumps(body) + "\n"


def _planned(tmp_path: Path, files: dict[str, str] | None = None, *,
             declaration: str | None = None,
             before_commit: Callable[[Path], None] | None = None) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    product = repo / "prod"
    for name, content in (files or FILES).items():
        (product / name).parent.mkdir(parents=True, exist_ok=True)
        (product / name).write_text(content)
    if declaration is not None:
        (repo / DECLARATION).write_text(declaration)
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


def _capture(repo: Path, product: Path, run_id: str = "r") -> dict:
    return engine.regression_baseline(repo_root=repo, run_id=run_id, target=product)


def _nothing_captured(repo: Path, run_id: str = "r") -> bool:
    paths = engine.RunPaths(repo, run_id)
    return (not paths.artifact(regression.BASELINE_FILE).exists()
            and engine.load_state(paths).regression_baseline_sha256 is None)


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _with(kind: str) -> tuple[dict[str, str], str]:
    path, content, recorded = KINDS[kind]
    return {**FILES, path: content}, recorded


# ---- honest control -------------------------------------------------------------------------

def test_clean_tree_with_every_test_passing_is_captured(tmp_path: Path) -> None:
    repo, target = _planned(tmp_path)
    baseline = _capture(repo, target)["baseline"]
    assert baseline["passing"] == 2 and baseline["not_passing_ids"] == []
    assert baseline["declared_not_passing"] == []


# ---- any test that did not pass is refused unless declared ----------------------------------

@pytest.mark.parametrize("kind", sorted(KINDS))
def test_undeclared_not_passing_test_is_refused(tmp_path: Path, kind: str) -> None:
    files, recorded = _with(kind)
    repo, target = _planned(tmp_path, files)
    with pytest.raises(engine.EngineError, match=UNDECLARED) as refusal:
        _capture(repo, target)
    message = str(refusal.value)
    assert f"prod/{recorded}" in message
    assert DECLARATION in message and "not_passing_at_capture" in message
    assert _nothing_captured(repo)


def test_undeclared_failure_is_refused(tmp_path: Path) -> None:
    repo, target = _planned(tmp_path, {**FILES, "tests/test_env.py": FAILING})
    with pytest.raises(engine.EngineError, match="did not pass cleanly"):
        _capture(repo, target)
    assert _nothing_captured(repo)


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_declared_not_passing_test_is_captured_and_recorded(tmp_path: Path, kind: str) -> None:
    files, recorded = _with(kind)
    declaration = _declaration([{"id": f"prod/{recorded}", "reason": f"{kind} on purpose"}])
    repo, target = _planned(tmp_path, files, declaration=declaration)
    baseline = _capture(repo, target)["baseline"]
    assert baseline["not_passing_ids"] == [recorded]
    assert recorded not in baseline["passing_ids"]
    assert baseline["declared_not_passing"] == [{
        "id": recorded, "declared_as": f"prod/{recorded}", "reason": f"{kind} on purpose",
        "list": "not_passing_at_capture"}]


def test_declared_known_failure_counts_as_declared(tmp_path: Path) -> None:
    declaration = _declaration(entries=[{"id": "prod/tests/test_env.py::test_env",
                                         "reason": "needs a machine-local file"}])
    repo, target = _planned(tmp_path, {**FILES, "tests/test_env.py": FAILING},
                            declaration=declaration)
    baseline = _capture(repo, target)["baseline"]
    assert baseline["not_passing_ids"] == ["tests/test_env.py::test_env"]
    assert baseline["declared_not_passing"] == [{
        "id": "tests/test_env.py::test_env", "declared_as": "prod/tests/test_env.py::test_env",
        "reason": "needs a machine-local file", "list": "entries"}]


def test_name_pattern_covers_tests_but_not_an_uncollected_file(tmp_path: Path) -> None:
    files, _recorded = _with("module-skip")
    declaration = _declaration([{"id": "prod/tests/test_optional.py::test_*",
                                 "reason": "pattern only"}])
    repo, target = _planned(tmp_path, files, declaration=declaration)
    with pytest.raises(engine.EngineError, match=UNDECLARED):
        _capture(repo, target)


def test_one_undeclared_among_declared_is_refused(tmp_path: Path) -> None:
    files = {**FILES, "tests/test_device.py": SKIPPED, "tests/test_bug.py": XFAILED}
    declaration = _declaration([{"id": "prod/tests/test_device.py::test_device",
                                 "reason": "needs a device"}])
    repo, target = _planned(tmp_path, files, declaration=declaration)
    with pytest.raises(engine.EngineError, match=UNDECLARED) as refusal:
        _capture(repo, target)
    assert "prod/tests/test_bug.py::test_bug" in str(refusal.value)
    assert "test_device" not in str(refusal.value).split(UNDECLARED, 1)[1]


# ---- the declaration counts only as committed at base_commit --------------------------------

def _declare_skip_later(repo: Path, *, commit: bool) -> None:
    (repo / DECLARATION).write_text(_declaration(
        [{"id": "prod/tests/test_device.py::test_device", "reason": "added during the run"}]))
    if commit:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "declare later")


@pytest.mark.parametrize("commit", [False, True])
def test_declaration_made_after_base_commit_is_refused(tmp_path: Path, commit: bool) -> None:
    files, _recorded = _with("skip")
    repo, target = _planned(tmp_path, files)
    _declare_skip_later(repo, commit=commit)
    with pytest.raises(engine.EngineError):
        _capture(repo, target)
    assert _nothing_captured(repo)


def _skip_baseline(tmp_path: Path, declaration: str | None = None) -> tuple[Path, Path, dict]:
    files, _recorded = _with("skip")
    repo, target = _planned(tmp_path, files, declaration=declaration)
    baseline = regression.capture_baseline(target)
    assert baseline["not_passing_ids"] == ["tests/test_device.py::test_device"]
    return repo, target, baseline


@pytest.mark.parametrize("commit", [False, True])
def test_check_reads_the_declaration_from_base_commit_only(tmp_path: Path, commit: bool) -> None:
    repo, target, baseline = _skip_baseline(tmp_path)
    base = _base(repo)
    _declare_skip_later(repo, commit=commit)
    with pytest.raises(regression.RegressionError, match=UNDECLARED):
        regression.require_declared_not_passing(target, base, baseline, repo_root=repo)


def test_check_ignores_a_declaration_planted_through_a_replace_ref(tmp_path: Path) -> None:
    repo, target, baseline = _skip_baseline(tmp_path)
    base = _base(repo)
    _declare_skip_later(repo, commit=True)
    _git(repo, "replace", base, "HEAD")
    with pytest.raises(regression.RegressionError, match=UNDECLARED):
        regression.require_declared_not_passing(target, base, baseline, repo_root=repo)


def test_check_accepts_the_base_commit_declaration_directly(tmp_path: Path) -> None:
    declaration = _declaration([{"id": "prod/tests/test_device.py::test_device",
                                 "reason": "needs a device"}])
    repo, target, baseline = _skip_baseline(tmp_path, declaration)
    declared = regression.require_declared_not_passing(
        target, _base(repo), baseline, repo_root=repo)
    assert [item["id"] for item in declared] == ["tests/test_device.py::test_device"]


def test_declared_test_file_edited_since_base_is_refused(tmp_path: Path) -> None:
    declaration = _declaration([{"id": "prod/tests/test_device.py::test_device",
                                 "reason": "needs a device"}])
    repo, target, baseline = _skip_baseline(tmp_path, declaration)
    (target / "tests" / "test_device.py").write_text(SKIPPED + "\n# weakened later\n")
    with pytest.raises(regression.RegressionError, match="changed since base_commit"):
        regression.require_declared_not_passing(target, _base(repo), baseline, repo_root=repo)


# ---- the declaration is as strict as the known-failure list ---------------------------------

INVALID = {
    "wildcard-path": [{"id": "prod/tests/test_*.py::test_device", "reason": "broad"}],
    "directory": [{"id": "prod/tests/", "reason": "whole directory"}],
    "missing-reason": [{"id": "prod/tests/test_device.py::test_device", "reason": "  "}],
    "all-wildcard-name": [{"id": "prod/tests/test_device.py::*", "reason": "everything"}],
    "file-not-at-base": [{"id": "prod/tests/test_device.py::test_device", "reason": "ok"},
                         {"id": "prod/tests/test_gone.py", "reason": "file does not exist"}],
    "too-many": [{"id": f"prod/tests/test_device.py::test_device_{n}", "reason": "many"}
                 for n in range(known_failures.MAX_ENTRIES)]
                + [{"id": "prod/tests/test_device.py::test_device", "reason": "one too many"}],
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_invalid_declaration_excuses_nothing(tmp_path: Path, case: str) -> None:
    files, _recorded = _with("skip")
    repo, target = _planned(tmp_path, files, declaration=_declaration(INVALID[case]))
    with pytest.raises(engine.EngineError, match="invalid"):
        _capture(repo, target)
    assert _nothing_captured(repo)


def test_directory_known_failure_entry_does_not_declare_a_skip(tmp_path: Path) -> None:
    files, _recorded = _with("skip")
    declaration = _declaration(entries=[{"id": "prod/tests/", "reason": "env-only directory"}])
    repo, target = _planned(tmp_path, files, declaration=declaration)
    with pytest.raises(engine.EngineError, match=UNDECLARED):
        _capture(repo, target)


def test_directory_known_failure_entry_covers_the_failure_but_not_a_skip_beside_it(
    tmp_path: Path,
) -> None:
    files = {**FILES, "tests/test_env.py": FAILING, "tests/test_device.py": SKIPPED}
    declaration = _declaration(entries=[{"id": "prod/tests/", "reason": "env-only directory"}])
    repo, target = _planned(tmp_path, files, declaration=declaration)
    with pytest.raises(engine.EngineError, match=UNDECLARED) as refusal:
        _capture(repo, target)
    named = str(refusal.value).split(UNDECLARED, 1)[1]
    assert "prod/tests/test_device.py::test_device" in named and "test_env" not in named
    assert _nothing_captured(repo)


# ---- a skip caused by metadata git does not store is refused (T1b F1a/F1b) ------------------

def _tool_test(bit: str) -> dict[str, str]:
    return {**FILES, "tool.sh": "#!/bin/sh\nexit 0\n", "tests/test_calc.py": (
        "import stat\nfrom pathlib import Path\n\nimport pytest\n\nfrom calc import add\n\n"
        "TOOL = Path(__file__).parent.parent / 'tool.sh'\n\n\n"
        f"@pytest.mark.skipif(not TOOL.stat().st_mode & stat.{bit}, reason='tool')\n"
        "def test_add():\n    assert add(2, 2) == 4\n")}


def _executable_tool(repo: Path) -> None:
    (repo / "prod" / "tool.sh").chmod(0o755)


@pytest.mark.parametrize("bit,mode", [("S_IXGRP", 0o745), ("S_IXOTH", 0o754)])
def test_permission_metadata_skip_is_refused_when_undeclared(
    tmp_path: Path, bit: str, mode: int,
) -> None:
    repo, target = _planned(tmp_path, _tool_test(bit), before_commit=_executable_tool)
    (target / "tool.sh").chmod(mode)
    assert (target / "tool.sh").stat().st_mode & stat.S_IXUSR
    with pytest.raises(engine.EngineError, match=UNDECLARED) as refusal:
        _capture(repo, target)
    assert "prod/tests/test_calc.py::test_add" in str(refusal.value)
    assert _nothing_captured(repo)


@pytest.mark.parametrize("bit", ["S_IXGRP", "S_IXOTH"])
def test_permission_metadata_control_is_captured(tmp_path: Path, bit: str) -> None:
    repo, target = _planned(tmp_path, _tool_test(bit), before_commit=_executable_tool)
    baseline = _capture(repo, target)["baseline"]
    assert "tests/test_calc.py::test_add" in baseline["passing_ids"]
    assert baseline["not_passing_ids"] == []


# ---- stated limit kept, inheritance and the gate --------------------------------------------

def test_capture_outside_git_keeps_the_stated_limit(tmp_path: Path) -> None:
    files, recorded = _with("skip")
    repo = tmp_path / "repo"
    product = repo / "prod"
    for name, content in files.items():
        (product / name).parent.mkdir(parents=True, exist_ok=True)
        (product / name).write_text(content)
    engine.plan(
        repo_root=repo, run_id="r", mode=BuildMode.IMPROVE, product_dir="prod",
        objective=BuildObjective(id="o", title="Protect baseline", objective="improve calc"),
    )
    baseline = _capture(repo, product)["baseline"]
    assert baseline["not_passing_ids"] == [recorded]
    assert "declared_not_passing" not in baseline


def test_inheritance_refuses_a_donor_with_undeclared_not_passing_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    files, _recorded = _with("skip")
    repo, target = _planned(tmp_path, files)
    with monkeypatch.context() as patched:  # a donor captured before this rule existed
        patched.setattr(regression, "require_declared_not_passing",
                        lambda *_args, **_kwargs: None, raising=False)
        _capture(repo, target)
    engine.plan(
        repo_root=repo, run_id="r2", mode=BuildMode.IMPROVE, product_dir="prod",
        objective=BuildObjective(id="o", title="Protect baseline", objective="improve calc"),
    )
    source = engine.RunPaths(repo, "r").artifact(regression.BASELINE_FILE)
    with pytest.raises(engine.EngineError, match="not declared"):
        engine.inherit_regression_baseline(
            repo_root=repo, run_id="r2", source_run_id="r", source_file=source)
    assert _nothing_captured(repo, "r2")


def test_inheritance_keeps_a_declared_donor(tmp_path: Path) -> None:
    files, _recorded = _with("skip")
    declaration = _declaration([{"id": "prod/tests/test_device.py::test_device",
                                 "reason": "needs a device"}])
    repo, target = _planned(tmp_path, files, declaration=declaration)
    _capture(repo, target)
    engine.plan(
        repo_root=repo, run_id="r2", mode=BuildMode.IMPROVE, product_dir="prod",
        objective=BuildObjective(id="o", title="Protect baseline", objective="improve calc"),
    )
    source = engine.RunPaths(repo, "r").artifact(regression.BASELINE_FILE)
    inherited = engine.inherit_regression_baseline(
        repo_root=repo, run_id="r2", source_run_id="r", source_file=source)
    assert inherited["baseline"]["declared_not_passing"][0]["reason"] == "needs a device"


def test_gate_reports_declared_tests_as_unprotected(tmp_path: Path) -> None:
    declaration = _declaration([{"id": "prod/tests/test_device.py::test_device",
                                 "reason": "needs a device"}])
    files, _recorded = _with("skip")
    repo, target = _planned(tmp_path, files, declaration=declaration)
    baseline = _capture(repo, target)["baseline"]
    result = regression.compare(target, baseline, base_commit=_base(repo))
    assert result.passed
    assert "1 declared not-passing test(s) recorded, not protected" in (result.details or "")
