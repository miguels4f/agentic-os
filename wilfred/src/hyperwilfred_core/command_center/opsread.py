"""The allowlisted ``ops`` reader: the only process the command center starts besides git.

``ops`` is the owner's command for the tasks, projects and sales files kept in
``<workspace>/ops`` (design: Ideas/ops/specs/2026-10-14-notion-to-ops-design.md, outside this
repository). The command center runs exactly two of its read-only forms, ``export --json`` and
``status --json``, each followed by ``--root <workspace>/ops`` (ops takes its options after the
subcommand and never reads the working directory). Anything else raises ``ValueError`` BEFORE a
process starts.

Every call: the program's absolute path and an argument list (never a shell), no standard
input, a 30-second timeout and a cap on the output size. ``ops`` itself guarantees that both
commands are read-only and offline, and runs its git with ``GIT_OPTIONAL_LOCKS=0`` and
``core.fsmonitor=false`` after dropping git's repository-local variables. The environment given
here is a second line of defence that survives that scrubbing: inherited ``GIT_*`` and
``PYTHON*`` variables are dropped (they could point git or Python at other files),
``GIT_OPTIONAL_LOCKS=0``, ``GIT_TERMINAL_PROMPT=0`` so a credential prompt fails instead of
hanging, and ``PYTHONDONTWRITEBYTECODE=1`` so Python writes no ``__pycache__`` next to the
tool's code, which lives inside the workspace. No ``GIT_CONFIG_*`` entries are passed: ``ops``
drops ``GIT_CONFIG_COUNT``, so they would never reach git.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_STDERR_LINE_CHARS = 200
TIMEOUT_SECONDS = 30
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
# The only argument lists accepted (spec section 14: "ops export --json", "ops status --json").
# ``--root`` is never accepted from a caller: ``build_argv`` appends it.
ALLOWED_ARGS = (("export", "--json"), ("status", "--json"))


@dataclass(frozen=True)
class OpsResult:
    """Outcome of one allowlisted ops call. ``returncode`` is ``None`` if ops never finished."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None

    @property
    def command(self) -> str:
        """The exact command line, for use as the source of a fact."""
        return shlex.join(self.argv)

    @property
    def failure_reason(self) -> str:
        if self.timed_out:
            return f"ops timed out after {TIMEOUT_SECONDS} seconds"
        if self.error is not None:
            return self.error
        detail = self.stderr.strip().splitlines()
        line = detail[-1] if detail else ""
        if len(line) > MAX_STDERR_LINE_CHARS:
            line = line[: MAX_STDERR_LINE_CHARS - 1] + "…"
        return f"ops exited {self.returncode}" + (f": {line}" if line else "")


def validate_args(args: list[str]) -> None:
    """Raise ``ValueError`` unless ``args`` is one of the two read-only forms."""
    if tuple(args) not in ALLOWED_ARGS:
        raise ValueError(f"ops call is not in the read-only allowlist: {shlex.join(args)}")


def build_argv(ops_bin: Path, args: list[str], root: Path) -> tuple[str, ...]:
    """``<ops_bin> <subcommand> --json --root <root>``: options after the subcommand."""
    validate_args(args)
    if not root.is_absolute():
        raise ValueError(f"ops root must be an absolute path: {root}")
    return (str(ops_bin), *args, "--root", str(root))


def _environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "PYTHON"))}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_ops(ops_bin: Path, root: Path, args: list[str]) -> OpsResult:
    """Run one allowlisted ops command on the ops repository ``root``; capture its output.

    Never raises for a failure, a timeout, a missing program or oversized output: those come
    back in the result so the caller can record a problem. Raises ``ValueError`` only for a
    call outside the allowlist or a relative ``root``, before any process starts.
    """
    argv = build_argv(ops_bin, args, root)
    try:
        # Argument list, no shell; ``args`` was validated against the allowlist above. The
        # working directory is the ops repository too, although ops only reads ``--root``.
        done = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        return OpsResult(argv, None, "", "", timed_out=True)
    except OSError as exc:
        return OpsResult(argv, None, "", "", error=f"ops could not be started: {exc}")
    stderr = done.stderr.decode("utf-8", "replace")
    if len(done.stdout) > MAX_OUTPUT_BYTES:
        reason = f"ops printed more than {MAX_OUTPUT_BYTES} bytes"
        return OpsResult(argv, done.returncode, "", stderr, error=reason)
    return OpsResult(argv, done.returncode, done.stdout.decode("utf-8", "replace"), stderr)
