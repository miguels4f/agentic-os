"""Review provenance never depends on what happened earlier in the same process.

A review accepted with an explicitly passed verification key stays verifiable only by callers
that pass that key again; every other check uses the installed default key.  A fresh process
and a long-lived one therefore reach the same verdict.  Every key lives in a temporary
directory outside the reviewed run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_final_trust_root_recovery_trust import _accept_subjective, _key
from test_quality import _run_at_quality

from hyperwilfred_core import approvals, engine, quality

_SRC = Path(__file__).resolve().parents[1] / "src"
_FRESH = """
import json, sys
from pathlib import Path
from hyperwilfred_core import approvals, engine, quality
repo, default, explicit = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH = default
report = quality.provenance_report(
    repo_root=repo, run_id="q", state=engine.load_state(engine.RunPaths(repo, "q")),
    review_public_key_path=Path(explicit) if explicit else None,
)
print(json.dumps(report["independent"]))
"""


def _live(repo: Path, explicit: Path | None) -> list[str]:
    return quality.provenance_report(
        repo_root=repo, run_id="q", state=engine.load_state(engine.RunPaths(repo, "q")),
        review_public_key_path=explicit,
    )["independent"]


def _fresh(repo: Path, default: Path, explicit: Path | None) -> list[str]:
    done = subprocess.run(
        [sys.executable, "-c", _FRESH, str(repo), str(default), str(explicit or "")],
        check=True, capture_output=True, text=True,
        env={"PYTHONPATH": str(_SRC), "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(repo)},
    )
    return json.loads(done.stdout)


@pytest.mark.parametrize("default_kind", ["missing", "other-key"])
def test_fresh_and_long_lived_process_reach_the_same_verdict(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch, default_kind: str,
) -> None:
    _run_at_quality(tmp_path)
    keys = tmp_path_factory.mktemp("external-keys")
    default = keys / "installed-default.pem"
    if default_kind == "other-key":
        _key(default)
    monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", default)
    private, explicit = _key(keys / "explicit-review.pem")
    accepted, _kwargs = _accept_subjective(tmp_path, private, explicit)
    assert accepted["author"] == "independent"

    # Without the key the review is not independent, whatever the process remembers.
    assert _live(tmp_path, None) == _fresh(tmp_path, default, None) == []
    # Passing the key again verifies it identically in both processes.
    assert _live(tmp_path, explicit) == _fresh(tmp_path, default, explicit) == ["subjective"]
    assert quality.quality_gate(repo_root=tmp_path, run_id="q")["action"] != "pass"


def test_quality_module_keeps_no_review_key_registry() -> None:
    assert not hasattr(quality, "_REVIEW_KEY_SOURCES")
    mutable = {
        name for name, value in vars(quality).items()
        if not name.startswith("__") and isinstance(value, (dict, list, set))
    }
    assert mutable == set()


def test_installed_default_key_verifies_later_checks_without_a_key(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_at_quality(tmp_path)
    keys = tmp_path_factory.mktemp("external-keys")
    private, installed = _key(keys / "installed-default.pem")
    monkeypatch.setattr(approvals, "DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH", installed)
    _accept_subjective(tmp_path, private, installed)
    assert _live(tmp_path, None) == _fresh(tmp_path, installed, None) == ["subjective"]
