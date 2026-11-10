"""Residual risks accepted at Gate 0 (D-S0.3-RESIDUAL, D-S0.3-REG), closed.

The acceptance gate used to ask ``git status`` / ``git diff`` what a worker changed. Both honour
configuration the worker can write (``.git/config``, ``core.attributesFile``, the personal
ignore file), so a deliberate worker could hide an edit from the gate or run its own program
inside it. The gate now compares the bytes on disk with the HEAD tree itself and reads ignore
rules only from ignore files that are committed and unchanged. Each test below is one probe of
the S0.3 re-probe (``reprobe.json``) that bypassed the gate before this change.

The second half: a commit the worker made before its worktree was registered would become the
pinned HEAD, so its changes were never inspected. Registration now refuses any HEAD that is not
the run's base commit or exactly the work accepted for the previous packet of that worktree.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from hyperwilfred_core import engine, guardrails
from hyperwilfred_core.schemas import BuildObjective, Plan, Slice


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
         "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *arguments],
        check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _accept(worktree: Path, *, do_rollback: bool = False):
    return guardrails.accept(
        packet_id="p", worktree=str(worktree), allowed_globs=["src/**", "app/**"],
        blocked_globs=["**/conftest.py", ".git/**"], result_file=None,
        max_changed_lines=400, max_hunk_span=120, full_file_ratio=0.9,
        do_rollback=do_rollback,
    )


FILES = {"src/a.py": "a = 1\n", "other/protected.py": "secret = 1\n"}


def _program(tmp_path: Path, name: str, body: str) -> tuple[Path, Path]:
    """A worker program that leaves a marker when anyone runs it."""
    marker = tmp_path / f"{name}-ran"
    program = tmp_path / f"{name}.sh"
    program.write_text(f"#!/bin/sh\necho ran >> '{marker}'\n{body}\n")
    program.chmod(0o755)
    return program, marker


def _forge_same_stat_edit(repo: Path) -> Path:
    """Rewrite other/protected.py in place, same size, and restore its mtime."""
    protected = repo / "other" / "protected.py"
    old = os.stat(protected).st_mtime_ns - 3600 * 10**9
    os.utime(protected, ns=(old, old))  # older than the index: the cached stat is trusted
    time.sleep(1.1)
    subprocess.run(["git", "-C", str(repo), "update-index", "-q", "--refresh"],
                   capture_output=True, check=False)
    _git(repo, "status", "--porcelain")
    before = os.stat(protected)
    with protected.open("r+") as handle:
        handle.write("secret = 2\n")
    os.utime(protected, ns=(before.st_atime_ns, before.st_mtime_ns))
    return protected


def _assert_out_of_scope_edit_caught(repo: Path, marker: Path | None = None) -> None:
    assert "other/protected.py" in guardrails.changed_paths(str(repo))
    assert guardrails.is_clean(str(repo)) is False
    verdict = _accept(repo)
    assert verdict.accepted is False
    assert "other/protected.py" in verdict.escapes
    if marker is not None:
        assert not marker.exists(), "the acceptance gate executed the worker's program"


# ---- stat forgery: core.trustctime / core.checkStat --------------------------------------------

def test_stat_forgery_cannot_hide_a_same_size_edit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    _git(repo, "config", "core.trustctime", "false")
    _git(repo, "config", "core.checkStat", "minimal")
    _forge_same_stat_edit(repo)
    assert _git(repo, "status", "--porcelain") == ""  # precondition: git really is fooled
    _assert_out_of_scope_edit_caught(repo)


def test_stat_forgery_cannot_hide_a_rewrite_from_the_patch_budget(tmp_path: Path) -> None:
    lines = "".join(f"line {i:03d}\n" for i in range(50))
    repo = _repo(tmp_path, {"src/a.py": lines})
    _git(repo, "config", "core.trustctime", "false")
    _git(repo, "config", "core.checkStat", "minimal")
    target = repo / "src" / "a.py"
    old = os.stat(target).st_mtime_ns - 3600 * 10**9
    os.utime(target, ns=(old, old))
    time.sleep(1.1)
    subprocess.run(["git", "-C", str(repo), "update-index", "-q", "--refresh"],
                   capture_output=True, check=False)
    before = os.stat(target)
    target.write_text("".join(f"LINE {i:03d}\n" for i in range(50)))  # every line, same size
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert _git(repo, "diff", "--numstat", "HEAD") == ""  # precondition

    verdict = _accept(repo)
    assert verdict.accepted is False
    assert any("full-file-replacement" in reason
               for violation in verdict.patch_violations for reason in violation.reasons)


def test_disabled_file_mode_cannot_hide_a_new_executable_bit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    _git(repo, "config", "core.fileMode", "false")
    (repo / "other" / "protected.py").chmod(0o755)
    assert _git(repo, "status", "--porcelain") == ""  # precondition
    _assert_out_of_scope_edit_caught(repo)


# ---- clean filters and attribute sources ------------------------------------------------------

CLEAN_BACK = "sed 's/secret = 2/secret = 1/'"


def _edit_with_bumped_mtime(repo: Path) -> None:
    protected = repo / "other" / "protected.py"
    protected.write_text("secret = 2\n")
    later = time.time_ns() + 5 * 10**9
    os.utime(protected, ns=(later, later))


def test_committed_attribute_filter_neither_hides_an_edit_nor_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {**FILES, ".gitattributes": "*.py filter=x diff=x\n"})
    clean, marker = _program(tmp_path, "clean", CLEAN_BACK)
    _git(repo, "config", "filter.x.clean", str(clean))
    _git(repo, "config", "diff.x.textconv", str(clean))
    _edit_with_bumped_mtime(repo)
    assert _git(repo, "status", "--porcelain") == ""  # precondition: the filter hides it
    marker.unlink()
    _assert_out_of_scope_edit_caught(repo, marker)


def test_configured_attributes_file_neither_hides_an_edit_nor_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    clean, marker = _program(tmp_path, "clean", CLEAN_BACK)
    attributes = tmp_path / "attrs"
    attributes.write_text("*.py filter=y\n")
    _git(repo, "config", "core.attributesFile", str(attributes))
    _git(repo, "config", "filter.y.clean", str(clean))
    _edit_with_bumped_mtime(repo)
    assert _git(repo, "status", "--porcelain") == ""  # precondition
    marker.unlink()
    _assert_out_of_scope_edit_caught(repo, marker)


def test_info_attributes_filter_neither_hides_an_edit_nor_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    clean, marker = _program(tmp_path, "clean", CLEAN_BACK)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.py filter=z\n")
    _git(repo, "config", "filter.z.clean", str(clean))
    _edit_with_bumped_mtime(repo)
    assert _git(repo, "status", "--porcelain") == ""  # precondition
    marker.unlink()
    _assert_out_of_scope_edit_caught(repo, marker)


def test_rollback_restores_bytes_without_running_a_smudge_filter(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {**FILES, ".gitattributes": "*.py filter=s\n"})
    smudge, marker = _program(tmp_path, "smudge", "cat")
    _git(repo, "config", "filter.s.smudge", str(smudge))
    (repo / "other" / "protected.py").write_text("tampered = True\n")
    verdict = _accept(repo, do_rollback=True)
    assert verdict.accepted is False and verdict.rolled_back is True
    assert verdict.tree_clean_after is True
    assert (repo / "other" / "protected.py").read_text() == "secret = 1\n"
    assert not marker.exists(), "rollback executed the worker's smudge filter"


# ---- ignore files the worker controls ---------------------------------------------------------

def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def test_case_folded_ignore_file_cannot_hide_a_planted_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    if not _case_insensitive(repo):
        pytest.skip("needs a case-insensitive file system (git reads .GITIGNORE as .gitignore)")
    (repo / "app").mkdir()
    (repo / "app" / ".GITIGNORE").write_text("*\n")
    (repo / "app" / "conftest.py").write_text("x = 1\n")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""  # precondition

    assert "app/conftest.py" in guardrails.changed_paths(str(repo))
    assert guardrails.is_clean(str(repo)) is False
    verdict = _accept(repo, do_rollback=True)
    assert verdict.accepted is False and "app/conftest.py" in verdict.escapes
    assert verdict.tree_clean_after is True
    assert not (repo / "app" / "conftest.py").exists()
    assert not (repo / "app" / ".GITIGNORE").exists()


def test_rollback_removes_case_folded_ignore_file_and_its_planted_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    if not _case_insensitive(repo):
        pytest.skip("needs a case-insensitive file system")
    (repo / "app").mkdir()
    (repo / "app" / ".GITIGNORE").write_text("*\n")
    (repo / "app" / "conftest.py").write_text("x = 1\n")
    (repo / "other" / "protected.py").write_text("tamper\n")
    verdict = _accept(repo, do_rollback=True)
    assert verdict.rolled_back is True and verdict.tree_clean_after is True
    left = sorted(str(p.relative_to(repo)) for p in repo.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(repo).parts)
    assert left == ["other/protected.py", "src/a.py"]
    assert (repo / "other" / "protected.py").read_text() == "secret = 1\n"


def test_quoted_directory_name_cannot_hide_a_planted_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    planted = repo / "app" / "sp ace é"
    planted.mkdir(parents=True)
    (planted / ".gitignore").write_text("*\n!.gitignore\n")
    (planted / "conftest.py").write_text("x = 1\n")
    assert "\\303" in _git(repo, "status", "--porcelain", "--untracked-files=all")  # quoted

    changed = guardrails.changed_paths(str(repo))
    assert "app/sp ace é/conftest.py" in changed
    assert "app/sp ace é/.gitignore" in changed
    verdict = _accept(repo)
    assert verdict.accepted is False
    assert "app/sp ace é/conftest.py" in verdict.escapes


def test_personal_ignore_file_cannot_hide_a_planted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path, FILES)
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("conftest.py\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (repo / "src" / "conftest.py").write_text("x = 1\n")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""  # precondition

    assert "src/conftest.py" in guardrails.changed_paths(str(repo))
    verdict = _accept(repo)
    assert verdict.accepted is False and "src/conftest.py" in verdict.escapes


def test_configured_excludes_file_cannot_hide_a_planted_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    excludes = tmp_path / "excludes"
    excludes.write_text("conftest.py\n")
    _git(repo, "config", "core.excludesFile", str(excludes))
    (repo / "src" / "conftest.py").write_text("x = 1\n")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""  # precondition

    verdict = _accept(repo)
    assert verdict.accepted is False and "src/conftest.py" in verdict.escapes


def test_force_staged_file_in_an_ignored_directory_is_a_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {**FILES, ".gitignore": "build/\n"})
    (repo / "build").mkdir()
    (repo / "build" / "conftest.py").write_text("x = 1\n")
    _git(repo, "add", "-f", "build/conftest.py")

    assert "build/conftest.py" in guardrails.changed_paths(str(repo))
    verdict = _accept(repo)
    assert verdict.accepted is False and "build/conftest.py" in verdict.escapes


def test_configured_worktree_elsewhere_cannot_vouch_for_this_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    decoy = tmp_path / "decoy"
    for name, content in FILES.items():
        (decoy / name).parent.mkdir(parents=True, exist_ok=True)
        (decoy / name).write_text(content)
    _git(repo, "config", "core.worktree", str(decoy))
    (repo / "other" / "protected.py").write_text("secret = 2\n")
    try:
        verdict = _accept(repo)
    except (guardrails.GuardrailError, ValueError):
        return  # failing closed is fine
    assert verdict.accepted is False


# ---- what stays the same ----------------------------------------------------------------------

def test_in_scope_edit_with_untouched_committed_ignores_is_still_accepted(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {**FILES, ".gitignore": "build/\n*.log\n"})
    guardrails.ensure_engine_state_excluded(repo)
    (repo / "build" / "deep").mkdir(parents=True)
    (repo / "build" / "deep" / "out.py").write_text("artifact\n")
    (repo / "run.log").write_text("log\n")
    (repo / ".hw" / "r").mkdir(parents=True)
    (repo / ".hw" / "r" / "state.json").write_text("{}\n")
    (repo / "src" / "a.py").write_text("a = 1\nb = 2\n")
    (repo / "src" / "b.py").write_text("b = 1\n")

    assert sorted(guardrails.changed_paths(str(repo))) == ["src/a.py", "src/b.py"]
    verdict = _accept(repo)
    assert verdict.accepted is True and verdict.escapes == []
    assert guardrails._numstat(str(repo)) == {"src/a.py": (1, 0)}


def test_symlinked_directory_cannot_redirect_rollback_outside_the_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "protected.py").write_text("outside\n")
    import shutil
    shutil.rmtree(repo / "other")
    (repo / "other").symlink_to(outside, target_is_directory=True)
    verdict = _accept(repo, do_rollback=True)
    assert verdict.accepted is False and verdict.tree_clean_after is True
    assert (outside / "protected.py").read_text() == "outside\n"
    assert not (repo / "other").is_symlink()
    assert (repo / "other" / "protected.py").read_text() == "secret = 1\n"


def test_accepted_changes_record_bytes_modes_links_and_deletions(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {**FILES, ".gitignore": "build/\n", "bin/tool.sh": "#!/bin/sh\n"})
    (repo / "bin" / "tool.sh").chmod(0o755)  # mode-only change
    (repo / "src" / "a.py").write_text("a = 2\n")
    (repo / "src" / "link").symlink_to("a.py")
    (repo / "other" / "protected.py").unlink()
    (repo / "build").mkdir()
    (repo / "build" / "x").write_text("ignored\n")
    changes = guardrails.accepted_changes(str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "work")
    committed = guardrails.tree_entries(str(repo), "HEAD")
    assert set(changes) == {"bin/tool.sh", "src/a.py", "src/link", "other/protected.py"}
    assert changes["other/protected.py"] is None
    for path in ("bin/tool.sh", "src/a.py", "src/link"):
        assert tuple(changes[path]) == committed[path]


# ---- registration refuses a HEAD the run did not start from ------------------------------------

def _planned(tmp_path: Path) -> tuple[Path, engine.RunPaths]:
    repo = _repo(tmp_path, {"src/a.py": "a = 1\n", "other/protected.py": "secret = 1\n"})
    engine.plan(repo_root=repo, run_id="r", product_dir="src",
                objective=BuildObjective(id="o", title="Packets", objective="build app"))
    paths = engine.RunPaths(repo, "r")
    slices = [Slice(id=packet, title=packet, allowed_globs=["src/**"])
              for packet in ("a", "b", "c")]
    paths.plan.write_text(
        Plan(objective_id="o", product_dir="src", slices=slices).model_dump_json())
    engine.fanout_plan(repo_root=repo, run_id="r")
    return repo, paths


def _result(paths: engine.RunPaths, packet_id: str) -> None:
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    (paths.results_dir / f"{packet_id}.json").write_text('{"ok": true}\n')


def test_commit_made_before_registration_is_refused(tmp_path: Path) -> None:
    repo, _paths = _planned(tmp_path)
    worker = tmp_path / "worker"
    _git(repo, "worktree", "add", "-q", "--detach", str(worker), "HEAD")
    (worker / "other" / "protected.py").write_text("secret = 2\n")
    _git(worker, "commit", "-qam", "out of scope, committed before registration")
    with pytest.raises(engine.EngineError, match="base commit|accepted"):
        engine.register_worktree(repo_root=repo, run_id="r", packet_id="a",
                                 worktree=str(worker))


def test_commit_of_more_than_the_accepted_work_is_refused(tmp_path: Path) -> None:
    repo, paths = _planned(tmp_path)
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(repo))
    (repo / "src" / "a.py").write_text("a = 1\nb = 2\n")
    _result(paths, "a")
    out = engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                               worktree=str(repo), do_rollback=False)
    assert out["verdict"]["accepted"]
    (repo / "other" / "protected.py").write_text("secret = 2\n")  # slipped into the commit
    _git(repo, "add", "src/a.py", "other/protected.py")
    _git(repo, "commit", "-qm", "accepted work plus an extra")
    with pytest.raises(engine.EngineError, match="base commit|accepted"):
        engine.register_worktree(repo_root=repo, run_id="r", packet_id="b", worktree=str(repo))


def test_commit_of_exactly_the_accepted_work_can_serve_the_next_packet(tmp_path: Path) -> None:
    repo, paths = _planned(tmp_path)
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(repo))
    (repo / "src" / "a.py").write_text("a = 1\nb = 2\n")
    _result(paths, "a")
    assert engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                                worktree=str(repo), do_rollback=False)["verdict"]["accepted"]
    _git(repo, "add", "src/a.py")
    _git(repo, "commit", "-qm", "accepted work")
    out = engine.register_worktree(repo_root=repo, run_id="r", packet_id="b", worktree=str(repo))
    assert out["registered"] is True


def test_fresh_worktree_from_merged_accepted_work_can_serve_the_next_wave(tmp_path: Path) -> None:
    repo, paths = _planned(tmp_path)
    workers = {packet: tmp_path / f"worker-{packet}" for packet in ("a", "b")}
    for packet, worker in workers.items():
        _git(repo, "worktree", "add", "-q", "-b", f"w-{packet}", str(worker), "HEAD")
        engine.register_worktree(repo_root=repo, run_id="r", packet_id=packet,
                                 worktree=str(worker))
        (worker / "src" / f"{packet}_new.py").write_text(f"{packet} = 1\n")
        _result(paths, packet)
        assert engine.record_result(repo_root=repo, run_id="r", packet_id=packet,
                                    worktree=str(worker),
                                    do_rollback=False)["verdict"]["accepted"]
        _git(worker, "add", "-A")
        _git(worker, "commit", "-qm", f"packet {packet}")
    _git(repo, "merge", "-q", "--no-edit", "w-a", "w-b")
    wave_two = tmp_path / "worker-c"
    _git(repo, "worktree", "add", "-q", "--detach", str(wave_two), "HEAD")
    out = engine.register_worktree(repo_root=repo, run_id="r", packet_id="c",
                                   worktree=str(wave_two))
    assert out["registered"] is True


def test_uninitialised_and_clean_submodules_are_not_changes(tmp_path: Path) -> None:
    inner = _repo(tmp_path / "inner", {"lib.py": "x = 1\n"})
    repo = _repo(tmp_path, FILES)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "vendor")
    _git(repo, "commit", "-qm", "submodule")
    assert guardrails.changed_paths(str(repo)) == []
    (repo / "vendor" / "lib.py").write_text("x = 2\n")
    assert "vendor" in guardrails.changed_paths(str(repo))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    assert (clone / "vendor").is_dir() and not any((clone / "vendor").iterdir())
    assert guardrails.changed_paths(str(clone)) == []


def test_packets_recorded_at_the_same_time_keep_both_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, paths = _planned(tmp_path)
    workers = {packet: tmp_path / f"worker-{packet}" for packet in ("a", "b")}
    for packet, worker in workers.items():
        _git(repo, "worktree", "add", "-q", "-b", f"w-{packet}", str(worker), "HEAD")
        engine.register_worktree(repo_root=repo, run_id="r", packet_id=packet,
                                 worktree=str(worker))
        (worker / "src" / f"{packet}_new.py").write_text(f"{packet} = 1\n")
        _result(paths, packet)
    real_accept = guardrails.accept
    interleaved: list[dict] = []

    def accept_while_a_records(**kwargs):  # b's gate runs while a is being recorded
        verdict = real_accept(**kwargs)
        if kwargs["packet_id"] == "b" and not interleaved:
            interleaved.append(engine.record_result(
                repo_root=repo, run_id="r", packet_id="a", worktree=str(workers["a"]),
                do_rollback=False))
        return verdict

    monkeypatch.setattr(guardrails, "accept", accept_while_a_records)
    engine.record_result(repo_root=repo, run_id="r", packet_id="b",
                         worktree=str(workers["b"]), do_rollback=False)
    state = engine.load_state(paths)
    assert "accepted_changes" in state.packet_worktrees["build:a"]
    assert "accepted_changes" in state.packet_worktrees["build:b"]


def test_file_written_after_the_gate_read_is_not_recorded_as_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, paths = _planned(tmp_path)
    engine.register_worktree(repo_root=repo, run_id="r", packet_id="a", worktree=str(repo))
    (repo / "src" / "a.py").write_text("a = 1\nb = 2\n")
    _result(paths, "a")
    real_accept = guardrails.accept

    def late_writer(**kwargs):  # a process the worker left running writes after the read
        verdict = real_accept(**kwargs)
        (repo / "other" / "protected.py").write_text("secret = 2\n")
        return verdict

    monkeypatch.setattr(guardrails, "accept", late_writer)
    assert engine.record_result(repo_root=repo, run_id="r", packet_id="a",
                                worktree=str(repo), do_rollback=False)["verdict"]["accepted"]
    _git(repo, "add", "src/a.py", "other/protected.py")
    _git(repo, "commit", "-qm", "accepted work plus the late write")
    with pytest.raises(engine.EngineError, match="no accepted packet"):
        engine.register_worktree(repo_root=repo, run_id="r", packet_id="b", worktree=str(repo))


# ---- found by the adversarial re-probe of this redesign ---------------------------------------

def test_staged_gitlink_cannot_hide_the_files_under_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},src/sub")
    (repo / "src" / "sub").mkdir()
    (repo / "src" / "sub" / "conftest.py").write_text("x = 1\n")
    verdict = _accept(repo)
    assert verdict.accepted is False and "src/sub/conftest.py" in verdict.escapes


def test_embedded_repository_cannot_hide_the_files_inside_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    nested = repo / "src" / "nested"
    nested.mkdir()
    (nested / "conftest.py").write_text("x = 1\n")
    _git(nested, "init", "-q")
    _git(nested, "add", "-A")
    _git(nested, "commit", "-qm", "nested")
    _git(repo, "add", "src/nested")  # records a gitlink, not the files
    verdict = _accept(repo)
    assert verdict.accepted is False and "src/nested/conftest.py" in verdict.escapes


def test_file_planted_inside_a_committed_submodule_is_named(tmp_path: Path) -> None:
    inner = _repo(tmp_path / "inner", {"lib.py": "x = 1\n"})
    repo = _repo(tmp_path, FILES)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner),
         "src/vendor")
    _git(repo, "commit", "-qm", "submodule")
    (repo / "src" / "vendor" / "conftest.py").write_text("x = 1\n")
    verdict = _accept(repo)
    assert verdict.accepted is False and "src/vendor/conftest.py" in verdict.escapes


def test_one_nul_byte_cannot_carry_a_rewrite_past_the_patch_budget(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.js": "".join(f"line {i:03d}\n" for i in range(50))})
    (repo / "src" / "a.js").write_bytes(
        b"\0" + "".join(f"LINE {i:03d}\n" for i in range(50)).encode())
    verdict = _accept(repo)
    assert verdict.accepted is False
    assert any("full-file-replacement" in reason
               for violation in verdict.patch_violations for reason in violation.reasons)


def test_binary_asset_edit_keeps_no_line_budget(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    (repo / "src" / "logo.png").write_bytes(b"\x89PNG\0" + bytes(range(256)) * 40)
    _git(repo, "add", "src/logo.png")
    _git(repo, "commit", "-qm", "logo")
    (repo / "src" / "logo.png").write_bytes(b"\x89PNG\0" + bytes(reversed(range(256))) * 40)
    assert _accept(repo).accepted is True


def test_unreadable_head_is_not_read_as_an_empty_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"src/a.py": "".join(f"line {i:03d}\n" for i in range(50))})
    (repo / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
    (repo / "src" / "a.py").write_text("".join(f"LINE {i:03d}\n" for i in range(50)))
    with pytest.raises((guardrails.GuardrailError, ValueError)):
        _accept(repo)


def test_fifo_named_gitignore_fails_closed_without_hanging(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    (repo / "app").mkdir()
    os.mkfifo(repo / "app" / ".gitignore")
    (repo / "app" / "conftest.py").write_text("x = 1\n")
    with pytest.raises(guardrails.GuardrailError, match="not a regular file"):
        _accept(repo)


def _with_submodule(tmp_path: Path) -> tuple[Path, Path]:
    inner = _repo(tmp_path / "inner", {"lib.py": "x = 1\n"})
    repo = _repo(tmp_path, FILES)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner),
         "src/vendor")
    _git(repo, "commit", "-qm", "submodule")
    return repo, repo / "src" / "vendor"


def test_commit_inside_a_submodule_cannot_hide_a_planted_file(tmp_path: Path) -> None:
    repo, vendor = _with_submodule(tmp_path)
    (vendor / "conftest.py").write_text("x = 1\n")
    _git(vendor, "add", "conftest.py")
    _git(vendor, "commit", "-qm", "planted inside the submodule")
    verdict = _accept(repo)
    assert verdict.accepted is False and "src/vendor/conftest.py" in verdict.escapes


def test_submodule_exclude_file_cannot_hide_a_planted_file(tmp_path: Path) -> None:
    repo, vendor = _with_submodule(tmp_path)
    exclude = Path(_git(vendor, "rev-parse", "--path-format=absolute",
                        "--git-path", "info/exclude").strip())
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("conftest.py\n")
    (vendor / "conftest.py").write_text("x = 1\n")
    verdict = _accept(repo)
    assert verdict.accepted is False and "src/vendor/conftest.py" in verdict.escapes


def test_untouched_submodule_with_an_in_scope_edit_is_accepted(tmp_path: Path) -> None:
    repo, _vendor = _with_submodule(tmp_path)
    (repo / "src" / "a.py").write_text("a = 1\nb = 2\n")
    verdict = _accept(repo)
    assert verdict.accepted is True and verdict.changed_paths == ["src/a.py"]


def test_submodule_object_store_that_lies_about_a_tree_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, vendor = _with_submodule(tmp_path)
    (vendor / "conftest.py").write_text("x = 1\n")
    real_raw = guardrails._raw

    def forged_store(worktree, *args, **kwargs):  # a worker-written pack serving other bytes
        out = real_raw(worktree, *args, **kwargs)
        if args[:2] == ("cat-file", "tree") and Path(worktree) == vendor:
            return out + b"100644 conftest.py\0" + bytes(20)
        return out

    monkeypatch.setattr(guardrails, "_raw", forged_store)
    with pytest.raises(guardrails.GuardrailError, match="does not match its id"):
        _accept(repo)


def test_in_scope_link_to_an_ignored_folder_is_an_escape(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {**FILES, ".gitignore": "build/\n"})
    (repo / "build").mkdir()
    (repo / "build" / "conftest.py").write_text("x = 1\n")
    (repo / "src" / "linked").symlink_to("../build", target_is_directory=True)
    verdict = _accept(repo)
    assert verdict.accepted is False and "src/linked" in verdict.escapes


def test_in_scope_link_out_of_the_tree_is_an_escape(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    (repo / "src" / "elsewhere.py").symlink_to(outside)
    verdict = _accept(repo)
    assert verdict.accepted is False and "src/elsewhere.py" in verdict.escapes


def test_in_scope_link_to_a_file_in_the_tree_is_still_accepted(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FILES)
    (repo / "src" / "alias.py").symlink_to("a.py")
    assert _accept(repo).accepted is True


@pytest.mark.parametrize("target", ["missing.py", "../build/evil.py", "../build", "/etc/hosts",
                                    "../other/new.py"])
def test_link_to_anything_but_a_visible_in_scope_file_is_an_escape(
    tmp_path: Path, target: str,
) -> None:
    repo = _repo(tmp_path, {**FILES, ".gitignore": "build/\n"})
    (repo / "build").mkdir()
    (repo / "build" / "evil.py").write_text("x = 1\n")  # hidden by the committed rule
    (repo / "other" / "new.py").write_text("x = 1\n")  # itself out of scope
    (repo / "src" / "alias.py").symlink_to(target)
    verdict = _accept(repo)
    assert verdict.accepted is False and "src/alias.py" in verdict.escapes


def test_link_text_that_folds_differently_on_disk_is_an_escape(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {**FILES, ".gitignore": "build/\n"})
    (repo / "build" / "deep").mkdir(parents=True)
    (repo / "build" / "a.py").write_text("hidden = True\n")
    (repo / "src" / "hop").symlink_to("../build/deep", target_is_directory=True)
    # As text "hop/../a.py" is src/a.py; on disk it is build/a.py, behind the hop link.
    (repo / "src" / "alias.py").symlink_to("hop/../a.py")
    assert (repo / "src" / "alias.py").read_text() == "hidden = True\n"  # precondition
    verdict = _accept(repo)
    assert verdict.accepted is False and "src/alias.py" in verdict.escapes
