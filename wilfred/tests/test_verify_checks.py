"""L6 reproducibility (pilot replay (e)) + L7 live-demo check, wired into the verify step."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, engine, live_demo, reproducibility, verify_checks
from hyperwilfred_core.schemas import (
    BuildObjective,
    GeneratedArtifact,
    LiveDemo,
    StepStatus,
)

PY = sys.executable


@pytest.fixture
def approval_key(tmp_path: Path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = tmp_path / "fixture-approval-public.pem"
    public.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    monkeypatch.setattr(approvals, "DEFAULT_PUBLIC_KEY_PATH", public)
    return private, public


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _gen_script(repo: Path, body: str) -> None:
    (repo / "gen.py").write_text(body, encoding="utf-8")


def _artifact(**kw: object) -> GeneratedArtifact:
    return GeneratedArtifact(
        path="snap/accounts.json", command=f"{PY} gen.py", **kw  # type: ignore[arg-type]
    )


def _commit_artifact(repo: Path, payload: dict) -> None:
    (repo / "snap").mkdir(exist_ok=True)
    (repo / "snap" / "accounts.json").write_text(json.dumps(payload), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add generated snapshot")


_GEN_STABLE = (
    "import json, pathlib\n"
    "pathlib.Path('snap/accounts.json').write_text(json.dumps("
    "{'accounts': [1, 2, 3], 'generated_at': 'NOW'}))\n"
)


def test_bytes_equal_is_reproducible(git_repo: Path) -> None:
    _gen_script(git_repo, _GEN_STABLE)
    _commit_artifact(git_repo, {"accounts": [1, 2, 3], "generated_at": "NOW"})
    rep = reproducibility.check(repo_root=git_repo, artifacts=[_artifact()])
    assert rep.ok, rep.message


def test_replay_e_volatile_field_only_difference_passes(git_repo: Path) -> None:
    """pilot replay (e): regenerated artifact differs only in a DECLARED volatile field → pass."""
    _gen_script(git_repo, _GEN_STABLE)
    _commit_artifact(git_repo, {"accounts": [1, 2, 3], "generated_at": "2026-09-17T10:00:00Z"})
    rep = reproducibility.check(
        repo_root=git_repo, artifacts=[_artifact(volatile_fields=["generated_at"])]
    )
    assert rep.ok, rep.message


def test_replay_e_difference_in_another_field_fails(git_repo: Path) -> None:
    """…and differing in any OTHER field → fail."""
    _gen_script(git_repo, _GEN_STABLE)
    _commit_artifact(git_repo, {"accounts": [1, 2, 99], "generated_at": "2026-09-17T10:00:00Z"})
    rep = reproducibility.check(
        repo_root=git_repo, artifacts=[_artifact(volatile_fields=["generated_at"])]
    )
    assert not rep.ok
    assert "outside the declared volatile fields" in rep.message


def test_difference_fails_when_no_volatile_field_is_declared(git_repo: Path) -> None:
    _gen_script(git_repo, _GEN_STABLE)
    _commit_artifact(git_repo, {"accounts": [1, 2, 3], "generated_at": "older"})
    rep = reproducibility.check(repo_root=git_repo, artifacts=[_artifact()])
    assert not rep.ok and "differ" in rep.message


def test_nested_and_wildcard_volatile_paths() -> None:
    a = json.dumps({"meta": {"at": 1}, "rows": [{"ts": 1, "v": 1}, {"ts": 2, "v": 2}]}).encode()
    b = json.dumps({"meta": {"at": 9}, "rows": [{"ts": 7, "v": 1}, {"ts": 8, "v": 2}]}).encode()
    assert reproducibility.compare_bytes(a, b, ["meta.at", "rows.*.ts"]) is None
    assert reproducibility.compare_bytes(a, b, ["meta.at"]) is not None


def test_uncommitted_artifact_command_failure_and_bad_repo_fail_closed(
    git_repo: Path, tmp_path: Path
) -> None:
    _gen_script(git_repo, _GEN_STABLE)
    rep = reproducibility.check(repo_root=git_repo, artifacts=[_artifact()])
    assert not rep.ok and "commit the generated artifact first" in rep.message

    _commit_artifact(git_repo, {"accounts": [1, 2, 3], "generated_at": "NOW"})
    failing = GeneratedArtifact(path="snap/accounts.json", command=f"{PY} -c 'raise SystemExit(3)'")
    rep = reproducibility.check(repo_root=git_repo, artifacts=[failing])
    assert not rep.ok and "exited 3" in rep.message

    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    rep = reproducibility.check(repo_root=not_a_repo, artifacts=[_artifact()])
    assert not rep.ok and "fresh worktree" in rep.message


def test_check_leaves_no_worktree_behind(git_repo: Path) -> None:
    _gen_script(git_repo, _GEN_STABLE)
    _commit_artifact(git_repo, {"accounts": [1, 2, 3], "generated_at": "NOW"})
    reproducibility.check(repo_root=git_repo, artifacts=[_artifact()])
    listing = subprocess.run(
        ["git", "-C", str(git_repo), "worktree", "list"], capture_output=True, text=True, check=True
    ).stdout
    assert len(listing.strip().splitlines()) == 1


# --------------------------------------------------------------------------- live demo (L7)


def _demo(repo: Path, **kw: object) -> live_demo.LiveDemoResult:
    return live_demo.check(repo_root=repo, demo=LiveDemo(promise="find_champion works", **kw))  # type: ignore[arg-type]


def test_live_demo_achievable(tmp_path: Path) -> None:
    res = _demo(
        tmp_path, command=f"{PY} -c \"print('3 actions available')\"",
        must_contain=["available"], failure_markers=["all actions blocked"],
    )
    assert res.achievable


def test_live_demo_every_action_blocked_is_a_finding(tmp_path: Path) -> None:
    res = _demo(
        tmp_path, command=f"{PY} -c \"print('all actions blocked: 5/5')\"",
        failure_markers=["all actions blocked"],
    )
    assert not res.achievable and "failure marker" in res.finding
    assert "do not assert" in res.finding


def test_live_demo_empty_result_command_error_and_missing_evidence(tmp_path: Path) -> None:
    assert "empty result" in _demo(tmp_path, command=f"{PY} -c pass").finding
    assert "exited 4" in _demo(tmp_path, command=f"{PY} -c 'raise SystemExit(4)'").finding
    assert "does not exist" in _demo(tmp_path, evidence_path="nope.txt").finding
    assert "no live-demo command or evidence" in _demo(tmp_path).finding


def test_live_demo_from_adapter_evidence_file(tmp_path: Path) -> None:
    (tmp_path / "demo.txt").write_text("account A: prepare_deck ALLOWED\n", encoding="utf-8")
    assert _demo(tmp_path, evidence_path="demo.txt", must_contain=["ALLOWED"]).achievable
    assert not _demo(tmp_path, evidence_path="demo.txt", must_contain=["find_champion"]).achievable


# --------------------------------------------------------------------------- verify step wiring


def _at_verify(repo: Path, obj: BuildObjective) -> engine.RunPaths:
    engine.plan(repo_root=repo, objective=obj, run_id="v", product_dir="snap")
    paths = engine.RunPaths(repo, "v")
    state = engine.load_state(paths)
    for s in state.steps:
        if s.id == "verify":
            break
        state.step_status[s.id] = StepStatus.DONE
    engine.save_state(paths, state)
    paths.artifact("verify.md").write_text("ran\n", encoding="utf-8")
    return paths


def test_verify_step_blocks_on_drift_and_passes_when_reproducible(git_repo: Path) -> None:
    _gen_script(git_repo, _GEN_STABLE)
    _commit_artifact(git_repo, {"accounts": [1, 2, 99], "generated_at": "x"})
    obj = BuildObjective(
        id="o", title="t", objective="x",
        generated_artifacts=[_artifact(volatile_fields=["generated_at"])],
    )
    _at_verify(git_repo, obj)
    with pytest.raises(engine.EngineError, match="not reproducible"):
        engine.complete_step(repo_root=git_repo, run_id="v", step_id="verify")

    _commit_artifact(git_repo, {"accounts": [1, 2, 3], "generated_at": "old"})
    out = engine.complete_step(repo_root=git_repo, run_id="v", step_id="verify")
    assert out["verify_checks"]["reproducibility"]["ok"] is True


def test_verify_step_blocks_on_live_demo_gap_until_a_recorded_human_override(
    tmp_path: Path, approval_key,
) -> None:
    obj = BuildObjective(
        id="o", title="t", objective="x",
        live_demo=LiveDemo(
            promise="account actions work", command=f"{PY} -c \"print('every action blocked')\"",
            failure_markers=["every action blocked"],
        ),
    )
    paths = _at_verify(tmp_path, obj)
    with pytest.raises(engine.EngineError, match="Live-demo gap"):
        engine.complete_step(repo_root=tmp_path, run_id="v", step_id="verify")
    saved = json.loads((paths.run_dir / "verify-checks.json").read_text())
    assert saved["findings"][0]["severity"] == "major"  # surfaced as a finding

    with pytest.raises(verify_checks.VerifyCheckError):
        verify_checks.record_override(
            repo_root=tmp_path, run_id="v", check="traceability", by="m", reason="r"
        )
    reason = "data refresh due"
    with pytest.raises(verify_checks.VerifyCheckError, match="sign"):
        verify_checks.record_override(
            repo_root=tmp_path, run_id="v", check="live_demo", by="owner", reason=reason
        )
    marker = paths.run_dir / "gates" / "verify-override-live_demo.approved"
    marker.write_text("ok\n", encoding="utf-8")
    with pytest.raises(verify_checks.VerifyCheckError, match="not authorized"):
        verify_checks.record_override(
            repo_root=tmp_path, run_id="v", check="live_demo", by="owner", reason=reason
        )
    private, public = approval_key
    gate_id = "verify-override-live_demo"
    gates_dir = paths.run_dir / "gates"
    challenge = approvals.load_challenge(gates_dir, gate_id)
    payload = approvals.build_decision_payload(
        challenge, decision="approve", decided_by="fixture-human", note=reason,
        public_key_path=public,
    )
    (gates_dir / f"{gate_id}.decision.json").write_text(json.dumps({
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }), encoding="utf-8")
    verify_checks.record_override(
        repo_root=tmp_path, run_id="v", check="live_demo", by="owner", reason=reason
    )
    out = engine.complete_step(repo_root=tmp_path, run_id="v", step_id="verify")
    finding = out["verify_checks"]["findings"][0]
    assert finding["override"]["by"] == "fixture-human"
