"""Exact control authority exercised only with isolated repositories and external test keys."""

import base64
import functools
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, engine, regression
from hyperwilfred_core import control_evolution as control
from hyperwilfred_core.schemas import BuildMode, BuildObjective


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _public(private, path):
    path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return path


def _fixture(tmp_path, monkeypatch, *, legacy=False):
    """Real pre-change capture; all signing material stays outside the synthetic repo."""
    repo = (tmp_path / "repo").resolve()
    repo.mkdir(parents=True)
    tests = repo / "tests"
    tests.mkdir()
    marker = tmp_path / "protected-executed"
    helper = tests / "review_attestation.py"
    helper.write_text("def answer():\n    return 1\n")
    (repo / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (repo / ".gitignore").write_text(".hw/\n.pytest_cache/\n__pycache__/\n")
    count = 809 if legacy else 1
    (tests / "test_guard.py").write_text(
        "from pathlib import Path\nimport pytest\nfrom review_attestation import answer\n"
        f"@pytest.mark.parametrize('index', range({count}))\n"
        "def test_guard(index):\n    assert answer() > 0\n"
        f"    if answer() == 2:\n        Path({str(marker)!r}).write_text('executed')\n")
    for args in [("init", "-q"), ("config", "user.email", "isolated@example.invalid"),
                 ("config", "user.name", "Isolated Test"),
                 ("config", "core.hooksPath", "/dev/null"),
                 ("config", "commit.gpgsign", "false"), ("config", "gc.auto", "0"),
                 ("add", "."), ("commit", "-qm", "isolated baseline")]:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    run_id = "isolated-control"
    engine.plan(repo_root=repo, run_id=run_id, product_dir=".", mode=BuildMode.IMPROVE,
                objective=BuildObjective(id="control", title="Control", objective="Protect tests"))
    paths = engine.RunPaths(repo, run_id)
    candidate = regression.baseline_candidate
    if legacy:
        def legacy_candidate(target):
            data, check = candidate(target)
            # Convert the actual synthetic capture BEFORE its first immutable engine pin.
            return {"target": str(tests), "passing_ids": data["passing_ids"],
                    "passing": data["passing"], "test_file_hashes": {
                        Path(path).name: sha for path, sha in data["test_file_hashes"].items()
                    }}, check
        with monkeypatch.context() as patch:
            patch.setattr(regression, "baseline_candidate", legacy_candidate)
            # The current engine cannot capture this shape: it has no witness to compare
            # with the base commit, so the comparison is replaced together with the capture.
            patch.setattr(regression, "require_base_commit_witness", lambda *_: None)
            engine.regression_baseline(repo_root=repo, run_id=run_id, target=repo)
    else:
        engine.regression_baseline(repo_root=repo, run_id=run_id, target=repo)
    baseline_path = paths.artifact(regression.BASELINE_FILE)
    baseline_bytes = baseline_path.read_bytes()
    state = engine.load_state(paths)
    old_sha = hashlib.sha256(helper.read_bytes()).hexdigest()
    helper.write_text("def answer():\n    return 2\n")
    report, dispatch = paths.run_dir / "control-report.json", paths.run_dir / "dispatch.json"
    _write_json(report, {"explanation": "The positive-value assertion remains meaningful after "
                        "this helper update; this control review does not approve the product."})
    _write_json(dispatch, {"reviewer": "isolated independent control reviewer"})
    private = Ed25519PrivateKey.generate()
    public = _public(private, tmp_path / "external-review-public.pem")
    return SimpleNamespace(repo=repo, run_id=run_id, paths=paths, helper=helper, marker=marker,
        report=report, dispatch=dispatch, private=private, public=public, state=state,
        baseline_path=baseline_path, baseline_bytes=baseline_bytes,
        baseline=json.loads(baseline_bytes), before=old_sha,
        after=hashlib.sha256(helper.read_bytes()).hexdigest())


def _kwargs(f, *, verify=False):
    result = {"repo_root": f.repo, "run_id": f.run_id, "target": f.repo,
              "review_public_key_path": f.public}
    if verify:
        result.update(baseline=f.baseline, base_commit=f.state.base_commit,
                      baseline_sha256=f.state.regression_baseline_sha256)
    else:
        result.update(report_file=f.report, dispatch_file=f.dispatch)
    return result


def _sign(f, first, *, private=None, decision="approve"):
    challenge = json.loads(Path(first["attestation_challenge"]).read_text())
    payload = approvals.build_decision_payload(challenge, decision=decision,
        decided_by="isolated-independent-reviewer", note="exact helper only",
        public_key_path=f.public)
    token = Path(first["attestation_decision"])
    _write_json(token, {"payload": payload, "signature": base64.b64encode(
        (private or f.private).sign(approvals.canonical_bytes(payload))).decode()})
    return token


def _record(f):
    first = control.record_control_review(**_kwargs(f))
    return control.record_control_review(**_kwargs(f), attestation_file=_sign(f, first))


def _compare(f, **overrides):
    # As the engine does: the caller binds the control authority and hands it to compare.
    args = _kwargs(f, verify=True) | overrides
    authority = functools.partial(control.verified_control_transition, **args)
    return regression.compare(args["target"], args["baseline"],
                              base_commit=args["base_commit"], control_authority=authority)


def _assert_immutable(f):
    assert f.baseline_path.read_bytes() == f.baseline_bytes
    assert engine.load_state(f.paths).regression_baseline_sha256 == hashlib.sha256(
        f.baseline_bytes).hexdigest()


@pytest.mark.parametrize("legacy", [False, True], ids=["current", "representative809"])
def test_signed_exact_control_runs_original_tests_and_preserves_baseline(
        tmp_path, monkeypatch, legacy):
    f = _fixture(tmp_path, monkeypatch, legacy=legacy)
    assert not f.marker.exists()
    assert not _compare(f).passed
    assert not f.marker.exists()
    first = _record(f)
    assert first["status"] == "recorded"
    assert control.verified_control_transition(**_kwargs(f, verify=True)) == {
        control.CONTROL_PATH: (f.before, f.after)}
    check = _compare(f)
    assert check.passed, check.details
    assert f.marker.read_text() == "executed"
    assert f.baseline["passing"] == (809 if legacy else 1)
    assert control.CONTROL_PATH not in f.baseline["test_file_hashes"]
    assert engine.load_state(f.paths).quality is None
    _assert_immutable(f)


def test_unsigned_challenge_has_no_authority_and_never_launches_protected_tests(
        tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    state_bytes = f.paths.state.read_bytes()
    monkeypatch.setattr(regression, "_pytest_passing", lambda *a, **k: pytest.fail(
        "protected pytest started before external authorization"))
    first = control.record_control_review(**(_kwargs(f) | {"review_public_key_path": None}))
    assert first["status"] == "pending"
    assert control.verified_control_transition(**_kwargs(f, verify=True)) == {}
    assert not _compare(f).passed
    assert not f.marker.exists()
    assert f.paths.state.read_bytes() == state_bytes
    _assert_immutable(f)


@pytest.mark.parametrize("kind", ["wrong-signature", "deny", "missing-key", "changed-key"])
def test_invalid_external_authority_blocks_before_execution(tmp_path, monkeypatch, kind):
    f = _fixture(tmp_path, monkeypatch)
    first = control.record_control_review(**_kwargs(f))
    token = _sign(f, first, private=Ed25519PrivateKey.generate()
                  if kind == "wrong-signature" else None,
                  decision="deny" if kind == "deny" else "approve")
    if kind in {"missing-key", "changed-key"}:
        control.record_control_review(**_kwargs(f), attestation_file=token)
        if kind == "missing-key":
            f.public.unlink()
        else:
            _public(Ed25519PrivateKey.generate(), f.public)
    else:
        with pytest.raises(control.ControlEvolutionError):
            control.record_control_review(**_kwargs(f), attestation_file=token)
    monkeypatch.setattr(regression, "_pytest_passing", lambda *a, **k: pytest.fail(
        "invalid authority launched protected pytest"))
    assert control.verified_control_transition(**_kwargs(f, verify=True)) == {}
    assert not _compare(f).passed
    assert not f.marker.exists()
    _assert_immutable(f)


@pytest.mark.parametrize("field", [
    "run_id", "repo_root", "target", "base_commit", "baseline_sha256", "control_path",
    "control_class", "before_sha256", "after_sha256", "action", "purpose", "report_file",
    "report_sha256", "dispatch_file", "dispatch_sha256", "product_snapshot_sha256",
])
def test_valid_signature_for_wrong_binding_cannot_authorize_current_control(
        tmp_path, monkeypatch, field):
    f = _fixture(tmp_path, monkeypatch)
    first = control.record_control_review(**_kwargs(f))
    path = Path(first["attestation_challenge"])
    challenge = json.loads(path.read_text())
    pending = path.with_name(first["gate_id"] + ".pending.json")
    context = json.loads(pending.read_text())["context"]
    context[field] = "b" * 64 if field.endswith("sha256") else str(context[field]) + "-other"
    challenge["evidence"] = sorted(f"{key}:{value}" for key, value in context.items())
    challenge["evidence_sha256"] = approvals.digest(challenge["evidence"])
    if field in {"action", "purpose", "run_id"}:
        challenge[field] = context[field]
    challenge["action_sha256"] = approvals.digest(challenge["action"])
    _write_json(path, challenge)
    token = _sign(f, first)
    with pytest.raises(control.ControlEvolutionError):
        control.record_control_review(**_kwargs(f), attestation_file=token)
    assert control.verified_control_transition(**_kwargs(f, verify=True)) == {}
    assert not _compare(f).passed
    assert not f.marker.exists()
    _assert_immutable(f)


def test_coherent_attacker_records_and_local_key_cannot_replace_external_trust(
        tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    trusted_public = f.public
    # Fabricate a complete internally coherent history using a different isolated key.
    f.private = Ed25519PrivateKey.generate()
    f.public = _public(f.private, tmp_path / "attacker-public.pem")
    _record(f)
    assert control.verified_control_transition(**_kwargs(f, verify=True))
    local = f.repo / ".hw" / "attacker-public.pem"
    local.write_bytes(f.public.read_bytes())
    assert control.verified_control_transition(**(
        _kwargs(f, verify=True) | {"review_public_key_path": local})) == {}
    f.public = trusted_public
    assert control.verified_control_transition(**_kwargs(f, verify=True)) == {}
    assert not _compare(f).passed
    assert not f.marker.exists()
    _assert_immutable(f)


@pytest.mark.parametrize("changed", ["helper", "test", "config", "report", "dispatch", "product"])
def test_recorded_authority_does_not_cover_later_mutations(tmp_path, monkeypatch, changed):
    f = _fixture(tmp_path, monkeypatch)
    _record(f)
    paths = {"helper": f.helper, "test": f.repo / "tests/test_guard.py",
             "config": f.repo / "pytest.ini", "report": f.report,
             "dispatch": f.dispatch, "product": f.repo / "new-product.py"}
    path = paths[changed]
    path.write_text((path.read_text() if path.exists() else "") + "\n ")
    monkeypatch.setattr(regression, "_pytest_passing", lambda *a, **k: pytest.fail(
        "stale authority launched protected pytest"))
    assert control.verified_control_transition(**_kwargs(f, verify=True)) == {}
    assert not _compare(f).passed
    assert not f.marker.exists()
    _assert_immutable(f)


@pytest.mark.parametrize("variant", [
    "exact", "other-path", "two-paths", "wrong-pair", "extra-control"])
def test_compare_accepts_only_the_single_path_its_authority_names(
        tmp_path, monkeypatch, variant):
    f = _fixture(tmp_path, monkeypatch)
    pair, other = (f.before, f.after), "tests/other_helper.py"
    authority = {"exact": {control.CONTROL_PATH: pair},
                 "other-path": {other: pair},
                 "two-paths": {control.CONTROL_PATH: pair, other: pair},
                 "wrong-pair": {control.CONTROL_PATH: (f.before, f.before)},
                 "extra-control": {control.CONTROL_PATH: pair}}[variant]
    if variant == "extra-control":
        # The authority covers one helper; a second changed control stays unauthorized.
        (f.repo / other).write_text("VALUE = 1\n")
    check = regression.compare(f.repo, f.baseline, base_commit=f.state.base_commit,
                               control_authority=lambda: dict(authority))
    assert check.passed is (variant == "exact"), check.details
    assert f.marker.exists() is (variant == "exact")
    _assert_immutable(f)
