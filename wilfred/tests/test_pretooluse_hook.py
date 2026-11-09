"""The standalone Claude PreToolUse write-time hook — the Phase-3 real-time block.

Covers the load-bearing claims:
* it BLOCKS an out-of-scope / blocked-glob write (deny) and ALLOWS an in-scope write (defer);
* it is FAIL-CLOSED on missing / malformed / ambiguous config;
* it composes with (does not replace) the engine acceptance reject — it is pure + stdlib-only;
* its glob matcher does NOT drift from the engine's ``guardrails`` matcher;
* the EMITTED standalone script (run as a real subprocess) makes the same decisions.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hyperwilfred_core import guardrails
from hyperwilfred_core.hooks import pretooluse


def _registry(worktree: str) -> dict:
    return {
        "run_id": "r",
        "repo_root": "/repo",
        "global_blocked_globs": [
            ".git/**",
            ".git",
            "**/.env",
            "**/.env.*",
            "**/secrets/**",
            "**/*.pem",
            "**/*.key",
            "**/migrations/**",
        ],
        "entries": [
            {
                "packet_id": "lib",
                "worktree": worktree,
                "allowed_globs": ["product/**"],
                "blocked_globs": ["**/.env"],
            }
        ],
    }


def _run(payload: dict, registry: dict, tmp_path: Path) -> tuple[int, str]:
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps(registry), encoding="utf-8")
    code, out, _err = pretooluse.run(["--registry", str(reg_path)], stdin_text=json.dumps(payload))
    decision = "allow_defer"
    if out:
        decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    return code, decision


def test_in_scope_write_is_allowed(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")
    code, dec = _run(
        {"tool_name": "Write", "tool_input": {"file_path": f"{wt}/product/a.py"}},
        _registry(wt), tmp_path,
    )
    assert code == 0 and dec == "allow_defer"


def test_out_of_scope_write_is_denied(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")
    code, dec = _run(
        {"tool_name": "Edit", "tool_input": {"file_path": f"{wt}/elsewhere/a.py"}},
        _registry(wt), tmp_path,
    )
    assert code == 2 and dec == "deny"


def test_review_reads_are_limited_to_declared_changed_paths(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")
    reviewed = Path(wt) / "product/reviewed.py"
    reviewed.parent.mkdir(parents=True)
    reviewed.write_text("safe\n", encoding="utf-8")
    (reviewed.parent / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (reviewed.parent / "unreviewed.py").write_text("other\n", encoding="utf-8")
    registry = _registry(wt)
    registry["deny_unmatched_reads"] = True
    registry["deny_unmatched_writes"] = True
    registry["entries"][0]["allowed_read_globs"] = ["product/reviewed.py"]

    allowed = {"tool_name": "Read", "tool_input": {"file_path": f"{wt}/product/reviewed.py"}}
    secret = {"tool_name": "Read", "tool_input": {"file_path": f"{wt}/product/.env"}}
    unrelated = {"tool_name": "Grep", "tool_input": {"path": f"{wt}/product/other.py"}}
    allowed_glob = {
        "tool_name": "Glob",
        "tool_input": {"path": wt, "pattern": "product/reviewed.py"},
    }
    unscoped = {
        "tool_name": "Glob",
        "tool_input": {"path": wt, "pattern": "product/*"},
    }

    assert _run(allowed, registry, tmp_path) == (0, "allow_defer")
    assert _run(allowed_glob, registry, tmp_path) == (0, "allow_defer")
    assert _run(secret, registry, tmp_path) == (2, "deny")
    assert _run(unrelated, registry, tmp_path) == (2, "deny")
    assert _run(unscoped, registry, tmp_path) == (2, "deny")


NEXTJS_ROUTE = "app/api/v1/projects/[project-id]/route.ts"
NEXTJS_PAGE = "app/(studio)/projects/[projectId]/page.tsx"


class _Py39Fnmatch:
    """``fnmatch`` as the macOS system ``python3`` (3.9) behaves: a reversed range such as
    ``[t-i]`` raises ``re.error`` instead of compiling to a never-matching class (3.10+)."""

    @staticmethod
    def _check(pat: str) -> None:
        if re.search(r"\[[^\]]*t-i", pat):
            raise re.error("bad character range t-i")

    @classmethod
    def fnmatch(cls, name: str, pat: str) -> bool:
        cls._check(pat)
        return fnmatch.fnmatch(name, pat)

    @classmethod
    def translate(cls, pat: str) -> str:
        cls._check(pat)
        return fnmatch.translate(pat)


@pytest.fixture(params=["native", "py39"])
def fnmatch_flavour(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test against this interpreter's fnmatch AND the stricter 3.9 behaviour."""
    if request.param == "py39":
        monkeypatch.setattr(pretooluse, "fnmatch", _Py39Fnmatch)
        monkeypatch.setattr(guardrails, "fnmatch", _Py39Fnmatch)
    return request.param


def _nextjs_review_registry(tmp_path: Path, read_globs: list[str]) -> tuple[str, dict]:
    wt = str(tmp_path / "wt")
    for rel in (
        NEXTJS_ROUTE,
        NEXTJS_PAGE,
        "app/api/v1/projects/other-id/route.ts",
        "app/(studio)/projects/[otherId]/page.tsx",
    ):
        f = Path(wt) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n", encoding="utf-8")
    registry = _registry(wt)
    registry["deny_unmatched_reads"] = True
    registry["deny_unmatched_writes"] = True
    registry["entries"][0]["allowed_read_globs"] = read_globs
    return wt, registry


def _run_full(payload: dict, registry: dict, tmp_path: Path) -> tuple[int, str]:
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps(registry), encoding="utf-8")
    code, _out, err = pretooluse.run(
        ["--registry", str(reg_path)], stdin_text=json.dumps(payload)
    )
    return code, err


def test_review_reads_match_literal_nextjs_bracket_paths(
    tmp_path: Path, fnmatch_flavour: str
) -> None:
    """Regression 2026-09-29: literal ``[project-id]`` paths crashed fnmatch (bad range t-i)."""
    wt, registry = _nextjs_review_registry(tmp_path, [NEXTJS_ROUTE, NEXTJS_PAGE])

    for rel in (NEXTJS_ROUTE, NEXTJS_PAGE):
        read = {"tool_name": "Read", "tool_input": {"file_path": f"{wt}/{rel}"}}
        assert _run_full(read, registry, tmp_path) == (0, ""), rel

    for sibling in (
        "app/api/v1/projects/other-id/route.ts",
        "app/(studio)/projects/[otherId]/page.tsx",
    ):
        read = {"tool_name": "Read", "tool_input": {"file_path": f"{wt}/{sibling}"}}
        code, err = _run_full(read, registry, tmp_path)
        assert code == 2, sibling
        assert "escapes packet" in err and "hook error" not in err, err


def test_malformed_allow_pattern_is_skipped_not_fatal(tmp_path: Path, fnmatch_flavour: str) -> None:
    """One uncompilable allow pattern must not deny every call via a hook crash."""
    wt, registry = _nextjs_review_registry(tmp_path, ["bad[t-i]glob", NEXTJS_ROUTE])
    allowed = {"tool_name": "Read", "tool_input": {"file_path": f"{wt}/{NEXTJS_ROUTE}"}}
    other = {"tool_name": "Read", "tool_input": {"file_path": f"{wt}/{NEXTJS_PAGE}"}}

    assert _run_full(allowed, registry, tmp_path) == (0, "")
    code, err = _run_full(other, registry, tmp_path)
    assert code == 2 and "hook error" not in err, err


def test_malformed_blocked_pattern_fails_closed(tmp_path: Path, fnmatch_flavour: str) -> None:
    """A blocked list we cannot evaluate must still deny (fail-closed), with a clear reason."""
    if fnmatch_flavour != "py39":
        pytest.skip("pattern is only malformed under the 3.9 fnmatch")
    wt, registry = _nextjs_review_registry(tmp_path, [NEXTJS_ROUTE])
    registry["entries"][0]["blocked_globs"] = ["bad[t-i]glob"]
    read = {"tool_name": "Read", "tool_input": {"file_path": f"{wt}/{NEXTJS_ROUTE}"}}

    code, err = _run_full(read, registry, tmp_path)
    assert code == 2
    assert "malformed blocked glob" in err, err


def test_matchers_treat_bracket_paths_literally_and_never_raise(fnmatch_flavour: str) -> None:
    for matcher in (pretooluse.matches, guardrails.matches):
        assert matcher(NEXTJS_ROUTE, NEXTJS_ROUTE)
        assert matcher(NEXTJS_PAGE, NEXTJS_PAGE)
        assert not matcher("app/api/v1/projects/other-id/route.ts", NEXTJS_ROUTE)
        assert not matcher("x/y.py", "bad[t-i]glob")


def test_blocked_glob_beats_allow(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")
    # product/.env matches the allow-list product/** BUT is a blocked glob → blocked wins.
    code, dec = _run(
        {"tool_name": "Write", "tool_input": {"file_path": f"{wt}/product/.env"}},
        _registry(wt), tmp_path,
    )
    assert code == 2 and dec == "deny"


def test_globally_blocked_path_denied_anywhere(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")
    # a write to an ABSOLUTE path far outside any worktree, but to a secret → still denied.
    for target in ("/home/u/.ssh/id_rsa.pem", "/etc/app/secrets/db.txt", "/x/app/migrations/1.sql"):
        code, dec = _run(
            {"tool_name": "Write", "tool_input": {"file_path": target}}, _registry(wt), tmp_path
        )
        assert code == 2 and dec == "deny", target


def test_non_write_tool_passes(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")
    code, dec = _run(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, _registry(wt), tmp_path
    )
    # the hook only gates Write/Edit/MultiEdit/NotebookEdit; Bash is out of scope here.
    assert code == 0 and dec == "allow_defer"


def test_write_outside_any_worktree_defers(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")
    # a benign path under no registered worktree and not globally-blocked → defer (the engine
    # acceptance gate remains authoritative for that residual case).
    code, dec = _run(
        {"tool_name": "Write", "tool_input": {"file_path": f"{tmp_path}/scratch/notes.md"}},
        _registry(wt), tmp_path,
    )
    assert code == 0 and dec == "allow_defer"


def test_review_profile_denies_write_outside_any_worktree(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")
    registry = _registry(wt)
    registry["deny_unmatched_writes"] = True
    code, dec = _run(
        {"tool_name": "Write", "tool_input": {"file_path": f"{tmp_path}/outside/report.json"}},
        registry, tmp_path,
    )
    assert code == 2 and dec == "deny"


# ----------------------------------------------------------------- fail-closed


def test_fail_closed_no_target_path(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")
    code, dec = _run({"tool_name": "Write", "tool_input": {}}, _registry(wt), tmp_path)
    assert code == 2 and dec == "deny"


def test_fail_closed_missing_registry(tmp_path: Path) -> None:
    code, out, _ = pretooluse.run(
        ["--registry", str(tmp_path / "nope.json")],
        stdin_text='{"tool_name":"Write","tool_input":{"file_path":"/a/b.py"}}',
    )
    assert code == 2 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_fail_closed_malformed_registry(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    code, out, _ = pretooluse.run(
        ["--registry", str(bad)],
        stdin_text='{"tool_name":"Write","tool_input":{"file_path":"/a/b.py"}}',
    )
    assert code == 2 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_fail_closed_no_registry_arg() -> None:
    code, out, _ = pretooluse.run(
        [], stdin_text='{"tool_name":"Write","tool_input":{"file_path":"/a/b.py"}}'
    )
    assert code == 2 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_fail_closed_garbage_payload(tmp_path: Path) -> None:
    reg = tmp_path / "r.json"
    reg.write_text(json.dumps(_registry(str(tmp_path))), encoding="utf-8")
    code, out, _ = pretooluse.run(["--registry", str(reg)], stdin_text="not json at all")
    assert code == 2 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_fail_closed_registry_missing_entries(tmp_path: Path) -> None:
    reg = tmp_path / "r.json"
    reg.write_text(json.dumps({"global_blocked_globs": []}), encoding="utf-8")  # no entries key
    code, _out, _ = pretooluse.run(
        ["--registry", str(reg)],
        stdin_text='{"tool_name":"Write","tool_input":{"file_path":"/a/b.py"}}',
    )
    assert code == 2


# ----------------------------------------------------------------- drift guard


def test_hook_matcher_does_not_drift_from_guardrails(fnmatch_flavour: str) -> None:
    """The hook's stdlib matcher must agree with guardrails.matches/allowlist_escapes exactly."""
    paths = [
        "product/a.py", ".env", "a/b/.env", "deep/secrets/x", "src/x.py", "src/sub/y.py",
        "x/y.pem", "a/b/c/d.py", "other.txt", "product/sub/deep.py",
        NEXTJS_ROUTE, NEXTJS_PAGE, "app/(studio)/projects/p/page.tsx",
    ]
    test_globs = [
        "product/**", "**/.env", "**/secrets/**", "src/*.py", "**/*.pem", "a/b/**",
        NEXTJS_ROUTE, NEXTJS_PAGE, "bad[t-i]glob",
    ]
    for p in paths:
        for g in test_globs:
            assert pretooluse.matches(p, g) == guardrails.matches(p, g), (p, g)
    for allow in (["product/**"], ["src/*.py"], ["a/b/**"]):
        for blocked in ([], ["**/.env"], ["**/secrets/**", "**/*.pem"]):
            hook_escapes = pretooluse.allowlist_escapes(paths, allow, blocked)
            engine_escapes = guardrails.allowlist_escapes(paths, allow, blocked)
            assert hook_escapes == engine_escapes, (allow, blocked)


# ----------------------------------------------------------------- real subprocess (emitted copy)


def test_emitted_standalone_script_runs_as_subprocess(tmp_path: Path) -> None:
    """Run the EMITTED hook file end-to-end as a separate process (the harness's real path)."""
    from hyperwilfred_core import compiler

    # copy the committed hook exactly as the compiler would, then invoke it via python3.
    script = tmp_path / "pretooluse.py"
    script.write_text(
        Path(compiler._HOOK_SOURCE).read_text(encoding="utf-8"), encoding="utf-8"
    )
    wt = str(tmp_path / "wt")
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps(_registry(wt)), encoding="utf-8")

    def invoke(payload: dict) -> int:
        proc = subprocess.run(
            [sys.executable, str(script), "--registry", str(reg)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode

    assert invoke({"tool_name": "Write", "tool_input": {"file_path": f"{wt}/product/a.py"}}) == 0
    assert invoke({"tool_name": "Write", "tool_input": {"file_path": f"{wt}/evil/a.py"}}) == 2
    assert invoke({"tool_name": "Write", "tool_input": {"file_path": "/home/u/secrets/k"}}) == 2


def test_emitted_script_is_stdlib_only() -> None:
    """The standalone hook must not import the package (it runs where it isn't installed)."""
    import ast

    from hyperwilfred_core import compiler

    tree = ast.parse(Path(compiler._HOOK_SOURCE).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("hyperwilfred_core"), node.module
            assert not node.module.startswith("common_py"), node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("hyperwilfred_core")
                assert not alias.name.startswith("common_py")


def test_symlinked_worktree_prefix_still_denies_out_of_scope(tmp_path: Path) -> None:
    """Regression (verifier #1): a write via a SYMLINKED prefix must still resolve to the canonical
    worktree the compiler recorded (`Path.resolve()`), so an out-of-scope write is not silently
    deferred. Reproduces the macOS /var → /private/var mismatch with an explicit symlink.
    """
    real = tmp_path / "real_wt"
    (real / "product").mkdir(parents=True)
    link = tmp_path / "link_wt"
    link.symlink_to(real)
    # the registry records the CANONICAL (resolved) worktree, exactly as compiler.py does
    canonical = str(real.resolve())
    reg = {
        "global_blocked_globs": ["**/.env"],
        "entries": [
            {"packet_id": "lib", "worktree": canonical,
             "allowed_globs": ["product/**"], "blocked_globs": ["**/.env"]}
        ],
    }
    # a worker writes OUT OF SCOPE through the symlinked path
    code, dec = _run(
        {"tool_name": "Write", "tool_input": {"file_path": f"{link}/elsewhere/evil.py"}},
        reg, tmp_path,
    )
    assert code == 2 and dec == "deny", "symlinked-prefix out-of-scope write must be DENIED"
    # in-scope through the symlink still allowed
    code2, dec2 = _run(
        {"tool_name": "Write", "tool_input": {"file_path": f"{link}/product/ok.py"}}, reg, tmp_path
    )
    assert code2 == 0 and dec2 == "allow_defer"


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_all_write_tools_gated(tool: str, tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")
    if tool == "MultiEdit":
        ti = {"edits": [{"file_path": f"{wt}/evil.sh"}]}
    elif tool == "NotebookEdit":
        ti = {"notebook_path": f"{wt}/evil.ipynb"}
    else:
        ti = {"file_path": f"{wt}/evil.sh"}
    code, dec = _run({"tool_name": tool, "tool_input": ti}, _registry(wt), tmp_path)
    assert code == 2 and dec == "deny"


@pytest.mark.skipif(shutil.which("python3") is None, reason="no python3 on PATH")
def test_emitted_hook_under_harness_python3_handles_nextjs_bracket_paths(tmp_path: Path) -> None:
    """The harness runs ``python3 <hook>`` — on macOS that is the 3.9 system interpreter, where
    the 2026-09-29 crash ('bad character range t-i') reproduced. Run the real emitted file."""
    from hyperwilfred_core import compiler

    script = tmp_path / "pretooluse.py"
    script.write_text(Path(compiler._HOOK_SOURCE).read_text(encoding="utf-8"), encoding="utf-8")
    wt, registry = _nextjs_review_registry(tmp_path, [NEXTJS_ROUTE, NEXTJS_PAGE])
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps(registry), encoding="utf-8")

    def invoke(rel: str) -> subprocess.CompletedProcess[str]:
        payload = {"tool_name": "Read", "tool_input": {"file_path": f"{wt}/{rel}"}}
        return subprocess.run(
            [shutil.which("python3") or "python3", str(script), "--registry", str(reg)],
            input=json.dumps(payload), capture_output=True, text=True, check=False,
        )

    for rel in (NEXTJS_ROUTE, NEXTJS_PAGE):
        proc = invoke(rel)
        assert proc.returncode == 0, (rel, proc.stderr)
    sibling = invoke("app/api/v1/projects/other-id/route.ts")
    assert sibling.returncode == 2 and "hook error" not in sibling.stderr, sibling.stderr
