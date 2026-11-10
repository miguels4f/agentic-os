"""The base-commit witness is read from hash-verified git objects, never from what a
repository-local replacement, attribute or inherited git variable offers."""

from __future__ import annotations

import hashlib
import os
import subprocess
import zlib
from pathlib import Path

import pytest

from hyperwilfred_core import regression

HONEST = "import pytest\n\n\n@pytest.fixture\ndef strict():\n    return True\n"
FORGED = "import pytest\n\n\n@pytest.fixture\ndef strict():\n    return False\n"
TEST = "from calc import add\n\n\ndef test_add(strict):\n    assert add(1, 2) == 3\n"
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.test")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *IDENTITY, *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


def _product(root: Path, conftest: str = HONEST) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "conftest.py").write_text(conftest)
    (root / "tests" / "test_calc.py").write_text(TEST)
    return root


def _commit(repo: Path, message: str = "base") -> str:
    if not (repo / ".git").exists():
        _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _witness(target: Path, base: str) -> str | None:
    _config, controls = regression.legacy_control_witness(target, (target / "tests",), base)
    return controls.get("tests/conftest.py")


def _replaced(repo: Path) -> str:
    """Commit a forged control and make the base commit id resolve to it; return the base id."""
    base = _commit(repo)
    (repo / "tests" / "conftest.py").write_text(FORGED)
    _git(repo, "replace", base, _commit(repo, "forged"))
    return base


def test_replace_ref_does_not_change_the_base_witness(tmp_path: Path) -> None:
    repo = _product(tmp_path / "repo")
    base = _replaced(repo)
    assert _git(repo, "show", f"{base}:tests/conftest.py") + "\n" == FORGED
    assert _witness(repo, base) == _sha(HONEST)


def test_replace_ref_cannot_hide_a_forged_control_from_compare(tmp_path: Path) -> None:
    repo = _product(tmp_path / "repo")
    captured = regression.capture_baseline(repo)
    legacy = {key: value for key, value in captured.items() if key not in {
        "control_file_hashes", "pytest_config_file_sha256", "data_file_hashes"}}
    base = _replaced(repo)
    result = regression.compare(repo, legacy, base_commit=base)
    assert not result.passed
    assert result.details == (
        "regression vs baseline: pytest control files changed from the base commit")


def test_inherited_git_directory_does_not_change_the_base_witness(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _product(tmp_path / "repo")
    base = _commit(repo)
    worker = tmp_path / "worker"
    _git(tmp_path, "clone", "-q", str(repo), str(worker))
    (worker / "tests" / "conftest.py").write_text(FORGED)
    _git(worker, "replace", base, _commit(worker, "forged"))
    monkeypatch.setenv("GIT_DIR", str(worker / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    assert _witness(repo, base) == _sha(HONEST)


def test_git_runs_without_inherited_variables_or_replacement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _product(tmp_path / "repo")
    base = _commit(repo)
    inherited = {"GIT_OBJECT_DIRECTORY": str(repo / ".git" / "objects"),
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(tmp_path), "GIT_NAMESPACE": "",
                 "GIT_REPLACE_REF_BASE": "refs/replace/", "GIT_CONFIG_COUNT": "0"}
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    calls: list[tuple[list[str], dict[str, str] | None]] = []
    real = subprocess.run

    def spy(argv, *args, **kwargs):
        calls.append((list(argv), kwargs.get("env")))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(regression.subprocess, "run", spy)
    assert _witness(repo, base) == _sha(HONEST)
    assert calls
    for argv, env in calls:
        assert argv[:2] == ["git", "--no-replace-objects"] and "archive" not in argv
        assert env is not None and not set(env) & set(inherited)
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1" and env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_planted_repository_inside_the_target_fails_closed(tmp_path: Path) -> None:
    outer = tmp_path / "real"
    target = _product(outer / "prod")
    base = _commit(outer)
    assert _witness(target, base) == _sha(HONEST)
    # A worker repository with the product layout at its root, the genuine base commit object
    # and a replacement that maps the base id to the forged commit.
    worker = _product(tmp_path / "worker", FORGED)
    forged = _commit(worker, "forged root layout")
    _git(worker, "fetch", "-q", str(outer), "HEAD")
    _git(worker, "replace", base, forged)
    (target / ".git").write_text(f"gitdir: {worker / '.git'}\n")
    assert _git(target, "rev-parse", "--show-toplevel") == str(target.resolve())
    with pytest.raises(regression.RegressionError):
        _witness(target, base)


def test_export_ignore_attribute_cannot_drop_a_control(tmp_path: Path) -> None:
    outer = tmp_path / "repo"
    target = _product(outer / ".hidden" / "prod")
    _product(outer / "other", FORGED)
    base = _commit(outer)
    (outer / ".git" / "info").mkdir(exist_ok=True)
    (outer / ".git" / "info" / "attributes").write_text(
        ".hidden/prod/tests/conftest.py export-ignore\n")
    config, controls = regression.legacy_control_witness(target, (target / "tests",), base)
    assert controls == {"tests/conftest.py": _sha(HONEST), "tests/test_calc.py": _sha(TEST)}
    assert config is not None


def test_overwritten_loose_object_is_refused(tmp_path: Path) -> None:
    repo = _product(tmp_path / "repo")
    base = _commit(repo)
    blob = _git(repo, "rev-parse", f"{base}:tests/conftest.py")
    loose = repo / ".git" / "objects" / blob[:2] / blob[2:]
    os.chmod(loose, 0o644)
    body = FORGED.encode()
    loose.write_bytes(zlib.compress(b"blob %d\0" % len(body) + body))
    assert _git(repo, "show", f"{base}:tests/conftest.py") + "\n" == FORGED
    with pytest.raises(regression.RegressionError,
                       match="base_commit object bytes do not match their object id"):
        _witness(repo, base)
    # The same holds for every kind of object on the way to the file: the commit itself.
    other = _product(tmp_path / "other")
    base = _commit(other)
    tree = _git(other, "rev-parse", f"{base}^{{tree}}")
    loose = other / ".git" / "objects" / base[:2] / base[2:]
    os.chmod(loose, 0o644)
    body = f"tree {tree}\nauthor a <a@example.test> 0 +0000\n\nforged\n".encode()
    loose.write_bytes(zlib.compress(b"commit %d\0" % len(body) + body))
    with pytest.raises(regression.RegressionError, match="do not match their object id"):
        _witness(other, base)
