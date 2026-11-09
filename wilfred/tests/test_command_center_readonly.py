"""Command center: the read-only proof and the forbidden-import guard.

Both are mandatory from S2.1 on and must keep passing in every later session.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path

import pytest
from ops_fakes import export_report, fake_ops, status_report, task_entry

from hyperwilfred_core import cli
from hyperwilfred_core.command_center import snapshot

PACKAGE = (
    Path(__file__).resolve().parent.parent / "src" / "hyperwilfred_core" / "command_center"
)

FORBIDDEN_NETWORK = (
    "http", "socket", "requests", "ftplib", "smtplib",  # named by the specification
    "ssl", "asyncio", "socketserver", "xmlrpc", "telnetlib", "poplib", "imaplib", "nntplib",
    "httpx", "aiohttp", "urllib3", "websockets",
)
# Loading a module by name would hide it from this scanner.
FORBIDDEN_DYNAMIC = ("importlib", "runpy", "ctypes", "multiprocessing", "pty")
# The only module allowed to write (and only into the output directory).
WRITER = "snapshot.py"
# The only modules allowed to start a process, each through its own argument allowlist.
PROCESS_MODULES = ("gitread.py", "opsread.py")
FORBIDDEN_ENGINE = ("engine", "quality", "gates", "resume", "observability", "learning", "review")


@pytest.fixture(autouse=True)
def _hermetic_memory(tmp_path_factory, monkeypatch) -> Path:
    """No test reads the real shared memory: the default points at a small fixture tree."""
    root = tmp_path_factory.mktemp("memory")
    (root / "MEMORY.md").write_text("- [Note](note.md) — hook\n", encoding="utf-8")
    (root / "note.md").write_text(
        "---\nname: note\ndescription: d\nmetadata:\n  type: project\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(snapshot, "default_memory_dir", lambda: root)
    return root


def _fingerprint(root: Path) -> dict[str, tuple]:
    """Every file (SHA-256, size, mode, mtime) and every directory (listing, mode, mtime).

    A directory's mtime changes when an entry is created or removed in it, so a lock or
    temporary file that appeared and vanished again is detected too.
    """
    seen: dict[str, tuple] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        here = Path(current)
        info = here.lstat()
        seen[str(here)] = ("dir", tuple(sorted(os.listdir(here))), info.st_mode, info.st_mtime_ns)
        for name in sorted(files):
            path = here / name
            info = path.lstat()
            if path.is_symlink():
                seen[str(path)] = ("link", os.readlink(path))
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                seen[str(path)] = ("file", digest, info.st_size, info.st_mode, info.st_mtime_ns)
    return seen


def _touch_later(path: Path) -> None:
    """Make a tracked file "stat-dirty": same content, newer mtime than the index recorded.

    This is the state in which a plain ``git status`` rewrites ``.git/index`` and in which it
    runs a configured content filter to compare the file with the index.
    """
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000_000))


def _init(git, path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "config", "core.hooksPath", str(path / ".git" / "no-hooks"))
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "gc.auto", "0")
    (path / "README.md").write_text("# project\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


def _build_workspace(tmp_path: Path, git) -> tuple[Path, Path]:
    """Two repositories, a worktree, a run store — and every bait for an accidental write.

    Returns (workspace, sentinel). ``sentinel`` lies outside the workspace and is created by
    the content filter of repository ``one`` whenever git runs that filter.
    """
    workspace = tmp_path / "workspace"
    sentinel = tmp_path / "filter-was-executed"
    one = _init(git, workspace / "one")
    two = _init(git, workspace / "group" / "two")
    # Uncommitted work: a modified tracked file, a staged file and an untracked file.
    (one / "README.md").write_text("# project\nchanged\n", encoding="utf-8")
    (one / "staged.txt").write_text("s\n", encoding="utf-8")
    git(one, "add", "staged.txt")
    (one / "untracked.txt").write_text("u\n", encoding="utf-8")
    git(one, "remote", "add", "origin", "https://user:token@example.com/o/one.git")
    git(one, "worktree", "add", "-q", "-b", "side", str(workspace / "one-side"))
    # Bait 1: a content filter (what git-lfs installs) on a stat-dirty tracked file.
    (one / ".gitattributes").write_text("*.dat filter=probe\n", encoding="utf-8")
    (one / "data.dat").write_text("payload\n", encoding="utf-8")
    git(one, "config", "filter.probe.clean", f"touch '{sentinel}' && cat")
    git(one, "config", "filter.probe.required", "true")
    git(one, "add", ".gitattributes", "data.dat")
    git(one, "commit", "-q", "-m", "filtered file")
    _touch_later(one / "data.dat")
    # Bait 2: a stat-dirty tracked file, which makes a plain ``git status`` rewrite the index.
    git(two, "checkout", "-q", "--detach")
    _touch_later(two / "README.md")
    store = one / ".hw"
    good = store / "run-good"
    good.mkdir(parents=True)
    (good / "state.json").write_text(
        json.dumps({"run_id": "run-good", "mode": "build", "tier": "full", "status": "completed",
                    "step_status": {"plan": "done"}, "extra": True}),
        encoding="utf-8",
    )
    (good / "events.jsonl").write_text(
        '{"timestamp": "2026-09-01T10:00:00+00:00", "stage": "plan", "status": "running", '
        '"message": "started"}\n{corrupt line\n',
        encoding="utf-8",
    )
    (good / "usage.json").write_text(json.dumps({"totals": {"input_tokens": 5}}), encoding="utf-8")
    gates = good / "gates"
    gates.mkdir()
    ship = {"kind": "deploy", "action": "x"}
    (gates / "ship.json").write_text(json.dumps(ship), encoding="utf-8")
    (gates / "ship.pending").write_text("pending\n", encoding="utf-8")
    (gates / "ship.challenge.json").write_text(
        json.dumps({"expires_at": "2026-09-01T11:00:00+00:00"}), encoding="utf-8"
    )
    (gates / "old.pending").write_text("pending\n", encoding="utf-8")
    (gates / "old.json").write_text(json.dumps({"kind": "deploy"}), encoding="utf-8")
    (gates / "old.decision.json").write_text("signed-token-never-read", encoding="utf-8")
    (gates / "old.decision.consumed").write_text("", encoding="utf-8")
    bad = store / "run-bad"
    bad.mkdir()
    (bad / "state.json").write_text("{corrupt", encoding="utf-8")
    (store / ".lock").write_text("", encoding="utf-8")
    (store / "observability").mkdir()
    sentinel.unlink(missing_ok=True)  # ``git add`` above ran the filter legitimately
    return workspace, sentinel


def _build_memory(tmp_path: Path) -> Path:
    memory = tmp_path / "shared-memory"
    (memory / "projects" / "one").mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- [Good](good.md) — hook\n", encoding="utf-8")
    (memory / "good.md").write_text(
        "---\nname: good\ndescription: d\nmetadata:\n  type: project\n---\nPRIVATE-BODY\n",
        encoding="utf-8",
    )
    (memory / "projects" / "one" / "bad.md").write_text("no frontmatter\n", encoding="utf-8")
    return memory


def test_snapshot_changes_nothing_under_the_workspace(tmp_path, git_helper, capsys) -> None:
    workspace, sentinel = _build_workspace(tmp_path, git_helper)
    memory = _build_memory(tmp_path)
    out_dir = tmp_path / "out"
    before = _fingerprint(workspace)
    memory_before = _fingerprint(memory)
    assert any("/.git/" in key for key in before), "the proof must cover .git"
    assert any("/gates/" in key for key in before) and any("events.jsonl" in k for k in before)

    result = snapshot.run_snapshot(workspace=workspace, out_dir=out_dir, memory_dir=memory)
    assert _fingerprint(workspace) == before
    assert _fingerprint(memory) == memory_before
    assert not sentinel.exists(), "git executed a configured content filter"

    # The same through the CLI, twice, to cover a re-run over an existing snapshot.
    for _ in range(2):
        assert cli.main(["command-center", "snapshot", "--workspace", str(workspace),
                         "--out-dir", str(out_dir), "--memory", str(memory)]) == 0
    capsys.readouterr()
    assert _fingerprint(workspace) == before
    assert _fingerprint(memory) == memory_before
    assert not sentinel.exists()

    # The fixture was really read: status ran in every project and saw the uncommitted work.
    written = json.loads((out_dir / "snapshot.json").read_text(encoding="utf-8"))
    by_name = {p["name"]: p for p in written["projects"]}
    assert by_name["one"]["uncommitted_paths"] >= 3
    assert by_name["two"]["uncommitted_paths"] == 0 and by_name["two"]["branch"] is None
    assert by_name["one-side"]["kind"] == "worktree"
    assert result["totals"]["repositories"] == 2
    assert result["totals"]["projects"] == 4  # workspace folder + 2 repositories + 1 worktree
    assert result["totals"]["runs"] == 2
    # Two problems: the corrupt state.json and the corrupt line in events.jsonl.
    assert result["problems"] == 2, written["problems"]
    good = next(r for r in written["runs"] if r["run_id"] == "run-good")
    assert good["event_count"] == 1 and good["cost"]["state"] == "recorded"
    assert [g["gate_id"] for g in good["pending_gates"]] == ["ship"]
    assert written["memory"]["notes"] == 2 and len(written["memory"]["malformed"]) == 1
    text = (out_dir / "snapshot.json").read_text(encoding="utf-8")
    assert "PRIVATE-BODY" not in text, "a memory note body leaked into the snapshot"
    assert "signed-token-never-read" not in text
    assert sorted(os.listdir(out_dir)) == ["snapshot.json"]


def test_the_proof_has_teeth_a_plain_git_status_is_detected(tmp_path, git_helper) -> None:
    """Negative control: the same fixture DOES change when git is called without protection.

    If this test fails, the fixture no longer provokes a write and the proof above would pass
    for a collector that is not read-only.
    """
    workspace, sentinel = _build_workspace(tmp_path, git_helper)
    before = _fingerprint(workspace)
    git_helper(workspace / "group" / "two", "status", "--porcelain")
    assert _fingerprint(workspace) != before, "plain git status no longer rewrites the index"
    git_helper(workspace / "one", "status", "--porcelain")
    assert sentinel.exists(), "plain git status no longer runs the content filter"


def test_filters_defined_in_the_user_configuration_are_not_executed(
    tmp_path, git_helper, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    sentinel = tmp_path / "user-filter-was-executed"
    (home / ".gitconfig").write_text(
        f"[filter \"userwide\"]\n\tclean = touch '{sentinel}' && cat\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    workspace = tmp_path / "workspace"
    repo = _init(git_helper, workspace / "repo")
    (repo / ".gitattributes").write_text("*.bin filter=userwide\n", encoding="utf-8")
    (repo / "blob.bin").write_text("payload\n", encoding="utf-8")
    git_helper(repo, "add", "-A")
    git_helper(repo, "commit", "-q", "-m", "filtered")
    _touch_later(repo / "blob.bin")
    sentinel.unlink(missing_ok=True)
    before = _fingerprint(workspace)

    result = snapshot.run_snapshot(workspace=workspace, out_dir=tmp_path / "out")
    assert not sentinel.exists(), "git executed a filter from the user configuration"
    assert _fingerprint(workspace) == before
    assert result["problems"] == 0

    git_helper(repo, "status", "--porcelain")  # negative control
    assert sentinel.exists()


def _imports(path: Path) -> list[str]:
    """Every module a file imports; ``from a import b`` is reported as both ``a`` and ``a.b``."""
    modules: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                modules.append("<relative>")
                continue
            modules.append(node.module)
            modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _import_violations(path: Path) -> list[str]:
    offenders: list[str] = []
    for module in _imports(path):
        parts = module.split(".")
        if module == "<relative>":
            # Absolute imports only, so this scanner sees every dependency by its full name.
            offenders.append(f"{path.name}: relative import")
        elif parts[0] in FORBIDDEN_NETWORK or parts[0] in FORBIDDEN_DYNAMIC:
            offenders.append(f"{path.name}: {module}")
        elif parts[0] == "urllib" and parts[:2] != ["urllib", "parse"]:
            offenders.append(f"{path.name}: {module}")
        elif parts[0] == "hyperwilfred_core" and len(parts) > 1 and parts[1] in FORBIDDEN_ENGINE:
            offenders.append(f"{path.name}: {module}")
        elif parts[0] == "subprocess" and path.name not in PROCESS_MODULES:
            offenders.append(f"{path.name}: {module}")
    return offenders


_WRITE_METHODS = (
    ".write_text", ".write_bytes", ".mkdir", ".unlink", ".rmdir", ".rename", ".touch",
    ".symlink_to", ".hardlink_to", ".chmod", ".lchmod",
)
_WRITE_CALLS = {
    "open", "os.open", "os.fdopen", "os.replace", "os.remove", "os.rename", "os.unlink",
    "os.mkdir", "os.makedirs", "os.rmdir", "os.chmod", "os.utime", "os.symlink", "os.link",
    "os.truncate",
}


def _call_violations(path: Path) -> list[str]:
    """Shell or dynamic-code use anywhere; any write call outside the writer module."""
    offenders: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "subprocess":
            # e.g. ``gitread.subprocess.run`` reached from another module
            if path.name not in PROCESS_MODULES:
                offenders.append(f"{path.name}: reaches subprocess through an attribute")
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name in {"os.system", "os.popen", "eval", "exec", "compile", "__import__"} or \
                name.startswith(("os.spawn", "os.exec", "os.posix_spawn", "os.fork")):
            offenders.append(f"{path.name}: calls {name}")
        if any(keyword.arg == "shell" for keyword in node.keywords):
            offenders.append(f"{path.name}: passes shell= to {name}")
        writes = (
            name in _WRITE_CALLS
            or name.endswith(_WRITE_METHODS)
            or name.startswith(("shutil.", "tempfile."))
        )
        if writes and path.name != WRITER:
            offenders.append(f"{path.name}: write call {name}")
    return offenders


def _package_files() -> list[Path]:
    files = sorted(PACKAGE.rglob("*.py"))
    assert len(files) >= 6, "the command_center package was not found"
    return files


def test_package_imports_no_network_no_engine_and_subprocess_only_in_gitread() -> None:
    offenders = [v for path in _package_files() for v in _import_violations(path)]
    assert offenders == [], f"forbidden imports in command_center: {offenders}"
    gitread_imports = _imports(PACKAGE / "gitread.py")
    assert "subprocess" in gitread_imports, "the scanner no longer sees gitread's subprocess"


def test_package_never_uses_a_shell_and_only_the_writer_module_writes() -> None:
    offenders = [v for path in _package_files() for v in _call_violations(path)]
    assert offenders == [], f"shell, dynamic code or write call in command_center: {offenders}"
    writer_calls = {
        ast.unparse(node.func)
        for node in ast.walk(ast.parse((PACKAGE / WRITER).read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
    }
    assert "os.replace" in writer_calls, "the scanner no longer sees the writer's os.replace"


def test_the_import_guard_itself_detects_each_forbidden_form(tmp_path: Path) -> None:
    """A scanner that silently stopped matching would make the guard above worthless."""
    bad = [
        "import http.client",
        "import socket",
        "import requests",
        "import ftplib",
        "import smtplib",
        "import urllib",
        "import urllib.request",
        "from urllib import request",
        "from urllib import error",
        "from urllib.error import URLError",
        "from hyperwilfred_core import engine",
        "import hyperwilfred_core.quality",
        "from hyperwilfred_core.gates import GateError",
        "from hyperwilfred_core import resume",
        "from hyperwilfred_core.observability import build_dashboard",
        "from hyperwilfred_core import learning",
        "import hyperwilfred_core.review as r",
        "import subprocess",
        "from subprocess import run",
        "import ssl",
        "import asyncio",
        "from importlib import import_module",
        "import importlib",
        "from . import model",
    ]
    for line in bad:
        probe = tmp_path / "collect_probe.py"
        probe.write_text(line + "\n", encoding="utf-8")
        assert _import_violations(probe), f"not detected: {line}"
    good = tmp_path / "gitread.py"
    good.write_text(
        "import subprocess\nfrom urllib.parse import urlsplit\nimport urllib.parse\n"
        "from hyperwilfred_core import __version__\nimport json\nimport os\n"
        "from hyperwilfred_core.command_center.model import Run\n",
        encoding="utf-8",
    )
    assert _import_violations(good) == []
    shelly = tmp_path / "shelly.py"
    shelly.write_text(
        "import os\nos.system('x')\nrun(['a'], shell=True)\ngitread.subprocess.run(['a'])\n"
        "path.write_text('x')\nopen('f', 'w')\nos.replace('a', 'b')\nshutil.rmtree('d')\n",
        encoding="utf-8",
    )
    assert len(_call_violations(shelly)) == 7


def test_build_changes_nothing_under_the_workspace(tmp_path, git_helper, capsys) -> None:
    """S2.3: ``build`` (snapshot + page) keeps the read-only proof."""
    workspace, sentinel = _build_workspace(tmp_path, git_helper)
    memory = _build_memory(tmp_path)
    out_dir = tmp_path / "out"
    before = _fingerprint(workspace)
    memory_before = _fingerprint(memory)
    for _ in range(2):
        assert cli.main(["command-center", "build", "--workspace", str(workspace),
                         "--out-dir", str(out_dir), "--memory", str(memory)]) == 0
    assert cli.main(["command-center", "render", "--out-dir", str(out_dir)]) == 0
    capsys.readouterr()
    assert _fingerprint(workspace) == before
    assert _fingerprint(memory) == memory_before
    assert not sentinel.exists()
    assert sorted(os.listdir(out_dir)) == ["index.html", "snapshot.json"]
    page = (out_dir / "index.html").read_text(encoding="utf-8")
    assert "PRIVATE-BODY" not in page and "signed-token-never-read" not in page


def test_only_the_two_readers_may_start_a_process(tmp_path: Path) -> None:
    """The guard names exactly gitread.py and opsread.py; any other module is still refused."""
    assert PROCESS_MODULES == ("gitread.py", "opsread.py")
    assert "subprocess" in _imports(PACKAGE / "opsread.py"), "the scanner no longer sees opsread"
    for name in ("collect_ops.py", "snapshot.py", "attention.py"):
        probe = tmp_path / name
        probe.write_text("import subprocess\nopsread.subprocess.run(['x'])\n", encoding="utf-8")
        assert _import_violations(probe), name
        assert _call_violations(probe), name


def test_snapshot_with_an_ops_repository_changes_nothing_under_the_workspace(
    tmp_path, git_helper, capsys
) -> None:
    """The ops reader keeps the proof: nothing changes, the ops repository included."""
    workspace, sentinel = _build_workspace(tmp_path, git_helper)
    ops_repo = _init(git_helper, workspace / "ops")
    (ops_repo / "tasks").mkdir()
    (ops_repo / "tasks" / "T-0001-x.md").write_text("---\nid: T-0001\n---\n", encoding="utf-8")
    git_helper(ops_repo, "add", "-A")
    git_helper(ops_repo, "commit", "-q", "-m", "task")
    _touch_later(ops_repo / "tasks" / "T-0001-x.md")
    memory = _build_memory(tmp_path)
    program = fake_ops(
        tmp_path / "fake-ops",
        export=export_report(root=str(workspace / "ops"), tasks=[task_entry("T-0001", "Blocked")]),
        status=status_report(uncommitted=[], unapproved_sales=[], unapproved_edits=[]),
    )
    out_dir = tmp_path / "out"
    before = _fingerprint(workspace)
    for _ in range(2):
        assert cli.main(["command-center", "build", "--workspace", str(workspace),
                         "--out-dir", str(out_dir), "--memory", str(memory),
                         "--ops-bin", str(program)]) == 0
    capsys.readouterr()
    assert _fingerprint(workspace) == before
    assert not sentinel.exists()
    written = json.loads((out_dir / "snapshot.json").read_text(encoding="utf-8"))
    assert [task["id"] for task in written["ops"]["open_tasks"]] == ["T-0001"]
    seen = (tmp_path / "fake-ops" / "env-status.txt").read_text(encoding="utf-8")
    assert "PYTHONDONTWRITEBYTECODE=1" in seen and "GIT_OPTIONAL_LOCKS=0" in seen
    args = (tmp_path / "fake-ops" / "args-status.txt").read_text(encoding="utf-8")
    assert args.splitlines() == ["status", "--json", "--root", written["ops"]["root"]]
