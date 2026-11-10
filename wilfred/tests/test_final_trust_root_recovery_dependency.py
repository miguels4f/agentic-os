"""Explicit review-key dependency must never arise from temporary DEFAULT configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_final_trust_root_recovery_trust import (
    _accept_subjective,
    _key,
    _sign,
    _subjective_input,
)
from test_quality import _run_at_quality

from hyperwilfred_core import approvals, engine, quality


def _report(repo: Path, key: Path | None = None) -> dict:
    return quality.provenance_report(
        repo_root=repo,
        run_id="q",
        state=engine.load_state(engine.RunPaths(repo, "q")),
        review_public_key_path=key,
    )


def _accept_with_configured_default(repo: Path, private, public: Path) -> None:
    source = _subjective_input(repo)
    kwargs = {
        "repo_root": repo,
        "run_id": "q",
        "result_file": str(source),
        "author": "independent",
    }
    first = quality.record_subjective(**kwargs)
    token = _sign(first, private, public)
    accepted = quality.record_subjective(**kwargs, attestation_file=str(token))
    assert accepted["author"] == "independent"


@pytest.mark.parametrize("mutation", ["missing", "changed"])
def test_temporary_default_never_becomes_implicit_dependency(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    _run_at_quality(tmp_path)
    keys = tmp_path_factory.mktemp("external-keys")
    private_a, public_a = _key(keys / "default-a.pem")
    monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", public_a)
    _accept_with_configured_default(tmp_path, private_a, public_a)

    assert "subjective" in _report(tmp_path)["independent"]
    if mutation == "missing":
        current_default = keys / "missing-production-default.pem"
    else:
        _private_b, current_default = _key(keys / "changed-default-b.pem")
    monkeypatch.setattr(
        approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", current_default,
    )

    assert "subjective" not in _report(tmp_path)["independent"]


@pytest.mark.parametrize("mutation", ["missing", "changed"])
def test_explicit_dependency_is_revalidated_not_blindly_cached(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, mutation: str,
) -> None:
    _run_at_quality(tmp_path)
    keys = tmp_path_factory.mktemp("external-keys")
    private_a, public_a = _key(keys / "explicit-a.pem")
    _accept_subjective(tmp_path, private_a, public_a)
    # An explicitly passed key is not remembered: later checks must pass it again.
    assert "subjective" not in _report(tmp_path)["independent"]
    assert "subjective" in _report(tmp_path, public_a)["independent"]

    if mutation == "missing":
        public_a.unlink()
    else:
        _private_b, public_b = _key(keys / "explicit-b.pem")
        public_a.write_bytes(public_b.read_bytes())

    assert "subjective" not in _report(tmp_path, public_a)["independent"]
