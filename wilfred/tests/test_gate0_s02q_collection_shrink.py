"""Baseline capture refuses a test that was collected at plan time but vanished from collection
at capture (S0.2q, after the S0.2p/T1 finding F3), and the known-failures reader ignores git
replacement refs (Codex audit A1, CX-02).

F3: with a committed conftest hook or a parametrize list that reads file metadata git does not
store (the group-execute bit, a macOS ACL), changing only that metadata after ``hw plan`` made
a protecting test disappear from collection; it was neither passing nor "not passing", so the
capture succeeded and the regression gate was green on a broken product.

``hw plan`` now collects the tests of the base commit in a snapshot rebuilt from hash-verified
git objects (file modes as committed, nothing else from the working tree) and pins that record
in run state. Capture compares: a test collected at plan time and not collected at capture is
treated as a test that did not pass, so it is refused unless it is declared at base_commit."""

from __future__ import annotations

import getpass
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from hyperwilfred_core import engine, known_failures, regression
from hyperwilfred_core.schemas import BuildMode, BuildObjective

DECLARATION = known_failures.DECLARATION_FILE
PRODUCT = "def add(a, b):\n    return a + b\n"
TOOL = "#!/bin/sh\nexit 0\n"
OTHER = "def test_other():\n    assert True\n"
PLAIN_CALC = ("from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n\n\n"
              "def test_also():\n    assert add(0, 0) == 0\n")
# e1: a committed conftest hook deselects test_add when tool.sh lacks the group-execute bit.
DESELECT_HOOK = (
    "import os\nimport stat\nfrom pathlib import Path\n\n"
    "TOOL = Path(__file__).resolve().parent.parent / 'tool.sh'\n\n\n"
    "def pytest_collection_modifyitems(config, items):\n"
    "    if os.stat(TOOL).st_mode & stat.S_IXGRP:\n"
    "        return\n"
    "    dropped = [item for item in items if item.name == 'test_add']\n"
    "    items[:] = [item for item in items if item.name != 'test_add']\n"
    "    config.hook.pytest_deselected(items=dropped)\n"
)


def _param_shrink(condition: str) -> str:
    return (
        "import os\nimport stat\nfrom pathlib import Path\n\nimport pytest\n\n"
        "from calc import add\n\n"
        "TOOL = Path(__file__).resolve().parent.parent / 'tool.sh'\n"
        f"CASES = [(0, 0, 0)] + ([(2, 2, 4)] if {condition} else [])\n\n\n"
        "@pytest.mark.parametrize(('a', 'b', 'c'), CASES)\n"
        "def test_add(a, b, c):\n    assert add(a, b) == c\n"
    )


# e3: group-execute bit; e4: os.access, which a macOS ACL "deny execute" turns off.
PARAM_GROUP = _param_shrink("os.stat(TOOL).st_mode & stat.S_IXGRP")
PARAM_ACCESS = _param_shrink("os.access(TOOL, os.X_OK)")

BASE_FILES = {
    "calc.py": PRODUCT,
    "tool.sh": TOOL,
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
    "tests/test_other.py": OTHER,
}
DESELECT_FILES = {**BASE_FILES, "tests/conftest.py": DESELECT_HOOK,
                  "tests/test_calc.py": PLAIN_CALC}
GROUP_FILES = {**BASE_FILES, "tests/test_calc.py": PARAM_GROUP}
ACCESS_FILES = {**BASE_FILES, "tests/test_calc.py": PARAM_ACCESS}

VANISHED = "not collected at capture"
ADD = "prod/tests/test_calc.py::test_add"
ADD_CASE = "prod/tests/test_calc.py::test_add[2-2-4]"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
         "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *arguments],
        check=True, capture_output=True, text=True).stdout


def _plan(repo: Path, run_id: str = "r") -> None:
    engine.plan(
        repo_root=repo, run_id=run_id, mode=BuildMode.IMPROVE, product_dir="prod",
        objective=BuildObjective(id="o", title="Protect baseline", objective="improve calc"),
    )


def _planned(tmp_path: Path, files: dict[str, str], *, declaration: str | None = None,
             before_plan: Callable[[Path], None] | None = None) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    product = repo / "prod"
    for name, content in files.items():
        (product / name).parent.mkdir(parents=True, exist_ok=True)
        (product / name).write_text(content)
    (product / "tool.sh").chmod(0o755)
    if declaration is not None:
        (repo / DECLARATION).write_text(declaration)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    if before_plan is not None:
        before_plan(product)
    _plan(repo)
    return repo, product


def _capture(repo: Path, product: Path, run_id: str = "r") -> dict:
    return engine.regression_baseline(repo_root=repo, run_id=run_id, target=product)


def _nothing_captured(repo: Path, run_id: str = "r") -> bool:
    paths = engine.RunPaths(repo, run_id)
    return (not paths.artifact(regression.BASELINE_FILE).exists()
            and engine.load_state(paths).regression_baseline_sha256 is None)


def _group_bit_off(product: Path) -> None:
    (product / "tool.sh").chmod(0o745)


def _deny_execute_acl(product: Path) -> None:
    subprocess.run(["/bin/chmod", "+a", f"user:{getpass.getuser()} deny execute",
                    str(product / "tool.sh")], check=True, capture_output=True)
    assert stat.S_IMODE((product / "tool.sh").stat().st_mode) == 0o755
    assert not os.access(product / "tool.sh", os.X_OK)


macos_acl = pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACLs (chmod +a)")


def _record_path(repo: Path) -> Path:
    return engine.RunPaths(repo, "r").artifact(regression.COLLECTION_FILE)


# ---- honest controls ------------------------------------------------------------------------

@pytest.mark.parametrize("files", [DESELECT_FILES, GROUP_FILES, ACCESS_FILES],
                         ids=["deselect-hook", "param-group", "param-access"])
def test_honest_clean_tree_is_captured(tmp_path: Path, files: dict[str, str]) -> None:
    repo, product = _planned(tmp_path, files)
    record = json.loads(_record_path(repo).read_text())
    assert record["status"] == "collected"
    baseline = _capture(repo, product)["baseline"]
    assert baseline["not_passing_ids"] == [] and baseline["vanished_since_plan"] == []
    assert set(record["collected_ids"]) <= set(baseline["passing_ids"])
    assert baseline["base_collection_sha256"] == engine.load_state(
        engine.RunPaths(repo, "r")).base_collection_sha256


def test_plan_records_collection_from_base_commit_not_working_tree(tmp_path: Path) -> None:
    repo, _product = _planned(tmp_path, GROUP_FILES)
    record = json.loads(_record_path(repo).read_text())
    assert record["base_commit"] == _git(repo, "rev-parse", "HEAD").strip()
    assert record["target"] == "prod"
    assert "tests/test_calc.py::test_add[2-2-4]" in record["collected_ids"]


# ---- F3: a test that vanishes from collection after planning is refused ----------------------

def _refused_naming(repo: Path, product: Path, vanished: str) -> None:
    with pytest.raises(engine.EngineError, match=VANISHED) as refusal:
        _capture(repo, product)
    message = str(refusal.value)
    assert vanished in message and "prod/tests/test_calc.py" in message
    assert DECLARATION in message and "not_passing_at_capture" in message
    assert _nothing_captured(repo)


def test_e1_deselect_hook_via_group_bit_after_plan_is_refused(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path, DESELECT_FILES)
    _group_bit_off(product)
    _refused_naming(repo, product, ADD)


def test_e3_param_shrink_via_group_bit_after_plan_is_refused(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path, GROUP_FILES)
    _group_bit_off(product)
    _refused_naming(repo, product, ADD_CASE)


@macos_acl
def test_e4_param_shrink_via_macos_acl_after_plan_is_refused(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path, ACCESS_FILES)
    _deny_execute_acl(product)
    _refused_naming(repo, product, ADD_CASE)


def test_metadata_changed_before_plan_is_still_refused(tmp_path: Path) -> None:
    """The plan-time record is collected from the base commit's objects, not from the working
    tree, so metadata changed before ``hw plan`` cannot shrink the record itself."""
    repo, product = _planned(tmp_path, DESELECT_FILES, before_plan=_group_bit_off)
    assert "tests/test_calc.py::test_add" in json.loads(
        _record_path(repo).read_text())["collected_ids"]
    _refused_naming(repo, product, ADD)


@macos_acl
def test_acl_set_before_plan_is_still_refused(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path, ACCESS_FILES, before_plan=_deny_execute_acl)
    _refused_naming(repo, product, ADD_CASE)


# ---- a vanished test that is declared at base_commit is captured and recorded ----------------

def _declaration(not_passing: list[dict]) -> str:
    return json.dumps({"version": 1, "entries": [],
                       "not_passing_at_capture": not_passing}) + "\n"


@pytest.mark.parametrize("declared_as", [ADD_CASE, "prod/tests/test_calc.py"])
def test_declared_vanished_test_is_captured_not_protected(tmp_path: Path,
                                                          declared_as: str) -> None:
    repo, product = _planned(
        tmp_path, GROUP_FILES,
        declaration=_declaration([{"id": declared_as, "reason": "needs the tool executable"}]))
    _group_bit_off(product)
    baseline = _capture(repo, product)["baseline"]
    assert baseline["vanished_since_plan"] == ["tests/test_calc.py::test_add[2-2-4]"]
    assert "tests/test_calc.py::test_add[2-2-4]" in baseline["not_passing_ids"]
    assert "tests/test_calc.py::test_add[2-2-4]" not in baseline["passing_ids"]
    assert [entry["id"] for entry in baseline["declared_not_passing"]] == [
        "tests/test_calc.py::test_add[2-2-4]"]


def test_vanished_test_declared_only_in_working_tree_is_refused(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path, GROUP_FILES)
    (repo / DECLARATION).write_text(
        _declaration([{"id": ADD_CASE, "reason": "planted after plan"}]))
    _group_bit_off(product)
    with pytest.raises(engine.EngineError):
        _capture(repo, product)
    assert _nothing_captured(repo)


# ---- the plan record must be present, pinned and intact (fail closed) ------------------------

REPLAN = "plan a new run"


def test_missing_plan_record_is_refused(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path, GROUP_FILES)
    _record_path(repo).unlink()
    with pytest.raises(engine.EngineError, match=REPLAN):
        _capture(repo, product)
    assert _nothing_captured(repo)


def test_tampered_plan_record_is_refused(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path, GROUP_FILES)
    record = json.loads(_record_path(repo).read_text())
    record["collected_ids"] = [node for node in record["collected_ids"] if "2-2-4" not in node]
    _record_path(repo).write_text(json.dumps(record, indent=2) + "\n")
    _group_bit_off(product)
    with pytest.raises(engine.EngineError, match=REPLAN):
        _capture(repo, product)
    assert _nothing_captured(repo)


def test_unpinned_plan_record_is_refused(tmp_path: Path) -> None:
    repo, product = _planned(tmp_path, GROUP_FILES)
    paths = engine.RunPaths(repo, "r")
    state = engine.load_state(paths)
    state.base_collection_sha256 = None
    engine.save_state(paths, state)
    with pytest.raises(engine.EngineError, match=REPLAN):
        _capture(repo, product)
    assert _nothing_captured(repo)


def test_collection_unavailable_at_plan_is_refused_at_capture(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> list[str]:
        raise regression.RegressionError("collection failed")

    monkeypatch.setattr(regression, "collect_base_commit", unavailable)
    repo, product = _planned(tmp_path, GROUP_FILES)  # planning itself still succeeds
    assert json.loads(_record_path(repo).read_text())["status"] == "unavailable"
    monkeypatch.undo()
    with pytest.raises(engine.EngineError, match=REPLAN):
        _capture(repo, product)
    assert _nothing_captured(repo)


def test_snapshot_that_cannot_hold_committed_modes_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _product = _planned(tmp_path, GROUP_FILES)
    real_chmod = os.chmod

    def lossy(path: object, mode: int, *args: object, **kwargs: object) -> None:
        real_chmod(path, mode & ~stat.S_IXGRP, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(regression.os, "chmod", lossy)
    with pytest.raises(regression.RegressionError, match="committed file mode"):
        regression.collect_base_commit(repo / "prod", _git(repo, "rev-parse", "HEAD").strip())


# ---- CX-02: the known-failures reader ignores git replacement refs ---------------------------

def test_git_replace_on_base_commit_changes_nothing_for_known_failures_reader(
        tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / DECLARATION).write_text(json.dumps({"version": 1, "entries": []}) + "\n")
    (repo / "pkg" / "test_x.py").write_text("def test_x():\n    assert True\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    # A different commit that declares a failure and changes the test file.
    (repo / DECLARATION).write_text(json.dumps({"version": 1, "entries": [
        {"id": "pkg/test_x.py::test_x", "reason": "planted through git replace"}]}) + "\n")
    (repo / "pkg" / "test_x.py").write_text("def test_x():\n    assert False\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decoy")
    decoy = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "replace", base, decoy)
    assert "assert False" in _git(repo, "show", f"{base}:pkg/test_x.py")  # replace is live

    declaration, error = known_failures.read_declaration(repo, base)
    assert error is None and declaration is not None and declaration.entries == []
    assert known_failures._unchanged_since(repo.resolve(), "pkg/test_x.py", base) is False
