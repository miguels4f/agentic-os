"""S0.3 closing pass: three gate bypasses found by the reviews of the merged engine.

G1 (security s1): the acceptance gate's ``git status`` honoured a worker-set ``core.fsmonitor``
hook, so a hook that reports "nothing changed" hid out-of-scope edits, and the engine ran the
worker's program while judging the worker.

G2 (correctness c1 / adversarial a1): an untracked ``<dir>/.gitignore`` containing ``*`` hides
itself and a planted file beside it from ``git status``, so acceptance returned accepted with no
escapes. Untracked files hidden by an ignore file the worker added or changed are now changed
paths; output ignored by a rule that is committed and unchanged stays invisible, as before.

G3 (tests t1): inheriting a baseline skipped the S0.2q comparison with the run's pinned
plan-time collection, so a donor captured by an older engine after a test vanished from
collection protected less than the base commit collects. Inheritance now refuses it.

G4 (security round, observed): the patch-budget helpers ran ``git diff`` honouring a worker-set
``diff.external`` command or textconv driver, so acceptance could run the worker's program and
judge counts that program made up. They now diff with ``--no-ext-diff --no-textconv``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core import engine, guardrails, regression
from hyperwilfred_core.schemas import BuildMode, BuildObjective


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
         "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *arguments],
        check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _accept(worktree: Path, *, do_rollback: bool = True):
    return guardrails.accept(
        packet_id="p", worktree=str(worktree), allowed_globs=["src/**", "app/**"],
        blocked_globs=["**/conftest.py", ".git/**"], result_file=None,
        max_changed_lines=400, max_hunk_span=120, full_file_ratio=0.9,
        do_rollback=do_rollback,
    )


# ---- G1: a worker-set core.fsmonitor hook neither hides edits nor runs ----------------------

def _fsmonitor_hook(tmp_path: Path, repo: Path) -> Path:
    marker = tmp_path / "hook-ran"
    hook = tmp_path / "fsmonitor-hook.sh"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker}'\nprintf 'tok\\0'\n")
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(hook))
    _git(repo, "status", "--porcelain")  # the worker primes the index while the tree is clean
    _git(repo, "status", "--porcelain")
    marker.unlink(missing_ok=True)
    return marker


def test_g1_fsmonitor_hook_cannot_hide_an_out_of_scope_edit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n", "other/protected.py": "secret = 1\n"})
    marker = _fsmonitor_hook(tmp_path, repo)
    (repo / "other" / "protected.py").write_text("secret = 2\n")
    # Precondition: the hook really hides the edit from a plain status.
    assert "protected" not in _git(repo, "status", "--porcelain")
    marker.unlink(missing_ok=True)

    assert "other/protected.py" in guardrails.changed_paths(str(repo))
    assert guardrails.is_clean(str(repo)) is False
    verdict = _accept(repo, do_rollback=False)
    assert verdict.accepted is False
    assert "other/protected.py" in verdict.escapes
    assert not marker.exists(), "the engine executed the worker's fsmonitor hook"


# ---- G2: a self-ignoring .gitignore cannot hide planted files ------------------------------

def test_g2_self_ignoring_gitignore_cannot_hide_a_planted_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n"})
    (repo / "app").mkdir()
    (repo / "app" / ".gitignore").write_text("*\n")
    (repo / "app" / "conftest.py").write_text("def pytest_configure(config):\n    pass\n")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""  # precondition

    changed = guardrails.changed_paths(str(repo))
    assert {"app/.gitignore", "app/conftest.py"} <= set(changed)
    assert guardrails.is_clean(str(repo)) is False
    verdict = _accept(repo)
    assert verdict.accepted is False
    assert "app/conftest.py" in verdict.escapes
    assert verdict.rolled_back is True and verdict.tree_clean_after is True
    assert not (repo / "app" / "conftest.py").exists()
    assert not (repo / "app" / ".gitignore").exists()


def test_g2_changed_committed_gitignore_cannot_hide_a_planted_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n", ".gitignore": "build/\n"})
    with (repo / ".gitignore").open("a") as fh:
        fh.write("conftest.py\n")
    (repo / "src" / "conftest.py").write_text("x = 1\n")

    assert "src/conftest.py" in guardrails.changed_paths(str(repo))
    verdict = _accept(repo, do_rollback=False)
    assert verdict.accepted is False
    assert "src/conftest.py" in verdict.escapes


def test_g2_output_ignored_by_an_unchanged_committed_rule_stays_invisible(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n", ".gitignore": "build/\n.venv/\n"})
    guardrails.ensure_engine_state_excluded(repo)
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("artifact\n")
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / ".venv" / ".gitignore").write_text("*\n")  # what venv tools write
    (repo / ".venv" / "lib" / "mod.py").write_text("x = 1\n")
    (repo / ".hw" / "r").mkdir(parents=True)
    (repo / ".hw" / "r" / "state.json").write_text("{}\n")
    (repo / "src" / "b.py").write_text("b = 1\n")

    assert guardrails.changed_paths(str(repo)) == ["src/b.py"]
    verdict = _accept(repo, do_rollback=False)
    assert verdict.accepted is True and verdict.escapes == []


# ---- G3: inheritance compares the donor with the run's plan-time collection -----------------

TOOL = "#!/bin/sh\nexit 0\n"
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
PRODUCT_FILES = {
    "prod/calc.py": "def add(a, b):\n    return a + b\n",
    "prod/tool.sh": TOOL,
    "prod/pytest.ini": "[pytest]\ntestpaths = tests\n",
    "prod/tests/test_other.py": "def test_other():\n    assert True\n",
    "prod/tests/conftest.py": DESELECT_HOOK,
    "prod/tests/test_calc.py": ("from calc import add\n\n\ndef test_add():\n"
                                "    assert add(2, 2) == 4\n\n\ndef test_also():\n"
                                "    assert add(0, 0) == 0\n"),
}


def _plan(repo: Path, run_id: str) -> None:
    engine.plan(
        repo_root=repo, run_id=run_id, mode=BuildMode.IMPROVE, product_dir="prod",
        objective=BuildObjective(id="o", title="Protect baseline", objective="improve calc"),
    )


def _planned_product(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    for name, content in PRODUCT_FILES.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    (repo / "prod" / "tool.sh").chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _plan(repo, "r")
    return repo, repo / "prod"


def _nothing_captured(repo: Path, run_id: str) -> bool:
    paths = engine.RunPaths(repo, run_id)
    return (not paths.artifact(regression.BASELINE_FILE).exists()
            and engine.load_state(paths).regression_baseline_sha256 is None)


def test_g3_inheritance_refuses_a_donor_that_lost_a_planned_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, product = _planned_product(tmp_path)
    (product / "tool.sh").chmod(0o745)  # metadata git does not store deselects test_add
    original = regression.require_base_commit_witness
    with monkeypatch.context() as patched:  # a donor captured before the S0.2q comparison
        patched.setattr(regression, "require_base_commit_witness",
                        lambda target, base, baseline, repo_root=None, planned=None:
                        original(target, base, baseline, repo_root))
        donor = engine.regression_baseline(repo_root=repo, run_id="r", target=product)
    assert "tests/test_calc.py::test_add" not in donor["baseline"]["passing_ids"]
    _plan(repo, "r2")
    source = engine.RunPaths(repo, "r").artifact(regression.BASELINE_FILE)
    with pytest.raises(engine.EngineError, match="not collected") as refusal:
        engine.inherit_regression_baseline(
            repo_root=repo, run_id="r2", source_run_id="r", source_file=source)
    assert "tests/test_calc.py::test_add" in str(refusal.value)
    assert _nothing_captured(repo, "r2")


def test_g3_inheritance_keeps_an_honest_donor(tmp_path: Path) -> None:
    repo, product = _planned_product(tmp_path)
    engine.regression_baseline(repo_root=repo, run_id="r", target=product)
    _plan(repo, "r2")
    source = engine.RunPaths(repo, "r").artifact(regression.BASELINE_FILE)
    inherited = engine.inherit_regression_baseline(
        repo_root=repo, run_id="r2", source_run_id="r", source_file=source)
    assert "tests/test_calc.py::test_add" in inherited["baseline"]["passing_ids"]


# ---- G4: the patch-budget diffs never run a worker-set external diff or textconv ----------

def test_g4_patch_budget_diff_does_not_run_a_configured_external_diff(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "".join(f"line {i}\n" for i in range(10))})
    marker = tmp_path / "diff-tool-ran"
    tool = tmp_path / "diff-tool.sh"
    tool.write_text(f"#!/bin/sh\necho ran >> '{marker}'\necho '@@ -1 +1,1 @@'\n")
    tool.chmod(0o755)
    _git(repo, "config", "diff.external", str(tool))
    _git(repo, "config", "diff.evil.textconv", str(tool))
    (repo / ".git" / "info" / "attributes").write_text("*.py diff=evil\n")
    (repo / "src" / "a.py").write_text(
        "".join(f"line {i}\n" for i in range(10)).replace("line 3\n", "changed 3\nadded\n")
    )
    # Precondition: a plain diff in this worktree really runs the configured tool.
    _git(repo, "diff", "--unified=0", "HEAD")
    assert marker.exists()
    marker.unlink()

    assert guardrails._numstat(str(repo)) == {"src/a.py": (2, 1)}
    assert guardrails._largest_hunk_span(str(repo), "src/a.py") == 2
    assert not marker.exists(), "the engine executed the worker's external diff or textconv"
