"""CLI surface for the Phase-5 commands: learn, propose, promote, dashboard, anomalies, escalations.

Exercises the autonomy loop through the REAL ``main()`` entrypoint (exit codes + JSON output).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyperwilfred_core import approvals, gates
from hyperwilfred_core.cli import main


def _run(args: list[str], capsys) -> tuple[int, dict]:
    code = main(args)
    out = json.loads(capsys.readouterr().out)
    return code, out


@pytest.fixture
def approval_key(tmp_path: Path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = tmp_path / "cli-public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    monkeypatch.setattr(approvals, "DEFAULT_PUBLIC_KEY_PATH", public)
    return private, public


def test_learn_emits_proposals(hw_history: Path, capsys) -> None:
    code, out = _run(["--repo", str(hw_history), "learn"], capsys)
    assert code == 0
    assert out["proposals"] >= 1
    assert all(pid.startswith("learn-") for pid in out["proposal_ids"])


def test_propose_lists_and_opens_gate(hw_history: Path, capsys) -> None:
    _run(["--repo", str(hw_history), "learn"], capsys)
    code, listing = _run(["--repo", str(hw_history), "propose"], capsys)
    assert code == 0 and listing["count"] >= 1
    assert all(p["auto_apply"] is False for p in listing["proposals"])
    pid = listing["proposals"][0]["proposal_id"]

    code, gate = _run(["--repo", str(hw_history), "propose", "--id", pid], capsys)
    assert code == 0 and gate["status"] == "under_review" and gate["blocked"] is True


def test_promote_refuses_without_approval_then_succeeds(
    hw_history: Path, capsys, approval_key
) -> None:
    _run(["--repo", str(hw_history), "learn"], capsys)
    _, listing = _run(["--repo", str(hw_history), "propose"], capsys)
    pid = listing["proposals"][0]["proposal_id"]
    _run(["--repo", str(hw_history), "propose", "--id", pid], capsys)

    # unapproved → exit 1, not promoted
    code, out = _run(["--repo", str(hw_history), "promote", "--id", pid], capsys)
    assert code == 1 and out["promoted"] is False
    assert not (hw_history / ".hw" / f"imp-{pid}").exists()

    # A trusted-host fixture signs the exact proposal challenge; labels/markers are insufficient.
    private, public = approval_key
    gdir = hw_history / ".hw" / "proposals" / "gates"
    challenge = approvals.load_challenge(gdir, pid)
    payload = approvals.build_decision_payload(
        challenge,
        decision="approve",
        decided_by="fixture-human",
        note="fixture",
        public_key_path=public,
    )
    token = {
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }
    gates.decision_file_in(gdir, pid).write_text(json.dumps(token), encoding="utf-8")
    code, out = _run(["--repo", str(hw_history), "promote", "--id", pid], capsys)
    assert code == 0 and out["promoted"] is True
    assert out["mode"] == "improve"
    assert (hw_history / ".hw" / out["run_id"] / "state.json").exists()


def test_dashboard_cli(hw_history: Path, capsys) -> None:
    code, out = _run(["--repo", str(hw_history), "dashboard"], capsys)
    assert code == 0
    assert out["total_runs"] == 3
    assert out["anomalies"]


def test_anomalies_cli_routes_alerts(hw_history: Path, capsys) -> None:
    code, out = _run(["--repo", str(hw_history), "anomalies"], capsys)
    assert code == 0 and out["count"] >= 1
    assert out["alerts_routed"], "high-severity anomalies should route to the sink"

    # escalations command surfaces them
    code, esc = _run(["--repo", str(hw_history), "escalations"], capsys)
    assert code == 0 and esc["count"] >= 1
    assert esc["notifications"].endswith("notifications.md")


def test_anomalies_no_alert_flag(hw_history: Path, capsys) -> None:
    code, out = _run(["--repo", str(hw_history), "anomalies", "--no-alert"], capsys)
    assert code == 0 and out["alerts_routed"] == []


def test_promote_unknown_proposal_is_engine_error(hw_history: Path, capsys) -> None:
    code = main(["--repo", str(hw_history), "promote", "--id", "learn-nope"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["kind"] == "learning_error"
