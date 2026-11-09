"""CLI-surface behaviour, incl. the Phase-2 IMPROVE mode and the quality commands."""

from __future__ import annotations

import json
from pathlib import Path

from hyperwilfred_core.cli import main


def test_improve_mode_plans_the_improve_graph(tmp_path: Path, capsys) -> None:
    # Phase 2: IMPROVE is implemented — `hw plan --mode improve` now stands up the IMPROVE graph.
    code = main(
        [
            "--repo",
            str(tmp_path),
            "plan",
            "--objective",
            "improve the thing",
            "--run-id",
            "imp",
            "--mode",
            "improve",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "improve"
    # the net-new IMPROVE front + the shared quality/verify/ship core
    assert out["steps"] == [
        "comprehend",
        "locate",
        "implement",
        "regression",
        "quality",
        "verify",
        "ship",
    ]
    assert out["first_step"]["step"]["id"] == "comprehend"


def test_engine_error_returns_exit_2(tmp_path: Path, capsys) -> None:
    # next-step on a non-existent run -> clean engine error, exit 2
    code = main(["--repo", str(tmp_path), "next-step", "--run-id", "ghost"])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "engine_error"


def test_plan_then_next_step_via_cli(tmp_path: Path, capsys) -> None:
    code = main(
        ["--repo", str(tmp_path), "plan", "--objective", "build x", "--run-id", "c1"]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run_id"] == "c1"
    assert out["first_step"]["step"]["id"] == "frame"


def test_plan_records_stack_via_cli(tmp_path: Path, capsys) -> None:
    code = main(
        ["--repo", str(tmp_path), "plan", "--objective", "a node app", "--run-id", "c2",
         "--stack", "node"]
    )
    assert code == 0
    capsys.readouterr()
    obj = json.loads((tmp_path / ".hw" / "c2" / "objective.json").read_text(encoding="utf-8"))
    assert obj["stack"] == "node"


def test_quality_commands_are_wired_and_error_cleanly(tmp_path: Path, capsys) -> None:
    # quality-status on a non-existent run → clean engine error, exit 2 (surface, not traceback)
    assert main(["--repo", str(tmp_path), "quality-status", "--run-id", "ghost"]) == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "engine_error"
    # record-subjective with a missing result file → clean quality error, exit 2
    main(["--repo", str(tmp_path), "plan", "--objective", "x", "--run-id", "c3"])
    capsys.readouterr()
    code = main(
        ["--repo", str(tmp_path), "record-subjective", "--run-id", "c3",
         "--result-file", str(tmp_path / "nope.json")]
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "quality_error"


def test_traversal_run_id_is_a_structured_error_not_a_traceback(tmp_path: Path, capsys) -> None:
    code = main(["--repo", str(tmp_path), "codex-batches", "--run-id", "../outside"])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "input_error"
    assert "run_id" in out["error"]
