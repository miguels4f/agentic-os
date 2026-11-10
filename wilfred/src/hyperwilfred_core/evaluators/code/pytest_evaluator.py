"""Pytest evaluator (Blueprint §5.1, §6.6 — `evaluators/code/`).

Wraps ``pytest`` invocation as a subprocess and surfaces the result as a
``CheckResult``. The evaluator does *not* import pytest as a library: that
would couple our process to the version of pytest the test target itself
uses, which can drift between repos.
"""

from __future__ import annotations

import ast
import configparser
import contextlib
import os
import re
import shlex
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from hyperwilfred_core.evaluators.base import BaseEvaluator, run_subprocess
from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Finding, Severity

# ``-rfE`` short-summary lines: ``FAILED path::name - msg`` / ``ERROR path::name - msg``.
_SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+)")
_COUNT = re.compile(r"(\d+) (failed|errors?)\b")
# The runner (pytest, _pytest, pluggy, iniconfig) is imported before the target joins the module
# search path, so no target file can replace it; the target then gets the leading entry that
# ``python -m pytest`` would give it, keeping target-local imports and plugins working.
_BOOTSTRAP = ("import sys, pytest; sys.path.insert(0, sys.argv.pop(1)); "
              "raise SystemExit(pytest.console_main())")
# The protected regression run adds an engine-supplied outcome recorder to the same bootstrap:
# every phase report per node id goes to a JSON file whose path the parent passes after the
# target, so nothing the tests print can register a passing test and no module has to be
# importable from the target (json is loaded before the target joins the search path).
# Limit: the recorder shares the interpreter with the tests and the product modules they
# import. It is not a boundary against code under test that deliberately alters the recorder,
# the result file or the runner; such code can make the recorded result say what it wants.
_RECORDING_BOOTSTRAP = (
    "import sys, pytest; import json\n"
    "target, result = sys.argv.pop(1), sys.argv.pop(1)\n"
    "class Recorder:\n"
    "    collected = None\n"
    "    def __init__(self):\n"
    "        self.reports = {}\n"
    "    def pytest_collection_finish(self, session):\n"
    "        self.collected = [item.nodeid for item in session.items]\n"
    "    def pytest_runtest_logreport(self, report):\n"
    "        self.reports.setdefault(report.nodeid, []).append(\n"
    "            [report.when, report.outcome, hasattr(report, 'wasxfail')])\n"
    "recorder = Recorder()\n"
    "sys.path.insert(0, target)\n"
    "code = int(pytest.main(sys.argv[1:], plugins=[recorder]))\n"
    "sys.stdout.flush()\n"
    "with open(result, 'x', encoding='utf-8') as handle:\n"
    "    json.dump({'exitstatus': code, 'collected': recorder.collected,\n"
    "               'reports': recorder.reports}, handle)\n"
    "raise SystemExit(code)\n"
)


@dataclass(frozen=True)
class PytestScope:
    """One immutable, target-contained description of a pytest invocation."""

    target: Path
    roots: tuple[Path, ...]
    config_path: Path | None
    control_files: tuple[Path, ...]
    test_files: tuple[Path, ...] = ()

    def argv(self, *options: str, pycache_prefix: Path,
             result_path: Path | None = None) -> list[str]:
        """Isolated interpreter: ``-I`` keeps the working directory, user site and every
        ``PYTHON*`` variable out of module lookup until the runner is loaded; ``-B`` with an
        empty *pycache_prefix* means no bytecode next to a source file is ever read or written
        (pytest's assertion-rewrite cache honours the prefix too). With *result_path* the
        engine's outcome recorder writes the structured per-test result there. The recorder
        runs inside the test process, so code under test can influence that result. The
        runner cache is off: a planted one must not select tests, and none is written into
        the target.
        """
        config = str(self.config_path) if self.config_path is not None else os.devnull
        recorder = [str(result_path)] if result_path is not None else []
        bootstrap = _RECORDING_BOOTSTRAP if result_path is not None else _BOOTSTRAP
        return [
            sys.executable, "-I", "-B", "-X", f"pycache_prefix={pycache_prefix}",
            "-c", bootstrap, str(self.target), *recorder, *options,
            "-p", "no:cacheprovider", "-c", config,
            "--rootdir", str(self.target), "--confcutdir", str(self.target),
            *(str(path) for path in (self.test_files or self.roots)),
        ]


@contextlib.contextmanager
def isolated_pycache(target: Path) -> Iterator[Path]:
    """Yield a fresh empty bytecode cache directory outside *target*; removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="wilfred-pycache-") as temporary:
        prefix = Path(temporary).resolve()
        if prefix.is_relative_to(target.resolve()):
            raise ValueError(f"bytecode cache prefix must be outside the target: {prefix}")
        yield prefix


def pytest_environment() -> dict[str, str]:
    """Return a deterministic environment or reject caller-controlled injection.

    Every inherited ``PYTHON*`` variable is dropped (the isolated interpreter ignores them,
    and grandchild processes must not inherit a module search path either).
    """
    forbidden = [name for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
                 if os.environ.get(name, "").strip()]
    if forbidden:
        raise ValueError(f"non-empty {' and '.join(forbidden)} is not allowed")
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("PYTHON")
           and name not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS"}}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# Supported pytest configuration files in precedence order, each with the section that makes it
# active. This table is the only place the per-format section name is decided.
_CONFIG_SECTIONS = {
    "pytest.ini": "pytest",
    "pyproject.toml": "tool.pytest.ini_options",
    "tox.ini": "pytest",
    "setup.cfg": "tool:pytest",
}


@dataclass(frozen=True)
class _PytestConfig:
    """The active pytest configuration file, read and parsed exactly once.

    ``testpaths`` and ``addopts`` hold the raw values as read; their consumers validate them.
    """

    path: Path
    section: str
    testpaths: Any
    addopts: Any


def _load_pytest_config(path: Path) -> _PytestConfig | None:
    """Parse one configuration file; ``None`` when it declares no pytest section.

    ``pytest.ini`` is active whatever it holds, so it always yields a record.
    """
    section = _CONFIG_SECTIONS[path.name]
    try:
        text = path.read_text(encoding="utf-8")
        if path.name == "pyproject.toml":
            options = tomllib.loads(text).get("tool", {}).get("pytest", {}).get("ini_options")
            if not isinstance(options, dict):
                return None
            return _PytestConfig(path, section, options.get("testpaths", []),
                                 options.get("addopts", ""))
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
    except (OSError, TypeError, configparser.Error, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid pytest configuration at {path}") from exc
    if not parser.has_section(section):
        return _PytestConfig(path, section, [], "") if path.name == "pytest.ini" else None
    return _PytestConfig(path, section, parser.get(section, "testpaths", fallback="").split(),
                         parser.get(section, "addopts", fallback=""))


def _active_pytest_config(target: Path) -> _PytestConfig | None:
    """Return the one unambiguous target-local pytest configuration, or ``None``."""
    active: list[Path] = []
    loaded: dict[Path, _PytestConfig] = {}
    for name in _CONFIG_SECTIONS:
        candidate = target / name
        if candidate.is_symlink():
            raise ValueError(f"pytest configuration must not be a symlink: {candidate}")
        if not candidate.is_file():
            continue
        if name != "pytest.ini":  # pytest.ini is active unparsed; it is loaded once below
            config = _load_pytest_config(candidate)
            if config is None:
                continue
            loaded[candidate] = config
        active.append(candidate)
    if len(active) > 1:
        raise ValueError(f"ambiguous pytest configuration in {target}")
    if not active:
        return None
    return loaded.get(active[0]) or _load_pytest_config(active[0])


def maintained_test_scope(target_path: Path) -> tuple[tuple[Path, ...], Path | None]:
    """Resolve one unambiguous target-local pytest config and its contained roots."""
    target = target_path.resolve()
    config = _active_pytest_config(target)
    return _config_roots(target, config), (config.path if config is not None else None)


def _config_roots(target: Path, config: _PytestConfig | None) -> tuple[Path, ...]:
    if config is None:
        return (target,)
    configured, config_path = config.testpaths, config.path
    if not isinstance(configured, list) or any(not isinstance(value, str) for value in configured):
        raise ValueError(f"invalid pytest testpaths in {config_path}")
    if not configured:
        return (target,)
    unresolved = tuple(config_path.parent / value for value in configured)
    if any(_has_symlink(path, target) for path in unresolved):
        raise ValueError(f"pytest testpaths must not be symlinks in {config_path}")
    roots = tuple(path.resolve() for path in unresolved)
    if len(set(roots)) != len(roots) or any(
            not root.is_relative_to(target) or not root.is_dir() for root in roots):
        raise ValueError(f"pytest testpaths escape the target or are missing in {config_path}")
    return roots


def maintained_test_roots(target_path: Path) -> tuple[Path, ...]:
    """Compatibility wrapper returning roots from the single resolved pytest scope."""
    return maintained_test_scope(target_path)[0]


def _pytest_plugins(path: Path) -> tuple[str, ...]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"invalid pytest control file at {path}") from exc
    declared: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(item, ast.Name) and item.id == "pytest_plugins" for item in targets):
            continue
        if node.value is None:
            raise ValueError(f"invalid pytest_plugins declaration in {path}")
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"dynamic pytest_plugins declaration in {path}") from exc
        values = (value,) if isinstance(value, str) else value
        if not isinstance(values, (tuple, list)) or not all(
                isinstance(item, str) and item for item in values):
            raise ValueError(f"invalid pytest_plugins declaration in {path}")
        declared.extend(values)
    return tuple(declared)


def _local_plugin(target: Path, module: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module):
        raise ValueError(f"unsafe local pytest plugin name: {module!r}")
    relative = Path(*module.split("."))
    choices = (target / relative.with_suffix(".py"), target / relative / "__init__.py")
    existing = [path for path in choices if path.exists() or path.is_symlink()]
    if len(existing) > 1:
        raise ValueError(f"ambiguous local pytest plugin: {module}")
    if not existing:
        raise ValueError(f"unwitnessed external pytest plugin: {module}")
    return existing[0]


def _configured_plugins(config: _PytestConfig | None) -> tuple[str, ...]:
    if config is None:
        return ()
    value = config.addopts
    addopts = value if isinstance(value, str) else " ".join(value)
    tokens = shlex.split(addopts)
    plugins = [tokens[index + 1] for index, token in enumerate(tokens[:-1]) if token == "-p"]
    plugins += [token[2:] for token in tokens if token.startswith("-p") and token != "-p"]
    return tuple(value for value in plugins if not value.startswith("no:"))


def _has_symlink(path: Path, target: Path) -> bool:
    return any(part.is_symlink() for part in (path, *path.parents)
               if part.is_relative_to(target))


_TEST_DIRECTORY_NAMES = frozenset({"test", "tests", "testing"})


def _holds_tests(directory: Path) -> bool:
    """True when *directory* itself holds a test module or a ``conftest.py``."""
    return any(path.name == "conftest.py" or path.match("test_*.py") or path.match("*_test.py")
               for path in directory.glob("*.py"))


def _control_files(
    target: Path, roots: tuple[Path, ...], plugins: tuple[str, ...],
) -> tuple[Path, ...]:
    """Every file pytest can run or import as test code, whatever its directory is called.

    A configured root below the target is test code as a whole; for the target itself only
    directories under it that hold a test module or ``conftest.py`` are (with every module
    below them), so product modules at the target root stay product code.
    """
    candidates: set[Path] = {target / "conftest.py"}
    for root in roots:
        for pattern in ("test_*.py", "*_test.py"):
            candidates.update(root.rglob(pattern))
        parent = root
        while parent.is_relative_to(target):
            candidates.add(parent / "conftest.py")
            if parent == target:
                break
            parent = parent.parent
        candidates.update(root.rglob("conftest.py"))
        directories = (path for path in root.rglob("*") if path.is_dir())
        for directory in directories:
            if directory.is_symlink():
                raise ValueError(f"pytest scope must not contain symlink directories: {directory}")
            if directory.name.lower() in _TEST_DIRECTORY_NAMES or _holds_tests(directory):
                candidates.update(directory.rglob("*.py"))
        if root != target or root.name.lower() in _TEST_DIRECTORY_NAMES:
            candidates.update(root.rglob("*.py"))
    controls = {path for path in candidates if path.exists() or path.is_symlink()}
    for plugin in plugins:
        local = _local_plugin(target, plugin)
        if local is not None:
            controls.add(local)
    pending = list(controls)
    while pending:
        path = pending.pop()
        if (_has_symlink(path, target) or not path.is_file()
                or not path.resolve().is_relative_to(target)):
            raise ValueError(f"pytest control file escapes the target or is a symlink: {path}")
        for plugin in _pytest_plugins(path):
            local = _local_plugin(target, plugin)
            if local is not None and local not in controls:
                controls.add(local)
                pending.append(local)
    return tuple(sorted(path.resolve() for path in controls))


def resolve_pytest_scope(target_path: Path) -> PytestScope:
    target = target_path.resolve()
    config = _active_pytest_config(target)
    roots = _config_roots(target, config)
    controls = _control_files(target, roots, _configured_plugins(config))
    return PytestScope(target, roots, config.path if config is not None else None, controls)


class PytestEvaluator(BaseEvaluator):
    """Run pytest against ``target_path`` and capture pass/fail.

    Pytest exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html):

    * ``0`` → all tests passed
    * ``1`` → some tests failed
    * ``2`` → test execution interrupted by user
    * ``3`` → internal pytest error
    * ``4`` → pytest CLI usage error
    * ``5`` → no tests collected

    We treat ``0`` as ``passed=True`` and everything else as ``passed=False``.
    No-tests-collected (``5``) is reported as a WARNING rather than ERROR
    since "no tests" may be intentional for the path under check.
    """

    kind: ClassVar[CheckKind] = CheckKind.TEST
    name: ClassVar[str] = "pytest"
    binary: ClassVar[str] = "pytest"
    max_lines_in_details: ClassVar[int] = 30
    default_timeout_s: ClassVar[float] = 600.0

    def run(
        self,
        target_path: Path,
        *,
        config: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Run pytest on *target_path* and return a :class:`CheckResult`."""
        timeout_s = self._resolve_timeout(config)
        try:
            with isolated_pycache(target_path) as pycache_prefix:
                argv = self._build_argv(target_path, config, pycache_prefix)
                outcome = run_subprocess(
                    argv, cwd=target_path.resolve(), timeout_s=timeout_s,
                    env=pytest_environment(),
                )
        except Exception as exc:  # noqa: BLE001
            return self._unexpected_error_result(exc)

        if outcome.tool_missing:
            return self._tool_missing_result(outcome)
        if outcome.timed_out:
            return self._timeout_result(outcome)

        rc = outcome.returncode
        details_body = _tail(outcome.stdout, self.max_lines_in_details)

        if rc == 0:
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=True,
                severity=Severity.INFO,
                details=f"pytest: passed\n{details_body}".strip(),
                duration_ms=outcome.duration_ms,
            )
        if rc == 5:
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=False,
                severity=Severity.WARNING,
                details="pytest: no tests collected",
                duration_ms=outcome.duration_ms,
            )

        severity = Severity.ERROR if rc == 1 else Severity.CRITICAL
        prefix = f"pytest (rc={rc})"
        body = details_body or (outcome.stderr or "").strip() or "test run failed"
        return CheckResult(
            kind=self.kind,
            name=self.name,
            passed=False,
            severity=severity,
            details=f"{prefix}:\n{body}",
            duration_ms=outcome.duration_ms,
            findings=_failing_findings(outcome.stdout) if rc == 1 else [],
        )

    def _build_argv(self, target_path: Path, config: dict[str, Any] | None,
                    pycache_prefix: Path) -> list[str]:
        # Run the pytest of the current interpreter's environment (works under uv-managed
        # venvs) through the same isolated interpreter as the regression witness.
        options = [
            "--tb=short",
            "-q",
            "-rfE",
            "--no-header",
        ]
        if config and config.get("markers"):
            options.extend(["-m", str(config["markers"])])
        return resolve_pytest_scope(target_path).argv(*options, pycache_prefix=pycache_prefix)


def _failing_findings(stdout: str) -> list[Finding]:
    """One finding per failing/erroring node id, parsed from the ``-rfE`` short summary.

    Fail-closed: if the parsed ids do not reconcile with pytest's own ``N failed, M errors``
    count, a single ``pytest-unparsed`` finding is returned instead, so no downstream consumer
    (the known-failures tolerance) can act on a partial list.
    """
    findings: list[Finding] = []
    seen: set[str] = set()
    for raw in (stdout or "").splitlines():
        m = _SUMMARY_LINE.match(raw.strip())
        if m and m.group(2) not in seen:
            seen.add(m.group(2))
            kind = "failed" if m.group(1) == "FAILED" else "error"
            findings.append(
                Finding(
                    file=m.group(2),
                    message=f"test {kind}",
                    severity=Severity.ERROR,
                    rule_id=f"pytest-{kind}",
                )
            )
    expected: int | None = None
    for raw in reversed((stdout or "").splitlines()):
        counts = _COUNT.findall(raw)
        if counts:
            expected = sum(int(n) for n, _ in counts)
            break
    if expected is None or expected != len(findings):
        return [
            Finding(
                file="<pytest>",
                message=f"could not reconcile failing ids ({len(findings)} parsed, "
                f"{expected} reported)",
                severity=Severity.ERROR,
                rule_id="pytest-unparsed",
            )
        ]
    return findings


def _tail(text: str, limit: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text.strip()
    return "\n".join(lines[-limit:])


__all__ = [
    "PytestEvaluator", "PytestScope", "isolated_pycache", "maintained_test_roots",
    "maintained_test_scope", "pytest_environment", "resolve_pytest_scope",
]
