"""The active pytest configuration is read and parsed in exactly one place."""

import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hyperwilfred_core.evaluators.code import pytest_evaluator

_CONFIGS = {
    "pytest.ini": "[pytest]\ntestpaths = suite\naddopts = -p local_plugin -q\n",
    "pyproject.toml": (
        '[tool.pytest.ini_options]\ntestpaths = ["suite"]\naddopts = "-p local_plugin -q"\n'
    ),
    "tox.ini": "[pytest]\ntestpaths = suite\naddopts = -p local_plugin -q\n",
    "setup.cfg": "[tool:pytest]\ntestpaths = suite\naddopts = -p local_plugin -q\n",
}
_SECTIONS = {
    "pytest.ini": "pytest",
    "pyproject.toml": "tool.pytest.ini_options",
    "tox.ini": "pytest",
    "setup.cfg": "tool:pytest",
}
_INACTIVE = {
    "pyproject.toml": "[tool.other]\nvalue = 1\n",
    "tox.ini": "[tox]\nenvlist = py\n",
    "setup.cfg": "[pytest]\ntestpaths = suite\n",
}


def _target(root: Path, name: str, text: str | None = None) -> Path:
    target = root.resolve()
    (target / name).write_text(_CONFIGS[name] if text is None else text)
    suite = target / "suite"
    suite.mkdir()
    (suite / "test_ok.py").write_text("def test_ok(): assert True\n")
    (target / "local_plugin.py").write_text("VALUE = 1\n")
    return target


def _count_reads(monkeypatch: pytest.MonkeyPatch, watched: Path) -> list[Path]:
    reads: list[Path] = []
    original = Path.read_text

    def counting(self: Path, *args: object, **kwargs: object) -> str:
        if self == watched:
            reads.append(self)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counting)
    return reads


@pytest.mark.parametrize("name", list(_CONFIGS))
def test_resolving_the_scope_reads_the_active_config_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    target = _target(tmp_path, name)
    reads = _count_reads(monkeypatch, target / name)
    scope = pytest_evaluator.resolve_pytest_scope(target)
    assert scope.config_path == target / name
    assert scope.roots == (target / "suite",)
    assert target / "local_plugin.py" in scope.control_files
    assert len(reads) == 1


@pytest.mark.parametrize("name", list(_CONFIGS))
def test_loader_returns_one_frozen_record(tmp_path: Path, name: str) -> None:
    target = _target(tmp_path, name)
    config = pytest_evaluator._load_pytest_config(target / name)
    assert config is not None
    assert (config.path, config.section) == (target / name, _SECTIONS[name])
    assert list(config.testpaths) == ["suite"]
    assert config.addopts == "-p local_plugin -q"
    with pytest.raises(FrozenInstanceError):
        config.path = tmp_path  # type: ignore[misc]


@pytest.mark.parametrize("name", list(_INACTIVE))
def test_file_without_a_pytest_section_is_not_active(tmp_path: Path, name: str) -> None:
    target = _target(tmp_path, name, _INACTIVE[name])
    assert pytest_evaluator._load_pytest_config(target / name) is None
    assert pytest_evaluator.maintained_test_scope(target) == ((target,), None)


def test_pytest_ini_is_active_even_without_a_section(tmp_path: Path) -> None:
    target = _target(tmp_path, "pytest.ini", "[other]\nkey = value\n")
    assert pytest_evaluator.maintained_test_scope(target) == ((target,), target / "pytest.ini")
    assert pytest_evaluator.resolve_pytest_scope(target).roots == (target,)


@pytest.mark.parametrize("name", list(_CONFIGS))
def test_malformed_config_keeps_the_same_error(tmp_path: Path, name: str) -> None:
    broken = "[tool.pytest.ini_options\n" if name == "pyproject.toml" else "[pytest\nkey\n"
    target = _target(tmp_path, name, broken)
    with pytest.raises(ValueError, match="invalid pytest configuration at "):
        pytest_evaluator.maintained_test_scope(target)


def test_bad_testpaths_and_list_addopts_keep_their_behaviour(tmp_path: Path) -> None:
    target = _target(tmp_path, "pyproject.toml",
                     '[tool.pytest.ini_options]\ntestpaths = "suite"\n')
    with pytest.raises(ValueError, match="invalid pytest testpaths"):
        pytest_evaluator.maintained_test_scope(target)
    (target / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["suite"]\naddopts = ["-p", "local_plugin"]\n')
    scope = pytest_evaluator.resolve_pytest_scope(target)
    assert target / "local_plugin.py" in scope.control_files


def test_section_rule_and_parsers_exist_once() -> None:
    source = inspect.getsource(pytest_evaluator)
    assert source.count('"tool:pytest"') == 1
    assert source.count("tomllib.loads(") == 1
    assert source.count("ConfigParser(") == 1
