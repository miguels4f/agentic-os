"""Two follow-ups the acceptance-reads redesign (PR #24) left open, closed.

1. The commit the adapter makes after the LAST accepted packet of a run was never checked:
   registration only checks HEAD when a worktree is bound to the NEXT packet, and after the last
   packet there is none. The run's product checkout is now checked with the same rule (every
   path where HEAD differs from the base commit holds what an accepted packet left there) when
   the implement step completes, when the quality loop passes and at the ship gate.

2. New files had no line budget, so a worker could delete ``src/a.py`` and write the same code
   as ``src/a_new.py`` without the patch guard firing. Now a new file counts its lines like an
   edit, a deleted file whose words reappear in a new file is judged a rewrite, and a packet's
   deleted lines and new-file lines share one budget. Declared generated artifacts stay exempt.
"""

from __future__ import annotations

import base64
import json
import random
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_quality import _independent_panel, _record_subjective, _run_at_quality

from hyperwilfred_core import approvals, engine, gates, guardrails, quality
from hyperwilfred_core.schemas import (
    BuildObjective,
    GateDecision,
    GateKind,
    GeneratedArtifact,
    Plan,
    Slice,
)


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
         "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *arguments],
        check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


# ---- 1. the commit after the last accepted packet ----------------------------------------------

def _one_packet_run(tmp_path: Path) -> tuple[Path, engine.RunPaths]:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n", "other/protected.py": "secret = 1\n"})
    engine.plan(repo_root=repo, run_id="r", product_dir="src",
                objective=BuildObjective(id="o", title="Packets", objective="build app"))
    paths = engine.RunPaths(repo, "r")
    paths.plan.write_text(Plan(objective_id="o", product_dir="src", slices=[
        Slice(id="a", title="a", allowed_globs=["src/**"])]).model_dump_json())
    engine.fanout_plan(repo_root=repo, run_id="r")
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(repo))
    (repo / "src" / "a.py").write_text("a = 1\nb = 2\n")
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    (paths.results_dir / "a.json").write_text('{"ok": true}\n')
    assert engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                                worktree=str(repo), do_rollback=False)["verdict"]["accepted"]
    return repo, paths


def _commit_accepted_plus_extra(repo: Path) -> None:
    (repo / "other" / "protected.py").write_text("secret = 2\n")  # never judged by any packet
    _git(repo, "add", "src/a.py", "other/protected.py")
    _git(repo, "commit", "-qm", "accepted work plus an extra")


def test_final_commit_with_unaccepted_change_blocks_the_implement_step(tmp_path: Path) -> None:
    repo, paths = _one_packet_run(tmp_path)
    _commit_accepted_plus_extra(repo)
    with pytest.raises(engine.EngineError, match="no accepted packet made"):
        engine.complete_step(repo_root=repo, run_id="r", step_id="implement")
    assert engine.load_state(paths).step_status["implement"].value != "done"


def test_final_commit_of_exactly_the_accepted_work_completes_implement(tmp_path: Path) -> None:
    repo, _paths = _one_packet_run(tmp_path)
    _git(repo, "add", "src/a.py")
    _git(repo, "commit", "-qm", "accepted work")
    out = engine.complete_step(repo_root=repo, run_id="r", step_id="implement")
    assert out["completed_step"] == "implement"


def test_final_commit_rule_names_the_unexplained_path(tmp_path: Path) -> None:
    repo, _paths = _one_packet_run(tmp_path)
    _commit_accepted_plus_extra(repo)
    with pytest.raises(engine.EngineError, match="other/protected.py"):
        engine.require_accepted_head(repo_root=repo, run_id="r")


def test_quality_cannot_pass_over_an_unaccepted_commit(git_repo: Path) -> None:
    repo = git_repo
    paths = _run_at_quality(repo, run_id="q", product_dir="src")
    # Committed BEFORE review, so the independent reviews are signed over this very HEAD.
    (repo / "README.md").write_text("# product\nchanged outside any packet\n")
    _git(repo, "commit", "-qam", "unaccepted")
    _record_subjective(repo, "q", [5, 5, 5, 5, 5])
    _independent_panel(repo, "q")
    with pytest.raises(engine.EngineError, match="no accepted packet made"):
        quality.quality_gate(repo_root=repo, run_id="q")
    state = engine.load_state(paths)
    assert state.quality is not None and state.quality.passed is False
    assert state.step_status["quality"].value != "done"


def test_quality_still_passes_on_the_base_commit(git_repo: Path) -> None:
    _run_at_quality(git_repo, run_id="q", product_dir="src")
    _record_subjective(git_repo, "q", [5, 5, 5, 5, 5])
    _independent_panel(git_repo, "q")
    assert quality.quality_gate(repo_root=git_repo, run_id="q")["action"] == "pass"


def _key(path: Path) -> tuple[Ed25519PrivateKey, Path]:
    path.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    public = path / "fixture-public.pem"
    public.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, public


def _signed(path: Path, opened: dict, private: Ed25519PrivateKey, public: Path) -> Path:
    payload = approvals.build_decision_payload(
        opened["challenge"], decision="approve", decided_by="fixture-reviewer",
        note="test fixture only", public_key_path=public)
    path.write_text(json.dumps({
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }), encoding="utf-8")
    return path


def _open_ship(repo: Path) -> dict:
    return gates.gate_open(repo_root=repo, run_id="r", kind=GateKind.MERGE, step_id="ship",
                           action="open a PR", evidence=["verify.md"])


def test_ship_gate_does_not_open_over_an_unaccepted_commit(tmp_path: Path) -> None:
    repo, _paths = _one_packet_run(tmp_path)
    _commit_accepted_plus_extra(repo)
    with pytest.raises(gates.GateError, match="no accepted packet made"):
        _open_ship(repo)
    assert gates.load_gate(repo, "r", "merge-ship") is None


def test_ship_approval_refuses_a_commit_made_after_the_gate_opened(tmp_path: Path) -> None:
    private, public = _key(tmp_path / "keys")
    repo, _paths = _one_packet_run(tmp_path)
    _git(repo, "add", "src/a.py")
    _git(repo, "commit", "-qm", "accepted work")
    opened = _open_ship(repo)
    token = _signed(tmp_path / "decision.json", opened, private, public)
    _commit_accepted_plus_extra(repo)
    with pytest.raises(gates.GateError, match="no accepted packet made"):
        gates.gate_approve(repo_root=repo, run_id="r", gate_id="merge-ship",
                           decision=GateDecision.APPROVE, token_file=token,
                           public_key_path=public)
    gate = gates.load_gate(repo, "r", "merge-ship")
    assert gate is not None and gate.status.value == "pending"
    assert engine.load_state(engine.RunPaths(repo, "r")).step_status["ship"].value != "done"


def test_ship_approval_completes_ship_over_the_accepted_work(tmp_path: Path) -> None:
    private, public = _key(tmp_path / "keys")
    repo, paths = _one_packet_run(tmp_path)
    _git(repo, "add", "src/a.py")
    _git(repo, "commit", "-qm", "accepted work")
    opened = _open_ship(repo)
    token = _signed(tmp_path / "decision.json", opened, private, public)
    gates.gate_approve(repo_root=repo, run_id="r", gate_id="merge-ship",
                       decision=GateDecision.APPROVE, token_file=token, public_key_path=public)
    assert engine.load_state(paths).step_status["ship"].value == "done"


def test_refused_ship_approval_keeps_the_signed_token_for_a_retry(tmp_path: Path) -> None:
    private, public = _key(tmp_path / "keys")
    repo, paths = _one_packet_run(tmp_path)
    _git(repo, "add", "src/a.py")
    _git(repo, "commit", "-qm", "accepted work")
    accepted = _git(repo, "rev-parse", "HEAD").strip()
    token = _signed(tmp_path / "decision.json", _open_ship(repo), private, public)
    _commit_accepted_plus_extra(repo)
    with pytest.raises(gates.GateError, match="no accepted packet made"):
        gates.gate_approve(repo_root=repo, run_id="r", gate_id="merge-ship",
                           decision=GateDecision.APPROVE, token_file=token,
                           public_key_path=public)
    _git(repo, "reset", "-q", "--hard", accepted)  # the owner drops the unaccepted commit
    gates.gate_approve(repo_root=repo, run_id="r", gate_id="merge-ship",
                       decision=GateDecision.APPROVE, token_file=token, public_key_path=public)
    assert engine.load_state(paths).step_status["ship"].value == "done"


def test_gate_status_holds_a_placed_ship_approval_without_consuming_it(tmp_path: Path) -> None:
    private, public = _key(tmp_path / "keys")
    repo, paths = _one_packet_run(tmp_path)
    _git(repo, "add", "src/a.py")
    _git(repo, "commit", "-qm", "accepted work")
    accepted = _git(repo, "rev-parse", "HEAD").strip()
    opened = _open_ship(repo)
    _signed(Path(opened["approval"]["decision_file"]), opened, private, public)
    _commit_accepted_plus_extra(repo)
    status = gates.gate_status(repo_root=repo, run_id="r", public_key_path=public)
    assert "no accepted packet made" in status["refused"]["merge-ship"]
    assert status["pending"] == ["merge-ship"]
    _git(repo, "reset", "-q", "--hard", accepted)
    status = gates.gate_status(repo_root=repo, run_id="r", public_key_path=public)
    assert status["pending"] == [] and "refused" not in status
    assert engine.load_state(paths).step_status["ship"].value == "done"


def test_ship_rejection_is_never_held_back(tmp_path: Path) -> None:
    private, public = _key(tmp_path / "keys")
    repo, paths = _one_packet_run(tmp_path)
    _git(repo, "add", "src/a.py")
    _git(repo, "commit", "-qm", "accepted work")
    opened = _open_ship(repo)
    payload = approvals.build_decision_payload(
        opened["challenge"], decision="reject", decided_by="fixture-reviewer",
        note="no", public_key_path=public)
    token = tmp_path / "reject.json"
    token.write_text(json.dumps({
        "payload": payload,
        "signature": base64.b64encode(private.sign(approvals.canonical_bytes(payload))).decode(),
    }), encoding="utf-8")
    _commit_accepted_plus_extra(repo)
    gates.gate_approve(repo_root=repo, run_id="r", gate_id="merge-ship",
                       decision=GateDecision.REJECT, token_file=token, public_key_path=public)
    assert engine.load_state(paths).step_status["ship"].value == "failed"


def _generator_run(tmp_path: Path) -> tuple[Path, engine.RunPaths]:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n"})
    engine.plan(repo_root=repo, run_id="r", product_dir="src", objective=BuildObjective(
        id="o", title="Gen", objective="build app", generated_artifacts=[GeneratedArtifact(
            path="src/gen.txt", command="git checkout HEAD -- src/gen.txt")]))
    paths = engine.RunPaths(repo, "r")
    paths.plan.write_text(Plan(objective_id="o", product_dir="src", slices=[
        Slice(id="a", title="a", allowed_globs=["src/**"])]).model_dump_json())
    engine.fanout_plan(repo_root=repo, run_id="r")
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(repo))
    (repo / "src" / "a.py").write_text("a = 1\nb = 2\n")
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    (paths.results_dir / "a.json").write_text('{"ok": true}\n')
    assert engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                                worktree=str(repo), do_rollback=False)["verdict"]["accepted"]
    return repo, paths


def test_generated_artifact_committed_outside_any_packet_is_refused(tmp_path: Path) -> None:
    # A regeneration command can "reproduce" any committed bytes (here it restores them from
    # git), so reproducibility is no proof of origin: only an accepted packet explains a path.
    repo, _paths = _generator_run(tmp_path)
    (repo / "src" / "gen.txt").write_text("forged\n")
    _git(repo, "add", "src/a.py", "src/gen.txt")
    _git(repo, "commit", "-qm", "accepted work and an artifact no packet made")
    with pytest.raises(engine.EngineError, match="src/gen.txt"):
        engine.require_accepted_head(repo_root=repo, run_id="r")


# ---- 2. a line budget for new files ------------------------------------------------------------

BODY = "".join(f"def handler_{i}(request, context):\n    return compute_{i}(request)\n\n"
               for i in range(20))  # 60 lines


def _accept(worktree: Path, *, max_changed_lines: int = 400, generated: list[str] | None = None):
    return guardrails.accept(
        packet_id="p", worktree=str(worktree), allowed_globs=["src/**"],
        blocked_globs=[], result_file=None, max_changed_lines=max_changed_lines,
        max_hunk_span=120, full_file_ratio=0.9, do_rollback=False,
        generated_globs=generated,
    )


def _reasons(verdict) -> dict[str, list[str]]:
    return {violation.path: violation.reasons for violation in verdict.patch_violations}


def test_delete_and_rewrite_under_a_new_name_is_a_rewrite(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": BODY})
    (repo / "src" / "a.py").unlink()
    (repo / "src" / "a_new.py").write_text(BODY)  # the same code under a new name
    verdict = _accept(repo)
    assert verdict.accepted is False
    assert any("rewrite-under-new-name" in reason and "src/a.py" in reason
               for reason in _reasons(verdict).get("src/a_new.py", []))


def test_regenerated_copy_under_a_new_name_is_a_rewrite(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": BODY})
    (repo / "src" / "a.py").unlink()
    regenerated = BODY.replace("    return compute", "    result = compute").replace(
        "(request)\n", "(request)\n    return result\n")
    (repo / "src" / "a_new.py").write_text(regenerated)
    assert _accept(repo).accepted is False


def test_staged_rename_is_judged_like_an_untracked_one(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": BODY})
    _git(repo, "mv", "src/a.py", "src/a_new.py")
    verdict = _accept(repo)
    assert verdict.accepted is False
    assert "src/a_new.py" in _reasons(verdict)


def test_new_file_over_the_line_budget_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n"})
    (repo / "src" / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(401)))
    verdict = _accept(repo)
    assert verdict.accepted is False
    assert any("too-many-changed-lines (401 > 400)" in reason
               for reason in _reasons(verdict)["src/big.py"])


def test_deleted_and_new_lines_share_one_budget(tmp_path: Path) -> None:
    old = "".join(f"legacy_value_{i} = {i}\n" for i in range(250))
    repo = _repo(tmp_path, {"src/legacy.py": old})
    (repo / "src" / "legacy.py").unlink()
    (repo / "src" / "fresh.py").write_text(
        "".join(f"def unrelated_{i}():\n    pass\n" for i in range(100)))  # 200 other lines
    verdict = _accept(repo)
    assert verdict.accepted is False
    assert any("delete-and-create" in reason
               for reasons in _reasons(verdict).values() for reason in reasons)


def test_new_file_within_budget_is_still_accepted(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": BODY})
    (repo / "src" / "b.py").write_text(BODY)  # a new file; nothing deleted
    assert _accept(repo).accepted is True


def test_deleting_a_file_alone_is_judged_as_before(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": BODY, "src/b.py": "b = 1\n"})
    (repo / "src" / "a.py").unlink()
    assert _accept(repo).accepted is True


def test_unrelated_small_new_file_beside_a_deletion_is_accepted(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": BODY})
    (repo / "src" / "a.py").unlink()
    (repo / "src" / "notes.py").write_text("VERSION = '2'\n")
    assert _accept(repo).accepted is True


def test_declared_generated_new_file_keeps_its_exemption(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n"})
    (repo / "src" / "snapshot.json").write_text("".join(f'"{i}",\n' for i in range(1000)))
    assert _accept(repo, generated=["src/snapshot.json"]).accepted is True


def test_declared_generated_rename_keeps_its_exemption(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/gen/v1.json": BODY})
    (repo / "src" / "gen" / "v1.json").unlink()
    (repo / "src" / "gen" / "v2.json").write_text(BODY)
    assert _accept(repo, generated=["src/gen/**"]).accepted is True


def test_binary_new_file_keeps_no_line_budget(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n"})
    noise = random.Random(0).randbytes(64 * 1024)  # compressed image data looks like noise
    (repo / "src" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + noise)
    assert _accept(repo).accepted is True


def test_one_nul_byte_cannot_make_a_new_file_binary(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n"})
    (repo / "src" / "big.py").write_text("\0" + "".join(f"x{i} = {i}\n" for i in range(401)))
    assert _accept(repo).accepted is False


def test_one_nul_byte_cannot_hide_a_rewrite_under_a_new_name(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": BODY})
    (repo / "src" / "a.py").unlink()
    (repo / "src" / "a_new.py").write_text("\0" + BODY)
    assert _accept(repo).accepted is False


def test_staged_new_file_with_one_nul_byte_keeps_its_budget(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n"})
    (repo / "src" / "big.py").write_text("\0" + "".join(f"x{i} = {i}\n" for i in range(401)))
    _git(repo, "add", "src/big.py")
    assert _accept(repo).accepted is False


def test_nul_and_a_few_invalid_bytes_cannot_make_a_new_file_binary(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n"})
    body = "".join(f"x{i} = {i}\n" for i in range(401)).encode()
    (repo / "src" / "big.py").write_bytes(b"\0\xff\xfe" + body)
    assert _accept(repo).accepted is False


def test_mostly_text_with_salted_invalid_bytes_is_still_text(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n"})
    body = "".join(f"x{i} = {i}\n" for i in range(401)).encode()
    (repo / "src" / "big.py").write_bytes(b"\0" + b"\xc3" * 200 + body)  # < 10% invalid
    assert _accept(repo).accepted is False
