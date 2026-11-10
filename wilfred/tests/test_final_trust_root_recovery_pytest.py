import hashlib
import os
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hyperwilfred_core import regression
from hyperwilfred_core.evaluators.base import SubprocessOutcome
from hyperwilfred_core.evaluators.code import pytest_evaluator


def _suite(root: Path) -> Path:
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]\n')
    (tests / "test_guard.py").write_text("def test_guard(): assert True\n")
    return tests


def test_frozen_scope_blocks_parent_discovery_and_pins_absence(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths=elsewhere\n")
    target = tmp_path / "product"
    target.mkdir()
    (target / "test_ok.py").write_text("def test_ok(): assert True\n")
    scope = pytest_evaluator.resolve_pytest_scope(target)
    argv = scope.argv("-q", pycache_prefix=tmp_path / "pycache")
    pytest_config = argv.index("-c", argv.index("-c") + 1)  # the first -c is the bootstrap
    assert scope.config_path is None and argv[pytest_config + 1] == os.devnull
    assert argv[argv.index("--confcutdir") + 1] == str(target)
    with pytest.raises(FrozenInstanceError):
        scope.target = tmp_path  # type: ignore[misc]


@pytest.mark.parametrize("name", ["PYTEST_ADDOPTS", "PYTEST_PLUGINS"])
def test_environment_rejects_pytest_injection(name: str, monkeypatch) -> None:
    monkeypatch.setenv(name, "injected")
    with pytest.raises(ValueError, match=name):
        pytest_evaluator.pytest_environment()


def test_autoload_is_verifiably_disabled(tmp_path: Path, monkeypatch) -> None:
    tmp_path.joinpath("test_ok.py").write_text("def test_ok(): assert True\n")
    observed = {}
    def fake(argv, **kwargs):
        observed.update(kwargs["env"])
        return SubprocessOutcome(0, "1 passed", "", 1)
    monkeypatch.setattr(pytest_evaluator, "run_subprocess", fake)
    assert pytest_evaluator.PytestEvaluator().run(tmp_path).passed
    assert observed["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert "PYTEST_ADDOPTS" not in observed and "PYTEST_PLUGINS" not in observed


def test_controls_helpers_and_explicit_plugin_are_pinned(tmp_path: Path) -> None:
    tests = _suite(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths=["tests"]\naddopts="-p local_plugin"\n')
    files = [tmp_path / "conftest.py", tests / "conftest.py", tests / "helpers.py",
             tmp_path / "local_plugin.py"]
    for path in files:
        path.write_text("VALUE = 1\n")
    assert set(files) <= set(pytest_evaluator.resolve_pytest_scope(tmp_path).control_files)
    baseline = regression.capture_baseline(tmp_path)
    files[2].write_text("VALUE = 2\n")
    assert "control files changed" in (regression.compare(tmp_path, baseline).details or "")


def test_multiple_configs_and_symlinked_config_control_test_fail(tmp_path: Path) -> None:
    tests = _suite(tmp_path)
    real = tmp_path / "real.ini"
    real.write_text("[pytest]\ntestpaths=tests\n")
    (tmp_path / "pytest.ini").write_text(real.read_text())
    with pytest.raises(ValueError, match="ambiguous"):
        pytest_evaluator.resolve_pytest_scope(tmp_path)
    (tmp_path / "pytest.ini").unlink()
    (tmp_path / "pyproject.toml").unlink()
    for relative in ("pytest.ini", "tests/conftest.py"):
        link = tmp_path / relative
        link.symlink_to(real)
        with pytest.raises(ValueError, match="symlink"):
            pytest_evaluator.resolve_pytest_scope(tmp_path)
        link.unlink()
    tests.joinpath("test_link.py").symlink_to(real)
    with pytest.raises(regression.RegressionError, match="symlink"):
        regression.capture_baseline(tmp_path)


def _commit(repo: Path) -> str:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "fixture@example.test"), ("user.name", "Fixture")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("count,historical", [(809, False), (618, True)])
def test_legacy_controls_derive_only_from_base(
    tmp_path: Path, monkeypatch, count: int, historical: bool,
) -> None:
    tests = _suite(tmp_path)
    test = tests / "test_guard.py"
    tests.joinpath("conftest.py").write_text("VALUE = 1\n")
    commit = _commit(tmp_path)
    ids = [f"tests/test_guard.py::test_{n}" for n in range(count)]
    hashes = {"tests/test_guard.py" if historical else "test_guard.py":
              hashlib.sha256(test.read_bytes()).hexdigest()}
    if historical:
        hashes[".hw/archive/tests/test_guard.py"] = "0" * 64
    baseline = {"target": str(tmp_path if historical else tests), "passing_ids": ids,
                "passing": count, "test_file_hashes": hashes}
    monkeypatch.setattr(regression, "_pytest_passing",
                        lambda *_args, **_kwargs: (set(ids), 0, []))
    assert regression.compare(tmp_path, baseline, base_commit=commit).passed
    tests.joinpath("conftest.py").write_text("VALUE = 2\n")
    assert not regression.compare(tmp_path, baseline, base_commit=commit).passed
    assert "evidence unavailable" in (regression.compare(tmp_path, baseline).details or "")
