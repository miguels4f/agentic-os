"""Command center: the ``hw command-center snapshot`` action and the snapshot writer."""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from hyperwilfred_core import cli
from hyperwilfred_core.command_center import snapshot
from hyperwilfred_core.command_center.model import SCHEMA_VERSION, Snapshot


@pytest.fixture(autouse=True)
def _hermetic_memory(tmp_path_factory, monkeypatch) -> Path:
    """No test reads the real shared memory: the default points at a small fixture tree."""
    root = tmp_path_factory.mktemp("memory")
    (root / "MEMORY.md").write_text("- [Note](note.md) — hook\n", encoding="utf-8")
    (root / "note.md").write_text(
        "---\nname: note\ndescription: d\nmetadata:\n  type: project\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(snapshot, "default_memory_dir", lambda: root)
    return root


def _run(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(["command-center", "snapshot", *argv])
    return code, json.loads(capsys.readouterr().out)


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_snapshot_command_writes_a_valid_snapshot(hw_history: Path, tmp_path, capsys) -> None:
    out_dir = tmp_path / "out" / "command-center"
    code, printed = _run(capsys, "--workspace", str(hw_history), "--out-dir", str(out_dir))
    assert code == 0
    assert set(printed) == {"snapshot", "totals", "problems"}
    target = Path(printed["snapshot"])
    assert target == out_dir.resolve() / "snapshot.json"
    assert printed["problems"] == 0
    assert printed["totals"] == {
        "projects": 1,
        "repositories": 0,
        "runs": 3,
        "run_copies": 3,
        "runs_by_status": printed["totals"]["runs_by_status"],
        "problems": 0,
    }
    assert sum(printed["totals"]["runs_by_status"].values()) == 3

    loaded = Snapshot.model_validate_json(target.read_text(encoding="utf-8"))
    assert loaded.schema_version == SCHEMA_VERSION == "wilfred-command-center-snapshot/v1"
    assert loaded.workspace == str(hw_history.resolve())
    assert loaded.generated_at.endswith("Z")
    assert loaded.generator.wilfred_version
    assert loaded.attention == []
    assert loaded.memory is not None and loaded.memory.notes == 1
    assert sorted(r.run_id for r in loaded.runs) == ["r1", "r2", "r3"]
    assert loaded.totals.runs == len(loaded.runs) and loaded.totals.problems == len(loaded.problems)

    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert os.listdir(out_dir) == ["snapshot.json"], "no temporary file may be left behind"


def test_problems_do_not_change_the_exit_code(tmp_path, capsys) -> None:
    workspace = tmp_path / "ws"
    run_dir = workspace / ".hw" / "broken"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{nope", encoding="utf-8")
    code, printed = _run(capsys, "--workspace", str(workspace), "--out-dir", str(tmp_path / "out"))
    assert code == 0
    assert printed["problems"] == 1 and printed["totals"]["problems"] == 1
    assert printed["totals"]["runs_by_status"] == {"unknown": 1}
    written = json.loads(Path(printed["snapshot"]).read_text(encoding="utf-8"))
    assert written["runs"][0]["readable"] is False
    assert written["problems"][0]["path"].endswith("broken/state.json")


@pytest.mark.parametrize("relative", [".", "out", "nested/deep/out", ".hw/out"])
def test_output_directory_inside_the_workspace_is_refused(relative, hw_history, capsys) -> None:
    before = _tree(hw_history)
    code, printed = _run(
        capsys, "--workspace", str(hw_history), "--out-dir", str(hw_history / relative)
    )
    assert code == 2
    assert printed["kind"] == "command_center_error"
    assert "inside the workspace" in printed["error"]
    assert _tree(hw_history) == before, "a refused run must not create anything"


def test_output_directory_behind_a_symlink_is_refused(hw_history, tmp_path, capsys) -> None:
    link = tmp_path / "innocent-looking"
    link.symlink_to(hw_history, target_is_directory=True)
    code, printed = _run(capsys, "--workspace", str(hw_history), "--out-dir", str(link / "out"))
    assert code == 2 and "inside the workspace" in printed["error"]
    assert not (hw_history / "out").exists()


def test_output_directory_inside_an_outside_worktree_is_refused(
    tmp_path, git_repo, git_helper, capsys
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    git_repo.rename(workspace / "repo")
    outside = tmp_path / "elsewhere" / "wt"
    git_helper(workspace / "repo", "worktree", "add", "-q", "-b", "side", str(outside))
    code, printed = _run(capsys, "--workspace", str(workspace), "--out-dir", str(outside / "out"))
    assert code == 2 and "inside the repository" in printed["error"]
    assert not (outside / "out").exists()


def test_output_directory_at_a_missing_worktree_path_is_refused(
    tmp_path, git_repo, git_helper, capsys
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    git_repo.rename(workspace / "repo")
    gone = tmp_path / "elsewhere" / "gone"
    git_helper(workspace / "repo", "worktree", "add", "-q", "-b", "side", str(gone))
    shutil.rmtree(gone)
    code, printed = _run(capsys, "--workspace", str(workspace), "--out-dir", str(gone / "out"))
    assert code == 2 and "inside the project" in printed["error"]
    assert not gone.exists()
    shouted = gone.parent / "GONE" / "out"  # another letter case of the missing path
    code, printed = _run(capsys, "--workspace", str(workspace), "--out-dir", str(shouted))
    assert code == 2 and "inside the project" in printed["error"]
    assert os.listdir(gone.parent) == []


def test_output_directory_inside_an_unrelated_repository_is_refused(
    tmp_path, git_repo, capsys
) -> None:
    # ``git_repo`` (tmp_path/"repo") is NOT part of the workspace below.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    code, printed = _run(
        capsys, "--workspace", str(workspace), "--out-dir", str(git_repo / "sub" / "out")
    )
    assert code == 2 and "inside the repository" in printed["error"]
    assert not (git_repo / "sub").exists()


def test_output_directory_spelled_in_another_letter_case_is_refused(tmp_path, capsys) -> None:
    workspace = tmp_path / "WorkSpace"
    workspace.mkdir()
    if not (tmp_path / "workspace").exists():
        pytest.skip("case-sensitive filesystem: the two spellings are different directories")
    code, printed = _run(
        capsys, "--workspace", str(workspace), "--out-dir", str(tmp_path / "workspace" / "OUT")
    )
    assert code == 2 and "inside the workspace" in printed["error"]
    assert os.listdir(workspace) == []


def test_workspace_under_a_secrets_directory_is_refused(tmp_path, capsys) -> None:
    workspace = tmp_path / "secrets" / "ws"
    workspace.mkdir(parents=True)
    code, printed = _run(capsys, "--workspace", str(workspace), "--out-dir", str(tmp_path / "o"))
    assert code == 2 and "never read" in printed["error"]
    assert not (tmp_path / "o").exists()


def test_invalid_workspace_exits_2(tmp_path, capsys) -> None:
    code, printed = _run(
        capsys, "--workspace", str(tmp_path / "missing"), "--out-dir", str(tmp_path / "out")
    )
    assert code == 2 and printed["kind"] == "command_center_error"
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    code, printed = _run(capsys, "--workspace", str(a_file), "--out-dir", str(tmp_path / "out"))
    assert code == 2 and "not a directory" in printed["error"]
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["command-center"],
        ["command-center", "snapshot"],
        ["command-center", "snapshot", "--workspace"],
        ["command-center", "nonsense", "--workspace", "."],
        ["command-center", "snapshot", "--workspace", ".", "--unknown-flag"],
    ],
)
def test_invalid_arguments_exit_2(argv, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 2
    capsys.readouterr()


def test_default_output_directory_is_outside_the_workspace(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    code, printed = _run(capsys, "--workspace", str(workspace))
    assert code == 0
    expected = home.resolve() / "Library" / "Application Support" / "Wilfred" / "command-center"
    assert Path(printed["snapshot"]) == expected / "snapshot.json"
    assert stat.S_IMODE(expected.stat().st_mode) == 0o700
    assert os.listdir(workspace) == []


def test_rerun_replaces_the_snapshot_atomically(hw_history, tmp_path, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    first = snapshot.run_snapshot(workspace=hw_history, out_dir=out_dir)
    target = Path(first["snapshot"])
    original = target.read_text(encoding="utf-8")

    def fail(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr(snapshot.os, "replace", fail)
    with pytest.raises(snapshot.CommandCenterError, match="disk full"):
        snapshot.run_snapshot(workspace=hw_history, out_dir=out_dir)
    assert target.read_text(encoding="utf-8") == original, "a failed write keeps the old file"
    assert os.listdir(out_dir) == ["snapshot.json"], "the temporary file is cleaned up"


def test_output_path_that_is_a_file_is_refused(hw_history, tmp_path, capsys) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    code, printed = _run(capsys, "--workspace", str(hw_history), "--out-dir", str(blocker))
    assert code == 2 and "not a directory" in printed["error"]
    assert blocker.read_text(encoding="utf-8") == "x"


def test_a_write_failure_is_one_json_error_not_a_traceback(hw_history, tmp_path, capsys) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    if os.access(locked, os.W_OK):
        pytest.skip("running with privileges that ignore directory permissions")
    code, printed = _run(capsys, "--workspace", str(hw_history), "--out-dir", str(locked / "out"))
    assert code == 2 and printed["kind"] == "command_center_error"
    assert "could not be written" in printed["error"]


def test_workspace_spelling_is_not_replaced_by_a_sibling_link(tmp_path, capsys) -> None:
    real = tmp_path / "WS"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    typed = tmp_path / "ws"
    if not typed.exists():
        pytest.skip("case-sensitive filesystem")
    code, printed = _run(capsys, "--workspace", str(typed), "--out-dir", str(tmp_path / "out"))
    assert code == 0
    written = json.loads(Path(printed["snapshot"]).read_text(encoding="utf-8"))
    assert written["workspace"] == str(real.resolve())


def test_memory_option_selects_the_directory(hw_history, tmp_path, capsys) -> None:
    memory = tmp_path / "mem"
    (memory / "projects" / "p").mkdir(parents=True)
    (memory / "MEMORY.md").write_text("intro\n- [A](a.md) — x\n- [B](b.md) — y\n", encoding="utf-8")
    front = "---\nname: a\ndescription: d\ntype: user\n---\n"
    (memory / "a.md").write_text(front, encoding="utf-8")
    (memory / "projects" / "p" / "b.md").write_text("no frontmatter\n", encoding="utf-8")
    code, printed = _run(
        capsys, "--workspace", str(hw_history), "--out-dir", str(tmp_path / "out"),
        "--memory", str(memory),
    )
    assert code == 0 and printed["problems"] == 0
    written = json.loads(Path(printed["snapshot"]).read_text(encoding="utf-8"))["memory"]
    assert written["root"] == str(memory.resolve())
    assert written["notes"] == 2 and written["index_lines"] == 2
    assert written["by_type"]["user"] == 1 and written["by_type"]["untyped"] == 1


def test_missing_memory_directory_is_a_problem_not_a_failure(hw_history, tmp_path, capsys) -> None:
    code, printed = _run(
        capsys, "--workspace", str(hw_history), "--out-dir", str(tmp_path / "out"),
        "--memory", str(tmp_path / "no-such-memory"),
    )
    assert code == 0 and printed["problems"] == 1
    written = json.loads(Path(printed["snapshot"]).read_text(encoding="utf-8"))
    assert written["memory"]["notes"] == 0
    assert written["problems"][0]["what"] == "memory directory"


def test_output_directory_inside_the_memory_directory_is_refused(
    hw_history, _hermetic_memory, capsys
) -> None:
    before = sorted(os.listdir(_hermetic_memory))
    code, printed = _run(
        capsys, "--workspace", str(hw_history), "--out-dir", str(_hermetic_memory / "out")
    )
    assert code == 2 and "inside the memory directory" in printed["error"]
    assert sorted(os.listdir(_hermetic_memory)) == before


# --- S2.3: render and build ---


def test_build_writes_snapshot_and_page(hw_history: Path, tmp_path, capsys) -> None:
    out_dir = tmp_path / "out"
    code = cli.main(["command-center", "build", "--workspace", str(hw_history),
                     "--out-dir", str(out_dir)])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert {"snapshot", "page", "totals", "problems"} <= set(printed)
    page = Path(printed["page"])
    assert page == out_dir.resolve() / "index.html"
    assert sorted(os.listdir(out_dir)) == ["index.html", "snapshot.json"]
    assert stat.S_IMODE(page.stat().st_mode) == 0o600
    text = page.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>") and "r1" in text


def test_render_rewrites_the_page_from_an_existing_snapshot(hw_history, tmp_path, capsys) -> None:
    out_dir = tmp_path / "out"
    assert cli.main(["command-center", "snapshot", "--workspace", str(hw_history),
                     "--out-dir", str(out_dir)]) == 0
    capsys.readouterr()
    code = cli.main(["command-center", "render", "--out-dir", str(out_dir)])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and set(printed) == {"snapshot", "page"}
    assert (out_dir / "index.html").is_file()


def test_render_without_a_snapshot_exits_2(tmp_path, capsys) -> None:
    code = cli.main(["command-center", "render", "--out-dir", str(tmp_path / "empty")])
    printed = json.loads(capsys.readouterr().out)
    assert code == 2 and printed["kind"] == "command_center_error"
    assert "no snapshot" in printed["error"]
    assert not (tmp_path / "empty").exists(), "a failed render creates nothing"


def test_render_with_an_invalid_snapshot_exits_2(tmp_path, capsys) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for content in ("{not json", '{"schema_version": "something-else/v9"}', "[]"):
        (out_dir / "snapshot.json").write_text(content, encoding="utf-8")
        code = cli.main(["command-center", "render", "--out-dir", str(out_dir)])
        printed = json.loads(capsys.readouterr().out)
        assert code == 2 and "not a valid command-center snapshot" in printed["error"]
        assert not (out_dir / "index.html").exists()


def test_render_refuses_a_snapshot_whose_workspace_contains_the_output(
    hw_history, tmp_path, capsys
) -> None:
    """A snapshot copied into its own workspace must not make render write there."""
    out_dir = tmp_path / "out"
    assert cli.main(["command-center", "snapshot", "--workspace", str(hw_history),
                     "--out-dir", str(out_dir)]) == 0
    capsys.readouterr()
    inside = hw_history / "copied"
    inside.mkdir()
    shutil.copy(out_dir / "snapshot.json", inside / "snapshot.json")
    code = cli.main(["command-center", "render", "--out-dir", str(inside)])
    printed = json.loads(capsys.readouterr().out)
    assert code == 2 and "inside the workspace" in printed["error"]
    assert not (inside / "index.html").exists()


def test_build_output_inside_the_workspace_is_refused(hw_history, capsys) -> None:
    before = _tree(hw_history)
    code = cli.main(["command-center", "build", "--workspace", str(hw_history),
                     "--out-dir", str(hw_history / "out")])
    printed = json.loads(capsys.readouterr().out)
    assert code == 2 and "inside the workspace" in printed["error"]
    assert _tree(hw_history) == before


def test_render_refuses_output_inside_a_project_named_by_the_snapshot(
    hw_history, tmp_path, capsys
) -> None:
    out_dir = tmp_path / "out"
    assert cli.main(["command-center", "snapshot", "--workspace", str(hw_history),
                     "--out-dir", str(out_dir)]) == 0
    capsys.readouterr()
    data = json.loads((out_dir / "snapshot.json").read_text(encoding="utf-8"))
    data["workspace"] = str(tmp_path / "elsewhere")
    data["projects"][0]["path"] = str(out_dir)
    (out_dir / "snapshot.json").write_text(json.dumps(data), encoding="utf-8")
    code = cli.main(["command-center", "render", "--out-dir", str(out_dir)])
    printed = json.loads(capsys.readouterr().out)
    assert code == 2 and "inside the project" in printed["error"]
    assert not (out_dir / "index.html").exists()


def test_render_with_boundary_dates_still_renders(hw_history, tmp_path, capsys) -> None:
    out_dir = tmp_path / "out"
    assert cli.main(["command-center", "snapshot", "--workspace", str(hw_history),
                     "--out-dir", str(out_dir)]) == 0
    capsys.readouterr()
    text = (out_dir / "snapshot.json").read_text(encoding="utf-8")
    data = json.loads(text)
    data["generated_at"] = "0001-01-01T00:00:00Z"
    data["runs"][0]["updated_at"] = "9999-12-31T23:59:59-10:00"
    (out_dir / "snapshot.json").write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(["command-center", "render", "--out-dir", str(out_dir)]) == 0
    capsys.readouterr()
