"""Control review records are built by one helper each, shared by recorder and verifier.

Every repository, run and signing key is created inside temporary directories.
"""

import base64
import hashlib
import json
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, engine, regression
from hyperwilfred_core import control_evolution as control
from hyperwilfred_core.schemas import BuildMode, BuildObjective


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(tmp_path):
    """One isolated repository whose review helper changed after the pinned baseline."""
    repo = (tmp_path / "repo").resolve()
    tests = repo / "tests"
    tests.mkdir(parents=True)
    helper = tests / "review_attestation.py"
    helper.write_text("def answer():\n    return 1\n")
    (repo / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (repo / ".gitignore").write_text(".hw/\n.pytest_cache/\n__pycache__/\n")
    (tests / "test_guard.py").write_text(
        "from review_attestation import answer\n\n"
        "def test_guard():\n    assert answer() > 0\n")
    for args in [("init", "-q"), ("config", "user.email", "isolated@example.invalid"),
                 ("config", "user.name", "Isolated Test"),
                 ("config", "core.hooksPath", "/dev/null"),
                 ("config", "commit.gpgsign", "false"), ("config", "gc.auto", "0"),
                 ("add", "."), ("commit", "-qm", "isolated baseline")]:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    run_id = "isolated-records"
    engine.plan(repo_root=repo, run_id=run_id, product_dir=".", mode=BuildMode.IMPROVE,
                objective=BuildObjective(id="records", title="Records", objective="Records"))
    paths = engine.RunPaths(repo, run_id)
    engine.regression_baseline(repo_root=repo, run_id=run_id, target=repo)
    baseline_bytes = paths.artifact(regression.BASELINE_FILE).read_bytes()
    state = engine.load_state(paths)
    before = hashlib.sha256(helper.read_bytes()).hexdigest()
    helper.write_text("def answer():\n    return 2\n")
    report, dispatch = paths.run_dir / "control-report.json", paths.run_dir / "dispatch.json"
    _write_json(report, {"explanation": "The positive-value assertion remains meaningful after "
                        "this helper update; this control review does not approve the product."})
    _write_json(dispatch, {"reviewer": "isolated independent control reviewer"})
    private = Ed25519PrivateKey.generate()
    public = tmp_path / "external-review-public.pem"
    public.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    record = {"repo_root": repo, "run_id": run_id, "target": repo,
              "review_public_key_path": public, "report_file": report,
              "dispatch_file": dispatch}
    verify = {"repo_root": repo, "run_id": run_id, "target": repo,
              "review_public_key_path": public, "baseline": json.loads(baseline_bytes),
              "base_commit": state.base_commit,
              "baseline_sha256": state.regression_baseline_sha256}
    after = hashlib.sha256(helper.read_bytes()).hexdigest()
    return record, verify, private, public, (before, after)


def _record(record, private, public):
    first = control.record_control_review(**record)
    challenge = json.loads(Path(first["attestation_challenge"]).read_text())
    payload = approvals.build_decision_payload(
        challenge, decision="approve", decided_by="isolated-independent-reviewer",
        note="exact helper only", public_key_path=public)
    token = Path(first["attestation_decision"])
    _write_json(token, {"payload": payload, "signature": base64.b64encode(
        private.sign(approvals.canonical_bytes(payload))).decode()})
    return control.record_control_review(**record, attestation_file=token)


def test_recorded_review_matches_the_shared_builders_and_stays_verified(tmp_path):
    record, verify, private, public, transition = _fixture(tmp_path)
    result = _record(record, private, public)
    assert result["status"] == "recorded"
    directory = Path(result["record_file"]).parent
    gate = result["gate_id"]
    pending_raw = (directory / f"{gate}.pending.json").read_bytes()
    context = json.loads(pending_raw)["context"]
    # The shape stays exactly what earlier runs recorded, so their records remain valid.
    assert control._gate_id(context) == gate == "control-" + approvals.digest(context)[:56]
    pending = control._pending_record(context, gate)
    assert pending == {"schema_version": control.SCHEMA, "context": context, "gate_id": gate}
    assert control._encoded(pending) == pending_raw
    token_bytes = (directory / f"{gate}.decision.json").read_bytes()
    challenge_bytes = approvals.challenge_file(directory, gate).read_bytes()
    prepared = control._prepared_record(pending, token_bytes, challenge_bytes, public)
    assert prepared == {**pending, "token_sha256": hashlib.sha256(token_bytes).hexdigest(),
                        "challenge_sha256": hashlib.sha256(challenge_bytes).hexdigest(),
                        "key_fingerprint": approvals.public_key_fingerprint(public)}
    assert control._encoded(prepared) == (directory / f"{gate}.prepared.json").read_bytes()
    assert control.verified_control_transition(**verify) == {control.CONTROL_PATH: transition}


def test_recorder_and_verifier_share_one_definition_of_each_record(tmp_path, monkeypatch):
    """Changing a builder once moves both sides together; nothing is restated elsewhere."""
    record, verify, private, public, transition = _fixture(tmp_path)
    original_gate = control._gate_id
    original_pending = control._pending_record
    original_prepared = control._prepared_record
    monkeypatch.setattr(control, "_gate_id", lambda context: original_gate(context)[:-8] + "a" * 8)
    monkeypatch.setattr(control, "_pending_record", lambda context, gate: {
        **original_pending(context, gate), "probe": "pending"})
    monkeypatch.setattr(control, "_prepared_record", lambda *args: {
        **original_prepared(*args), "probe": "prepared"})
    result = _record(record, private, public)
    assert result["gate_id"].endswith("a" * 8)
    directory = Path(result["record_file"]).parent
    gate = result["gate_id"]
    assert json.loads((directory / f"{gate}.pending.json").read_text())["probe"] == "pending"
    assert json.loads((directory / f"{gate}.prepared.json").read_text())["probe"] == "prepared"
    assert control.verified_control_transition(**verify) == {control.CONTROL_PATH: transition}
    # A verifier that restated the shapes on its own would now reject the altered record.
    monkeypatch.setattr(control, "_prepared_record", original_prepared)
    assert control.verified_control_transition(**verify) == {}
