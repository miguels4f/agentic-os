"""End-to-end BUILD spine walk-through (no LLM, no sub-agents — the engine's job only).

Simulates the full Phase-1 loop the Claude Code adapter drives:
plan -> frame -> decompose -> scaffold -> implement (fan-out + accept) -> evaluate (gate) ->
verify -> ship. The "adapter/worker" steps are stubbed by the test writing the artifacts the
adapter would write, so this proves the ENGINE sequences and gates correctly, and that the
guardrail accepts an in-scope build and would reject an out-of-scope one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from test_gates import _token, approval_key  # noqa: F401

from hyperwilfred_core import engine
from hyperwilfred_core.schemas import BuildObjective, Plan, Slice


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_full_build_spine_reaches_ship(git_repo: Path, approval_key) -> None:  # noqa: F811
    repo = git_repo
    obj = BuildObjective(
        id="obj-e2e",
        title="tiny library",
        objective="build a tiny add() library with a passing test",
        acceptance_criteria=["add() returns the sum", "tests pass"],
    )
    state = engine.plan(repo_root=repo, objective=obj, run_id="e2e", product_dir="product")
    paths = engine.RunPaths(repo, "e2e")
    assert state.product_dir == "product"

    # --- step: frame (single) ---
    s = engine.next_step(repo_root=repo, run_id="e2e")
    assert s["step"]["id"] == "frame"
    paths.artifact("frame.md").write_text("scope: add() library\n", encoding="utf-8")
    engine.complete_step(repo_root=repo, run_id="e2e", step_id="frame")

    # --- step: decompose (single) -> writes plan.json ---
    s = engine.next_step(repo_root=repo, run_id="e2e")
    assert s["step"]["id"] == "decompose"
    plan_obj = Plan(
        objective_id="obj-e2e",
        product_dir="product",
        slices=[
            Slice(
                id="lib",
                title="add() implementation",
                allowed_globs=["product/**"],
                acceptance_criteria=["add returns sum"],
            )
        ],
    )
    paths.plan.write_text(plan_obj.model_dump_json(indent=2), encoding="utf-8")
    engine.complete_step(repo_root=repo, run_id="e2e", step_id="decompose")

    # --- step: scaffold (single) ---
    s = engine.next_step(repo_root=repo, run_id="e2e")
    assert s["step"]["id"] == "scaffold"
    paths.artifact("scaffold.md").write_text("created product/\n", encoding="utf-8")
    engine.complete_step(repo_root=repo, run_id="e2e", step_id="scaffold")

    # --- step: implement (fanout) ---
    s = engine.next_step(repo_root=repo, run_id="e2e")
    assert s["step"]["id"] == "implement"
    assert s["kind"] == "fanout"
    fo = engine.fanout_plan(repo_root=repo, run_id="e2e")
    assert len(fo["waves"]) == 1 and fo["waves"][0][0]["packet_id"] == "lib"
    engine.register_worktree(repo_root=repo, run_id="e2e", packet_id="lib", worktree=str(repo))

    # the "worker" writes IN-SCOPE files into product/ + its result file
    product = repo / "product"
    product.mkdir()
    (product / "lib.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (product / "test_lib.py").write_text(
        "from lib import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    result_file = repo / paths.results_dir.relative_to(repo) / "lib.json"
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text('{"slice": "lib", "status": "done"}\n', encoding="utf-8")

    out = engine.record_result(
        repo_root=repo, run_id="e2e", packet_id="lib", worktree=str(repo)
    )
    assert out["verdict"]["accepted"] is True, out["verdict"]
    engine.complete_step(repo_root=repo, run_id="e2e", step_id="implement")

    # commit the accepted work so the next diff baseline is clean
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "implement lib")

    # --- step: evaluate (gate) — deterministic gates over the product, must PASS ---
    s = engine.next_step(repo_root=repo, run_id="e2e")
    assert s["step"]["id"] == "evaluate"
    from hyperwilfred_core.evaluators import tool_on_path

    if not all(tool_on_path(t) for t in ("ruff", "mypy", "pytest")):
        import pytest

        pytest.skip("toolchain (ruff/mypy/pytest) not on PATH — gate wiring covered elsewhere")
    ev = engine.evaluate(repo_root=repo, run_id="e2e", target=product)
    assert ev["passed"] is True, ev
    assert (paths.run_dir / "evidence" / "verification.json").exists()

    # --- step: quality (loop) — 0-5 evaluator + critics + (no) patch, clean PASS ---
    from hyperwilfred_core import quality

    s = engine.next_step(repo_root=repo, run_id="e2e")
    assert s["step"]["id"] == "quality"
    assert s["kind"] == "quality"
    st = quality.quality_status(repo_root=repo, run_id="e2e")
    assert st["phase"] == "evaluate" and st["det_gate_step"] == "evaluate"
    # the "evaluator" sub-agent writes an all-5 evaluation (MIN=5 >= threshold)
    subj_path = repo / st["subjective_result_path"]
    subj_path.parent.mkdir(parents=True, exist_ok=True)
    subj_path.write_text(
        '{"evaluator": "subjective-0to5", "iteration": 0, "criteria": ['
        '{"name": "clarity", "score": 5, "justification": "clear"},'
        '{"name": "design", "score": 5, "justification": "cohesive"},'
        '{"name": "robustness", "score": 5, "justification": "handles edges"},'
        '{"name": "api_quality", "score": 5, "justification": "clean"},'
        '{"name": "idiomatic", "score": 5, "justification": "pythonic"}'
        '], "global_score": 0, "issues": [], "suggestions": [], "author": "independent"}\n',
        encoding="utf-8",
    )
    from review_attestation import record_attested

    rs = record_attested(
        quality.record_subjective, repo=repo,
        repo_root=repo, run_id="e2e", result_file=str(subj_path),
    )
    assert rs["global_score"] == 5 and rs["aggregate_rule"] == "min"
    # critics: spawn one per lens, each finds nothing (empty findings)
    cp = quality.critique_plan(repo_root=repo, run_id="e2e")
    for pkt in cp["critics"]:
        crit_path = repo / pkt["result_path"]
        crit_path.parent.mkdir(parents=True, exist_ok=True)
        crit_path.write_text(
            f'{{"critic": "code-critic", "author": "independent", '
            f'"lens": "{pkt["lens"]}", "findings": []}}\n',
            encoding="utf-8",
        )
        record_attested(
            quality.record_critique, repo=repo, repo_root=repo, run_id="e2e",
            packet_id=pkt["packet_id"], result_file=str(crit_path),
        )
    g = quality.quality_gate(repo_root=repo, run_id="e2e")
    assert g["action"] == "pass", g

    # --- step: verify (single) ---
    s = engine.next_step(repo_root=repo, run_id="e2e")
    assert s["step"]["id"] == "verify"
    paths.artifact("verify.md").write_text("product imports + test green\n", encoding="utf-8")
    engine.complete_step(repo_root=repo, run_id="e2e", step_id="verify")

    # --- step: ship (human_gate) — Phase-3 retargets ship onto the disk-file HITL gate ---
    from hyperwilfred_core import gates

    s = engine.next_step(repo_root=repo, run_id="e2e")
    assert s["step"]["id"] == "ship"
    assert s["step"]["kind"] == "human_gate"
    # the adapter opens a HITL gate for the irreversible merge/ship action — the run BLOCKS
    go = gates.gate_open(
        repo_root=repo, run_id="e2e", kind=gates.GateKind.MERGE, step_id="ship",
        action="open a PR", evidence=["evidence/verification.json", "verify.md"],
    )
    assert go["gate"]["status"] == "pending"
    # while no human approval file exists, the gate stays blocking — the engine NEVER self-approves
    st = gates.gate_status(repo_root=repo, run_id="e2e")
    assert st["blocked"] is True and st["pending"] == [go["gate"]["gate_id"]]
    s = engine.next_step(repo_root=repo, run_id="e2e")
    assert s.get("awaiting_human") is True
    # An ephemeral signing fixture verifies protocol behavior; it is NOT a real human approval.
    private, public = approval_key
    signed = _token(repo, go, private, public, "approve")
    appr = gates.gate_approve(
        repo_root=repo, run_id="e2e", gate_id=go["gate"]["gate_id"],
        decision=gates.GateDecision.APPROVE, decided_by="owner",
        token_file=signed,
    )
    assert appr["blocked"] is False
    # the ship step is now DONE and the run completes
    s = engine.next_step(repo_root=repo, run_id="e2e")
    assert s.get("done") is True
