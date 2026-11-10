"""Synthetic protocol checks, not real independent reviews or signed evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_quality import _run_at_quality

from hyperwilfred_core import engine, quality
from hyperwilfred_core.schemas import (
    CritiqueProbe,
    CritiqueReport,
    SubjectiveCriterion,
    SubjectiveEvaluation,
)


def _git_base(repo: Path) -> None:
    """Create history only inside this test's disposable fixture repository."""
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "fixture@example.test"), ("user.name", "Fixture")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)


@pytest.mark.parametrize(
    "review", ["subjective", *quality.CRITIC_LENSES, quality.ADVERSARIAL_LENS],
)
def test_unsigned_review_challenges_bind_each_review_to_current_bytes_with_synthetic_probe(
    tmp_path: Path, review: str,
) -> None:
    product = tmp_path / "product"
    product.mkdir()
    source = product / "code.py"
    source.write_text("VALUE = 1\n")
    _git_base(tmp_path)
    _run_at_quality(tmp_path)
    source.write_text("VALUE = 2\n")
    paths = engine.RunPaths(tmp_path, "q")
    state = engine.load_state(paths)
    state.objective.critic_lenses = [*quality.CRITIC_LENSES, quality.ADVERSARIAL_LENS]
    engine.save_state(paths, state)
    if review == "subjective":
        result = tmp_path / ".hw/q/subjective-input.json"
        result.write_text(SubjectiveEvaluation(
            evaluator="fixture",
            criteria=[SubjectiveCriterion(name="current bytes", score=5)],
        ).model_dump_json())
        gate_id = "subjective-0"
        expected = quality._review_scope_sha256(tmp_path, "q", state, gate_id)
        recorded = quality.record_subjective(
            repo_root=tmp_path, run_id="q", result_file=str(result), author="independent",
        )
    else:
        packets = quality.critique_plan(repo_root=tmp_path, run_id="q")["critics"]
        packet = next(item for item in packets if item["lens"] == review)
        result = tmp_path / packet["result_path"]
        result.parent.mkdir(parents=True, exist_ok=True)
        probes = [CritiqueProbe(
            input_class="synthetic gate bypass fixture",
            command="fixture --probe gate-bypass",
            observed_output="blocked",
        )] if review == quality.ADVERSARIAL_LENS else []
        result.write_text(CritiqueReport(
            critic="fixture", lens=review, findings=[], probes=probes,
        ).model_dump_json())
        gate_id = packet["packet_id"]
        expected = quality._review_scope_sha256(tmp_path, "q", state, gate_id)
        recorded = quality.record_critique(
            repo_root=tmp_path, run_id="q", packet_id=gate_id,
            result_file=str(result), author="independent",
        )
    challenge = json.loads(Path(recorded["attestation_challenge"]).read_text())
    assert f"scope_sha256:{expected}" in challenge["evidence"]
    assert recorded["author"] == "adapter"
    source.write_text("VALUE = 3\n")
    changed = quality._review_scope_sha256(tmp_path, "q", state, gate_id)
    assert changed != expected
    assert f"scope_sha256:{changed}" not in challenge["evidence"]
