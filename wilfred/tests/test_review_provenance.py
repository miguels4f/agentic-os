"""L1 — review provenance: no silent PASS on adapter self-review (pilot replay (a)).

The pilot V1/V2 runs (A and B) passed ``hw quality-gate`` on critiques the orchestrating session
wrote about its own work once the sub-agent critics hit HTTP 429. These tests recreate that state
and prove the gate now returns a distinct non-passing status, fails closed on missing provenance,
loads legacy state as adapter-authored, and honours only an explicit, recorded human override.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from review_attestation import record_attested
from test_approval_boundary import _signed
from test_gates import approval_key  # noqa: F401
from test_quality import _record_subjective, _run_at_quality

from hyperwilfred_core import approvals, engine, gates, quality
from hyperwilfred_core.schemas import (
    CritiqueReport,
    GateKind,
    ReviewAuthor,
    StepStatus,
    SubjectiveCriterion,
    SubjectiveEvaluation,
)


def _write_critique(repo: Path, run_id: str, lens: str, author: ReviewAuthor | None) -> None:
    """Write + record one (empty) critique; ``author=None`` omits the field entirely (legacy)."""
    state = engine.load_state(engine.RunPaths(repo, run_id))
    iteration = state.quality.iteration if state.quality else 0
    if not (repo / ".hw" / run_id / "quality" / f"iter-{iteration}" / "critic-packets").exists():
        quality.critique_plan(repo_root=repo, run_id=run_id)
    payload = CritiqueReport(critic="c", lens=lens, findings=[]).model_dump(mode="json")
    if author is None:
        payload.pop("author")
    else:
        payload["author"] = author.value
    inp = repo / ".hw" / run_id / "quality" / f"iter-{iteration}" / "critics" / f"{lens}.json"
    inp.parent.mkdir(parents=True, exist_ok=True)
    inp.write_text(json.dumps(payload), encoding="utf-8")
    kwargs = {
        "repo_root": repo, "run_id": run_id,
        "packet_id": f"critic-{iteration}-{lens}", "result_file": str(inp),
    }
    if author is ReviewAuthor.INDEPENDENT:
        record_attested(quality.record_critique, repo=repo, **kwargs)
    else:
        quality.record_critique(
            **kwargs, author=author.value if author else None,
        )


def _panel(repo: Path, run_id: str, author: ReviewAuthor | None) -> None:
    for lens in quality.CRITIC_LENSES:
        _write_critique(repo, run_id, lens, author)


def _approve_override(repo: Path, key, *, by: str, reason: str, iteration: int = 0) -> None:
    private, public = key
    gate_dir = repo / ".hw/q/gates"
    gate_id = f"independent-override-{iteration}"
    with pytest.raises(quality.QualityError):
        quality.record_independent_override(repo_root=repo, run_id="q", by=by, reason=reason)
    challenge = approvals.load_challenge(gate_dir, gate_id)
    payload = approvals.build_decision_payload(challenge, decision="approve",
        decided_by="TEST FIXTURE; NOT A HUMAN", note="Protocol test", public_key_path=public)
    signature = base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode()
    token = {"payload": payload, "signature": signature}
    (gate_dir / f"{gate_id}.decision.json").write_text(json.dumps(token), encoding="utf-8")


def test_replay_a_adapter_authored_final_iteration_is_not_pass(tmp_path: Path) -> None:
    """pilot replay (a): self-reviewed critiques in the final iteration never PASS."""
    paths = _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])  # independent evaluator …
    _panel(tmp_path, "q", ReviewAuthor.ADAPTER)  # … but every critic written by the adapter
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "pending_independent_review"
    assert g["status"] == quality.PENDING_INDEPENDENT_REVIEW
    assert g["passed"] is False
    assert sorted(g["adapter_authored"]) == sorted(quality.CRITIC_LENSES)
    state = engine.load_state(paths)
    assert state.step_status["quality"] is not StepStatus.DONE
    assert state.quality is not None and state.quality.passed is False


def test_one_adapter_lens_is_enough_to_block(tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _panel(tmp_path, "q", ReviewAuthor.INDEPENDENT)
    _write_critique(tmp_path, "q", "security", ReviewAuthor.ADAPTER)  # re-recorded by adapter
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "pending_independent_review"
    assert g["adapter_authored"] == ["security"]


def test_missing_provenance_counts_as_adapter(tmp_path: Path) -> None:
    """Fail closed: legacy records with no ``author`` field are adapter-authored."""
    _run_at_quality(tmp_path, threshold=4)
    ev = SubjectiveEvaluation(
        evaluator="legacy",
        criteria=[SubjectiveCriterion(name="c", score=5)],
    ).model_dump(mode="json")
    ev.pop("author")
    src = tmp_path / ".hw" / "q" / "legacy_subj.json"
    src.write_text(json.dumps(ev), encoding="utf-8")
    out = quality.record_subjective(repo_root=tmp_path, run_id="q", result_file=str(src))
    assert out["author"] == "adapter"
    _panel(tmp_path, "q", None)
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "pending_independent_review"
    assert "subjective" in g["adapter_authored"]


def test_missing_lens_blocks(tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _write_critique(tmp_path, "q", "correctness", ReviewAuthor.INDEPENDENT)
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "pending_independent_review"
    assert set(g["missing"]) == {"security", "maintainability", "tests"}


def test_direct_independent_author_claim_cannot_pass_gate(tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4)
    subjective = tmp_path / ".hw/q/subjective-input.json"
    subjective.write_text(SubjectiveEvaluation(
        evaluator="coordinator",
        criteria=[SubjectiveCriterion(name="c", score=5)],
    ).model_dump_json(), encoding="utf-8")
    subjective_out = quality.record_subjective(
        repo_root=tmp_path, run_id="q", result_file=str(subjective), author="independent",
    )
    assert subjective_out["author"] == "adapter"
    quality.critique_plan(repo_root=tmp_path, run_id="q")
    for lens in quality.CRITIC_LENSES:
        inp = tmp_path / f".hw/q/quality/iter-0/critics/{lens}.json"
        inp.parent.mkdir(parents=True, exist_ok=True)
        inp.write_text(json.dumps({"critic": "c", "lens": lens, "findings": []}),
                       encoding="utf-8")
        out = quality.record_critique(
            repo_root=tmp_path, run_id="q", packet_id=f"critic-0-{lens}",
            result_file=str(inp), author="independent",
        )
        assert out["author"] == "adapter"
        assert Path(out["attestation_challenge"]).is_file()
    gate = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert gate["action"] == "pending_independent_review"
    assert set(gate["adapter_authored"]) == {"subjective", *quality.CRITIC_LENSES}


def test_signed_execution_attestation_promotes_exact_critique(
    tmp_path: Path, approval_key, monkeypatch,  # noqa: F811
) -> None:
    private, public = approval_key
    monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", public)
    _run_at_quality(tmp_path, threshold=4)
    quality.critique_plan(repo_root=tmp_path, run_id="q")
    inp = tmp_path / ".hw/q/quality/iter-0/critics/tests.json"
    inp.parent.mkdir(parents=True, exist_ok=True)
    inp.write_text(json.dumps({"critic": "c", "lens": "tests", "findings": []}))
    first = quality.record_critique(
        repo_root=tmp_path, run_id="q", packet_id="critic-0-tests",
        result_file=str(inp), author="independent",
    )
    challenge = approvals.load_challenge(
        Path(first["attestation_challenge"]).parent,
        Path(first["attestation_challenge"]).name.removesuffix(".challenge.json"),
    )
    payload = approvals.build_decision_payload(
        challenge, decision="approve", decided_by="fixture-independent-execution", note="",
        public_key_path=public,
    )
    token = {
        "payload": payload,
        "signature": base64.b64encode(
            private.sign(approvals.canonical_bytes(payload))
        ).decode(),
    }
    decision = Path(first["attestation_decision"])
    decision.write_text(json.dumps(token), encoding="utf-8")
    accepted = quality.record_critique(
        repo_root=tmp_path, run_id="q", packet_id="critic-0-tests",
        result_file=str(inp), author="independent", attestation_file=str(decision),
    )
    assert accepted["author"] == "independent"
    state = engine.load_state(engine.RunPaths(tmp_path, "q"))
    assert quality.provenance_report(
        repo_root=tmp_path, run_id="q", state=state
    )["independent"] == ["tests"]


def test_independent_panel_passes(tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _panel(tmp_path, "q", ReviewAuthor.INDEPENDENT)
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] == "pass"


def test_human_override_is_recorded_and_shown_at_ship_gate(
    tmp_path: Path, approval_key,  # noqa: F811
) -> None:
    paths = _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _panel(tmp_path, "q", ReviewAuthor.ADAPTER)
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] == (
        "pending_independent_review"
    )
    # an override with no reason is refused
    try:
        quality.record_independent_override(repo_root=tmp_path, run_id="q", by="owner", reason=" ")
    except quality.QualityError:
        pass
    else:  # pragma: no cover
        raise AssertionError("empty reason must be refused")
    # Self-asserted labels cannot substitute for a signed external decision.
    with pytest.raises(quality.QualityError):
        quality.record_independent_override(
            repo_root=tmp_path, run_id="q", by="owner",
            reason="rate limit until tomorrow; accepted"
        )
    _approve_override(tmp_path, approval_key, by="owner",
                      reason="rate limit until tomorrow; accepted")
    quality.record_independent_override(
        repo_root=tmp_path, run_id="q", by="owner", reason="rate limit until tomorrow; accepted"
    )
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "pass"
    assert g["independent_review_override"]["by"] == "TEST FIXTURE; NOT A HUMAN"
    status = quality.quality_status(repo_root=tmp_path, run_id="q")
    assert any("HUMAN OVERRIDE" in line for line in status["provenance_banner"])
    # the ship HITL gate carries the banner prominently
    state = engine.load_state(paths)
    assert state.quality is not None
    opened = gates.gate_open(
        repo_root=tmp_path, run_id="q", kind=GateKind.MERGE, step_id="ship", action="merge"
    )
    assert any("HUMAN OVERRIDE" in line and "TEST FIXTURE" in line for line in opened["banner"])
    assert opened["gate"]["warnings"] == opened["banner"]


def test_ship_gate_banner_flags_pending_independent_review(tmp_path: Path) -> None:
    _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _panel(tmp_path, "q", ReviewAuthor.ADAPTER)
    opened = gates.gate_open(
        repo_root=tmp_path, run_id="q", kind=GateKind.MERGE, step_id="ship", action="merge"
    )
    assert opened["banner"] and opened["banner"][0].startswith("PENDING_INDEPENDENT_REVIEW")


def test_override_is_scoped_to_its_iteration(tmp_path: Path, approval_key) -> None:  # noqa: F811
    _run_at_quality(tmp_path, threshold=4)
    _approve_override(tmp_path, approval_key, by="m", reason="r")
    quality.record_independent_override(repo_root=tmp_path, run_id="q", by="m", reason="r")
    paths = engine.RunPaths(tmp_path, "q")
    state = engine.load_state(paths)
    assert state.quality is not None
    state.quality.iteration = 1  # a later round: the earlier override no longer applies
    engine.save_state(paths, state)
    prov = quality.provenance_report(repo_root=tmp_path, run_id="q", state=state)
    assert prov["override"] is None


def test_author_claimed_inside_the_file_is_ignored(tmp_path: Path) -> None:
    """M1 (review): a file cannot vouch for itself — only the record-time declaration counts."""
    _run_at_quality(tmp_path, threshold=4)
    quality.critique_plan(repo_root=tmp_path, run_id="q")
    f = tmp_path / ".hw/q/quality/iter-0/critics/tests.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps({"critic": "c", "author": "independent", "lens": "tests", "findings": []}),
        encoding="utf-8",
    )
    out = quality.record_critique(
        repo_root=tmp_path, run_id="q", packet_id="critic-0-tests", result_file=str(f)
    )
    assert out["author"] == "adapter"


def test_record_edited_or_dropped_after_recording_is_not_independent(tmp_path: Path) -> None:
    """M1 (review): tampering with the recorded files on disk revokes 'independent'."""
    _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5, 5, 5, 5, 5])
    _panel(tmp_path, "q", ReviewAuthor.INDEPENDENT)
    crit = tmp_path / ".hw/q/quality/iter-0/critics/security.json"
    data = json.loads(crit.read_text())
    data["findings"] = []
    data["critic"] = "edited-later"
    crit.write_text(json.dumps(data), encoding="utf-8")
    subj = tmp_path / ".hw/q/quality/iter-0/subjective.json"
    subj.write_text(subj.read_text().replace('"score": 5', '"score": 5 '), encoding="utf-8")
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert g["action"] == "pending_independent_review"
    assert set(g["adapter_authored"]) == {"security", "subjective"}
    # and a hand-dropped critic file that was never recorded is "missing", not independent
    (tmp_path / ".hw/q/quality/iter-0/critics/tests.json").unlink()
    forged = tmp_path / ".hw/q/quality/iter-0/critics/tests.json"
    forged.write_text(
        json.dumps({"critic": "x", "author": "independent", "lens": "tests", "findings": []}),
        encoding="utf-8",
    )
    g = quality.quality_gate(repo_root=tmp_path, run_id="q")
    assert "tests" in g["adapter_authored"]


@pytest.mark.parametrize("mutation", ["subjective", "critic", "add", "delete", "tamper",
                                    "receipt", "token"])
def test_signed_override_is_bound_to_exact_evidence_and_receipt(
    tmp_path: Path, approval_key, mutation: str,  # noqa: F811
) -> None:
    paths = _run_at_quality(tmp_path, threshold=4)
    _record_subjective(tmp_path, "q", [5] * 5)
    for lens in quality.CRITIC_LENSES:
        if mutation != "add" or lens != "security":
            _write_critique(tmp_path, "q", lens, ReviewAuthor.ADAPTER)
    _approve_override(tmp_path, approval_key, by="fixture", reason="protocol test")
    quality.record_independent_override(repo_root=tmp_path, run_id="q",
                                       by="fixture", reason="protocol test")
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] == "pass"
    directory = paths.run_dir / "gates"
    old = {p.name: p.read_bytes() for p in directory.glob("independent-override-0.*")}
    review = paths.run_dir / "quality/iter-0/critics/security.json"
    if mutation == "subjective":
        _record_subjective(tmp_path, "q", [4] * 5)
    elif mutation in {"critic", "add"}:
        _write_critique(tmp_path, "q", "security", ReviewAuthor.INDEPENDENT)
    elif mutation == "delete":
        review.unlink()
    elif mutation == "tamper":
        review.write_text(review.read_text() + " ", encoding="utf-8")
    elif mutation == "receipt":
        approvals.consumed_file(directory, "independent-override-0").write_text("forged")
    else:
        token = directory / "independent-override-0.decision.json"
        payload = json.loads(token.read_text())
        payload["payload"]["note"] = "different signed receipt"
        payload["signature"] = base64.b64encode(
            approval_key[0].sign(approvals.canonical_bytes(payload["payload"]))
        ).decode()
        token.write_text(json.dumps(payload))
    state = engine.load_state(paths)
    report = quality.provenance_report(repo_root=tmp_path, run_id="q", state=state)
    assert report["override"] is None
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] == (
        "pending_independent_review"
    )
    assert engine.load_state(paths).step_status["quality"] is StepStatus.PENDING
    if mutation in {"receipt", "token"}:
        return
    with pytest.raises(quality.QualityError, match="sign"):
        quality.record_independent_override(repo_root=tmp_path, run_id="q",
                                           by="fixture", reason="protocol test")
    fresh = next(directory.glob("independent-override-0-*.challenge.json"))
    challenge = json.loads(fresh.read_text())
    _signed(directory / f"{challenge['gate_id']}.decision.json", {"challenge": challenge},
            *approval_key)
    quality.record_independent_override(repo_root=tmp_path, run_id="q",
                                       by="fixture", reason="protocol test")
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] == "pass"
    assert old == {p.name: p.read_bytes() for p in directory.glob("independent-override-0.*")}
