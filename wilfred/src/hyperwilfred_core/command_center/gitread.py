"""The allowlisted, read-only git reader (specification rule 8).

Every git call of the command center goes through ``run_git``. Only the sub-commands below
are accepted, each with a closed set of options and output formats; anything else raises
``ValueError`` BEFORE a process starts. Arguments are passed as a list (never through a
shell), with ``GIT_OPTIONAL_LOCKS=0`` and ``--no-optional-locks`` so git does not refresh the
index, and a 15-second timeout.

Configuration that would make a read start another program is switched off for every call:
the file-system monitor, signature display (``gpg.program``), the pager, lazy fetching of a
partial clone, the system configuration file, and — for ``status`` — the content filters the
caller names (see ``gitconfig``).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from hyperwilfred_core.command_center.gitconfig import SAFE_FILTER_NAME

TIMEOUT_SECONDS = 15

LOG_FORMAT = "--format=%cI"
REFS_FORMAT = "--format=%(refname:short)%09%(upstream:short)"
BRANCH_FORMAT = "--format=%(refname:short)"

# A revision or revision range we are willing to pass as a positional argument: no leading
# dash (so it can never be read as an option) and no whitespace or shell-like characters.
_SAFE_REV = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./@{}^~\-]*$")
_SAFE_REF_PATTERN = re.compile(r"^refs/[A-Za-z0-9_./\-]*$")

_REV_PARSE_FLAGS = frozenset(
    {
        "--verify",
        "--quiet",
        "--git-dir",
        "--git-common-dir",
        "--show-toplevel",
        "--path-format=absolute",
        "--abbrev-ref",
        "--is-inside-work-tree",
    }
)
_STATUS_FLAGS = frozenset(
    {
        "--branch",
        "--ignore-submodules=dirty",
        "--ignore-submodules=all",
        "--untracked-files=normal",
        "--untracked-files=no",
        "--untracked-files=all",
    }
)
_STATUS_PORCELAIN = frozenset({"--porcelain", "--porcelain=v1", "--porcelain=v2"})
_BRANCH_FLAGS = frozenset({"--list", "--all", "--remotes"})

# Fixed options placed before the sub-command on every call.
_FIXED = (
    "--no-optional-locks",
    "--no-pager",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "log.showSignature=false",
)


@dataclass(frozen=True)
class GitResult:
    """Outcome of one allowlisted git call. ``returncode`` is ``None`` if git never finished."""

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
            return f"git timed out after {TIMEOUT_SECONDS} seconds"
        if self.error is not None:
            return self.error
        detail = self.stderr.strip().splitlines()
        return f"git exited {self.returncode}" + (f": {detail[-1]}" if detail else "")


def validate_args(args: list[str]) -> None:
    """Raise ``ValueError`` unless ``args`` is one of the allowlisted read-only forms.

    Output formats are a closed set: a free ``--format`` could ask git to verify signatures
    (``%G?``), which runs the configured ``gpg.program``.
    """
    if not args or not all(isinstance(a, str) for a in args):
        raise ValueError("git arguments must be a non-empty list of strings")
    sub, rest = args[0], args[1:]
    ok = False
    if sub == "rev-parse":
        ok = bool(rest) and all(a in _REV_PARSE_FLAGS or _SAFE_REV.match(a) for a in rest)
    elif sub == "status":
        ok = (
            sum(a in _STATUS_PORCELAIN for a in rest) == 1
            and all(a in _STATUS_PORCELAIN or a in _STATUS_FLAGS for a in rest)
        )
    elif sub == "branch":
        # A positional argument would CREATE a branch, so only options are accepted.
        ok = rest.count(BRANCH_FORMAT) == 1 and all(
            a == BRANCH_FORMAT or a in _BRANCH_FLAGS for a in rest
        )
    elif sub == "for-each-ref":
        ok = rest.count(REFS_FORMAT) == 1 and all(
            a == REFS_FORMAT or _SAFE_REF_PATTERN.match(a) for a in rest
        )
    elif sub == "worktree":
        ok = rest == ["list", "--porcelain"]
    elif sub == "remote":
        ok = rest == ["-v"]
    elif sub == "log":
        ok = (
            len(rest) in (2, 3)
            and rest[:2] == ["-1", LOG_FORMAT]
            and all(_SAFE_REV.match(a) for a in rest[2:])
        )
    elif sub == "rev-list":
        ok = (
            len(rest) == 3
            and rest[:2] == ["--left-right", "--count"]
            and bool(_SAFE_REV.match(rest[2]))
        )
    if not ok:
        raise ValueError(f"git call is not in the read-only allowlist: {shlex.join(args)}")


def _environment() -> dict[str, str]:
    # Drop inherited GIT_* variables: they could redirect git to another repository or index.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["LC_ALL"] = "C"
    return env


def _filter_overrides(names: Iterable[str]) -> list[str]:
    overrides: list[str] = []
    for name in sorted(set(names)):
        if not SAFE_FILTER_NAME.match(name):
            raise ValueError(f"content filter name cannot be switched off safely: {name!r}")
        for key, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
            overrides += ["-c", f"filter.{name}.{key}={value}"]
    return overrides


def run_git(cwd: Path, args: list[str], *, disable_filters: Iterable[str] = ()) -> GitResult:
    """Run one allowlisted git sub-command in ``cwd`` and return its captured output.

    ``disable_filters`` names content filters to switch off for this call. Never raises for a
    git failure, a timeout or a missing git binary: those come back in the result so the
    caller can record a problem. Raises ``ValueError`` only for a call outside the allowlist
    or an unsafe filter name, before any process starts.
    """
    validate_args(args)
    argv = ("git", *_FIXED, *_filter_overrides(disable_filters), "-C", str(cwd), *args)
    try:
        # Argument list, no shell; ``args`` was validated against the allowlist above.
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=TIMEOUT_SECONDS,
            env=_environment(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return GitResult(argv, None, "", "", timed_out=True)
    except OSError as exc:
        return GitResult(argv, None, "", "", error=f"git could not be started: {exc}")
    return GitResult(argv, done.returncode, done.stdout, done.stderr)


_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://(.*)$", re.DOTALL)
_SCP_WITH_PASSWORD = re.compile(r"^([^@/:\s]+):[^@/\s]+@(.+)$", re.DOTALL)


def strip_credentials(url: str) -> str:
    """Remove secrets from a remote URL (``https://user:token@host/x`` → ``https://host/x``).

    - Any ``http`` flavour: the whole user part goes (a bare user there is usually a token).
    - Other schemes (``ssh://git@host/x``): the user name stays, a password goes.
    - The query string and fragment are dropped: they can carry ``?access_token=…``.
    - ``user:password@host:path`` without a scheme loses the password.
    When in doubt this removes too much rather than too little; the result is for display.
    """
    scheme_match = _SCHEME.match(url)
    if not scheme_match:
        scp = _SCP_WITH_PASSWORD.match(url)
        return f"{scp.group(1)}@{scp.group(2)}" if scp else url
    scheme, rest = scheme_match.groups()
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    userinfo: str | None = None
    if "@" in authority:
        userinfo, _, _ = authority.rpartition("@")
        remainder = rest[len(userinfo) + 1:]
    elif "@" in rest and ":" in authority and not authority.rsplit(":", 1)[1].isdigit():
        # ``user:pa/ss@host``: the password itself contains "/", "?" or "#".
        userinfo, _, remainder = rest.rpartition("@")
    else:
        remainder = rest
    remainder = re.split(r"[?#]", remainder, maxsplit=1)[0]
    if userinfo is None or "http" in scheme.lower():
        return f"{scheme}://{remainder}"
    return f"{scheme}://{userinfo.split(':', 1)[0]}@{remainder}"
