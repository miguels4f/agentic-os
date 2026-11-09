"""Command center: the allowlisted ``ops`` reader (``opsread.py``)."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path

import pytest
from ops_fakes import fake_ops

from hyperwilfred_core.command_center import opsread


@pytest.mark.parametrize(
    "args",
    [
        ["approve", "P-0001"],
        ["reject", "P-0001"],
        ["authorize", "T-0001"],
        ["sync"],
        ["board", "--open"],
        ["export"],
        ["status"],
        ["export", "--json", "--with-body"],
        ["--root", "/x", "export", "--json"],
        ["export", "--json", "--root", "/x"],
        [],
    ],
)
def test_allowlist_rejects_before_any_process(args, tmp_path, monkeypatch) -> None:
    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("a process was started for a call outside the allowlist")

    monkeypatch.setattr(opsread.subprocess, "run", boom)
    with pytest.raises(ValueError, match="allowlist"):
        opsread.run_ops(tmp_path / "ops", tmp_path, args)


def test_argv_carries_root_after_the_subcommand(tmp_path) -> None:
    """``--json`` and ``--root`` follow the subcommand (ops CLI convention, master A1)."""
    root = tmp_path / "ws" / "ops"
    root.mkdir(parents=True)
    program = fake_ops(tmp_path / "bin")
    for sub in ("export", "status"):
        assert opsread.build_argv(program, [sub, "--json"], root) == (
            str(program), sub, "--json", "--root", str(root)
        )
        assert opsread.run_ops(program, root, [sub, "--json"]).ok
        recorded = (tmp_path / "bin" / f"args-{sub}.txt").read_text(encoding="utf-8")
        assert recorded.splitlines() == [sub, "--json", "--root", str(root)]
    with pytest.raises(ValueError, match="allowlist"):
        opsread.build_argv(program, ["--root", str(root), "export", "--json"], root)
    with pytest.raises(ValueError, match="absolute"):
        opsread.build_argv(program, ["export", "--json"], Path("ops"))


def test_run_ops_uses_an_argument_list_a_clean_environment_and_no_stdin(
    tmp_path, monkeypatch
) -> None:
    program = fake_ops(tmp_path / "bin")
    root = tmp_path / "ws" / "ops"
    root.mkdir(parents=True)
    monkeypatch.setenv("GIT_DIR", "/nonexistent/redirect")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("PYTHONPATH", "/nonexistent/shadow")
    seen: dict = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return real_run(argv, **kwargs)

    monkeypatch.setattr(opsread.subprocess, "run", spy)
    result = opsread.run_ops(program, root, ["export", "--json"])
    assert result.ok, result.failure_reason
    assert json.loads(result.stdout)["schema"] == "ops-export/v1"
    assert seen["argv"] == (str(program), "export", "--json", "--root", str(root))
    kwargs = seen["kwargs"]
    assert "shell" not in kwargs and kwargs["check"] is False
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["cwd"] == str(root)
    assert kwargs["timeout"] == opsread.TIMEOUT_SECONDS == 30
    env = kwargs["env"]
    assert env["GIT_OPTIONAL_LOCKS"] == "0" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_NO_LAZY_FETCH"] == "1" and env["PYTHONDONTWRITEBYTECODE"] == "1"
    # ops drops GIT_CONFIG_COUNT before it runs git (master A5), so none is passed or kept.
    assert not [key for key in env if key.startswith("GIT_CONFIG")]
    assert "GIT_DIR" not in env and "PYTHONPATH" not in env
    # The program itself ran with that environment and exactly those arguments.
    recorded = (tmp_path / "bin" / "env-export.txt").read_text(encoding="utf-8")
    assert "PYTHONDONTWRITEBYTECODE=1" in recorded and "GIT_DIR=" not in recorded
    args = (tmp_path / "bin" / "args-export.txt").read_text(encoding="utf-8")
    assert args == f"export\n--json\n--root\n{root}\n"
    assert result.command == shlex.join(seen["argv"])


def test_timeout_missing_program_and_oversized_output_come_back_as_results(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(opsread, "TIMEOUT_SECONDS", 1)
    started = time.monotonic()
    hanging = fake_ops(tmp_path / "hang", hang=True)
    hung = opsread.run_ops(hanging, tmp_path, ["status", "--json"])
    assert hung.timed_out and not hung.ok
    assert hung.failure_reason == "ops timed out after 1 seconds"
    assert time.monotonic() - started < 10
    missing = opsread.run_ops(tmp_path / "no-such-ops", tmp_path, ["export", "--json"])
    assert not missing.ok and "could not be started" in missing.failure_reason
    monkeypatch.setattr(opsread, "MAX_OUTPUT_BYTES", 10)
    big = opsread.run_ops(fake_ops(tmp_path / "big"), tmp_path, ["export", "--json"])
    assert not big.ok and big.failure_reason == "ops printed more than 10 bytes"


def test_a_non_zero_exit_is_a_failure_that_names_the_last_error_line(tmp_path) -> None:
    program = fake_ops(tmp_path / "bin", exit_code=3, stderr="first\nrebase conflict kept\n")
    result = opsread.run_ops(program, tmp_path, ["status", "--json"])
    assert not result.ok and result.returncode == 3
    assert result.failure_reason == "ops exited 3: rebase conflict kept"


def test_the_error_line_in_a_failure_reason_is_capped(tmp_path) -> None:
    program = fake_ops(tmp_path / "bin", exit_code=2, stderr="x" * 5000 + "\n")
    result = opsread.run_ops(program, tmp_path, ["status", "--json"])
    assert result.failure_reason == "ops exited 2: " + "x" * 199 + "…"
