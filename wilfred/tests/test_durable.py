"""Env-guarded durable wrapper (Phase 4).

Two guarantees under test: durability is engaged ONLY when configured, and the pure fallback
path ALWAYS works with no external dependency (no DBOS installed → the run still completes).
All tests are hermetic — no Postgres, no ``dbos`` package required.
"""

from __future__ import annotations

import pytest

from hyperwilfred_core import durable


def test_dbos_configured_is_doubly_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HW_DURABLE", raising=False)
    monkeypatch.delenv("DBOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DBOS_SYSTEM_DATABASE", raising=False)
    assert durable.dbos_configured() is False

    # Endpoint alone is not enough — a stray DBOS_* var must not silently change behaviour.
    monkeypatch.setenv("DBOS_DATABASE_URL", "postgres://x")
    assert durable.dbos_configured() is False

    # HW_DURABLE alone is not enough either.
    monkeypatch.delenv("DBOS_DATABASE_URL", raising=False)
    monkeypatch.setenv("HW_DURABLE", "1")
    assert durable.dbos_configured() is False

    # Both → configured.
    monkeypatch.setenv("DBOS_DATABASE_URL", "postgres://x")
    assert durable.dbos_configured() is True


def test_pure_path_runs_with_no_external_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HW_DURABLE", raising=False)
    monkeypatch.delenv("DBOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DBOS_SYSTEM_DATABASE", raising=False)
    seen: list[str] = []

    def execute(wave_id: str) -> bool:
        seen.append(wave_id)
        return True

    report = durable.run_waves(["wave-0", "wave-1", "wave-2"], execute)
    assert report.durable is False  # pure fallback, no DBOS
    assert report.all_accepted is True
    assert seen == ["wave-0", "wave-1", "wave-2"]


def test_pure_path_halts_chain_at_first_rejected_wave() -> None:
    seen: list[str] = []

    def execute(wave_id: str) -> bool:
        seen.append(wave_id)
        return wave_id != "wave-1"  # wave-1 rejected → chain halts, wave-2 never runs

    report = durable.run_waves_in_process(["wave-0", "wave-1", "wave-2"], execute)
    assert report.all_accepted is False
    assert [r.wave_id for r in report.results] == ["wave-0", "wave-1"]
    assert seen == ["wave-0", "wave-1"], "no cascade past a halted wave"


def test_run_waves_durable_raises_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HW_DURABLE", raising=False)
    monkeypatch.delenv("DBOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DBOS_SYSTEM_DATABASE", raising=False)
    with pytest.raises(durable.DBOSNotConfigured):
        durable.run_waves_durable(["wave-0"], lambda _w: True)


def test_run_waves_falls_back_to_pure_when_durability_cannot_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Configured for durability, but the dbos package is absent / launch fails: durability must
    # NEVER break a run — run_waves transparently uses the pure path.
    monkeypatch.setattr(durable, "dbos_configured", lambda: True)
    monkeypatch.setattr(durable, "_launch_dbos", lambda: False)
    seen: list[str] = []
    report = durable.run_waves(["wave-0", "wave-1"], lambda w: seen.append(w) or True)
    assert report.durable is False
    assert report.all_accepted is True
    assert seen == ["wave-0", "wave-1"]


def test_run_wave_step_is_a_pure_unit() -> None:
    accepted = durable.run_wave_step("wave-0", lambda _w: True)
    assert accepted.accepted is True
    assert accepted.proceeded is True
    rejected = durable.run_wave_step("wave-0", lambda _w: False)
    assert rejected.accepted is False


def test_empty_wave_list_is_not_accepted() -> None:
    report = durable.run_waves_in_process([], lambda _w: True)
    assert report.results == []
    assert report.all_accepted is False
