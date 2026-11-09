import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core.quality import QualityError, _changed_product_paths, _critique_target_paths


def test_quality_review_includes_committed_changes_since_run_base(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    tracked = tmp_path / "product.py"
    tracked.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "product.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "change"], check=True)

    assert _changed_product_paths(tmp_path, base) == ["product.py"]


def test_invalid_base_commit_fails_closed(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(QualityError, match="cannot determine committed review scope"):
        _changed_product_paths(tmp_path, "missing-base-object")


def test_malformed_review_target_cannot_fall_back_to_another_scope(tmp_path: Path) -> None:
    target = tmp_path / ".hw" / "review-run" / "review-target.json"
    target.parent.mkdir(parents=True)
    target.write_text("[]", encoding="utf-8")
    with pytest.raises(QualityError, match="review target is unreadable or invalid"):
        _critique_target_paths(tmp_path, "review-run")
