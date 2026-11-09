"""G3 — per-run cost accounting (``cost.py``, ``hw record-usage``, ``hw cost-report``).

The counting rules are each pinned by a test because each is a real bug seen in the wild:
line-summing a streamed response (2.5-3x inflation), dropping sub-agent transcripts (the pilot
sessions put 42-56% of tokens there), and attributing pre-run chat to the run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hyperwilfred_core import cost, engine
from hyperwilfred_core.cli import main
from hyperwilfred_core.schemas import BuildObjective

SESSION = "0a1b2c3d-1111-2222-3333-444455556666"


def _assistant(msg_id: str, ts: datetime, *, sidechain: bool = False, model: str = "m-main",
               inp: int = 1, out: int = 10, cw: int = 100, cr: int = 1000) -> str:
    return json.dumps({
        "type": "assistant",
        "isSidechain": sidechain,
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "message": {
            "id": msg_id, "model": model,
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr},
        },
    })


def _write_session(projects: Path, t0: datetime) -> Path:
    """A main transcript + one sub-agent transcript, with every counting trap in it."""
    main_path = projects / "-some-project" / f"{SESSION}.jsonl"
    main_path.parent.mkdir(parents=True)
    us = timedelta(microseconds=1)
    lines = [
        _assistant("pre", t0 - timedelta(hours=1), out=999_999),  # before the run: excluded
        _assistant("m1", t0 + us, out=5),       # streamed: 3 lines, same id, same usage …
        _assistant("m1", t0 + 2 * us, out=5),
        _assistant("m1", t0 + 3 * us, out=10),  # … last line wins → counted ONCE with out=10
        json.dumps({"type": "user", "timestamp": t0.isoformat(), "message": {"content": "x"}}),
        json.dumps({"type": "assistant", "timestamp": t0.isoformat(),
                    "message": {"id": "nousage"}}),
        "not json at all",
        _assistant("m2", t0 + 4 * us, model="m-other"),
    ]
    main_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sub = main_path.parent / SESSION / "subagents" / "agent-1.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(
        _assistant("s1", t0 + 5 * us, sidechain=True, out=20) + "\n"
        + _assistant("s2", t0 + 6 * us, sidechain=True, out=30) + "\n",
        encoding="utf-8",
    )
    return main_path


# --------------------------------------------------------------------------- summarize


def test_summarize_dedupes_windows_and_splits_main_vs_subagent(tmp_path: Path) -> None:
    t0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    transcript = _write_session(tmp_path, t0)
    files = cost.transcript_files(transcript)
    assert len(files) == 2  # the sub-agent transcript is found automatically

    s = cost.summarize(files, since=t0, until=t0 + timedelta(minutes=1))
    assert s["totals"]["messages"] == 4  # m1 (once), m2, s1, s2 — not "pre", not "nousage"
    assert s["totals"]["output_tokens"] == 10 + 10 + 20 + 30
    assert s["by_role"]["main"]["output_tokens"] == 20
    assert s["by_role"]["subagent"]["output_tokens"] == 50
    assert s["by_model"]["m-other"]["messages"] == 1
    assert s["totals"]["cache_read_input_tokens"] == 4 * 1000


def test_main_transcript_only_would_undercount(tmp_path: Path) -> None:
    """The ECC cost-tracker reads only the main transcript; that misses every sub-agent token."""
    t0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    transcript = _write_session(tmp_path, t0)
    window = {"since": t0, "until": t0 + timedelta(minutes=1)}
    main_only = cost.summarize([transcript], **window)["totals"]["output_tokens"]
    full = cost.summarize(cost.transcript_files(transcript), **window)["totals"]["output_tokens"]
    assert main_only == 20 and full == 70


def test_wall_clock_from_events() -> None:
    t0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    events = [
        {"stage": "plan", "timestamp": t0.isoformat()},
        {"stage": "evaluate", "timestamp": (t0 + timedelta(seconds=30)).isoformat()},
        {"stage": "evaluate", "timestamp": (t0 + timedelta(seconds=90)).isoformat()},
        {"stage": "ship", "timestamp": "garbage"},
    ]
    wc = cost.wall_clock(events)
    assert wc["total_s"] == 90.0 and wc["active_s"] == 90.0 and wc["max_gap_s"] == 60.0
    assert wc["stages"] == {"plan": 0.0, "evaluate": 60.0}
    assert cost.wall_clock([])["total_s"] is None


def test_human_wait_is_separated_from_active_time() -> None:
    t0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

    def at(minutes: int, status: str) -> dict:
        return {"stage": "ship", "status": status,
                "timestamp": (t0 + timedelta(minutes=minutes)).isoformat()}

    wc = cost.wall_clock([at(0, "running"), at(10, "awaiting_human"), at(610, "accepted"),
                          at(612, "completed")])
    assert wc["total_s"] == 612 * 60
    assert wc["human_wait_s"] == 600 * 60  # the open ship gate, 10 h
    assert wc["active_s"] == 12 * 60


# --------------------------------------------------------------------------- CLI


def _plan(repo: Path) -> datetime:
    engine.plan(repo_root=repo, objective=BuildObjective(id="o", title="t", objective="x"),
                run_id="r")
    return engine.load_state(engine.RunPaths(repo, "r")).created_at


def test_record_usage_then_cost_report_via_cli(tmp_path: Path, capsys) -> None:
    repo, projects = tmp_path / "repo", tmp_path / "projects"
    repo.mkdir()
    created = _plan(repo)
    _write_session(projects, created)

    code = main(["--repo", str(repo), "record-usage", "--run-id", "r",
                 "--session-id", SESSION, "--projects-dir", str(projects)])
    assert code == 0
    recorded = json.loads(capsys.readouterr().out)["usage"]
    assert recorded["totals"]["output_tokens"] == 70
    assert len(recorded["sources"]) == 2 and all(s["sha256"] for s in recorded["sources"])
    assert (repo / ".hw" / "r" / cost.USAGE_FILE).exists()

    code = main(["--repo", str(repo), "cost-report", "--run-id", "r"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tokens"]["by_role"]["subagent"]["output_tokens"] == 50
    assert report["wall_clock"]["total_s"] is not None and report["note"] is None
    stages = [json.loads(line)["stage"] for line in
              (repo / ".hw" / "r" / "events.jsonl").read_text().splitlines()]
    assert "cost" in stages  # the recording itself is on the ledger


def test_cost_report_without_usage_says_so(tmp_path: Path, capsys) -> None:
    _plan(tmp_path)
    assert main(["--repo", str(tmp_path), "cost-report", "--run-id", "r"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tokens"] is None and "record-usage" in report["note"]
    assert report["wall_clock"]["total_s"] == 0.0


@pytest.mark.parametrize("bad", ["../../etc/passwd", "*", "a/b", "short"])
def test_session_id_is_validated_before_globbing(tmp_path: Path, bad: str) -> None:
    with pytest.raises(cost.CostError):
        cost.find_session_transcript(bad, tmp_path)


def test_missing_transcript_is_a_clean_exit_2(tmp_path: Path, capsys) -> None:
    _plan(tmp_path)
    code = main(["--repo", str(tmp_path), "record-usage", "--run-id", "r",
                 "--transcript", str(tmp_path / "nope.jsonl")])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "cost_error"
