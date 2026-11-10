"""Lifecycle and adversarial control tests; never access a production signer or run."""

import base64
import builtins
import hashlib
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_control_evolution import (
    _assert_immutable,
    _compare,
    _fixture,
    _kwargs,
    _public,
    _record,
    _sign,
    _write_json,
)

from hyperwilfred_core import approvals, cli, engine, path_safety, regression
from hyperwilfred_core import control_evolution as control
from hyperwilfred_core.evaluators.verification import VerificationReport


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _no_authority(f):
    assert control.verified_control_transition(**_kwargs(f, verify=True)) == {}
    assert not _compare(f).passed


def _cli(f):
    return ["--repo", str(f.repo), "record-control-review", "--run-id", f.run_id,
            "--target", str(f.repo), "--report-file", str(f.report),
            "--dispatch-file", str(f.dispatch)]


def test_cli_pending_signed_record_and_structured_rejection(tmp_path, monkeypatch, capsys):
    f = _fixture(tmp_path, monkeypatch)
    # The command line never selects a key; install the isolated fixture key as the default.
    monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", f.public)
    assert cli.main(_cli(f)) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "pending"
    _no_authority(f)
    token = _sign(f, first)
    assert cli.main(_cli(f) + ["--attestation-file", str(token)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "recorded"
    assert _compare(f).passed
    f.public.unlink()
    assert cli.main(_cli(f) + ["--attestation-file", str(token)]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["kind"] == "control_evolution_error" and error["error"]
    _assert_immutable(f)


@pytest.mark.parametrize("fabricated", [False, True], ids=["genuine-record", "forged-receipt"])
def test_expiry_blocks_even_coherently_fabricated_finalized_receipts(
        tmp_path, monkeypatch, fabricated):
    f = _fixture(tmp_path, monkeypatch)
    first = control.record_control_review(**_kwargs(f))
    token = _sign(f, first)
    gate, directory = first["gate_id"], token.parent
    if fabricated:
        pending = json.loads((directory / f"{gate}.pending.json").read_text())
        prepared = {**pending, "token_sha256": _sha(token),
                    "challenge_sha256": _sha(Path(first["attestation_challenge"])),
                    "key_fingerprint": approvals.public_key_fingerprint(f.public)}
        # Never consume the signed token: the attacker only fabricates editable local files.
        receipt = approvals.consumed_file(directory, gate)
        receipt.write_text(hashlib.sha256(approvals.canonical_bytes(
            json.loads(token.read_text()))).hexdigest() + "\n")
        _write_json(directory / f"{gate}.prepared.json", prepared)
        _write_json(directory / f"{gate}.recorded.json",
                    {**prepared, "receipt_sha256": _sha(receipt)})
    else:
        control.record_control_review(**_kwargs(f), attestation_file=token)
    # Decision TTL is five minutes; challenge remains live for fifteen minutes.
    future = approvals._now() + timedelta(minutes=6)
    monkeypatch.setattr(approvals, "_now", lambda: future)
    monkeypatch.setattr(regression, "_pytest_passing", lambda *a, **k: pytest.fail(
        "expired authority launched protected tests"))
    _no_authority(f)
    with pytest.raises(control.ControlEvolutionError, match="expired"):
        control.record_control_review(**_kwargs(f), attestation_file=token)
    assert not f.marker.exists()
    _assert_immutable(f)


@pytest.mark.parametrize("boundary", ["decision", "prepared", "receipt", "recorded"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_interruptions_recover_exact_token_and_never_authorize_partial_state(
        tmp_path, monkeypatch, boundary, when):
    f = _fixture(tmp_path, monkeypatch)
    first = control.record_control_review(**_kwargs(f))
    signed = _sign(f, first)
    token = tmp_path / "external-decision.json"
    signed.rename(token)
    assert not signed.exists()
    directory = Path(first["attestation_challenge"]).parent
    gate = first["gate_id"]
    original = control._put
    atomic = approvals._atomic_create_text
    wanted = approvals.consumed_file(directory, gate) if boundary == "receipt" else (
        directory / f"{gate}.{boundary}.json")
    reached = []
    def interrupt(writer, path, raw):
        if path != wanted:
            return writer(path, raw)
        reached.append(path)
        if when == "after":
            writer(path, raw)
        raise RuntimeError("simulated durable-write interruption")
    with monkeypatch.context() as patch:
        patch.setattr(control, "_put", lambda path, raw: interrupt(original, path, raw))
        if boundary == "receipt":
            patch.setattr(approvals, "_atomic_create_text",
                          lambda path, raw: interrupt(atomic, path, raw))
        with pytest.raises(control.ControlEvolutionError, match="interruption"):
            control.record_control_review(**_kwargs(f), attestation_file=token)
    assert reached
    complete = boundary == "recorded" and when == "after"
    assert bool(control.verified_control_transition(**_kwargs(f, verify=True))) == complete
    recovered = control.record_control_review(**_kwargs(f), attestation_file=token)
    assert recovered["status"] == "recorded"
    assert _compare(f).passed
    receipt = approvals.consumed_file(directory, gate)
    original_receipt = receipt.read_bytes()
    assert control.record_control_review(**_kwargs(f), attestation_file=token) == recovered
    assert receipt.read_bytes() == original_receipt
    _assert_immutable(f)


def test_different_valid_token_cannot_replace_consumed_token(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    final = _record(f)
    canonical = Path(final["attestation_decision"])
    original = canonical.read_bytes()
    token = json.loads(original)
    token["payload"]["note"] = "different signed review token"
    token["signature"] = base64.b64encode(f.private.sign(
        approvals.canonical_bytes(token["payload"]))).decode()
    other = f.paths.run_dir / "different-token.json"
    _write_json(other, token)
    with pytest.raises(control.ControlEvolutionError):
        control.record_control_review(**_kwargs(f), attestation_file=other)
    assert canonical.read_bytes() == original
    assert _compare(f).passed
    _assert_immutable(f)


@pytest.mark.parametrize("kind", ["missing-commit", "short-commit", "target", "run",
                                 "report", "dispatch", "outside-report", "reject"])
def test_invalid_calls_cannot_change_baseline_state_or_helper(tmp_path, monkeypatch, kind):
    f = _fixture(tmp_path, monkeypatch)
    args = _kwargs(f)
    if kind in {"missing-commit", "short-commit"}:
        state = engine.load_state(f.paths)
        state.base_commit = "f" * 40 if kind == "missing-commit" else state.base_commit[:7]
        engine.save_state(f.paths, state)
    elif kind == "target":
        args["target"] = f.repo / "tests"
    elif kind == "run":
        args["run_id"] = "absent"
    elif kind == "report":
        _write_json(f.report, {"explanation": "short"})
    elif kind == "dispatch":
        _write_json(f.dispatch, {})
    elif kind == "outside-report":
        outside = tmp_path / "report.json"
        outside.write_bytes(f.report.read_bytes())
        args["report_file"] = outside
    else:
        first = control.record_control_review(**args)
        args["attestation_file"] = _sign(f, first, decision="reject")
    state_bytes, helper_bytes = f.paths.state.read_bytes(), f.helper.read_bytes()
    with pytest.raises(control.ControlEvolutionError):
        control.record_control_review(**args)
    assert f.paths.state.read_bytes() == state_bytes
    assert f.helper.read_bytes() == helper_bytes
    assert not f.marker.exists()
    _assert_immutable(f)


@pytest.mark.parametrize("name", ["helper", "report", "dispatch", "baseline", "state", "key",
    "challenge", "decision", "pending", "prepared", "recorded", "receipt", "directory"])
def test_symlinked_evidence_never_restores_authority(tmp_path, monkeypatch, name):
    f = _fixture(tmp_path, monkeypatch)
    final = _record(f)
    gate, directory = final["gate_id"], Path(final["record_file"]).parent
    paths = {"helper": f.helper, "report": f.report, "dispatch": f.dispatch,
             "baseline": f.baseline_path, "state": f.paths.state, "key": f.public,
             "receipt": approvals.consumed_file(directory, gate), "directory": directory}
    path = paths.get(name, directory / f"{gate}.{name}.json")
    displaced = tmp_path / f"original-{name}"
    path.rename(displaced)
    path.symlink_to(displaced, target_is_directory=name == "directory")
    _no_authority(f)
    assert not f.marker.exists()
    if name not in {"baseline", "state"}:
        _assert_immutable(f)


def test_key_mid_read_symlink_swap_fails_closed(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    _record(f)
    original = path_safety.stable_regular_bytes
    moved = tmp_path / "original-key"
    swapped = []
    def swap(path, **kwargs):
        raw = original(path, **kwargs)
        if path == f.public and not swapped:
            f.public.rename(moved)
            f.public.symlink_to(moved)
            swapped.append(True)
        return raw
    monkeypatch.setattr(path_safety, "stable_regular_bytes", swap)
    _no_authority(f)
    assert swapped and not f.marker.exists()


@pytest.mark.parametrize("phase", ["before-execution", "during-execution"])
def test_authority_rechecked_immediately_before_and_after_protected_execution(
        tmp_path, monkeypatch, phase):
    f = _fixture(tmp_path, monkeypatch)
    _record(f)
    if phase == "before-execution":
        verified = control.verified_control_transition
        calls = []
        def revoke(**kwargs):
            value = verified(**kwargs)
            calls.append(True)
            if len(calls) == 1:
                f.public.unlink()
            return value
        monkeypatch.setattr(control, "verified_control_transition", revoke)
        monkeypatch.setattr(regression, "_pytest_passing", lambda *a, **k: pytest.fail(
            "revoked authority reached protected execution"))
    else:
        execute = regression._pytest_passing
        def revoke_during(*args, **kwargs):
            result = execute(*args, **kwargs)
            f.public.unlink()
            return result
        monkeypatch.setattr(regression, "_pytest_passing", revoke_during)
    assert not _compare(f).passed
    assert f.marker.exists() == (phase == "during-execution")
    _assert_immutable(f)


def test_control_import_survives_missing_fcntl_and_mutation_fails_closed(tmp_path, monkeypatch):
    # Existing compiler import behavior is outside this control module's scope.
    importer = builtins.__import__
    def without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("simulated platform without fcntl")
        return importer(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_fcntl)
    spec = importlib.util.spec_from_file_location(
        "isolated_control_without_fcntl", control.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = tmp_path / "must-not-be-created"
    with pytest.raises(module.ControlEvolutionError, match="locking unavailable"):
        with module._locked(directory):
            pytest.fail("protected control lock unexpectedly succeeded")
    assert not directory.exists()


def test_invalid_token_publishes_no_prepared_final_or_consumption(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    first = control.record_control_review(**_kwargs(f))
    token = f.paths.run_dir / "invalid-external-token.json"
    token.write_text('{"payload": {}, "signature": "invalid"}')
    directory, gate = Path(first["attestation_challenge"]).parent, first["gate_id"]
    with pytest.raises(control.ControlEvolutionError):
        control.record_control_review(**_kwargs(f), attestation_file=token)
    assert not Path(first["attestation_decision"]).exists()
    assert not (directory / f"{gate}.prepared.json").exists()
    assert not (directory / f"{gate}.recorded.json").exists()
    assert not approvals.consumed_file(directory, gate).exists()
    _no_authority(f)
    _assert_immutable(f)


def test_external_key_with_symlinked_ancestor_is_not_trusted(tmp_path, monkeypatch):
    f = _fixture(tmp_path, monkeypatch)
    _record(f)
    key_dir = tmp_path / "external-keys"
    key_dir.mkdir()
    key = key_dir / "public.pem"
    key.write_bytes(f.public.read_bytes())
    link = tmp_path / "linked-keys"
    link.symlink_to(key_dir, target_is_directory=True)
    f.public = link / "public.pem"
    _no_authority(f)
    assert not f.marker.exists()


@pytest.mark.parametrize("artifact", ["pending", "prepared", "recorded", "receipt"])
def test_missing_transaction_part_never_grants_partial_authority(
        tmp_path, monkeypatch, artifact):
    f = _fixture(tmp_path, monkeypatch)
    final = _record(f)
    directory, gate = Path(final["record_file"]).parent, final["gate_id"]
    path = approvals.consumed_file(directory, gate) if artifact == "receipt" else (
        directory / f"{gate}.{artifact}.json")
    path.unlink()
    _no_authority(f)
    assert not f.marker.exists()
    _assert_immutable(f)


# The command line never selects the review key; only the installed default is consulted.


@pytest.mark.parametrize("command", ["evaluate", "record-control-review"])
def test_command_line_rejects_caller_selected_review_key(tmp_path, command, capsys):
    base = ["--repo", str(tmp_path), command, "--run-id", "r", "--target", str(tmp_path)]
    if command == "record-control-review":
        base += ["--report-file", "report.json", "--dispatch-file", "dispatch.json"]
    assert cli.build_parser().parse_args(base).cmd == command
    for run in (cli.build_parser().parse_args, cli.main):
        with pytest.raises(SystemExit) as exc:
            run(base + ["--review-public-key", str(tmp_path / "k.pem")])
        assert exc.value.code == 2
        assert "--review-public-key" in capsys.readouterr().err


def _evaluate_regression(f, monkeypatch):
    monkeypatch.setattr(engine, "run_all", lambda *_a, **_k: VerificationReport(
        task_id="control", run_id=f.run_id, completed_at="2026-09-29T00:00:00Z",
        checks=[], blocks_merge=False))
    return cli.main(["--repo", str(f.repo), "evaluate", "--run-id", f.run_id,
                     "--target", str(f.repo), "--step", "regression"])


def test_command_line_uses_installed_default_key_end_to_end(tmp_path, monkeypatch, capsys):
    f = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", f.public)
    assert cli.main(_cli(f)) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "pending"
    assert cli.main(_cli(f) + ["--attestation-file", str(_sign(f, first))]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "recorded"
    verify = _kwargs(f, verify=True) | {"review_public_key_path": None}
    assert control.verified_control_transition(**verify) == {
        control.CONTROL_PATH: (f.before, f.after)}
    seen, semantic = [], regression.verified_semantic_transitions
    monkeypatch.setattr(regression, "verified_semantic_transitions",
                        lambda *a, **k: seen.append(k) or semantic(*a, **k))
    assert _evaluate_regression(f, monkeypatch) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] is True and all(c["passed"] for c in out["report"]["checks"])
    assert seen and seen[0]["public_key_path"] is None
    assert Path(seen[0]["repo_root"]) == f.repo
    assert f.marker.read_text() == "executed"


def test_command_line_evaluate_ignores_self_signed_history(tmp_path, monkeypatch, capsys):
    f = _fixture(tmp_path, monkeypatch)
    installed = f.public
    # The caller signs its own challenge with its own key; only the installed key counts.
    f.private = Ed25519PrivateKey.generate()
    (tmp_path / "caller").mkdir()
    f.public = _public(f.private, tmp_path / "caller" / "k.pem")
    _record(f)
    monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", installed)
    monkeypatch.setattr(regression, "_pytest_passing", lambda *a, **k: pytest.fail(
        "self-signed control authority launched protected tests"))
    assert _evaluate_regression(f, monkeypatch) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False
    assert not f.marker.exists()
    _assert_immutable(f)


@pytest.mark.parametrize("where", ["repository", "hw", "default-in-repository"])
def test_control_path_refuses_key_inside_repository_or_hw(tmp_path, monkeypatch, where):
    f = _fixture(tmp_path, monkeypatch)
    # Place the copy first: a new product file changes the reviewed snapshot binding.
    local = f.repo / ("keys" if where != "hw" else ".hw") / "review-public.pem"
    local.parent.mkdir(exist_ok=True)
    local.write_bytes(f.public.read_bytes())
    first = control.record_control_review(**_kwargs(f))
    token = _sign(f, first)
    injected = None if where == "default-in-repository" else local
    if injected is None:
        monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", local)
    with pytest.raises(control.ControlEvolutionError, match="outside the repository"):
        control.record_control_review(
            **(_kwargs(f) | {"review_public_key_path": injected}), attestation_file=token)
    control.record_control_review(**_kwargs(f), attestation_file=token)
    monkeypatch.setattr(regression, "_pytest_passing", lambda *a, **k: pytest.fail(
        "in-repository key launched protected tests"))
    assert control.verified_control_transition(
        **(_kwargs(f, verify=True) | {"review_public_key_path": injected})) == {}
    assert not _compare(f, review_public_key_path=injected).passed
    assert not f.marker.exists()
    _assert_immutable(f)
