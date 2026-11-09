"""Phase 5 — observability: disk-observing anomaly / alert / dashboard, retargeted to code."""

from __future__ import annotations

import ast
from pathlib import Path

from hyperwilfred_core import escalation, observability
from hyperwilfred_core.schemas import AnomalyCode, AnomalySeverity

SRC = Path(__file__).resolve().parent.parent / "src" / "hyperwilfred_core"


def _codes(repo: Path) -> set[str]:
    return {a.code.value for a in observability.detect_anomalies(repo_root=repo)}


def test_observe_run_mines_disk_evidence(hw_history: Path) -> None:
    obs = observability.observe_run(repo_root=hw_history, run_id="r1")
    assert obs.run_id == "r1"
    assert obs.quality_used_fallback is True
    assert obs.diff_over_budget_packets == 1
    assert obs.gate_failures == 2
    assert obs.rejected_packets == 1


def test_detects_every_code_signal(hw_history: Path) -> None:
    codes = _codes(hw_history)
    assert AnomalyCode.PATCH_LOOP_MAXED.value in codes
    assert AnomalyCode.DIFF_OVER_BUDGET.value in codes
    assert AnomalyCode.REPEATED_GATE_FAIL.value in codes
    assert AnomalyCode.HIGH_REJECT_RATE.value in codes
    assert AnomalyCode.HIGH_ROLLBACK_RATE.value in codes
    assert AnomalyCode.ESCALATION_SPIKE.value in codes


def test_reject_rate_needs_minimum_sample(tmp_path: Path) -> None:
    """A single rejected packet (1/1) must NOT trip the high-reject-rate rate signal."""
    from hyperwilfred_core.engine import RunPaths, plan
    from hyperwilfred_core.schemas import AcceptanceVerdict, BuildObjective

    repo = tmp_path / "repo"
    plan(repo_root=repo, objective=BuildObjective(id="o", title="t", objective="x"), run_id="solo")
    p = RunPaths(repo, "solo")
    p.verdicts_dir.mkdir(parents=True, exist_ok=True)
    (p.verdicts_dir / "s0.json").write_text(
        AcceptanceVerdict(packet_id="s0", worktree="wt", accepted=False, rolled_back=True,
                          escapes=["x.py"]).model_dump_json(),
        encoding="utf-8",
    )
    codes = {a.code.value for a in observability.detect_run_anomalies(
        observability.observe_run(repo_root=repo, run_id="solo"))}
    assert AnomalyCode.HIGH_REJECT_RATE.value not in codes  # below MIN_PACKETS_FOR_RATE


def test_dashboard_json_and_text_views(hw_history: Path) -> None:
    dash = observability.build_dashboard(repo_root=hw_history)
    assert dash.total_runs == 3
    assert dash.anomalies
    # JSON + text persisted on disk
    obs_dir = hw_history / ".hw" / observability.OBSERVABILITY_DIR
    assert (obs_dir / observability.DASHBOARD_JSON).exists()
    text = (obs_dir / observability.DASHBOARD_TXT).read_text(encoding="utf-8")
    assert "Observability Dashboard" in text
    assert "Anomalies:" in text


def test_memory_index_persisted(hw_history: Path) -> None:
    observability.build_dashboard(repo_root=hw_history)
    mem = hw_history / ".hw" / observability.OBSERVABILITY_DIR / observability.MEMORY_JSON
    assert mem.exists()
    from hyperwilfred_core.schemas import CodebaseMemoryIndex
    index = CodebaseMemoryIndex.model_validate_json(mem.read_text(encoding="utf-8"))
    assert {e.run_id for e in index.entries} == {"r1", "r2", "r3"}


def test_high_severity_alerts_routed_to_escalation_sink(hw_history: Path) -> None:
    report = observability.anomalies_report(repo_root=hw_history, alert=True)
    routed = report["alerts_routed"]
    # only HIGH severity anomalies route
    assert routed, "expected at least one HIGH-severity alert routed"
    escalated = escalation.list_escalations(repo_root=hw_history)
    assert escalated
    assert all(e.severity is AnomalySeverity.HIGH for e in escalated)
    assert all(e.source == "observability" for e in escalated)


def test_alert_routing_is_idempotent_and_stable(hw_history: Path) -> None:
    """No feedback loop: re-running yields the same anomaly + escalation set."""
    first = observability.anomalies_report(repo_root=hw_history, alert=True)
    n_esc_1 = len(escalation.list_escalations(repo_root=hw_history))
    second = observability.anomalies_report(repo_root=hw_history, alert=True)
    n_esc_2 = len(escalation.list_escalations(repo_root=hw_history))
    assert first["count"] == second["count"], "anomaly count must be stable across runs"
    assert n_esc_1 == n_esc_2, "no new escalations on a second pass (idempotent)"
    assert second["alerts_routed"] == [], "nothing new to route the second time"


def test_no_alert_flag_skips_routing(hw_history: Path) -> None:
    report = observability.anomalies_report(repo_root=hw_history, alert=False)
    assert report["alerts_routed"] == []
    assert escalation.list_escalations(repo_root=hw_history) == []


def test_empty_repo_dashboard(tmp_path: Path) -> None:
    dash = observability.build_dashboard(repo_root=tmp_path / "empty")
    assert dash.total_runs == 0 and dash.anomalies == []


def test_observability_is_llm_free_and_spawn_free() -> None:
    """AST import check — the module shells out to nothing and imports no LLM SDK."""
    mods: set[str] = set()
    for node in ast.walk(ast.parse((SRC / "observability.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    forbidden = ("anthropic", "openai", "subprocess", "common_py", "urllib", "http", "requests")
    offenders = [m for m in mods if m.startswith(forbidden)]
    assert offenders == [], f"observability imports forbidden modules: {offenders}"
