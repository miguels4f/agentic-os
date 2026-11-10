"""Regression baseline + comparison for the IMPROVE regression guard.

The plain deterministic gate catches a pre-existing test that now *fails*. But a change can
neutralise the very test that protects it — delete it, skip/xfail it, rename it out of
discovery, gut its assertion, or skip it while adding trivial passing tests to keep the count
up — and the suite goes "green" again. A "prove existing tests still pass" gate must catch all
of those, not just an outright failure.

This module closes the gap with TWO per-test signals snapshotted at comprehend (suite green):

* **passing node-ids.** The gate FAILS unless every test that PASSED at baseline still PASSES
  now — a *per-id subset* check, not an aggregate count (so deleting/skipping/xfailing/renaming
  a baseline test is caught, and it cannot be masked by adding new trivial passing tests).
* **test-file integrity.** The gate FAILS if any pre-existing test file was removed or modified
  — a change cannot gut or weaken a baseline test's assertion while keeping its node-id green,
  because editing the file that holds the regression witness is itself the red flag.

Capture binds the snapshot to the run's base commit: the recorded test, control, data and
configuration hashes must equal the commit's, and the whole repository must have equalled the
base commit when the tests ran, so no uncommitted edit decides which tests are collected or
pass. A test that did not pass at capture (skipped, expected to fail, failing, or in a module
that was not collected) is recorded, and refused unless a human declared it in the
known-failures file as committed at the base commit (``require_declared_not_passing``): its
skip can depend on what a commit does not store. The comparison covers file bytes, the
executable bit and link destinations; a base commit with a submodule entry is refused.
Timestamps and other permissions (which a commit does not store), files outside the
repository and the exempt top-level directories are not part of it. It describes the tree
as read before the tests start: a process of the same user that is already running can
change files after that read, as it could change the run's own state (see the limits below).

Pure stdlib + the same ``pytest`` subprocess discipline as the evaluators (no library coupling).
Scoped to the python stack (pytest); the node stack's delete/skip hardening is future work and
is documented as such — the node test gate still blocks an outright failure.

Limits. The passing ids are read from a result file written by the engine's outcome recorder,
which runs inside the pytest process together with the tests and the product modules they
import. Code under test can therefore influence the recorded result (alter the recorder, write
the file, replace runner functions, or behave correctly only while tested). The protected run
catches accidental regressions and edits to tests, controls and test data; it is not a boundary
against deliberately hostile product code. That case is left to the review of the changed
product files (see ``docs/remediation/control-evolution.md``).
"""

from __future__ import annotations

import ast
import configparser
import contextlib
import functools
import hashlib
import importlib.machinery
import importlib.metadata
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Iterator
from itertools import product
from pathlib import Path
from typing import Any

from hyperwilfred_core import approvals, known_failures, path_safety
from hyperwilfred_core.evaluators.base import run_subprocess
from hyperwilfred_core.evaluators.code.pytest_evaluator import (
    _TEST_DIRECTORY_NAMES,
    PytestScope,
    _active_pytest_config,
    _failing_findings,
    _holds_tests,
    isolated_pycache,
    maintained_test_roots,
    pytest_environment,
    resolve_pytest_scope,
)
from hyperwilfred_core.evaluators.verification import CheckKind, CheckResult, Severity
from hyperwilfred_core.path_safety import validate_relative_path
from hyperwilfred_core.run_store import atomic_write_text

BASELINE_FILE = "regression_baseline.json"
BASELINE_VERSION = "wilfred-regression-baseline/v2"
_PYTEST_TIMEOUT_S = 600.0
_PHASES = ["setup", "call", "teardown"]
_OUTCOMES = frozenset({"passed", "failed", "skipped"})
_TEST_GLOBS = ("test_*.py", "*_test.py")
_MAX_SHOWN = 5
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BYTECODE_EXEMPT = frozenset({".hw", ".git", ".venv"})
# Entries of a test directory that are never test inputs: the runner's own cache at the
# target root and the desktop metadata file. Bytecode is refused by its own rule.
_DATA_EXEMPT_AT_TARGET = _BYTECODE_EXEMPT | {".pytest_cache"}
_DATA_IGNORED_NAMES = frozenset({".DS_Store"})
# Distributions whose modules the runner imports, some lazily after the target joins the
# module search path; their top-level names must never resolve to a target file.
_RUNNER_DISTRIBUTIONS = frozenset({"pytest", "pluggy", "iniconfig", "packaging", "pygments"})
BaselineScope = tuple[tuple[Path, ...], set[str], dict[str, str]]
ControlAuthority = Callable[[], dict[str, tuple[str, str]]]


class RegressionError(RuntimeError):
    """The regression witness could not be established safely."""


@contextlib.contextmanager
def _private_result_file(target: Path) -> Iterator[Path]:
    """Yield a result path in a fresh owner-only directory outside *target*; removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="wilfred-pytest-result-") as temporary:
        directory = Path(temporary).resolve()
        if (directory.is_relative_to(target.resolve())
                or stat.S_IMODE(directory.stat().st_mode) != 0o700):
            raise RegressionError(f"pytest result directory is not private and external: "
                                  f"{directory}")
        yield directory / "outcomes.json"


def _checked_reports(collected: object, reports: object) -> dict[str, list[list[Any]]] | None:
    """The recorder's per-node phase reports when every field has its expected shape."""
    if collected is not None and not (
            isinstance(collected, list) and all(isinstance(node, str) for node in collected)):
        return None
    if not isinstance(reports, dict) or not all(
            isinstance(entries, list) and all(
                isinstance(entry, list) and len(entry) == 3 and entry[0] in _PHASES
                and entry[1] in _OUTCOMES and isinstance(entry[2], bool) for entry in entries)
            for entries in reports.values()):
        return None
    return reports


def _node_passed(entries: list[list[Any]]) -> bool:
    """One setup, then one or more calls (the test's own and one per ``subtests`` report), then
    one teardown; every report passed and none is an expected failure."""
    phases = [entry[0] for entry in entries]
    return (len(entries) >= 3 and phases[0] == "setup" and phases[-1] == "teardown"
            and all(phase == "call" for phase in phases[1:-1])
            and all(entry[1:] == ["passed", False] for entry in entries))


class _Recorded(set[str]):
    """Passing node ids of one run, with the collected node ids that did not pass."""

    not_passing: frozenset[str] = frozenset()


def _recorded_passing(result: Path, returncode: int) -> set[str] | None:
    """Passing node ids from the recorder's structured result; ``None`` (fail closed) when it
    is missing, unreadable, malformed or disagrees with pytest's exit code.

    A test passes only when its setup, every call report (including each subtest's) and its
    teardown passed, in that order, and none of them is an expected failure.

    The checks here reject an inconsistent file, not a forged one: the file is written from
    inside the test process, where code under test also runs (see the module limits).
    """
    try:
        data = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or type(data.get("exitstatus")) is not int:
        return None
    collected = data.get("collected")
    reports = _checked_reports(collected, data.get("reports"))
    if data["exitstatus"] != returncode or reports is None:
        return None
    known = set(collected) if isinstance(collected, list) else set()
    failed = any(outcome == "failed" and not xfail
                 for entries in reports.values() for _when, outcome, xfail in entries)
    finished = {node for node, entries in reports.items()
                if entries and entries[-1][0] == "teardown"}
    if (not set(reports) <= known or (returncode == 1 and not failed)
            or (returncode == 0 and (failed or not known or finished != known))):
        return None
    passing = _Recorded(node for node, entries in reports.items() if _node_passed(entries))
    passing.not_passing = frozenset(known - passing)
    return passing


def _pytest_passing(target: Path, roots: tuple[Path, ...] | None = None,
                    config_path: Path | None = None, *, scope: PytestScope | None = None,
                    ) -> tuple[set[str] | None, int | None, list]:
    """Return passing ids, exit code, and reconciled failure findings from one pytest run.

    Passing ids come only from the engine's outcome recorder, never from printed output.
    The recorder runs in the same process as the code under test, which can influence what
    it records (see the module limits).
    """
    if scope is None:
        resolved = resolve_pytest_scope(target)
        scope = resolved if roots is None else PytestScope(
            resolved.target, roots, config_path, resolved.control_files
        )
    with (isolated_pycache(scope.target) as pycache_prefix,
          _private_result_file(scope.target) as result):
        out = run_subprocess(
            scope.argv("-rfE", "-q", "--no-header", pycache_prefix=pycache_prefix,
                       result_path=result),
            cwd=scope.target,
            timeout_s=_PYTEST_TIMEOUT_S,
            env=pytest_environment(),
        )
        if not out.ran:
            return None, None, []
        ids = _recorded_passing(result, out.returncode)
    findings = _failing_findings(out.stdout) if out.returncode == 1 else []
    return ids, out.returncode, findings


def _passing_ids(target: Path) -> set[str] | None:
    """Passing node ids for comparison; failures are represented by missing baseline ids."""
    return _pytest_passing(target)[0]


def _raise_walk_error(exc: OSError) -> None:
    raise exc


def _planted_files(target: Path, planted: Callable[[str], bool]) -> list[str]:
    """Relative paths of every entry under *target* whose name is *planted* (links reported,
    never followed). Only the top-level ``.hw``, ``.git`` and ``.venv`` directories are exempt.
    """
    found: list[str] = []
    for directory, subdirs, files in os.walk(target, onerror=_raise_walk_error):
        here = Path(directory)
        if here == target:
            subdirs[:] = [name for name in subdirs if name not in _BYTECODE_EXEMPT]
        for name in (*subdirs, *files):
            if planted(name):
                found.append((here / name).relative_to(target).as_posix())
    return sorted(found)


def _planted_bytecode(target: Path) -> list[str]:
    """Relative paths of every bytecode cache under *target*."""
    return _planted_files(
        target, lambda name: name == "__pycache__" or name.endswith((".pyc", ".pyo")))


def _planted_extensions(target: Path) -> list[str]:
    """Relative paths of every compiled extension module under *target*: the import system
    prefers one over the witnessed source module of the same name in the same directory."""
    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    return _planted_files(target, lambda name: name.endswith(suffixes))


def _test_file_hashes(target: Path, roots: tuple[Path, ...] | None = None, *,
                      scope: PytestScope | None = None) -> dict[str, str]:
    """``{relative_path: sha256}`` for every test file under *target* (test_*.py / *_test.py)."""
    target = Path(target).resolve()
    hashes: dict[str, str] = {}
    selected = scope.roots if scope is not None else (roots or maintained_test_roots(target))
    for root, pattern in product(selected, _TEST_GLOBS):
        for f in sorted(root.rglob(pattern)):
            if f.is_file():
                if f.is_symlink() or not f.resolve().is_relative_to(target):
                    raise RegressionError(f"test file escapes the product or is a symlink: {f}")
                rel = str(f.relative_to(target))
                hashes[rel] = hashlib.sha256(_witness_bytes(f, target)).hexdigest()
    return hashes


_WITNESS_REFUSALS = {
    "symlink": "must not be a symlink",
    "not-regular": "is not a regular file",
    "replaced": "changed while being read",
    "changed": "changed while being read",
}


def _witness_bytes(path: Path, target: Path) -> bytes:
    """Read an executable witness through the shared stable reader, refusing any link at or
    below *target* and any identity change; an unreadable witness stays an ``OSError``."""
    try:
        payload = path_safety.stable_regular_bytes(
            path, root=Path(os.path.abspath(target)).parent, label="pytest witness",
        )
    except path_safety.UnstableFileError as exc:
        wording = _WITNESS_REFUSALS.get(exc.reason)
        if wording is None:
            raise OSError(f"pytest witness cannot be read ({exc.reason}): {path}") from exc
        raise RegressionError(f"pytest witness {wording}: {path}") from exc
    assert payload is not None
    return payload


def _control_file_hashes(scope: PytestScope) -> dict[str, str]:
    """Hash every executable local pytest control/helper selected in the immutable scope."""
    hashes: dict[str, str] = {}
    for path in scope.control_files:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(
                scope.target):
            raise RegressionError(f"pytest control file escapes the product: {path}")
        hashes[path.relative_to(scope.target).as_posix()] = hashlib.sha256(
            _witness_bytes(path, scope.target)
        ).hexdigest()
    return hashes


def _test_directories(scope: PytestScope) -> list[Path]:
    """Directories that are test code as a whole, by the rule that selects control modules."""
    found: set[Path] = set()
    for root in scope.roots:
        if root != scope.target or root.name.lower() in _TEST_DIRECTORY_NAMES:
            found.add(root)
            continue
        for directory, subdirs, _files in os.walk(root, onerror=_raise_walk_error):
            here = Path(directory)
            if here == scope.target:
                subdirs[:] = [name for name in subdirs if name not in _DATA_EXEMPT_AT_TARGET]
            if here != root and (here.name.lower() in _TEST_DIRECTORY_NAMES
                                 or _holds_tests(here)):
                found.add(here)
                subdirs[:] = []
    return sorted(found)


def _data_file_hashes(scope: PytestScope) -> dict[str, str]:
    """Hash every file of the test directories that is not a Python module: the inputs
    (golden files, fixtures, snapshots) the witnessed tests read. Links and special files
    are refused."""
    hashes: dict[str, str] = {}
    # Modules are control files; bytecode and compiled extensions have their own refusals.
    skipped = (".py", ".pyc", ".pyo", *importlib.machinery.EXTENSION_SUFFIXES)
    for top in _test_directories(scope):
        for directory, subdirs, files in os.walk(top, onerror=_raise_walk_error):
            here = Path(directory)
            exempt = _DATA_EXEMPT_AT_TARGET if here == scope.target else frozenset()
            subdirs[:] = [name for name in subdirs
                          if name not in exempt and name != "__pycache__"]
            for name in subdirs:
                if (here / name).is_symlink():
                    raise RegressionError(f"test data directory is a symlink: {here / name}")
            for name in files:
                if name.endswith(skipped) or name in _DATA_IGNORED_NAMES:
                    continue
                relative = (here / name).relative_to(scope.target).as_posix()
                hashes[relative] = hashlib.sha256(
                    _witness_bytes(here / name, scope.target)).hexdigest()
    return hashes


def _outside_tree(parts: tuple[str, ...], target: tuple[str, ...]) -> bool:
    """Whether a path (as parts below the compared root) is left out of the tree record: the
    desktop metadata file, and the directories exempt for test data at the top level of the
    root and of the target (given as its parts below the root)."""
    return (parts[-1] in _DATA_IGNORED_NAMES or parts[0] in _DATA_EXEMPT_AT_TARGET
            or (len(parts) > len(target) and parts[:len(target)] == target
                and parts[len(target)] in _DATA_EXEMPT_AT_TARGET))


def _tree(root: Path, target: Path) -> dict[str, str]:
    """``{relative path: record}`` of every entry under *root*: the SHA-256 of each regular
    file (marked when its owner may execute it, the one permission a commit stores), the
    destination of each link, and the kind of every directory and special or unreadable
    file. Nothing is followed. See ``_outside_tree`` for what is left out."""
    inner = target.relative_to(root).parts
    entries: dict[str, str] = {}

    def unreadable(error: OSError) -> None:
        if error.filename is not None:
            entries[Path(error.filename).relative_to(root).as_posix()] = "unreadable"

    for directory, subdirs, files in os.walk(root, onerror=unreadable):
        here = Path(directory)
        below = here.relative_to(root).parts
        subdirs[:] = [name for name in subdirs if not _outside_tree((*below, name), inner)]
        for name in (*subdirs, *files):
            parts = (*below, name)
            if _outside_tree(parts, inner):
                continue
            path, relative = here / name, "/".join(parts)
            try:
                if path.is_symlink():
                    entries[relative] = f"link -> {os.readlink(path)}"
                elif name in subdirs:
                    entries[relative] = "directory"
                else:
                    entries[relative] = _file_record(
                        _witness_bytes(path, root), bool(path.lstat().st_mode & stat.S_IXUSR))
            except RegressionError:
                entries[relative] = "special or changing"
            except OSError:
                entries[relative] = "unreadable"
    return entries


def _file_record(payload: bytes, executable: bool) -> str:
    return hashlib.sha256(payload).hexdigest() + (" executable" if executable else "")


def _base_tree(repo: Path, commit: str, target: Path) -> dict[str, str]:
    """The record ``_tree`` gives for a checkout of *commit*, from hash-verified objects.

    A commit with a submodule entry is refused: the entry names another repository's commit
    and holds no bytes to compare, so neither a checkout of it nor its absence can be judged."""
    inner = target.relative_to(repo).parts
    links: dict[str, str] = {}
    executable: set[str] = set()
    submodules: list[str] = []
    files = _base_files(repo, commit, "", links=links, executable=executable,
                        submodules=submodules)
    if submodules:
        raise RegressionError(
            "regression baseline capture does not support a base_commit with submodule "
            f"entries, whose contents cannot be compared: {_fmt(sorted(submodules))}"
        )
    entries: dict[str, str] = {}
    records = {path: _file_record(payload, path in executable)
               for path, payload in files.items()}
    records.update({path: f"link -> {destination}" for path, destination in links.items()})
    for path, record in records.items():
        parts = tuple(path.split("/"))
        if _outside_tree(parts, inner):
            continue
        entries[path] = record
        for depth in range(1, len(parts)):
            entries["/".join(parts[:depth])] = "directory"
    return entries


def _tree_root(target: Path) -> Path:
    """The directory whose tree a capture records: the work tree git discovers from *target*,
    or *target* itself outside git. The comparison checks it against the repository of the
    base commit, so a wrong answer here is refused there."""
    try:
        found = _run_git(target, "rev-parse", "--show-toplevel")
    except RegressionError:
        return target
    if found.returncode or not found.stdout.strip():
        return target
    root = Path(os.fsdecode(found.stdout.strip())).resolve()
    return root if target.is_relative_to(root) else target


def _tree_sha256(entries: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()


def _expected_data(target: Path, baseline: dict, roots: tuple[Path, ...],
                   base_commit: str | None) -> dict[str, str]:
    """Expected data hashes: pinned in the baseline, else rebuilt from the base commit."""
    if "data_file_hashes" in baseline:
        return dict(baseline["data_file_hashes"])
    with _base_scope(target, roots, base_commit) as base_scope:
        return _data_file_hashes(base_scope)


def _data_file_reasons(expected: dict[str, str], current: dict[str, str]) -> list[str]:
    """Any removed, modified or added test data file fails the gate."""
    reasons = []
    for label, found in (
        ("removed", sorted(set(expected) - set(current))),
        ("modified (regression-witness integrity)",
         sorted(path for path in set(expected) & set(current)
                if expected[path] != current[path])),
        ("added", sorted(set(current) - set(expected))),
    ):
        if found:
            reasons.append(f"{len(found)} test data file(s) {label}: {_fmt(found)}")
    return reasons


def _config_file_sha256(config_path: Path | None) -> str | None:
    if config_path is None:
        return None
    try:
        payload = _witness_bytes(config_path, config_path.parent)
    except OSError as exc:
        raise RegressionError(f"cannot read pytest configuration: {config_path}") from exc
    return hashlib.sha256(config_path.name.encode() + b"\0" + payload).hexdigest()


def _run_git(repo: Path, *arguments: str,
             stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run git on *repo* without replacement refs, inherited ``GIT_*`` variables (so no
    inherited ceiling can cut discovery short) or the user and system configuration."""
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_") and name != "LANGUAGE"}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_CONFIG_NOSYSTEM="1", GIT_NO_LAZY_FETCH="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
               LC_ALL="C")
    try:
        return subprocess.run(
            ["git", "--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(repo),
             *arguments], input=stdin, capture_output=True, check=False, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RegressionError("cannot read base_commit evidence") from exc


def _git(repo: Path, *arguments: str, stdin: bytes | None = None) -> bytes:
    """Hardened git on *repo*; any failure is a refusal."""
    result = _run_git(repo, *arguments, stdin=stdin)
    if result.returncode:
        raise RegressionError("cannot read base_commit evidence")
    return result.stdout


def _git_objects(repo: Path, ids: list[str]) -> dict[str, tuple[bytes, bytes]]:
    """Read *ids* in one batch; every object's bytes must hash to its own id, so neither the
    object store nor a replacement mechanism can substitute different content."""
    out = _git(repo, "cat-file", "--batch", stdin="".join(f"{i}\n" for i in ids).encode())
    found: dict[str, tuple[bytes, bytes]] = {}
    offset = 0
    for object_id in ids:
        end = out.find(b"\n", offset)
        header = out[offset:max(end, offset)].split()
        if len(header) != 3 or header[0] != object_id.encode() or not header[2].isdigit():
            raise RegressionError("base_commit object is unavailable")
        kind, size = header[1], int(header[2])
        body = out[end + 1:end + 1 + size]
        digest = hashlib.new("sha1" if len(object_id) == 40 else "sha256",
                             kind + b" %d\0" % size + body).hexdigest()
        if len(body) != size or digest != object_id:
            raise RegressionError("base_commit object bytes do not match their object id")
        found[object_id] = (kind, body)
        offset = end + 1 + size + 1
    return found


def _tree_entries(body: bytes, width: int) -> Iterator[tuple[bytes, str, str]]:
    offset = 0
    while offset < len(body):
        space, null = body.index(b" ", offset), body.index(b"\0", offset)
        yield (body[offset:space], os.fsdecode(body[space + 1:null]),
               body[null + 1:null + 1 + width].hex())
        offset = null + 1 + width


def _base_files(repo: Path, commit: str, prefix: str, *,
                links: dict[str, str] | None = None, executable: set[str] | None = None,
                submodules: list[str] | None = None) -> dict[str, bytes]:
    """``{repository path: bytes}`` of every regular file under *prefix* at *commit*.

    A link under *prefix* is refused, unless *links* is given: it then receives
    ``{repository path: destination}``. *executable* receives the paths of the files the
    commit marks executable, *submodules* the paths of its submodule entries."""
    kind, body = _git_objects(repo, [commit])[commit]
    if kind != b"commit" or not body.startswith(b"tree "):
        raise RegressionError("base_commit provenance cannot be proved")
    pending, blobs = {"": body[5:5 + len(commit)].decode()}, {}
    linked: dict[str, str] = {}
    while pending:
        objects = _git_objects(repo, sorted(set(pending.values())))
        level, pending = pending, {}
        for directory, tree_id in level.items():
            if objects[tree_id][0] != b"tree":
                raise RegressionError("base_commit provenance cannot be proved")
            for mode, name, object_id in _tree_entries(objects[tree_id][1], len(commit) // 2):
                path = f"{directory}/{name}" if directory else name
                if name.lower() in {"", ".", "..", ".git"} or "/" in name or "\\" in name:
                    raise RegressionError("base_commit path escapes its snapshot")
                inside = not prefix or path == prefix or path.startswith(f"{prefix}/")
                if mode == b"40000" and (inside or prefix.startswith(f"{path}/")):
                    pending[path] = object_id
                elif inside and mode == b"120000":
                    if links is None:
                        raise RegressionError("base_commit pytest scope contains a symlink")
                    linked[path] = object_id
                elif inside and mode.startswith(b"100"):
                    blobs[path] = object_id
                    if executable is not None and mode == b"100755":
                        executable.add(path)
                elif inside and mode == b"160000" and submodules is not None:
                    submodules.append(path)
    objects = _git_objects(repo, sorted({*blobs.values(), *linked.values()}))
    if any(objects[object_id][0] != b"blob" for object_id in (*blobs.values(), *linked.values())):
        raise RegressionError("base_commit provenance cannot be proved")
    if links is not None:
        links.update({path: os.fsdecode(objects[object_id][1])
                      for path, object_id in linked.items()})
    return {path: objects[object_id][1] for path, object_id in blobs.items()}


def baseline_sha256(path: Path) -> str:
    """Digest the exact persisted witness bytes (the digest lives in RunState)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _portable_roots(target: Path, roots: tuple[Path, ...]) -> list[str]:
    target = target.resolve()
    values = [root.resolve().relative_to(target).as_posix() or "." for root in roots]
    return [value if value != "." else "." for value in values]


def _discovered_repository(directory: Path) -> bytes | None:
    """The work tree and git directory that git itself discovers from *directory* (two
    lines), or None where it finds no repository. Any other failure is a refusal."""
    result = _run_git(directory, "rev-parse", "--show-toplevel", "--absolute-git-dir")
    if not result.returncode and result.stdout.count(b"\n") == 2 and result.stdout[-1:] == b"\n":
        return result.stdout
    if (result.returncode == 128 and not result.stdout
            and result.stderr.lower().startswith(b"fatal: not a git repository (or any ")):
        return None
    raise RegressionError("cannot read base_commit evidence")


def _refuse_second_holder(target: Path, commit: str) -> Path | None:
    """Fail closed when more than one repository at or above *target* holds *commit*; return
    the highest directory from which the repository of *target* itself is discovered.

    Discovery stops at the nearest repository, so one planted at or above a target that an
    outer repository owns would choose which part of the base tree is read as the target.
    No entry name is searched for: git is asked from the target and from every directory
    above it, so a plant made of loose metadata files is found exactly as git finds it. An
    unrelated outer repository does not hold the commit and changes nothing."""
    holders: dict[bytes, bool] = {}
    nearest: bytes | None = None
    seat: Path | None = None
    for directory in (target, *target.parents):
        identity = _discovered_repository(directory)
        if directory == target:
            nearest = identity
        if identity is None:
            continue
        if identity == nearest:
            seat = directory
        if identity not in holders:
            answer = _git(directory, "cat-file", "--batch-check", stdin=f"{commit}\n".encode())
            holders[identity] = answer.split() != [commit.encode(), b"missing"]
    if sum(holders.values()) > 1:
        raise RegressionError(
            "legacy baseline target lies inside more than one repository holding base_commit")
    return seat


def _base_repository(target: Path, base_commit: str | None) -> Path:
    """The work tree of the one repository that holds *base_commit* for the resolved *target*."""
    if not base_commit or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_commit.lower()):
        raise RegressionError("legacy baseline requires a full base_commit")
    repo = Path(_git(target, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if _refuse_second_holder(target, base_commit.lower()) != repo:
        raise RegressionError(
            "legacy baseline repository reports a work tree other than the directory it sits in")
    if not target.is_relative_to(repo):
        raise RegressionError("legacy baseline target is outside its git repository")
    return repo


def _committed_modes(snapshot: Path, files: dict[str, bytes], executable: set[str],
                     links: dict[str, str]) -> None:
    """Give every snapshot file and directory the mode a commit can express (0755 for an
    executable file and for directories, 0644 otherwise) and fail closed unless the file
    system then reports exactly that mode and that access for the owner: nothing else
    (group or other bits, an ACL, an inherited deny) may differ from a fresh checkout."""
    directories = {path.parent for path in (snapshot / name for name in (*files, *links))}
    directories = {parent for directory in directories
                   for parent in (directory, *directory.parents)
                   if parent.is_relative_to(snapshot) and parent != snapshot}
    expected = {**{directory: 0o755 for directory in directories},
                **{snapshot / name: 0o755 if name in executable else 0o644 for name in files}}
    for path, mode in expected.items():
        os.chmod(path, mode)
    for path, mode in expected.items():
        status = os.lstat(path)
        if (stat.S_IMODE(status.st_mode) != mode
                or os.access(path, os.X_OK) != bool(mode & stat.S_IXUSR)
                or not os.access(path, os.R_OK)):
            raise RegressionError(
                "base_commit snapshot cannot hold the committed file mode of "
                f"{path.relative_to(snapshot).as_posix()} on this file system")


@contextlib.contextmanager
def _base_scope(target: Path, roots: tuple[Path, ...], base_commit: str | None, *,
                portable_roots: list[str] | None = None) -> Iterator[PytestScope]:
    """Rebuild the pytest scope of *target* at *base_commit* from hash-verified git objects.

    The rebuilt scope must select *roots*; *portable_roots* gives them as recorded relative
    paths instead, so that nothing is resolved in the current tree."""
    target = target.resolve()
    repo = _base_repository(target, base_commit)
    assert base_commit is not None
    relative = target.relative_to(repo)
    try:
        files = _base_files(target, base_commit.lower(),
                            "" if target == repo else relative.as_posix())
    except ValueError as exc:
        raise RegressionError("base_commit pytest scope cannot be reconstructed") from exc
    if not files:
        raise RegressionError("base_commit target bytes are unavailable")
    with tempfile.TemporaryDirectory(prefix="wilfred-pytest-base-") as temporary:
        snapshot = Path(temporary).resolve()
        try:
            for name, payload in files.items():
                path = snapshot / name
                if not path.resolve(strict=False).is_relative_to(snapshot):
                    raise RegressionError("base_commit path escapes its snapshot")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            base_target = snapshot / relative
            base_scope = resolve_pytest_scope(base_target)
        except (OSError, ValueError) as exc:
            raise RegressionError("base_commit pytest scope cannot be reconstructed") from exc
        if _portable_roots(base_target, base_scope.roots) != (
                _portable_roots(target, roots) if portable_roots is None else portable_roots):
            raise RegressionError("legacy baseline scope disagrees with base_commit")
        yield base_scope


COLLECTION_FILE = "base_collection.json"
COLLECTION_VERSION = "wilfred-base-collection/v1"


def _collected_only(result: Path, returncode: int) -> list[str]:
    """Node ids from the recorder's result of a ``--collect-only`` run; anything other than a
    clean collection (exit 0, or 5 for no tests) is a refusal."""
    try:
        data = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RegressionError("base_commit collection left no structured result") from exc
    collected = data.get("collected") if isinstance(data, dict) else None
    if (returncode not in {0, 5} or data.get("exitstatus") != returncode
            or not isinstance(collected, list)
            or not all(isinstance(node, str) for node in collected)
            or data.get("reports") not in ({}, None)):
        raise RegressionError(
            f"base_commit tests could not be collected cleanly (pytest exit {returncode})")
    return sorted(set(collected))


def collect_base_commit(target: Path, base_commit: str) -> list[str]:
    """The test node ids that the tests of *base_commit* collect under *target*.

    The whole repository is rebuilt in a fresh directory outside it from hash-verified git
    objects (links with their committed destination), with the file modes the commit records
    and nothing else from the working tree (group or other permission bits, ACLs, extended
    attributes, untracked or ignored files). Collection runs the committed conftest and test
    modules, as capture will; the environment and files outside the repository are those of
    the moment it runs."""
    target = target.resolve()
    repo = _base_repository(target, base_commit)
    commit = base_commit.lower()
    links: dict[str, str] = {}
    executable: set[str] = set()
    try:
        files = _base_files(repo, commit, "", links=links, executable=executable)
    except ValueError as exc:
        raise RegressionError("base_commit tree cannot be reconstructed") from exc
    if not files:
        raise RegressionError("base_commit target bytes are unavailable")
    with tempfile.TemporaryDirectory(prefix="wilfred-pytest-collect-") as temporary:
        snapshot = Path(temporary).resolve()
        try:
            for name in (*files, *links):
                if not (snapshot / name).resolve(strict=False).is_relative_to(snapshot):
                    raise RegressionError("base_commit path escapes its snapshot")
            for name, payload in files.items():
                (snapshot / name).parent.mkdir(parents=True, exist_ok=True)
                (snapshot / name).write_bytes(payload)
            for name, destination in links.items():
                (snapshot / name).parent.mkdir(parents=True, exist_ok=True)
                os.symlink(destination, snapshot / name)
            _committed_modes(snapshot, files, executable, links)
            base_target = snapshot / target.relative_to(repo)
            base = resolve_pytest_scope(base_target)
        except (OSError, ValueError) as exc:
            raise RegressionError("base_commit tree cannot be reconstructed") from exc
        with (isolated_pycache(base.target) as pycache_prefix,
              _private_result_file(base.target) as result):
            out = run_subprocess(
                base.argv("--collect-only", "-q", "--no-header", pycache_prefix=pycache_prefix,
                          result_path=result),
                cwd=base.target, timeout_s=_PYTEST_TIMEOUT_S, env=pytest_environment())
            if not out.ran:
                raise RegressionError("base_commit collection did not run or timed out")
            return _collected_only(result, out.returncode)


def base_collection_record(target: Path, base_commit: str, declared_target: str) -> bytes:
    """The plan-time record of the tests *base_commit* collects; never raises. A collection
    that cannot be made is recorded as unavailable, and capture then refuses."""
    record: dict[str, Any] = {"schema_version": COLLECTION_VERSION, "base_commit": base_commit,
                              "target": declared_target}
    try:
        record.update(status="collected",
                      collected_ids=collect_base_commit(target, base_commit))
    except (OSError, RegressionError, ValueError) as exc:
        record.update(status="unavailable", reason=str(exc))
    return (json.dumps(record, indent=2) + "\n").encode("utf-8")


def read_base_collection(payload: bytes, base_commit: str, declared_target: str) -> list[str]:
    """The collected ids of a plan-time record whose bytes the caller already authenticated;
    a record for another commit or target, or one without a clean collection, is refused."""
    try:
        record = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RegressionError("the plan-time collection record is not valid JSON") from exc
    if (not isinstance(record, dict) or record.get("schema_version") != COLLECTION_VERSION
            or record.get("base_commit") != base_commit
            or record.get("target") != declared_target):
        raise RegressionError(
            "the plan-time collection record does not belong to this run's base_commit")
    if record.get("status") != "collected":
        raise RegressionError("the tests of base_commit could not be collected at plan time: "
                              f"{record.get('reason', 'no reason recorded')}")
    ids = record.get("collected_ids")
    if not isinstance(ids, list) or not all(isinstance(node, str) for node in ids):
        raise RegressionError("the plan-time collection record is malformed")
    return ids


def vanished_since_plan(baseline: dict, planned: list[str]) -> list[str]:
    """Ids collected from base_commit at plan time that the capture run did not collect.

    A collected id is a passing id or a not-passing node id; a not-passing file path (a file
    with no collected test) collects none of its tests."""
    collected = {*baseline["passing_ids"],
                 *(node for node in baseline["not_passing_ids"] if "::" in node)}
    return sorted(set(planned) - collected)


def legacy_control_witness(
    target: Path, roots: tuple[Path, ...], base_commit: str | None,
) -> tuple[str | None, dict[str, str]]:
    """Derive omitted legacy controls from authenticated git bytes, never the worktree."""
    with _base_scope(target, roots, base_commit) as base_scope:
        return (_config_file_sha256(base_scope.config_path),
                _control_file_hashes(base_scope))


def _captured_witness(
    baseline: dict,
) -> tuple[list[str], tuple[tuple[str, dict], ...], Any, list[str], str, str]:
    """The scope, hashes, configuration digest, ids that did not pass, and the root and digest
    of the recorded tree, from a captured *baseline*."""
    try:
        roots = baseline["test_roots"]
        hashes = (("test files", baseline["test_file_hashes"]),
                  ("pytest control files", baseline["control_file_hashes"]),
                  ("test data files", baseline["data_file_hashes"]))
        config = baseline["pytest_config_file_sha256"]
        not_passing = baseline["not_passing_ids"]
        tree_root, tree = baseline["tree_root"], baseline["tree_sha256"]
    except (KeyError, TypeError) as exc:
        raise RegressionError(
            "regression baseline lacks the witness to compare with base_commit") from exc
    if (not all(isinstance(value, list) and all(isinstance(item, str) for item in value)
                for value in (roots, not_passing))
            or not all(isinstance(recorded, dict) for _label, recorded in hashes)
            or not isinstance(tree, str) or not isinstance(tree_root, str)):
        raise RegressionError("regression baseline lacks the witness to compare with base_commit")
    return roots, hashes, config, not_passing, tree_root, tree


def _require_base_tree(target: Path, base_commit: str, not_passing: list[str],
                       tree_root: str, tree: str) -> None:
    """Refuse a capture whose tests did not run on the base-commit tree.

    The protected passing set is whatever the tests of the captured tree collect and pass.
    On the tree of the base commit that set is a property of the commit and the machine. On
    any other tree an edit, inside the target or elsewhere in the repository, may have skipped
    a test or removed it from collection with no witness file touched, so the capture is
    refused. *tree* is the digest recorded before the tests ran, for the tree at *tree_root*
    (relative to the target), which must be the repository of the base commit. *not_passing*
    is named in the refusal."""
    repo = _base_repository(target, base_commit)
    if tree_root != Path(os.path.relpath(repo, target)).as_posix():
        raise RegressionError(
            "regression baseline tree was not recorded from the repository of its base_commit")
    expected = _base_tree(repo, base_commit.lower(), target)
    if _tree_sha256(expected) == tree:
        return
    try:  # For the message only: the decision above used the recorded digest.
        current = _tree(repo, target)
    except OSError:
        current = expected
    differing = sorted(path for path in set(expected) | set(current)
                       if expected.get(path) != current.get(path))
    shown = _fmt(differing) if differing else "restored since the tests ran"
    outside = (f"; {len(not_passing)} test(s) did not pass at capture ({_fmt(not_passing)})"
               if not_passing else "")
    raise RegressionError(
        "regression baseline capture needs the repository exactly as committed at base_commit: "
        f"it differed when the tests ran ({shown}){outside}"
    )


def require_base_commit_witness(target: Path, base_commit: str | None, baseline: dict,
                                repo_root: Path | None = None,
                                planned: Callable[[], list[str]] | None = None,
                                ) -> list[dict[str, str]] | None:
    """Refuse capture if any test input recorded in *baseline* differs from the run base commit,
    or if a test that did not pass is not declared at it (``require_declared_not_passing``,
    whose result is returned; ``None`` outside git, where nothing is compared).

    *planned* returns the ids the base commit collected when the run was planned (the engine
    reads them from the pinned plan-time record and refuses when it cannot). On the
    base-commit tree, each of them that the capture did not collect is added to the
    baseline's ``not_passing_ids`` and listed in its ``vanished_since_plan``, so it must be
    declared like any test that did not pass. A caller without a plan (direct library use)
    passes nothing and gets no such comparison; ``hw regression-baseline`` always passes it.

    The base commit is compared with the hashes the capture recorded and will pin. The tree
    is not read a second time: bytes restored after the test run cannot stand in for the
    bytes the run used."""
    target = target.resolve()
    if _discovered_repository(target) is None:
        if base_commit is not None:
            # The run was planned in a repository: losing it must not drop the comparison.
            raise RegressionError(
                "regression baseline capture cannot find the git repository of its base_commit"
            )
        return None  # Historical non-git capture has no commit to compare.
    if base_commit is None:
        raise RegressionError("regression baseline capture needs a base_commit")
    roots, captured, config, not_passing, tree_root, tree = _captured_witness(baseline)
    with _base_scope(target, (), base_commit, portable_roots=roots) as base:
        expected = {
            "test files": _test_file_hashes(base.target, scope=base),
            "pytest control files": _control_file_hashes(base),
            "test data files": _data_file_hashes(base),
        }
        for label, observed in captured:
            original = expected[label]
            changed = sorted(path for path in set(original) | set(observed)
                             if original.get(path) != observed.get(path))
            if changed:
                raise RegressionError(
                    f"regression baseline {label} differ from base_commit: {_fmt(changed)}"
                )
        if _config_file_sha256(base.config_path) != config:
            raise RegressionError(
                "regression baseline pytest configuration differs from base_commit"
            )
        _require_base_tree(target, base_commit, not_passing, tree_root, tree)
    if planned is not None:
        vanished = vanished_since_plan(baseline, planned())
        baseline["not_passing_ids"] = sorted({*baseline["not_passing_ids"], *vanished})
        baseline["vanished_since_plan"] = vanished
    # Only on the base-commit tree: every test that did not pass must be declared at the base.
    return require_declared_not_passing(target, base_commit, baseline, repo_root=repo_root)


_MAX_NAMED = 20


def _named(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_NAMED])
    return shown + (f" … ({len(items)} in all)" if len(items) > _MAX_NAMED else "")


def require_declared_not_passing(target: Path, base_commit: str | None, baseline: dict, *,
                                 repo_root: Path | None = None) -> list[dict[str, str]] | None:
    """Refuse a capture with a test that did not pass unless a human declared it beforehand.

    A test that is skipped, expected to fail, failing, or in a test file that was not
    collected protects nothing. Whether it runs can depend on the machine (an environment
    variable, a timestamp, permission bits or an ACL that a commit does not store), so the
    comparison with the base commit cannot vouch for it. Each one must be covered by the
    ``not_passing_at_capture`` list of the declaration file; a test the engine already
    tolerated as a declared known failure (``tolerated_known_failures``) may instead be
    covered by that known-failure entry. The file is the one in *repo_root* (the repository
    root by default), read from hash-verified objects of *base_commit*: replacement refs and
    the working tree are never consulted. An invalid declaration excuses nothing, and a
    covered test's file must exist at *base_commit* with the same bytes now. Returns the
    declared tests, recorded in the baseline; they never count as protected.

    Outside git (no *base_commit* and no repository) nothing is compared and ``None`` is
    returned: the stated limit of a capture without a commit, unchanged.
    """
    target = target.resolve()
    if base_commit is None and _discovered_repository(target) is None:
        return None
    if base_commit is None:
        raise RegressionError("regression baseline capture needs a base_commit")
    not_passing = _captured_witness(baseline)[3]
    if not not_passing:
        return []
    repo = _base_repository(target, base_commit)
    commit = base_commit.lower()
    files = _base_files(repo, commit, "", links={})
    declaration: known_failures.KnownFailuresDeclaration | None = None
    try:
        location = (Path(repo_root or repo).resolve()
                    / known_failures.DECLARATION_FILE).relative_to(repo)
    except ValueError:
        location = None
    payload = None if location is None else files.get(location.as_posix())
    if payload is not None:
        declaration, problem = known_failures.parse_declaration(payload, commit)
        missing = [] if declaration is None else sorted(
            entry.id for entry in declaration.not_passing_at_capture
            if entry.id.partition("::")[0] not in files)
        if problem is None and missing:
            problem = (f"{known_failures.DECLARATION_FILE} at {commit[:12]} is invalid: "
                       f"declared test file(s) absent from base_commit: {_named(missing)}")
        if problem is not None:
            raise RegressionError(f"regression baseline capture refused: {problem}")
    tolerated = baseline.get("tolerated_known_failures")
    failures = set(tolerated) if isinstance(tolerated, list) else set()
    inner = target.relative_to(repo).as_posix()
    declared: list[dict[str, str]] = []
    undeclared: list[str] = []
    changed: list[str] = []
    for node in sorted(not_passing):
        named = f"{inner}/{node}" if inner != "." else node
        candidates = [] if declaration is None else [
            *(("not_passing_at_capture", entry)
              for entry in declaration.not_passing_at_capture),
            # A known-failure entry (directories included) covers only a tolerated failure.
            *(("entries", entry) for entry in declaration.entries if node in failures)]
        found = next(((kind, entry) for kind, entry in candidates
                      if known_failures.entry_matches(entry.id, named)), None)
        if found is None:
            undeclared.append(named)
            continue
        path = named.partition("::")[0]
        try:
            current = _witness_bytes(repo / path, repo)
        except (OSError, RegressionError):
            current = None
        if path not in files or current != files[path]:
            changed.append(named)
            continue
        declared.append({"id": node, "declared_as": found[1].id, "reason": found[1].reason,
                         "list": found[0]})
    if changed:
        raise RegressionError(
            "regression baseline capture refused: declared not-passing test(s) whose test file "
            f"is new or changed since base_commit: {_named(changed)}")
    if undeclared:
        recorded = baseline.get("vanished_since_plan")
        vanished = {f"{inner}/{node}" if inner != "." else node
                    for node in (recorded if isinstance(recorded, list) else [])}
        gone = [named for named in undeclared if named in vanished]
        shrink = (f" Of these, {len(gone)} were collected from base_commit when the run was "
                  f"planned but not collected at capture: {_named(gone)} (file metadata that "
                  "a commit does not store, such as a permission bit or an ACL, can deselect "
                  "a test or shrink a parametrized list)." if gone else "")
        raise RegressionError(
            f"regression baseline capture refused: {len(undeclared)} test(s) did not pass at "
            f"capture and are not declared: {_named(undeclared)}.{shrink} A test that does "
            "not pass protects nothing, and whether it runs can depend on the machine. If that is "
            "intended, list each one under \"not_passing_at_capture\" in "
            f"{known_failures.DECLARATION_FILE} at the repository root, with a reason (the "
            "exact test id, or the test file path for a whole skipped or uncollected file), "
            "commit it, and plan a new run from that commit. Otherwise make the test pass.")
    return declared


def baseline_candidate(target: Path) -> tuple[dict, CheckResult]:
    """Observe one baseline run; only the engine may contextualize its rc=1 result."""
    target = target.resolve()
    try:
        scope = resolve_pytest_scope(target)
        before = (_test_file_hashes(target, scope=scope),
                  _config_file_sha256(scope.config_path), _control_file_hashes(scope),
                  _data_file_hashes(scope))
        tree_root = _tree_root(target)
        tree = _tree_sha256(_tree(tree_root, target))
    except (OSError, RegressionError, TypeError, ValueError) as exc:
        raise RegressionError(f"cannot capture regression baseline: {exc}") from exc
    # The runner resolves its own scope; a scope or control change made during the run is
    # caught by the before/after witness comparison below.
    try:
        passing, returncode, findings = _pytest_passing(target)
    except (OSError, ValueError) as exc:
        raise RegressionError(f"cannot capture regression baseline: {exc}") from exc
    if passing is None:
        raise RegressionError("cannot capture regression baseline: pytest unavailable, timed out, "
                              "or its structured result was missing or inconsistent")
    if returncode not in {0, 1}:
        raise RegressionError(
            f"cannot capture regression baseline: pytest did not pass cleanly (exit {returncode})"
        )
    if not passing:
        raise RegressionError("cannot capture regression baseline: no passing tests were observed")
    try:
        hashes = _test_file_hashes(target, scope=scope)
        after_scope = resolve_pytest_scope(target)
        data = _data_file_hashes(after_scope)
    except (OSError, TypeError, ValueError) as exc:
        raise RegressionError(f"cannot capture regression baseline: {exc}") from exc
    if not hashes:
        raise RegressionError("cannot capture regression baseline: no test files were found")
    if before != (hashes, _config_file_sha256(after_scope.config_path),
                  _control_file_hashes(after_scope), data):
        raise RegressionError("pytest witness changed during baseline capture")
    # A test file none of whose tests was collected (skipped as a module) did not pass either.
    not_passing = set(getattr(passing, "not_passing", ()))
    collected = {node.split("::", 1)[0] for node in (*passing, *not_passing)}
    baseline = {
        "schema_version": BASELINE_VERSION,
        "target": ".",
        "test_roots": (portable := _portable_roots(target, scope.roots)),
        "test_config_sha256": approvals.digest(portable),
        "pytest_config_file_sha256": _config_file_sha256(scope.config_path),
        "control_file_hashes": _control_file_hashes(scope),
        "data_file_hashes": data,
        "passing_ids": sorted(passing),
        "passing": len(passing),
        "not_passing_ids": sorted(not_passing | (set(hashes) - collected)),
        "tree_root": Path(os.path.relpath(tree_root, target)).as_posix(),
        "tree_sha256": tree,
        "test_file_hashes": hashes,
    }
    check = CheckResult(
        kind=CheckKind.TEST,
        name="pytest",
        passed=returncode == 0,
        severity=Severity.INFO if returncode == 0 else Severity.ERROR,
        details=f"pytest baseline capture exited {returncode}",
        findings=findings,
    )
    return baseline, check


def capture_baseline(target: Path) -> dict:
    """Snapshot a wholly green baseline; nonzero pytest always fails closed."""
    baseline, check = baseline_candidate(target)
    if not check.passed:
        raise RegressionError(
            "cannot capture regression baseline: pytest did not pass cleanly (exit 1)"
        )
    return baseline


def load_baseline(baseline_path: Path) -> dict | None:
    if not baseline_path.exists():
        return None
    try:
        value = json.loads(baseline_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def validate_baseline(target: Path, baseline: dict) -> None:
    """Validate shape, portable/legacy scope and containment without executing pytest."""
    baseline_scope(target, baseline)


def checked_baseline(
    baseline_path: Path, target: Path, expected_sha256: str | None,
) -> tuple[dict | None, CheckResult | None]:
    """Authenticate exact bytes before parsing; all failures are auditable gate results."""
    try:
        payload = baseline_path.read_bytes()
        if not expected_sha256:
            raise RegressionError("baseline digest is missing from run state")
        if not _SHA256.fullmatch(expected_sha256):
            raise RegressionError("baseline digest in run state is invalid")
        if not hashlib.sha256(payload).hexdigest() == expected_sha256:
            raise RegressionError("baseline bytes do not match the digest pinned in run state")
        baseline = json.loads(payload)
        if not isinstance(baseline, dict):
            raise RegressionError("baseline payload must be an object")
        validate_baseline(target, baseline)
        return baseline, None
    except (OSError, json.JSONDecodeError, RegressionError, TypeError, ValueError) as exc:
        return None, CheckResult(
            kind=CheckKind.TEST, name="regression-baseline", passed=False,
            severity=Severity.ERROR, details=f"invalid regression baseline: {exc}")


def _id_heads(ids: Iterable[object]) -> set[str]:
    """Return the leading path component of every nested node id."""
    return {node.split("/", 1)[0] for node in ids if isinstance(node, str) and "/" in node}


def _relocation_head(ids: Iterable[object], target: Path) -> str | None:
    """Name the one directory of *target* that holds a tests-directory witness, if any.

    A witness captured with its tests directory as target and later evaluated from the product
    root names every nested node id under a single head that is a directory of *target*.
    """
    heads = _id_heads(ids)
    if len(heads) != 1:
        return None
    head = next(iter(heads))
    return head if (target / head).is_dir() else None


def _versioned_scope(target: Path, baseline: dict, raw_ids: list[str],
                     raw_hashes: dict[str, str]) -> BaselineScope:
    """Resolve a portable versioned baseline, including a relocated tests-directory witness."""
    stored = baseline.get("test_roots")
    if (baseline.get("target") != "." or not isinstance(stored, list) or not stored
            or not all(isinstance(value, str) for value in stored)):
        raise RegressionError("versioned baseline has an invalid portable scope")
    try:
        roots = tuple(target if value == "." else
                      (target / validate_relative_path(
                          value, label="baseline test root")).resolve()
                      for value in stored)
    except ValueError as exc:
        raise RegressionError(str(exc)) from exc
    if baseline.get("test_config_sha256") != approvals.digest(stored):
        raise RegressionError("baseline portable root digest disagrees")
    ids, hashes = set(raw_ids), dict(raw_hashes)
    # Compatibility for a witness captured with the tests directory itself as target and
    # later evaluated from its relocated product root (the preserved successor workflow).
    head = _relocation_head(ids, target) if stored == ["."] else None
    if head is not None:
        roots = ((target / head).resolve(),)
        hashes = {path if path.startswith(f"{head}/") else f"{head}/{path}": sha
                  for path, sha in hashes.items()}
    return roots, ids, hashes


def _legacy_scope(target: Path, baseline: dict, raw_ids: list[str],
                  raw_hashes: dict[str, str]) -> BaselineScope:
    """Migrate the preserved unversioned shapes: captured at tests/ or at the repo root.

    Scope comes only from node ids, never live mutable pytest configuration; archived hashes
    outside that scope (the repo-root shape's .hw copies) are discarded.
    """
    if not isinstance(baseline.get("target"), str) or not baseline["target"]:
        raise RegressionError("legacy baseline target is missing")
    captured = Path(baseline["target"]).resolve()
    legacy_heads = sorted(_id_heads(raw_ids))
    if len(legacy_heads) > 1 or any(head in {"", ".", ".."} for head in legacy_heads):
        raise RegressionError("legacy baseline test scope is ambiguous or unsafe")
    head = legacy_heads[0] if legacy_heads else ""
    moved = _relocation_head(raw_ids, target)
    roots = ((target / moved,) if moved is not None
             else (target,) if target.name == head or captured.name == head else ())
    prefix = f"{head}/" if roots and roots[0] != target else ""
    ids = {value for value in raw_ids if not prefix or
           value.split("::", 1)[0].startswith(prefix)}
    hashes = {path: sha for path, sha in raw_hashes.items() if
              (path.startswith(prefix) if prefix else "/" not in path)}
    if prefix and captured.name == head:
        hashes = {path if path.startswith(prefix) else prefix + path: sha
                  for path, sha in raw_hashes.items() if "/" not in path}
    return roots, ids, hashes


def baseline_scope(target: Path, baseline: dict) -> BaselineScope:
    """Validate *baseline* and return its test roots, passing node-ids and test-file hashes."""
    target = target.resolve()
    raw_ids = baseline.get("passing_ids")
    raw_hashes = baseline.get("test_file_hashes")
    raw_controls = baseline.get("control_file_hashes")
    raw_config_sha256 = baseline.get("pytest_config_file_sha256")
    count = baseline.get("passing")
    if (not isinstance(raw_ids, list) or not all(isinstance(v, str) and v for v in raw_ids)
            or len(set(raw_ids)) != len(raw_ids) or not isinstance(count, int)
            or isinstance(count, bool) or count != len(raw_ids)
            or not isinstance(raw_hashes, dict) or not all(
                isinstance(path, str) and path and isinstance(sha, str)
                and _SHA256.fullmatch(sha) for path, sha in raw_hashes.items())):
        raise RegressionError("baseline passing count and node-id witness disagree")
    for raw in (raw_controls, baseline.get("data_file_hashes")):
        if raw is not None and (not isinstance(raw, dict) or not all(
                isinstance(path, str) and path and isinstance(sha, str) and _SHA256.fullmatch(sha)
                for path, sha in raw.items())):
            raise RegressionError("baseline pytest control-file or data-file witness is invalid")
    if raw_config_sha256 is not None and (
            not isinstance(raw_config_sha256, str)
            or not _SHA256.fullmatch(raw_config_sha256)):
        raise RegressionError("baseline pytest configuration witness is invalid")
    version = baseline.get("schema_version")
    if version == BASELINE_VERSION:
        roots, ids, hashes = _versioned_scope(target, baseline, raw_ids, raw_hashes)
    elif version is not None:
        raise RegressionError(f"unsupported regression baseline version: {version!r}")
    else:
        roots, ids, hashes = _legacy_scope(target, baseline, raw_ids, raw_hashes)
    if (not roots or len(set(roots)) != len(roots)
            or any(not root.is_relative_to(target) or not root.is_dir() for root in roots)):
        raise RegressionError("baseline test root is missing or outside the product")
    for path in hashes:
        try:
            safe = target / validate_relative_path(path, label="baseline test path")
        except ValueError as exc:
            raise RegressionError(str(exc)) from exc
        if not safe.resolve(strict=False).is_relative_to(target):
            raise RegressionError("baseline test path escapes the product")
    files = {node.split("::", 1)[0] for node in ids}
    if not (files <= set(hashes)
            or {path.removeprefix(f"{target.name}/") for path in files} <= set(hashes)):
        raise RegressionError("baseline node ids are not backed by test-file hashes")
    return roots, ids, hashes


def _fmt(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_SHOWN])
    return shown + (" …" if len(items) > _MAX_SHOWN else "")


def missing_baseline_check() -> CheckResult:
    return CheckResult(
        kind=CheckKind.TEST,
        name="regression-baseline",
        passed=False,
        severity=Severity.ERROR,
        details="regression baseline is missing or unreadable; recapture it from a pristine tree",
    )


def _review_values(record: dict[str, Any]) -> tuple[str, list[str]]:
    path = validate_relative_path(str(record.get("path", "")), label="reviewed test path")
    values = [
        path,
        str(record.get("before_sha256", "")),
        str(record.get("after_sha256", "")),
        str(record.get("dispatch_sha256", "")),
        str(record.get("report_sha256", "")),
    ]
    if any(not _SHA256.fullmatch(value) for value in values[1:]):
        raise RegressionError("semantic review must bind four complete SHA-256 digests")
    return f"approve semantic test evolution {path}", values


def create_semantic_review_challenge(
    run_dir: Path, *, run_id: str, path: str, before_sha256: str, after_sha256: str,
    dispatch_sha256: str, report_sha256: str,
) -> dict[str, Any]:
    """Create an exact test-transition challenge for a separately dispatched reviewer."""
    record = {
        "schema_version": "wilfred-test-evolution-review/v1", "path": path,
        "before_sha256": before_sha256, "after_sha256": after_sha256,
        "dispatch_sha256": dispatch_sha256, "report_sha256": report_sha256,
    }
    action, evidence = _review_values(record)
    review_id = "test-" + hashlib.sha256(path.encode()).hexdigest()[:20]
    record["review_id"] = review_id
    review_dir = run_dir / "test-evolution"
    review_dir.mkdir(parents=True, exist_ok=True)
    challenge = approvals.issue_challenge(
        review_dir, run_id=run_id, gate_id=review_id, action=action, evidence=evidence,
        purpose="test-evolution",
    )
    atomic_write_text(
        review_dir / f"{review_id}.review.json", json.dumps(record, indent=2) + "\n"
    )
    return {"record": record, "challenge": challenge,
            "decision_file": str(review_dir / f"{review_id}.decision.json")}


def verified_semantic_transitions(
    run_dir: Path, *, run_id: str, target: Path, baseline: dict,
    public_key_path: Path | None = None, repo_root: Path | None = None,
) -> dict[str, tuple[str, str]]:
    """Load only independently signed reviews matching the exact old/new test bytes.

    The verification key defaults to the installed review key; a key inside ``.hw``, the
    target, *repo_root* or their git work tree is refused and approves nothing.
    """
    review_dir = run_dir / "test-evolution"
    attestation_key = public_key_path or approvals.DEFAULT_REVIEW_ATTESTATION_PUBLIC_KEY_PATH
    if not review_dir.exists():
        return {}
    fenced = [target] if repo_root is None else [target, repo_root]
    try:
        attestation_key = approvals.require_external_key_location(
            attestation_key, excluded_roots=fenced)
    except approvals.ApprovalError:
        return {}
    try:
        roots, _ids, old = baseline_scope(target, baseline)
        current = _test_file_hashes(target, roots)
    except (AttributeError, OSError, RegressionError, TypeError, ValueError):
        return {}
    approved: dict[str, tuple[str, str]] = {}
    for path in sorted(review_dir.glob("*.review.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            action, evidence = _review_values(record)
            review_id = str(record["review_id"])
            test_path = evidence[0]
            if old.get(test_path) != evidence[1] or current.get(test_path) != evidence[2]:
                continue
            # With the owner's policy switch on, the recorded review needs no signature.
            approvals.policy_approve(review_dir, review_id, eligible=True)
            decision, _reviewer, _note = approvals.verify_and_consume(
                review_dir, review_id, review_dir / f"{review_id}.decision.json",
                expected_run_id=run_id, public_key_path=attestation_key,
                expected_action=action, expected_evidence=evidence, consume=False,
                policy_eligible=True,
            )
            if decision == "approve":
                approved[test_path] = (evidence[1], evidence[2])
        except (approvals.ApprovalError, KeyError, OSError, ValueError, json.JSONDecodeError):
            continue
    return approved


def _relocated_scope(target: Path, baseline: dict) -> tuple[Path, dict] | None:
    """Retain the original capture boundary when a tests-directory witness is relocated."""
    if baseline.get("schema_version") != BASELINE_VERSION or baseline.get("test_roots") != ["."]:
        return None
    ids = baseline.get("passing_ids", [])
    head = _relocation_head(ids, target)
    hashes = baseline.get("test_file_hashes", {})
    if (head is None or not hashes or any("/" in path for path in hashes)
            or not isinstance(baseline.get("control_file_hashes"), dict)):
        return None
    if head in {".", ".."} or (target / head).is_symlink():
        return None
    if any(not node.startswith(f"{head}/") for node in ids):
        return None
    return target / head, {**baseline, "passing_ids": [node[len(head) + 1:] for node in ids]}


def _effective_witness(target: Path, baseline: dict) -> tuple[Path, dict, BaselineScope, bool]:
    """Validate *baseline* and follow each relocation back to its original capture boundary.

    Returns the comparison target, its baseline, the validated scope and whether it relocated.
    """
    relocated = False
    while True:
        scope = baseline_scope(target, baseline)
        moved = _relocated_scope(target, baseline)
        if moved is None:
            return target, baseline, scope, relocated
        target, baseline, relocated = moved[0].resolve(), moved[1], True


def _new_tests_are_helpers(scope: PytestScope, added: set[str]) -> bool:
    """Do not exempt a newly added test module that old controls can load as a helper."""
    if not added:
        return False
    names = {Path(path).stem for path in added}
    dynamic = {"__import__", "import_module", "exec", "eval", "run_path", "run_module"}
    for path in scope.control_files:
        if path.relative_to(scope.target).as_posix() in added:
            continue
        tree = ast.parse(_witness_bytes(path, scope.target), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports = [item.name for item in node.names]
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
                if any(names.intersection(value.split(".")) for value in imports):
                    return True
            if isinstance(node, ast.Call):
                function = node.func
                name = (function.id if isinstance(function, ast.Name) else
                        function.attr if isinstance(function, ast.Attribute) else "")
                if name in dynamic:
                    return True
    return False


def _control_mismatches(
    current: dict[str, str], expected: dict[str, str], baseline_hashes: dict[str, str],
    now_hashes: dict[str, str], transitions: dict[str, tuple[str, str]], scope: PytestScope,
) -> bool:
    # Newly added test modules do not execute in the protected regression invocation.
    added_tests = set(now_hashes) - set(baseline_hashes)
    if _new_tests_are_helpers(scope, added_tests):
        return True
    current = {path: sha for path, sha in current.items() if path not in added_tests}
    expected = {path: sha for path, sha in expected.items() if path not in added_tests}
    for path, before in baseline_hashes.items():
        after = now_hashes.get(path)
        if after is not None and transitions.get(path) == (before, after) and path in expected:
            expected[path] = after
    return current != expected


_EVIDENCE_ERRORS = (OSError, RegressionError, TypeError, ValueError)
_UNAVAILABLE = "regression evidence unavailable: "


def _read_authority(control_authority: ControlAuthority | None) -> dict[str, tuple[str, str]]:
    return {} if control_authority is None else dict(control_authority())


def _witnesses_unchanged(target: Path, now_hashes: dict[str, str], controls: dict[str, str],
                         current_config: str | None, data: dict[str, str]) -> bool:
    """Re-resolve the scope and prove test files, controls and configuration are unchanged."""
    scope = resolve_pytest_scope(target)
    return (now_hashes == _test_file_hashes(target, scope=scope)
            and controls == _control_file_hashes(scope)
            and current_config == _config_file_sha256(scope.config_path)
            and data == _data_file_hashes(scope))


def _expected_controls(target: Path, baseline: dict, roots: tuple[Path, ...],
                       base_commit: str | None, current_config: str | None,
                       ) -> tuple[dict[str, str], list[str]]:
    """Expected control hashes (baseline or base commit) and any configuration drift reason."""
    if "control_file_hashes" in baseline:
        return dict(baseline["control_file_hashes"]), []
    expected_config, expected = legacy_control_witness(target, roots, base_commit)
    if expected_config != current_config:
        return expected, ["pytest configuration changed from the base commit"]
    return expected, []


def _reconcile_control_authority(
    authority: dict[str, tuple[str, str]], expected: dict[str, str], controls: dict[str, str],
    baseline_hashes: dict[str, str], target: Path, roots: tuple[Path, ...],
    base_commit: str | None,
) -> dict[str, str]:
    """Accept the one externally reviewed control change exactly as authorized, or raise."""
    if not authority:
        return expected
    _base_config, base_controls = legacy_control_witness(target, roots, base_commit)
    if len(authority) != 1:
        raise RegressionError("control authority must name exactly one control file")
    ((helper, pair),) = authority.items()
    if (helper in baseline_hashes or helper not in controls
            or pair != (base_controls.get(helper), controls[helper])
            or expected.get(helper) != base_controls.get(helper)):
        raise RegressionError("control authority does not match the base witness")
    return {**expected, helper: controls[helper]}


def _test_file_reasons(baseline_hashes: dict[str, str], now_hashes: dict[str, str],
                       transitions: dict[str, tuple[str, str]]) -> tuple[list[str], int]:
    """Reasons for removed or unreviewed modified baseline test files, and the reviewed count."""
    removed = sorted(f for f in baseline_hashes if f not in now_hashes)
    modified = sorted(
        f for f, h in baseline_hashes.items() if f in now_hashes and now_hashes[f] != h
    )
    reviewed = {path for path in modified
                if transitions.get(path) == (baseline_hashes[path], now_hashes[path])}
    modified = sorted(set(modified) - reviewed)
    reasons = []
    if removed:
        reasons.append(f"{len(removed)} pre-existing test file(s) removed: {_fmt(removed)}")
    if modified:
        reasons.append(
            f"{len(modified)} pre-existing test file(s) modified "
            f"(regression-witness integrity): {_fmt(modified)}"
        )
    return reasons, len(reviewed)


@functools.cache
def _shadowed_names() -> frozenset[str]:
    """Top-level names the protected run must resolve outside the target: every standard
    library module (private accelerators included, since the standard library imports them
    lazily) and every top-level name of the runner's distributions."""
    runner = {"pytest", "_pytest", "py", "pluggy", "iniconfig", "packaging", "pygments"}
    runner.update(name for name, owners in importlib.metadata.packages_distributions().items()
                  if any(owner.lower() in _RUNNER_DISTRIBUTIONS for owner in owners))
    return frozenset(sys.stdlib_module_names) | frozenset(runner)


def _override_value(tokens: list[str], index: int) -> str | None:
    """The ``name=value`` of an ``-o`` / ``--override-ini`` option at *index*, if any."""
    token = tokens[index]
    if token in {"-o", "--override-ini"}:
        return tokens[index + 1] if index + 1 < len(tokens) else None
    if token.startswith("--override-ini="):
        return token.removeprefix("--override-ini=")
    return token[2:] if token.startswith("-o") and not token.startswith("--") else None


def _configured_search_path(target: Path) -> list[Path]:
    """Directories pytest's ``pythonpath`` option (ini value or ``-o`` override in addopts)
    prepends, resolved against the configuration directory, which is always the target."""
    config = _active_pytest_config(target)
    if config is None:
        return []
    text = config.path.read_text(encoding="utf-8")
    value: object
    if config.path.name == "pyproject.toml":
        value = tomllib.loads(text).get("tool", {}).get("pytest", {}).get(
            "ini_options", {}).get("pythonpath", [])
    else:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
        except configparser.Error as exc:
            raise RegressionError(f"invalid pytest configuration at {config.path}") from exc
        value = parser.get(config.section, "pythonpath", fallback="")
    addopts = config.addopts if isinstance(config.addopts, str) else " ".join(config.addopts)
    tokens = shlex.split(addopts)
    for index in range(len(tokens)):
        override = _override_value(tokens, index)
        if override is not None and override.partition("=")[0].strip() == "pythonpath":
            value = override.partition("=")[2]
    entries = shlex.split(value) if isinstance(value, str) else value
    if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
        raise RegressionError(f"invalid pytest pythonpath in {config.path}")
    return [(target / entry).resolve() for entry in entries]


def _prepended_directories(scope: PytestScope, test_files: tuple[Path, ...]) -> set[Path]:
    """The target, every configured ``pythonpath`` directory inside it and every basedir
    pytest's prepend import mode inserts for a witnessed test or control module: the first
    ancestor without ``__init__.py`` (kept inside the target)."""
    target = scope.target
    directories = {target, *(path for path in _configured_search_path(target)
                             if path.is_relative_to(target) and path.is_dir())}
    for path in (*scope.control_files, *test_files):
        directory = path.parent
        while (directory != target and directory.is_relative_to(target)
               and (directory / "__init__.py").is_file()):
            directory = directory.parent
        if directory.is_relative_to(target):
            directories.add(directory)
    return directories


def _importable_name(entry: Path, suffixes: tuple[str, ...]) -> str | None:
    """The top-level name *entry* provides from a search-path directory; a directory without
    an ``__init__`` module is a namespace portion, which never shadows a real module."""
    if entry.is_dir():
        return entry.name if any((entry / f"__init__{suffix}").exists()
                                 for suffix in suffixes) else None
    matched = [suffix for suffix in suffixes if entry.name.endswith(suffix)]
    return entry.name[:-max(map(len, matched))] if matched else None


def _shadowing_modules(scope: PytestScope, test_files: tuple[Path, ...]) -> list[str]:
    """Relative paths of modules or packages named like the standard library or the runner in
    a directory the protected run puts on the module search path."""
    suffixes = tuple(importlib.machinery.all_suffixes())
    names = _shadowed_names()
    found: set[str] = set()
    for directory in _prepended_directories(scope, test_files):
        for entry in directory.iterdir():
            if directory == scope.target and entry.name in _BYTECODE_EXEMPT:
                continue
            if _importable_name(entry, suffixes) in names:
                found.add(entry.relative_to(scope.target).as_posix())
    return sorted(found)


def _unwitnessed_importables(scope: PytestScope, test_files: tuple[Path, ...]) -> list[str]:
    """One fail-closed reason per kind of importable file the protected run does not witness."""
    reasons: list[str] = []
    for found, kind in (
        (_planted_bytecode(scope.target), "bytecode caches present under the target"),
        (_planted_extensions(scope.target), "compiled extension modules present under the target"),
        (_shadowing_modules(scope, test_files),
         "modules named like the standard library or the test runner on the protected module "
         "search path"),
    ):
        if found:
            reasons.append(f"{kind}: {_fmt(found)}")
    return reasons


def _run_protected_suite(
    target: Path, scope: PytestScope, baseline_passing: set[str],
    baseline_hashes: dict[str, str],
    witness: tuple[dict[str, str], dict[str, str], str | None, dict[str, str]],
    authority: dict[str, tuple[str, str]], control_authority: ControlAuthority | None,
) -> list[str]:
    """Run only the baseline test files once every witness is re-proved; return reasons.

    The authority is re-read and the witnesses re-checked immediately before and after the run.
    """
    reasons: list[str] = []
    try:
        execution = PytestScope(scope.target, scope.roots, scope.config_path, scope.control_files,
                                tuple(target / path for path in sorted(baseline_hashes)))
        if authority and _read_authority(control_authority) != authority:
            return [_UNAVAILABLE + "control authority changed before regression execution"]
        if not _witnesses_unchanged(target, *witness):
            return [_UNAVAILABLE + "pytest witnesses changed before regression execution"]
        refused = _unwitnessed_importables(execution, execution.test_files)
        if refused:
            return [_UNAVAILABLE + "; ".join(refused)]
        passing_now, returncode, _findings = _pytest_passing(target, scope=execution)
        if authority and _read_authority(control_authority) != authority:
            reasons.append("control authority changed during regression execution")
        if not _witnesses_unchanged(target, *witness):
            reasons.append("pytest witnesses changed during regression execution")
        if passing_now is None:
            reasons.append("current passing tests undeterminable (pytest unavailable "
                           "or structured result missing or inconsistent)")
        else:
            lost = sorted(baseline_passing - passing_now)
            if lost:
                reasons.append(f"{len(lost)} baseline test(s) no longer passing "
                               f"(failed/skipped/xfailed/removed): {_fmt(lost)}")
            if returncode not in {0, 1}:
                reasons.append(f"protected pytest execution failed (exit {returncode})")
    except _EVIDENCE_ERRORS as exc:
        reasons.append(f"{_UNAVAILABLE}{exc}")
    return reasons


def _failed(details: str) -> CheckResult:
    return CheckResult(kind=CheckKind.TEST, name="regression-baseline", passed=False,
                       severity=Severity.ERROR, details=details)


def policy_approved_paths(run_dir: Path) -> set[str]:
    """Test paths whose recorded semantic review was approved by policy, with no signature."""
    review_dir = run_dir / "test-evolution"
    paths: set[str] = set()
    for record_file in sorted(review_dir.glob("*.review.json")):
        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
            token = review_dir / f"{record['review_id']}.decision.json"
            if approvals.policy_provenance(token) is not None:
                paths.add(str(record["path"]))
        except (KeyError, OSError, TypeError, ValueError):
            continue
    return paths


def _approvals_note(
    baseline_hashes: dict[str, str], now_hashes: dict[str, str],
    approved_transitions: dict[str, tuple[str, str]] | None,
    policy_approved: set[str] | None, reviewed: int,
) -> str:
    """Report reviewed transitions, naming the ones approved by policy without a signature."""
    reviewed_paths = {
        path for path, before in baseline_hashes.items()
        if path in now_hashes and now_hashes[path] != before
        and (approved_transitions or {}).get(path) == (before, now_hashes[path])
    }
    unsigned = sorted(reviewed_paths & (policy_approved or set()))
    note = f"{reviewed} exact signed semantic transition(s) approved"
    if unsigned:
        note = (
            f"{reviewed - len(unsigned)} exact signed semantic transition(s) approved; "
            f"{len(unsigned)} auto-approved by policy (no human signature): {_fmt(unsigned)}"
        )
    return note


def compare(
    target: Path,
    baseline: dict,
    *,
    approved_transitions: dict[str, tuple[str, str]] | None = None,
    base_commit: str | None = None,
    control_authority: ControlAuthority | None = None,
    policy_approved: set[str] | None = None,
) -> CheckResult:
    """Compare the current suite against *baseline*; a regression is an ERROR CheckResult.

    Two per-test signals, either of which fails the gate:
      * a baseline-passing test no longer passes (failed / skipped / xfailed / removed / renamed),
        checked per node-id so new trivial passing tests cannot inflate the count to mask it; and
      * a pre-existing test file was removed or modified (its regression-witness integrity can no
        longer be assumed — a gutted/weakened assertion hides behind a still-green node-id).

    *control_authority* is supplied by the caller, bound to this exact target and baseline. It
    returns ``{path: (base_sha256, current_sha256)}`` for the one executable control file whose
    exact change an external review authorized, or ``{}``; it is re-read before and after the
    protected execution, and any other changed control file still fails the gate.
    """
    reasons: list[str] = []
    try:
        target, baseline, witness, relocated = _effective_witness(target.resolve(), baseline)
        roots, baseline_passing, baseline_hashes = witness
        scope = resolve_pytest_scope(target)
        current_config = _config_file_sha256(scope.config_path)
    except (AttributeError, *_EVIDENCE_ERRORS) as exc:
        return _failed(f"invalid regression baseline: {exc}")
    if relocated:
        # The authority is bound to the declared target, never to a relocated scope.
        control_authority = None
    if scope.roots != roots:
        reasons.append("configured test scope changed from the immutable baseline")
    pinned_config = baseline.get("pytest_config_file_sha256")
    if "pytest_config_file_sha256" in baseline and pinned_config != current_config:
        reasons.append("pytest configuration changed from the immutable baseline")
    if not baseline_passing or not baseline_hashes:
        reasons.append("baseline has no immutable passing-test witness")
    try:
        now_hashes = _test_file_hashes(target, scope=scope)
        controls = _control_file_hashes(scope)
        authority = _read_authority(control_authority)
        expected_controls, drift = _expected_controls(
            target, baseline, roots, base_commit, current_config)
        reasons.extend(drift)
        expected_controls = _reconcile_control_authority(
            authority, expected_controls, controls, baseline_hashes, target, roots, base_commit)
        if _control_mismatches(controls, expected_controls, baseline_hashes,
                               now_hashes, approved_transitions or {}, scope):
            origin = "immutable baseline" if "control_file_hashes" in baseline else "base commit"
            reasons.append(f"pytest control files changed from the {origin}")
        data = _data_file_hashes(scope)
        reasons.extend(_data_file_reasons(
            _expected_data(target, baseline, roots, base_commit), data))
    except _EVIDENCE_ERRORS as exc:
        return _failed(f"{_UNAVAILABLE}{exc}")
    file_reasons, reviewed = _test_file_reasons(
        baseline_hashes, now_hashes, approved_transitions or {})
    reasons.extend(file_reasons)

    # Validate all executable witnesses BEFORE launching the protected suite. Every
    # baseline test file still runs; new tests run separately in the complete-suite gate.
    if not reasons:
        reasons = _run_protected_suite(
            target, scope, baseline_passing, baseline_hashes,
            (now_hashes, controls, current_config, data), authority, control_authority)
    if reasons:
        return _failed("regression vs baseline: " + "; ".join(reasons))
    approvals_note = _approvals_note(
        baseline_hashes, now_hashes, approved_transitions, policy_approved, reviewed)
    declared = baseline.get("declared_not_passing")
    unprotected = (f"; {len(declared)} declared not-passing test(s) recorded, not protected"
                   if isinstance(declared, list) and declared else "")
    return CheckResult(
        kind=CheckKind.TEST,
        name="regression-baseline",
        passed=True,
        severity=Severity.INFO,
        details=(
            f"no regression: all {len(baseline_passing)} baseline-passing test(s) still pass, "
            f"{len(baseline_hashes) - reviewed} pre-existing test file(s) unchanged; "
            f"{approvals_note}; "
            f"{len(authority)} verified executable control transition(s) approved{unprotected}"
        ),
    )


__all__ = [
    "BASELINE_FILE",
    "ControlAuthority",
    "RegressionError",
    "baseline_scope",
    "baseline_sha256",
    "checked_baseline",
    "baseline_candidate",
    "capture_baseline",
    "compare",
    "create_semantic_review_challenge",
    "legacy_control_witness",
    "load_baseline",
    "missing_baseline_check",
    "require_declared_not_passing",
    "validate_baseline",
    "policy_approved_paths",
    "verified_semantic_transitions",
]
