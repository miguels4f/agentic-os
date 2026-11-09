"""Hardening of the generated-artifact exemption and the severity floor (PR #22 review).

Each test replays a bypass the independent review of PR #22 demonstrated:

* an agent-written plan declared executable config (package.json, CI workflows, compose files,
  requirements) as "generated" and so escaped every patch-never-regenerate budget;
* the reproducibility check accepted a do-nothing command (`true`) and a catch-all volatile field;
* a single ``*`` in a declared path exempted files in nested directories that were never checked;
* one negation anywhere in a finding switched the whole severity floor off.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from hyperwilfred_core import guardrails, reproducibility
from hyperwilfred_core.reproducibility import compare_bytes
from hyperwilfred_core.schemas import DEFAULT_BLOCKED_GLOBS, GeneratedArtifact
from hyperwilfred_core.severity_floor import floor_classes

PY = shlex.quote(sys.executable)


# --------------------------------------------------------------------------- declaration rules


@pytest.mark.parametrize(
    "path",
    [
        "package.json", "app/package.json", "package-lock.json", "pnpm-lock.yaml", "uv.lock",
        "requirements.txt", "services/api/requirements-dev.txt", "constraints.txt",
        ".github/workflows/ci.yml", ".gitlab-ci.yml", "docker-compose.yml",
        "deploy/docker-compose.prod.yaml", "tsconfig.json", ".pre-commit-config.yaml",
        ".husky/hooks.json", ".claude/settings.json", ".hw/run/state.json",
    ],
)
def test_executable_config_can_never_be_declared_generated(path: str) -> None:
    with pytest.raises(ValidationError):
        GeneratedArtifact(path=path, command="make x")


@pytest.mark.parametrize("path", ["*/*.yml", "src/*/x.json", "a/?/b.json", "[ab]/c.json"])
def test_wildcards_are_refused_in_directory_segments(path: str) -> None:
    with pytest.raises(ValidationError):
        GeneratedArtifact(path=path, command="make x")


@pytest.mark.parametrize("field", ["*", "a.*", "", "a..b", "*.*"])
def test_catch_all_volatile_fields_are_refused(field: str) -> None:
    with pytest.raises(ValidationError):
        GeneratedArtifact(path="snap/a.json", command="make x", volatile_fields=[field])


@pytest.mark.parametrize("field", ["generated_at", "meta.generated_at", "*.generated_at"])
def test_narrow_volatile_fields_are_accepted(field: str) -> None:
    art = GeneratedArtifact(path="snap/a.json", command="make x", volatile_fields=[field])
    assert art.volatile_fields == [field]


# --------------------------------------------------------------------------- acceptance gate


def _commit(repo: Path, git_helper, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    git_helper(repo, "add", "-A")
    git_helper(repo, "commit", "-q", "-m", "fixture")


def _accept(repo: Path, generated: list[str]):
    return guardrails.accept(
        packet_id="p", worktree=str(repo), allowed_globs=["**"],
        blocked_globs=list(DEFAULT_BLOCKED_GLOBS), result_file=None,
        max_changed_lines=400, max_hunk_span=120, full_file_ratio=0.9,
        generated_globs=generated, do_rollback=False,
    )


BIG = "".join(f'"line{i}": {i},\n' for i in range(20))


def test_a_single_line_snapshot_regeneration_is_still_accepted(git_repo: Path, git_helper) -> None:
    _commit(git_repo, git_helper, {"site/data/a.json": '{"v":1}'})
    (git_repo / "site/data/a.json").write_text('{"v":2}', encoding="utf-8")
    assert _accept(git_repo, ["site/data/*.json"]).accepted


def test_star_does_not_exempt_files_in_nested_directories(git_repo: Path, git_helper) -> None:
    _commit(git_repo, git_helper, {"site/data/deep/big.json": BIG})
    (git_repo / "site/data/deep/big.json").write_text(BIG.replace('"', "'"), encoding="utf-8")
    verdict = _accept(git_repo, ["site/data/*.json"])
    assert not verdict.accepted
    assert any("full-file-replacement" in r for v in verdict.patch_violations for r in v.reasons)


def test_a_wildcard_cannot_exempt_a_denied_concrete_file(git_repo: Path, git_helper) -> None:
    _commit(git_repo, git_helper, {"site/data/package.json": BIG})
    (git_repo / "site/data/package.json").write_text(BIG.replace('"', "'"), encoding="utf-8")
    assert not _accept(git_repo, ["site/data/*.json"]).accepted


def test_generated_exempt_is_segment_aware() -> None:
    assert guardrails.generated_exempt("site/a.json", ["site/*.json"])
    assert not guardrails.generated_exempt("site/x/a.json", ["site/*.json"])
    assert not guardrails.generated_exempt(".github/workflows/ci.yml", ["*/*/*.yml"])


# --------------------------------------------------------------------------- reproducibility


@pytest.fixture
def snap_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"],
                 ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "snap").mkdir()
    (repo / "snap/a.json").write_text('{"v":1}', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "s"], check=True,
                   capture_output=True)
    return repo


def _check(repo: Path, command: str):
    art = GeneratedArtifact(path="snap/a.json", command=command)
    return reproducibility.check(repo_root=repo, artifacts=[art], timeout_s=60)


def test_a_do_nothing_command_is_not_reproducible(snap_repo: Path) -> None:
    report = _check(snap_repo, "true")
    assert not report.ok and "not recreated" in report.message


def test_a_real_regeneration_is_reproducible(snap_repo: Path) -> None:
    cmd = f"{PY} -c \"open('snap/a.json','w').write('{{\\\"v\\\":1}}')\""
    assert _check(snap_repo, cmd).ok


def test_extra_uncommitted_outputs_fail_reproducibility(snap_repo: Path) -> None:
    cmd = (f"{PY} -c \"open('snap/a.json','w').write('{{\\\"v\\\":1}}');"
           f"open('snap/b.json','w').write('{{}}')\"")
    art = GeneratedArtifact(path="snap/*.json", command=cmd)
    report = reproducibility.check(repo_root=snap_repo, artifacts=[art], timeout_s=60)
    assert not report.ok and "not committed" in report.message


def test_volatile_fields_only_ignore_the_named_key() -> None:
    assert compare_bytes(b'{"a":1,"t":1}', b'{"a":1,"t":2}', ["t"]) is None
    assert compare_bytes(b'{"a":1,"t":1}', b'{"evil":2,"t":2}', ["t"]) is not None


# --------------------------------------------------------------------------- severity floor


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The API exposes customer phone numbers. Not covered by any leak test.",
         "personal_data_leak"),
        ("Leaks user emails in the export; no bypass found", "personal_data_leak"),
        ("The page offers a paste command while the engine blocks it; there is no test for "
         "this divergence", "ui_engine_divergence"),
        ("The UI state diverges from the engine rule", "ui_engine_divergence"),
    ],
)
def test_an_unrelated_negation_does_not_switch_the_floor_off(text: str, expected: str) -> None:
    assert expected in floor_classes([], text)


@pytest.mark.parametrize(
    "text",
    ["no divergence found", "Does not leak names", "Docstring of foo() diverges from its body",
     "The config loader diverges from the spec on defaults"],
)
def test_negated_or_ordinary_wording_stays_below_the_floor(text: str) -> None:
    assert floor_classes([], text) == []


def test_regeneration_commands_do_not_inherit_secrets(snap_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    cmd = (f"{PY} -c \"import os;v=os.environ.get('AWS_SECRET_ACCESS_KEY','');"
           f"open('snap/a.json','w').write('{{\\\"v\\\":1}}' if not v else v)\"")
    assert _check(snap_repo, cmd).ok  # the secret was not visible, so the bytes match
