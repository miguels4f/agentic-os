"""Expiry probes use ephemeral fixture signatures, never production signing authority."""
import json
import sys
from datetime import timedelta

import pytest
from test_approval_boundary import _signed
from test_gates import approval_key  # noqa: F401
from test_quality import _record_subjective, _run_at_quality
from test_verify_checks import _at_verify

from hyperwilfred_core import approvals, engine, quality, verify_checks
from hyperwilfred_core.schemas import BuildObjective, LiveDemo


@pytest.mark.parametrize("kind", ["quality", "verify"])
def test_expired_override_challenge_renews_without_replaying_token(
    tmp_path, monkeypatch, approval_key, kind,  # noqa: F811
):
    if kind == "quality":
        paths = _run_at_quality(tmp_path)
        _record_subjective(tmp_path, "q", [5] * 5)
        operation, error = quality.record_independent_override, quality.QualityError
        kwargs = dict(repo_root=tmp_path, run_id="q", by="fixture", reason="test")
        gate_id = "independent-override-0"
    else:
        paths = _at_verify(tmp_path, BuildObjective(id="o", title="test", objective="test",
            live_demo=LiveDemo(promise="demo", command=f"{sys.executable} -c 'print(123)'",
                               failure_markers=["123"])))
        verify_checks.run(repo_root=tmp_path, run_id="v")
        operation, error = verify_checks.record_override, verify_checks.VerifyCheckError
        kwargs = dict(repo_root=tmp_path, run_id="v", by="fixture", reason="test",
                      check="live_demo")
        gate_id = "verify-override-live_demo"
    with pytest.raises(error, match="sign"):
        operation(**kwargs)
    directory = paths.run_dir / "gates"
    previous = approvals.load_challenge(directory, gate_id)
    token = _signed(directory / f"{gate_id}.decision.json", {"challenge": previous}, *approval_key)
    later = approvals._now() + timedelta(minutes=16)
    monkeypatch.setattr(approvals, "_now", lambda: later)
    with pytest.raises(error, match="does not match"):
        operation(**kwargs)
    current = approvals.load_challenge(directory, gate_id)
    assert current["nonce"] != previous["nonce"]
    assert current["evidence"] == previous["evidence"]
    archive_path = next((directory / "history").glob("*.json"))
    archived = archive_path.read_bytes()
    assert json.loads(json.loads(archived)["challenge"]) == previous
    assert not approvals.consumed_file(directory, gate_id).exists()
    _signed(token, {"challenge": current}, *approval_key)
    operation(**kwargs)
    receipt = approvals.consumed_file(directory, gate_id).read_bytes()
    later += timedelta(minutes=16)
    if kind == "quality":
        report = quality.provenance_report(repo_root=tmp_path, run_id="q",
                                           state=engine.load_state(paths))
        assert report["override"] is not None
    else:
        assert verify_checks.run(repo_root=tmp_path, run_id="v")["findings"][0]["override"]
    with pytest.raises(error, match="consumed"):
        operation(**kwargs)
    assert archive_path.read_bytes() == archived
    assert approvals.consumed_file(directory, gate_id).read_bytes() == receipt


def test_unavailable_platform_lock_fails_closed_without_breaking_imports(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", None)
    with pytest.raises(approvals.ApprovalError, match="locking unavailable"):
        approvals.issue_challenge(tmp_path / "gate", run_id="fixture", gate_id="fixture",
                                  action="fixture", evidence=[])
    assert not (tmp_path / "gate").exists()
