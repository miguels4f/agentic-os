"""Review keys are fenced by directory identity, including alternate path spellings."""

from pathlib import Path

import pytest

from hyperwilfred_core import approvals


def test_repository_key_with_different_case_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "KeyCase"
    key = repo / "tools" / "review.pem"
    key.parent.mkdir(parents=True)
    key.write_text("throwaway key")
    alternate = tmp_path / "KEYCASE" / "tools" / "review.pem"
    if not alternate.exists():
        pytest.skip("volume is case-sensitive")
    assert alternate.samefile(key)
    with pytest.raises(approvals.ApprovalError, match="outside the repository"):
        approvals.require_external_key_location(alternate, excluded_roots=[repo])


def test_hw_key_with_different_case_is_refused(tmp_path: Path) -> None:
    key = tmp_path / ".hw" / "review.pem"
    key.parent.mkdir(parents=True)
    key.write_text("throwaway key")
    alternate = tmp_path / ".HW" / "review.pem"
    if not alternate.exists():
        pytest.skip("volume is case-sensitive")
    assert alternate.samefile(key)
    with pytest.raises(approvals.ApprovalError, match=".hw"):
        approvals.require_external_key_location(alternate)


def test_repository_key_with_different_unicode_form_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "Café"
    key = repo / "tools" / "review.pem"
    key.parent.mkdir(parents=True)
    key.write_text("throwaway key")
    alternate = tmp_path / "Cafe\N{COMBINING ACUTE ACCENT}" / "tools" / "review.pem"
    if not alternate.exists():
        pytest.skip("volume distinguishes Unicode normalization forms")
    assert alternate.samefile(key)
    with pytest.raises(approvals.ApprovalError, match="outside the repository"):
        approvals.require_external_key_location(alternate, excluded_roots=[repo])


def _repo_with_key(tmp_path: Path) -> tuple[Path, Path]:
    import subprocess

    repo = tmp_path / "repo"
    (repo / "prod").mkdir(parents=True)
    (repo / "prod" / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c",
         "user.email=t@example.test", "commit", "-qm", "base"],
        check=True,
    )
    key = repo / "tools" / "review.pem"
    key.parent.mkdir()
    key.write_text("throwaway key")
    return repo, key


def test_volume_inode_alias_of_repository_key_is_refused(tmp_path: Path) -> None:
    import os

    repo, key = _repo_with_key(tmp_path)
    st = key.stat()
    alias = Path("/.vol") / str(st.st_dev) / str(st.st_ino)
    if not alias.exists():
        pytest.skip("macOS volume inode alias unavailable")
    assert os.path.samefile(alias, key)
    with pytest.raises(approvals.ApprovalError, match="outside the repository"):
        approvals.require_external_key_location(alias, excluded_roots=[repo / "prod"])


def test_planted_product_git_entry_cannot_hide_outer_repo(tmp_path: Path) -> None:
    repo, key = _repo_with_key(tmp_path)
    (repo / "prod" / ".git").mkdir()
    with pytest.raises(approvals.ApprovalError, match="outside the repository"):
        approvals.require_external_key_location(key, excluded_roots=[repo / "prod"])


def test_deleted_or_replaced_linked_marker_does_not_release_key(tmp_path: Path) -> None:
    import subprocess

    repo, _key = _repo_with_key(tmp_path)
    linked = tmp_path / "workers" / "arbitrary"
    linked.parent.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(linked), "HEAD"],
        check=True,
    )
    worker_key = linked / "tools" / "review.pem"
    worker_key.parent.mkdir()
    worker_key.write_text("throwaway key")
    for change in ("intact", "deleted", "replaced"):
        if change == "deleted":
            (linked / ".git").unlink()
        if change == "replaced":
            (linked / ".git").mkdir()
        with pytest.raises(approvals.ApprovalError, match="outside the repository"):
            approvals.require_external_key_location(
                worker_key, excluded_roots=[repo / "prod"]
            )


def test_linked_marker_removed_during_key_check_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    repo, _key = _repo_with_key(tmp_path)
    linked = tmp_path / "workers" / "arbitrary"
    linked.parent.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(linked), "HEAD"],
        check=True,
    )
    worker_key = linked / "tools" / "review.pem"
    worker_key.parent.mkdir()
    worker_key.write_text("throwaway key")
    original = approvals._canonical_existing_path
    removed = False

    def remove_during_check(path: Path) -> Path:
        nonlocal removed
        if Path(path) == worker_key and not removed:
            (linked / ".git").unlink()
            removed = True
        return original(path)

    monkeypatch.setattr(approvals, "_canonical_existing_path", remove_during_check)
    with pytest.raises(approvals.ApprovalError, match="outside the repository"):
        approvals.require_external_key_location(worker_key, excluded_roots=[repo / "prod"])
    assert removed
