"""Command center: ``--ops-bin`` and the ``ops`` section of ``snapshot.json``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ops_fakes import export_report, fake_ops

from hyperwilfred_core import cli
from hyperwilfred_core.command_center import snapshot
from hyperwilfred_core.command_center.model import Snapshot


def _setup(tmp_path: Path, **fake) -> tuple[Path, Path, Path, Path]:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("", encoding="utf-8")
    workspace = tmp_path / "ws"
    (workspace / "ops").mkdir(parents=True)
    report = export_report(root=str(workspace / "ops"))
    program = fake_ops(tmp_path / "bin", export=report, **fake)
    return workspace, memory, program, tmp_path / "out"


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(["command-center", *argv])
    return code, json.loads(capsys.readouterr().out)


def _written(out: Path) -> Snapshot:
    return Snapshot.model_validate_json((out / "snapshot.json").read_text(encoding="utf-8"))


def test_build_with_ops_bin_writes_the_ops_section_and_keeps_it_in_the_refresh_command(
    tmp_path, capsys
) -> None:
    workspace, memory, program, out = _setup(tmp_path)
    code, printed = _cli(capsys, "build", "--workspace", str(workspace), "--out-dir", str(out),
                         "--memory", str(memory), "--ops-bin", str(program))
    assert code == 0 and printed["problems"] == 0
    written = _written(out)
    assert written.ops is not None and written.ops.bin == str(program)
    assert [t.id for t in written.ops.open_tasks] == ["T-0001", "T-0002"]
    assert written.ops.unapproved_edits == ["sales/volotea.md"]
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "--ops-bin" in page, "the refresh command keeps a non-default ops program"


def test_the_default_program_is_used_and_not_repeated_in_the_refresh_command(
    tmp_path, capsys, monkeypatch
) -> None:
    workspace, memory, program, out = _setup(tmp_path)
    monkeypatch.setattr(snapshot, "default_ops_bin", lambda: program)
    code, _ = _cli(capsys, "build", "--workspace", str(workspace), "--out-dir", str(out),
                   "--memory", str(memory))
    assert code == 0
    written = _written(out)
    assert written.ops is not None and written.ops.bin == str(program)
    assert "--ops-bin" not in (out / "index.html").read_text(encoding="utf-8")


def test_an_ops_failure_is_a_read_problem_and_the_command_still_succeeds(
    tmp_path, capsys
) -> None:
    workspace, memory, program, out = _setup(tmp_path, exit_code=2, stderr="usage: ops\n")
    code, printed = _cli(capsys, "snapshot", "--workspace", str(workspace), "--out-dir",
                         str(out), "--memory", str(memory), "--ops-bin", str(program))
    assert code == 0 and printed["problems"] == 1
    written = json.loads((out / "snapshot.json").read_text(encoding="utf-8"))
    assert written["ops"]["open_tasks"] is None
    assert [(p["what"], p["reason"]) for p in written["problems"]] == [
        ("ops export", "ops exited 2: usage: ops")
    ]


def test_a_workspace_without_ops_has_no_ops_section(tmp_path, capsys) -> None:
    workspace, memory, program, out = _setup(tmp_path)
    (workspace / "ops").rmdir()
    code, _ = _cli(capsys, "snapshot", "--workspace", str(workspace), "--out-dir", str(out),
                   "--memory", str(memory), "--ops-bin", str(program))
    assert code == 0
    assert json.loads((out / "snapshot.json").read_text(encoding="utf-8"))["ops"] is None
    assert not list((tmp_path / "bin").glob("args-*.txt"))


def test_a_relative_ops_bin_is_refused_before_anything_runs(tmp_path, capsys) -> None:
    workspace, memory, program, out = _setup(tmp_path)
    for command in ("snapshot", "build"):
        with pytest.raises(SystemExit) as raised:
            cli.main(["command-center", command, "--workspace", str(workspace), "--out-dir",
                      str(out), "--memory", str(memory), "--ops-bin", "bin/ops"])
        assert raised.value.code == 2
        assert "must be an absolute path" in capsys.readouterr().err
    assert not out.exists()
    assert not list((tmp_path / "bin").glob("args-*.txt"))
