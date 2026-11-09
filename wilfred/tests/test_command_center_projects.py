"""Command center: the allowlisted git reader and the projects collector."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hyperwilfred_core.command_center import gitconfig, gitread
from hyperwilfred_core.command_center.collect_projects import collect_projects
from hyperwilfred_core.command_center.model import Project


def _init(git, path: Path, *, commit: bool = True) -> Path:
    """A hermetic repository at ``path`` (no global hooks, no signing, no auto-gc)."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "config", "core.hooksPath", str(path / ".git" / "no-hooks"))
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "gc.auto", "0")
    if commit:
        (path / "README.md").write_text("# project\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "initial")
    return path


def _by_id(projects: list[Project]) -> dict[str, Project]:
    return {p.id: p for p in projects}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


# ----------------------------------------------------------------------------- git allowlist


@pytest.mark.parametrize(
    "args",
    [
        ["gc"],
        ["status"],
        ["status", "--porcelain", "--output=/tmp/x"],
        ["branch", "--format=%(refname)", "new-branch"],
        ["branch", "-D", "main"],
        ["log", "-1", "--format=%H", "--output=/tmp/x"],
        ["log", "--format=%H"],
        ["log", "-1", "--format=%G?"],
        ["log", "-1", "--format=%H"],
        ["for-each-ref", "--format=%(describe)", "refs/heads"],
        ["for-each-ref", gitread.REFS_FORMAT, "--exec=x"],
        ["branch", "--format=%(contents:signature)"],
        ["status", "--porcelain", "--porcelain=v2"],
        ["worktree", "prune"],
        ["worktree", "add", "x"],
        ["remote", "add", "x", "y"],
        ["rev-parse", "--output"],
        ["rev-list", "--left-right", "--count", "--all"],
        ["for-each-ref", "--format=%(refname)", "--shell"],
        ["fetch"],
        ["config", "user.name", "x"],
        [],
    ],
)
def test_allowlist_rejects_before_any_process(args, tmp_path, monkeypatch) -> None:
    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("a process was started for a call outside the allowlist")

    monkeypatch.setattr(gitread.subprocess, "run", boom)
    with pytest.raises(ValueError):
        gitread.run_git(tmp_path, args)


def test_allowlist_rejects_git_gc(git_repo: Path) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        gitread.run_git(git_repo, ["gc"])


def test_run_git_disables_optional_locks_and_never_uses_a_shell(git_repo, monkeypatch) -> None:
    seen: dict = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return real_run(argv, **kwargs)

    monkeypatch.setenv("GIT_DIR", "/nonexistent/redirect")
    monkeypatch.setattr(gitread.subprocess, "run", spy)
    result = gitread.run_git(git_repo, ["rev-parse", "--verify", "--quiet", "HEAD"])
    assert result.ok and len(result.stdout.strip()) == 40
    assert seen["argv"][:2] == ("git", "--no-optional-locks")
    assert "--no-pager" in seen["argv"]
    assert "core.fsmonitor=false" in seen["argv"] and "log.showSignature=false" in seen["argv"]
    env = seen["kwargs"]["env"]
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_NO_LAZY_FETCH"] == "1" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
    assert "GIT_DIR" not in seen["kwargs"]["env"]
    assert seen["kwargs"]["timeout"] == 15
    assert seen["kwargs"]["check"] is False
    assert "shell" not in seen["kwargs"]
    assert result.command.startswith("git --no-optional-locks ")


def test_timeout_and_missing_git_come_back_as_results(git_repo, monkeypatch) -> None:
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 15)

    monkeypatch.setattr(gitread.subprocess, "run", timeout)
    result = gitread.run_git(git_repo, ["remote", "-v"])
    assert result.timed_out and not result.ok and "timed out" in result.failure_reason

    def missing(argv, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(gitread.subprocess, "run", missing)
    result = gitread.run_git(git_repo, ["remote", "-v"])
    assert not result.ok and "could not be started" in result.failure_reason


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:s3cret@example.com/org/repo.git", "https://example.com/org/repo.git"),
        ("https://ghp_token@example.com/org/repo.git", "https://example.com/org/repo.git"),
        ("http://u:p@example.com:8080/r.git", "http://example.com:8080/r.git"),
        ("ssh://git:pw@example.com/org/repo.git", "ssh://git@example.com/org/repo.git"),
        ("ssh://git@example.com/org/repo.git", "ssh://git@example.com/org/repo.git"),
        ("git@github.com:org/repo.git", "git@github.com:org/repo.git"),
        ("https://example.com/org/repo.git", "https://example.com/org/repo.git"),
        ("/local/path/repo.git", "/local/path/repo.git"),
        ("https://user:pa/ss@host/repo", "https://host/repo"),
        ("https://user:p#ss@host/x", "https://host/x"),
        ("https://user:p?ss@host/x", "https://host/x"),
        ("https://host/x.git?access_token=SECRET", "https://host/x.git"),
        ("https://u:p@host/x?token=1#frag", "https://host/x"),
        ("user:pw@host:path", "user@host:path"),
        ("git+https://tok:x@h/r", "git+https://h/r"),
        ("https://host/path@v2", "https://host/path@v2"),
        ("https://host:8443/a@b", "https://host:8443/a@b"),
        ("file:///tmp/x.git", "file:///tmp/x.git"),
    ],
)
def test_strip_credentials(url: str, expected: str) -> None:
    assert gitread.strip_credentials(url) == expected


# ------------------------------------------------------------------------- projects collector


def test_repository_without_commits(workspace: Path, git_helper) -> None:
    _init(git_helper, workspace / "empty", commit=False)
    projects, problems = collect_projects(workspace)
    project = _by_id(projects)["empty"]
    assert problems == []
    assert project.kind == "repository"
    assert project.has_commits is False
    assert project.head is None and project.branch is None
    assert project.last_commit_at is None and project.local_branches == 0


def test_detached_head(workspace: Path, git_helper) -> None:
    repo = _init(git_helper, workspace / "detached")
    git_helper(repo, "checkout", "-q", "--detach")
    projects, problems = collect_projects(workspace)
    project = _by_id(projects)["detached"]
    assert problems == []
    assert project.branch is None
    assert project.has_commits is True
    assert project.head == git_helper(repo, "rev-parse", "HEAD").strip()
    assert project.ahead is None and project.behind is None


def test_branch_without_upstream_and_ahead_count(workspace: Path, tmp_path, git_helper) -> None:
    remote = tmp_path / "remotes" / "origin.git"
    remote.mkdir(parents=True)
    git_helper(remote, "init", "-q", "--bare")
    repo = _init(git_helper, workspace / "app")
    current = git_helper(repo, "branch", "--show-current").strip()
    git_helper(repo, "remote", "add", "origin", str(remote))
    git_helper(repo, "push", "-q", "-u", "origin", current)
    git_helper(repo, "branch", "feature-local")
    (repo / "more.txt").write_text("x\n", encoding="utf-8")
    git_helper(repo, "add", "-A")
    git_helper(repo, "commit", "-q", "-m", "second")
    (repo / "untracked.txt").write_text("y\n", encoding="utf-8")

    projects, problems = collect_projects(workspace)
    project = _by_id(projects)["app"]
    assert problems == []
    assert project.branch == current
    assert project.local_branches == 2
    assert project.branches_without_upstream == ["feature-local"]
    assert (project.ahead, project.behind) == (1, 0)
    assert project.uncommitted_paths == 1
    assert project.last_commit_at is not None
    assert [r.name for r in project.remotes] == ["origin"]


def test_worktree_whose_directory_is_missing(workspace: Path, tmp_path, git_helper) -> None:
    repo = _init(git_helper, workspace / "app")
    gone = tmp_path / "outside" / "gone"
    git_helper(repo, "worktree", "add", "-q", "-b", "gone-branch", str(gone))
    shutil.rmtree(gone)

    projects, problems = collect_projects(workspace)
    project = _by_id(projects)["app"]
    assert problems == []
    assert len(project.worktrees) == 1
    worktree = project.worktrees[0]
    assert worktree.exists is False and worktree.prunable is True
    assert worktree.branch == "gone-branch"
    assert [p.kind for p in projects].count("worktree") == 0


def test_worktrees_become_projects_except_inside_a_run_store(
    workspace: Path, tmp_path, git_helper
) -> None:
    repo = _init(git_helper, workspace / "app")
    outside = tmp_path / "outside" / "feature"
    git_helper(repo, "worktree", "add", "-q", "-b", "feature", str(outside))
    in_store = repo / ".hw" / "run1" / "worktrees" / "s0"
    git_helper(repo, "worktree", "add", "-q", "-b", "hw-s0", str(in_store))

    projects, problems = collect_projects(workspace)
    assert problems == []
    by_path = {p.path: p for p in projects}
    app = by_path[str(repo)]
    flags = {Path(w.path).name: w.inside_run_store for w in app.worktrees}
    assert flags == {"feature": False, "s0": True}
    linked = by_path[str(outside)]
    assert linked.kind == "worktree"
    assert linked.parent_repository == app.id
    assert linked.branch == "feature"
    assert linked.worktrees == []
    assert str(in_store) not in by_path


def test_remote_url_with_credentials_is_stripped(workspace: Path, git_helper) -> None:
    repo = _init(git_helper, workspace / "app")
    git_helper(repo, "remote", "add", "origin", "https://owner:t0k3n-secret@example.com/o/r.git")
    projects, _ = collect_projects(workspace)
    project = _by_id(projects)["app"]
    assert [(r.name, r.url) for r in project.remotes] == [("origin", "https://example.com/o/r.git")]
    assert "t0k3n-secret" not in project.model_dump_json()


def test_discovery_depth_skips_and_ids(workspace: Path, git_helper) -> None:
    _init(git_helper, workspace / "a" / "b" / "deep3")
    _init(git_helper, workspace / "a" / "b" / "c" / "deep4")
    _init(git_helper, workspace / "node_modules" / "dep")
    _init(git_helper, workspace / "x" / "_backups" / "old")
    _init(git_helper, workspace / "Private" / "hidden")
    _init(git_helper, workspace / "top")

    projects, problems = collect_projects(workspace)
    assert problems == []
    assert [p.id for p in projects] == [".", "a__b__deep3", "top"]
    assert projects[0].kind == "folder" and projects[0].path == str(workspace.resolve())


def test_nested_repository_names_its_parent(workspace: Path, git_helper) -> None:
    _init(git_helper, workspace)
    _init(git_helper, workspace / "group" / "child")
    projects, problems = collect_projects(workspace)
    assert problems == []
    by_id = _by_id(projects)
    assert by_id["."].kind == "repository" and by_id["."].parent_repository is None
    assert by_id["group__child"].parent_repository == "."


def test_unusable_git_directory_is_a_problem_not_a_silent_skip(workspace: Path) -> None:
    (workspace / "broken" / ".git").mkdir(parents=True)
    projects, problems = collect_projects(workspace)
    assert _by_id(projects)["broken"].kind == "folder"
    assert len(problems) == 1 and problems[0].path == str((workspace / "broken").resolve())


def test_git_timeout_becomes_a_problem(workspace: Path, git_helper, monkeypatch) -> None:
    _init(git_helper, workspace / "slow")

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 15)

    monkeypatch.setattr(gitread.subprocess, "run", timeout)
    projects, problems = collect_projects(workspace)
    assert problems and all("timed out" in p.reason for p in problems)
    # git never answered whether this is a repository, so it is listed as a plain folder and
    # nothing about it reads as "clean" or "empty".
    slow = _by_id(projects)["slow"]
    assert slow.kind == "folder"
    assert slow.has_commits is None and slow.uncommitted_paths is None
    assert slow.local_branches is None and slow.branches_without_upstream is None


def test_a_failed_git_call_leaves_its_facts_unknown_not_zero(
    workspace: Path, git_helper, monkeypatch
) -> None:
    _init(git_helper, workspace / "app")
    real_run = subprocess.run

    def flaky(argv, **kwargs):
        if "status" in argv or "for-each-ref" in argv:
            raise subprocess.TimeoutExpired(argv, 15)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gitread.subprocess, "run", flaky)
    projects, problems = collect_projects(workspace)
    app = _by_id(projects)["app"]
    assert app.kind == "repository" and app.has_commits is True and app.head
    assert app.uncommitted_paths is None and app.branch is None
    assert app.local_branches is None and app.branches_without_upstream is None
    assert len(problems) == 2 and all("timed out" in p.reason for p in problems)
    assert not {"uncommitted_paths", "local_branches"} & {s.field for s in app.sources}


def test_every_project_fact_names_its_git_command(workspace: Path, git_helper) -> None:
    _init(git_helper, workspace / "app")
    projects, _ = collect_projects(workspace)
    project = _by_id(projects)["app"]
    fields = {s.field for s in project.sources}
    assert {"head", "branch", "remotes", "local_branches", "last_commit_at", "worktrees",
            "uncommitted_paths"} <= fields
    for source in project.sources:
        assert source.kind == "git" and source.path == project.path
        assert source.command and source.command.startswith("git --no-optional-locks ")


def test_workspace_typed_in_another_letter_case(tmp_path: Path, git_helper) -> None:
    workspace = tmp_path / "WorkSpace"
    _init(git_helper, workspace / "app")
    typed = tmp_path / "workspace"
    if not typed.exists():
        pytest.skip("case-sensitive filesystem: the two spellings are different directories")
    projects, problems = collect_projects(typed)
    assert problems == []
    assert projects[0].path == str(workspace.resolve())
    assert _by_id(projects)["app"].kind == "repository"


def test_symbolic_link_to_a_repository_is_reported(workspace: Path, tmp_path, git_helper) -> None:
    target = _init(git_helper, tmp_path / "elsewhere" / "repo")
    (workspace / "linked").symlink_to(target, target_is_directory=True)
    (workspace / "plain-link").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    projects, problems = collect_projects(workspace)
    assert [p.id for p in projects] == ["."]
    assert len(problems) == 1 and problems[0].path.endswith("/linked")
    assert "links are not followed" in problems[0].reason


def test_worktree_under_a_secrets_directory_is_not_read(workspace, tmp_path, git_helper) -> None:
    repo = _init(git_helper, workspace / "app")
    hidden = tmp_path / "secrets" / "wt"
    git_helper(repo, "worktree", "add", "-q", "-b", "side", str(hidden))
    projects, problems = collect_projects(workspace)
    assert str(hidden.resolve()) not in {p.path for p in projects}
    assert len(problems) == 1 and "'secrets'" in problems[0].reason


def test_project_ids_stay_unique(workspace: Path, git_helper) -> None:
    _init(git_helper, workspace / "a" / "b")
    _init(git_helper, workspace / "a__b")
    projects, problems = collect_projects(workspace)
    ids = [p.id for p in projects]
    assert len(set(ids)) == len(ids) == 3
    assert "a__b" in ids and "a__b~2" in ids
    assert [p.what for p in problems] == ["project id"]


# ----------------------------------------------------------- content filters are never executed


def _git_oracle(config: Path) -> set[str]:
    """What git itself says: the filter names in ``config`` and everything it includes."""
    done = subprocess.run(
        ["git", "config", "--file", str(config), "--includes", "--name-only", "--list"],
        capture_output=True, text=True, check=True,
    )
    return {
        line[len("filter."):].rsplit(".", 1)[0]
        for line in done.stdout.splitlines()
        if line.startswith("filter.")
    }


_CONFIG_VARIANTS = {
    "plain": '[filter "probe"]\n\tclean = x\n',
    "mixed-case-section": '[Filter "Mixed.Case"] smudge = cat\n',
    "dotted-deprecated": "[filter.PROBE]\n\tclean = y\n",
    "bom": '\ufeff[filter "probe"]\n\tclean = x\n',
    "crlf": '[filter "probe"]\r\n\tclean = x\r\n',
    "escaped-subsection": '[filter "a\\"b"]\n\tclean = x\n',
    "after-continued-value": '[alias]\n\tx = a \\\n b\n[filter "probe"]\n\tclean = x\n',
    "include": "[include]\n\tpath = extra.inc\n",
    "include-comment-semicolon": "[include]\n\tpath = extra.inc ; note\n",
    "include-comment-hash": "[include]\n\tpath = extra.inc\t# c\n",
    "include-on-header-line": "[include] path = extra.inc # c\n",
    "include-continuation": "[include]\n\tpath = ex\\\ntra.inc\n",
    "include-inner-quotes": '[include]\n\tpath = ex"tra".inc\n',
    "include-quoted": '[include]\n\tpath = "extra.inc"\n',
    "include-if": '[includeIf "gitdir:/nowhere/"]\n\tpath = extra.inc\n',
    "include-missing": "[include]\n\tpath = missing.inc\n[core]\n\tbare = false\n",
    "two-filters": '[filter "one"]\n\tclean = x\n[core]\n\ta = b\n[filter "two"]\n\tsmudge = y\n',
    "comment-backslash-and-header-in-value":
        '# note \\\n[filter "probe"]\n\tclean = x\n[core]\n\tx = [filter "y"]\n',
    "no-filter": '[core]\n\tbare = false\n[remote "origin"]\n\turl = https://e.com/x.git\n',
}


@pytest.mark.parametrize("variant", sorted(_CONFIG_VARIANTS))
def test_filter_names_match_what_git_reads(variant: str, tmp_path: Path) -> None:
    (tmp_path / "extra.inc").write_text(
        '\ufeff[filter "from-include"]\n\tclean = x\n', encoding="utf-8"
    )
    config = tmp_path / "config"
    config.write_bytes(_CONFIG_VARIANTS[variant].encode("utf-8"))
    names, unverified = gitconfig.filter_names([config, tmp_path / "absent"])
    expected = _git_oracle(config)
    if variant == "include-if":
        expected = expected | {"from-include"}  # conditional includes are always followed
    # Either the scanner knows every filter git knows, or it says it could not verify.
    assert unverified or names >= expected, (names, expected)
    assert unverified == [], unverified
    assert names == expected


@pytest.mark.parametrize(
    "text",
    [
        '[filter "probe"\n\tclean = x\n',  # broken header
        '[include]\n\tpath = "unterminated\n',
        '[include]\n\tpath = a\\qb\n',  # unknown escape
        "[include]\n\tpath = ~nobody-such-user-xyz/inc\n",
        "[include]\n\tpath = %(prefix)/etc/extra.inc\n",
        "[include]\n\tpath = extra.inc\x00junk\n",
        "stray text\n",
    ],
)
def test_config_that_cannot_be_read_with_certainty_is_unverified(text, tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text(text, encoding="utf-8")
    (tmp_path / "extra.inc").write_text('[filter "inc"]\n\tclean = x\n', encoding="utf-8")
    names, unverified = gitconfig.filter_names([config])
    assert unverified, "this text must never be trusted"
    oracle = subprocess.run(
        ["git", "config", "--file", str(config), "--includes", "--name-only", "--list"],
        capture_output=True, text=True, check=False,
    )
    known_to_git = {
        line[len("filter."):].rsplit(".", 1)[0]
        for line in oracle.stdout.splitlines() if line.startswith("filter.")
    } if oracle.returncode == 0 else set()
    assert unverified or names >= known_to_git


def test_deep_include_chains_are_unverified(tmp_path: Path) -> None:
    for n in range(8):
        (tmp_path / f"c{n}").write_text(f"[include]\n\tpath = c{n + 1}\n", encoding="utf-8")
    (tmp_path / "c8").write_text('[filter "deep"]\n\tclean = x\n', encoding="utf-8")
    names, unverified = gitconfig.filter_names([tmp_path / "c0"])
    assert "deep" not in names and len(unverified) == 1


def test_unreadable_config_is_reported_as_unverified(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text('[filter "x"]\n', encoding="utf-8")
    config.chmod(0o000)
    try:
        if os.access(config, os.R_OK):
            pytest.skip("running with privileges that ignore file permissions")
        names, unverified = gitconfig.filter_names([config])
    finally:
        config.chmod(0o600)
    assert names == set() and len(unverified) == 1


def test_disabled_filters_are_passed_as_overrides_and_validated(git_repo: Path) -> None:
    result = gitread.run_git(
        git_repo, ["status", "--porcelain=v2", "--branch"], disable_filters=["lfs"]
    )
    assert result.ok
    for expected in ("filter.lfs.clean=", "filter.lfs.process=", "filter.lfs.required=false"):
        assert expected in result.argv
    with pytest.raises(ValueError, match="cannot be switched off"):
        gitread.run_git(git_repo, ["status", "--porcelain"], disable_filters=["bad name=x"])


def test_status_is_not_run_when_a_filter_cannot_be_disabled(
    workspace: Path, git_helper, monkeypatch
) -> None:
    repo = _init(git_helper, workspace / "app")
    with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write('[filter "odd name"]\n\tclean = cat\n')
    calls: list[tuple] = []
    real_run = subprocess.run

    def spy(argv, **kwargs):
        calls.append(tuple(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gitread.subprocess, "run", spy)
    projects, problems = collect_projects(workspace)
    app = _by_id(projects)["app"]
    assert not any("status" in argv for argv in calls)
    assert app.uncommitted_paths is None and app.has_commits is True
    assert len(problems) == 1 and problems[0].what == "git status"
    assert "odd name" in problems[0].reason


@pytest.mark.parametrize(
    "extra_config",
    [
        "[include]\n\tpath = extra.cfg ; note\n",
        "[include] path = extra.cfg # c\n",
        "[include]\n\tpath = ex\\\ntra.cfg\n",
        '[include]\n\tpath = ex"tra".cfg\n',
        "[filter.PROBE]\n\tclean = touch '{sentinel}' && cat\n",
    ],
)
def test_filter_reached_through_config_syntax_variants_is_not_executed(
    extra_config: str, workspace: Path, tmp_path, git_helper
) -> None:
    sentinel = tmp_path / "filter-ran"
    repo = _init(git_helper, workspace / "app")
    (repo / ".gitattributes").write_text("*.dat filter=probe\n", encoding="utf-8")
    (repo / "f.dat").write_text("payload\n", encoding="utf-8")
    git_helper(repo, "add", "-A")
    git_helper(repo, "commit", "-q", "-m", "data")
    (repo / ".git" / "extra.cfg").write_text(
        f"\ufeff[filter \"probe\"]\n\tclean = touch '{sentinel}' && cat\n", encoding="utf-8"
    )
    with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write(extra_config.replace("{sentinel}", str(sentinel)))
    info = (repo / "f.dat").stat()
    os.utime(repo / "f.dat", ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000_000))

    projects, problems = collect_projects(workspace)
    assert not sentinel.exists(), "git executed the content filter"
    assert problems == []
    assert _by_id(projects)["app"].uncommitted_paths == 0

    git_helper(repo, "status", "--porcelain")  # negative control: plain git does run it
    assert sentinel.exists()
