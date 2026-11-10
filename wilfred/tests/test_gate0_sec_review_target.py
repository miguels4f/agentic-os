"""A review target file is honoured only on the review run that owns it.

Planted in a build or improve run, carrying another run id, or pointing at an absolute or
parent-escaping root, it must stop the snapshot and the critic path list instead of narrowing
or moving the reviewed scope.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hyperwilfred_core import engine, quality, review
from hyperwilfred_core.quality import QualityError
from hyperwilfred_core.schemas import BuildMode, BuildObjective, ReviewTargetKind

REFUSED = "review target is unreadable or invalid"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        check=True, capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "other").mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    (repo / "pkg" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "pkg" / "decoy.py").write_text("DECOY = 1\n", encoding="utf-8")
    (repo / "other" / "decoy.py").write_text("DECOY = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".hw/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _objective() -> BuildObjective:
    return BuildObjective(id="o", title="t", objective="x")


def _plant(repo: Path, owner: str, **fields: Any) -> Path:
    body = {"run_id": owner, "kind": "paths", "root": ".", "resolved_paths": ["pkg/decoy.py"]}
    body.update(fields)
    target = repo / ".hw" / owner / "review-target.json"
    target.write_text(json.dumps(body), encoding="utf-8")
    return target


def _assert_refused(repo: Path, state: Any) -> None:
    with pytest.raises(QualityError, match=REFUSED):
        quality.product_snapshot_sha256(repo, state)
    with pytest.raises(QualityError, match=REFUSED):
        quality._critique_target_paths(repo, state.run_id)
    with pytest.raises(QualityError, match=REFUSED):
        quality.critique_plan(repo_root=repo, run_id=state.run_id)


@pytest.mark.parametrize("mode", [BuildMode.BUILD, BuildMode.IMPROVE], ids=lambda m: m.value)
def test_target_planted_outside_a_review_run_is_refused(tmp_path: Path, mode: BuildMode) -> None:
    repo = _repo(tmp_path)
    state = engine.plan(
        repo_root=repo, objective=_objective(), run_id="work", product_dir="pkg", mode=mode,
    )
    (repo / "pkg" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert quality._critique_target_paths(repo, "work") == ["pkg/app.py"]
    _plant(repo, "work")
    _assert_refused(repo, state)
    # Same root as the run's product directory: only the run-mode rule is left to refuse it.
    _plant(repo, "work", root="pkg", resolved_paths=["decoy.py"])
    _assert_refused(repo, state)


def _review_run(tmp_path: Path) -> tuple[Path, Any]:
    repo = _repo(tmp_path)
    review.create_review(
        repo_root=repo, objective=_objective(), kind=ReviewTargetKind.PATHS, run_id="rv",
        root="pkg", paths=["app.py"],
    )
    return repo, engine.load_state(engine.RunPaths(repo, "rv"))


def test_genuine_review_target_is_still_honoured(tmp_path: Path) -> None:
    repo, state = _review_run(tmp_path)
    assert state.mode is BuildMode.REVIEW
    assert quality._critique_target_paths(repo, "rv") == ["app.py"]
    first = quality.product_snapshot_sha256(repo, state)
    (repo / "pkg" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert quality.product_snapshot_sha256(repo, state) != first


@pytest.mark.parametrize(
    "fields",
    [
        {"run_id": "some-other-run", "root": "pkg"},
        {"root": "{outside}", "resolved_paths": ["x.py"]},
        {"root": "../outside", "resolved_paths": ["x.py"]},
        {"root": "pkg/../../outside", "resolved_paths": ["x.py"]},
        {"root": "other", "resolved_paths": ["decoy.py"]},
        {"root": "."},
    ],
    ids=["foreign-run-id", "absolute-root", "parent-root", "inner-parent-root",
         "root-is-not-the-product-directory", "root-widened-to-the-repository"],
)
def test_review_run_refuses_a_forged_target(tmp_path: Path, fields: dict[str, Any]) -> None:
    repo, state = _review_run(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.py").write_text("OUTSIDE = 1\n", encoding="utf-8")
    forged = {k: v.format(outside=outside) if isinstance(v, str) else v for k, v in fields.items()}
    _plant(repo, "rv", **forged)
    _assert_refused(repo, state)


def test_review_run_refuses_a_symlinked_target(tmp_path: Path) -> None:
    repo, state = _review_run(tmp_path)
    target = repo / ".hw" / "rv" / "review-target.json"
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(target.read_bytes())
    target.unlink()
    os.symlink(elsewhere, target)
    _assert_refused(repo, state)


def test_target_without_a_readable_run_state_is_refused(tmp_path: Path) -> None:
    repo, state = _review_run(tmp_path)
    (repo / ".hw" / "rv" / "state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(QualityError, match=REFUSED):
        quality._critique_target_paths(repo, "rv")
