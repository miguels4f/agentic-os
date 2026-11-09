"""CI-parity gate — run what the target repo's own pull-request CI runs, before it runs there.

The per-stack chains (ruff/mypy/pytest, ``npm run build/lint/test``) are a generic floor. They
never see the checks a real repository adds on top: a task-contract scope script, a domain
linter, contract tests in a sibling workspace, a secret scanner, ESLint over ``test/``. Work
that is green here then turns red on the pull request. This evaluator closes that gap: it reads
``.github/workflows/*.yml`` at the target's git root, selects the workflows a ``pull_request``
triggers, and executes their ``run:`` steps locally, in order, with the step's
``working-directory``, ``env`` and ``matrix`` values. A failing step is an ERROR, so it
hard-blocks exactly like a failing pytest.

It also checks statically that every path a workflow names (``working-directory``,
``cache-dependency-path``) exists — a missing lockfile fails ``actions/setup-node`` before any
test runs, and no local command would ever notice.

Safety and honesty rules:

* Only ``pull_request`` workflows are executed. Push/deploy workflows are only path-checked,
  never executed.
* The workflows that run are the ones committed at the run's ``base_commit`` — the work under
  evaluation cannot delete or weaken its own CI. A workflow the run changed is reported.
* Every job (and matrix entry) runs in its own fresh clone — HEAD plus the uncommitted work,
  without ignored files — removed afterwards, as GitHub gives each job its own runner. The clone
  has its own refs and no remote, so a ``git push``/``git tag`` in a step cannot reach the real
  repository, and nothing a step does touches the user's worktree. The base branch exists in the
  clone under its real name, so ``git diff main...HEAD`` works as on GitHub.
* Steps get an allow-listed environment (PATH, locale, proxy/CA settings, tool caches) and a
  throw-away HOME, never the engine's full environment: cloud credentials, API tokens,
  ``~/.npmrc``, ``~/.config/gh``, ``~/.aws`` and the git credential helpers stay out of reach;
  git and ssh run non-interactively without the user's keys. ``HW_CI_PARITY_ENV_PASS`` adds
  names. This is not a sandbox — steps run as the current user with network access and could
  still read files by absolute path, like the repo's own ``npm test`` would.
* The gate fails closed: no pull-request step able to run on this machine (e.g. every job is
  ``ubuntu-latest`` and this is a Mac), a clone that cannot be built, an unknown base commit or
  a used-up time budget (``ci_parity_budget_s``, default :data:`DEFAULT_BUDGET_S`) block.
* Setup actions are checked, not assumed: if the tool an ``actions/setup-*``/``setup-uv``/
  ``pnpm/action-setup`` step installs is missing here (or the action is unknown), the job is
  NOT verified rather than failed; if the installed version differs from the one requested,
  failures in that job are reported as not conclusive instead of blocking.
* ``needs:``/``if:`` follow GitHub: a job whose dependency failed or was skipped is skipped
  unless its ``if:`` uses a status function (``always()``, ``failure()``); later steps run after
  a failure only when their ``if:`` asks for it. ``continue-on-error`` may be an expression.
* A step that needs something unavailable here — a ``secrets.``/``vars.``/``steps.`` value, a
  non-bash shell — is skipped and reported. A job that cannot be reproduced — another OS,
  ``services:``/``container:``, a self-hosted runner, an ``if:`` that cannot be evaluated — is
  skipped and reported. Skips are WARNING findings: never a silent pass, never a false block.
* No environment variable turns the gate off. A human can, with an entry committed before the
  run in ``.hw-gates.json`` (``off``, or ``allow-unverified`` to accept that nothing could run
  here) — see :mod:`hyperwilfred_core.gate_declaration`. A nested invocation (a CI step whose
  suite calls the gate on its own checkout) is recognised only inside that step's job clone.

Workflow ``on.pull_request`` branch/path filters are not applied: every pull-request workflow
runs. That can only add checks, never hide one.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from hyperwilfred_core import gate_declaration
from hyperwilfred_core.evaluators.base import BaseEvaluator, run_subprocess
from hyperwilfred_core.evaluators.verification import (
    CheckKind,
    CheckResult,
    Finding,
    Severity,
)

WORKFLOW_DIR = ".github/workflows"
NESTED_ENV = "HW_CI_PARITY_ACTIVE"
JOB_PREFIX = "hw-ci-job-"
STEP_TIMEOUT_S: float = 900.0
DEFAULT_BUDGET_S: float = 3600.0
MAX_MATRIX_COMBOS = 16
OUTPUT_TAIL_CHARS = 1500
STATUS_KEY = "__status"


class Unresolvable(Exception):
    """An expression references something the local run cannot know (secrets, step outputs)."""


# --------------------------------------------------------------------------- expressions

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)|(?P<op>==|!=|<=|>=|&&|\|\||[!<>(),\[\]])"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_*][A-Za-z0-9_\-]*)*))"
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise Unresolvable(f"cannot parse expression {expr!r}")
        pos = m.end()
        kind = m.lastgroup or ""
        tokens.append((kind, m.group(kind)))
    return tokens


def _truthy(value: Any) -> bool:
    return value not in (None, False, 0, "")


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    return bool(a == b)


class _Parser:
    """Recursive-descent evaluator for the GitHub Actions expression subset CI files use."""

    def __init__(self, tokens: list[tuple[str, str]], context: dict[str, Any]) -> None:
        self.tokens = tokens
        self.i = 0
        self.context = context

    def _peek(self) -> str | None:
        return self.tokens[self.i][1] if self.i < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def parse(self) -> Any:
        value = self._or()
        if self.i != len(self.tokens):
            raise Unresolvable("trailing tokens in expression")
        return value

    def _or(self) -> Any:
        value = self._and()
        while self._peek() == "||":
            self._take()
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self) -> Any:
        value = self._cmp()
        while self._peek() == "&&":
            self._take()
            right = self._cmp()
            value = right if _truthy(value) else value
        return value

    def _cmp(self) -> Any:
        value = self._unary()
        while self._peek() in ("==", "!=", "<", ">", "<=", ">="):
            op = self._take()[1]
            right = self._unary()
            if op == "==":
                value = _equal(value, right)
            elif op == "!=":
                value = not _equal(value, right)
            else:
                try:
                    value = {
                        "<": value < right,
                        ">": value > right,
                        "<=": value <= right,
                        ">=": value >= right,
                    }[op]
                except TypeError as exc:
                    raise Unresolvable(f"cannot compare {value!r} {op} {right!r}") from exc
        return value

    def _unary(self) -> Any:
        if self._peek() == "!":
            self._take()
            return not _truthy(self._unary())
        return self._primary()

    def _primary(self) -> Any:
        if self.i >= len(self.tokens):
            raise Unresolvable("unexpected end of expression")
        kind, text = self._take()
        if text == "(":
            value = self._or()
            if self._peek() != ")":
                raise Unresolvable("unbalanced parentheses")
            self._take()
            return value
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return float(text) if "." in text else int(text)
        if kind != "name":
            raise Unresolvable(f"unexpected token {text!r}")
        if self._peek() == "(":
            return self._call(text)
        lowered = text.lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        if lowered == "null":
            return None
        value = self._lookup(text)
        while self._peek() == "[":  # matrix['os'] / env['X']
            self._take()
            key = self._or()
            if self._take()[1] != "]":
                raise Unresolvable("unbalanced index")
            value = value.get(str(key)) if isinstance(value, dict) else None
        return value

    def _lookup(self, dotted: str) -> Any:
        root, *rest = dotted.split(".")
        if root not in self.context:
            raise Unresolvable(f"'{root}' context is not available locally")
        value: Any = self.context[root]
        for part in rest:
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def _call(self, name: str) -> Any:
        self._take()  # "("
        args: list[Any] = []
        while self._peek() != ")":
            args.append(self._or())
            if self._peek() == ",":
                self._take()
            elif self._peek() != ")":
                raise Unresolvable(f"bad arguments to {name}()")
        self._take()  # ")"
        fn = name.lower()
        # Status functions read the job/step status the runner put in the context
        # ("success" | "failure" | "skipped"); with none recorded, nothing has failed yet.
        status = self.context.get(STATUS_KEY, "success")
        if fn == "always":
            return True
        if fn == "cancelled":
            return False
        if fn == "success":
            return status == "success"
        if fn == "failure":
            return status == "failure"
        if fn in ("contains", "startswith", "endswith") and len(args) == 2:
            hay, needle = args
            if fn == "contains" and isinstance(hay, list):
                return any(_equal(item, needle) for item in hay)
            hay_s, needle_s = str(hay or "").lower(), str(needle or "").lower()
            if fn == "contains":
                return needle_s in hay_s
            return hay_s.startswith(needle_s) if fn == "startswith" else hay_s.endswith(needle_s)
        if fn == "format" and args:
            text = str(args[0])
            for idx, arg in enumerate(args[1:]):
                text = text.replace("{" + str(idx) + "}", str(arg))
            return text
        raise Unresolvable(f"function {name}() is not evaluated locally")


def evaluate_expression(expr: str, context: dict[str, Any]) -> Any:
    """Evaluate one GitHub Actions expression body (without the ``${{ }}``)."""
    return _Parser(_tokenize(expr), context).parse()


_INTERP = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def interpolate(text: str, context: dict[str, Any]) -> str:
    """Replace every ``${{ expr }}`` in *text*; raise :class:`Unresolvable` if one cannot be."""

    def _sub(m: re.Match[str]) -> str:
        value = evaluate_expression(m.group(1), context)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _INTERP.sub(_sub, text)


_STATUS_FN = re.compile(r"\b(success|failure|always|cancelled)\s*\(", re.IGNORECASE)


def evaluate_condition(raw: Any, context: dict[str, Any]) -> bool:
    """Evaluate a job/step ``if:`` (bare or wrapped in ``${{ }}``).

    As on GitHub, a condition without a status function is implicitly ``success() && (...)``:
    it does not run after a failure (or, for a job, after a skipped/failed dependency).
    """
    implicit_success = context.get(STATUS_KEY, "success") == "success"
    if raw is None:
        return implicit_success
    if isinstance(raw, bool):
        return raw and implicit_success
    text = str(raw).strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.DOTALL)
    body = m.group(1) if m else text
    value = _truthy(evaluate_expression(body, context))
    return value if _STATUS_FN.search(body) else value and implicit_success


# --------------------------------------------------------------------------- workflow model


@dataclass
class _Report:
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    executed: int = 0
    duration_ms: int = 0
    pr_jobs: int = 0

    def error(self, file: str, message: str, rule: str) -> None:
        self.errors.append(
            Finding(file=file, message=message, severity=Severity.ERROR, rule_id=rule)
        )

    def warn(self, file: str, message: str, rule: str) -> None:
        self.warnings.append(
            Finding(file=file, message=message, severity=Severity.WARNING, rule_id=rule)
        )


def _triggers(doc: dict[Any, Any]) -> set[str]:
    # YAML 1.1 reads a bare ``on:`` key as boolean True.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(t) for t in on}
    if isinstance(on, dict):
        return {str(t) for t in on}
    return set()


class BaseUnavailable(RuntimeError):
    """The base commit's workflows cannot be read — the gate must block."""


def _workflow_texts(top: Path, base_commit: str | None) -> dict[str, str]:
    """``{repo-relative path: text}`` of the workflow files, at *base_commit* or on disk."""
    if base_commit is None:
        wf_dir = top / WORKFLOW_DIR
        if not wf_dir.is_dir():
            return {}
        return {
            p.relative_to(top).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted([*wf_dir.glob("*.yml"), *wf_dir.glob("*.yaml")])
        }
    env = gate_declaration.git_env()
    listing = subprocess.run(
        ["git", "-C", str(top), "ls-tree", "--name-only", f"{base_commit}:{WORKFLOW_DIR}"],
        capture_output=True, text=True, check=False, env=env,
    )  # fmt: skip
    if listing.returncode != 0:
        exists = subprocess.run(
            ["git", "-C", str(top), "cat-file", "-e", f"{base_commit}^{{commit}}"],
            capture_output=True, check=False, env=env,
        )  # fmt: skip
        if exists.returncode != 0:
            raise BaseUnavailable(f"base commit {base_commit[:12]} is not in {top.name}")
        return {}  # the base has no workflow directory
    out: dict[str, str] = {}
    for name in sorted(listing.stdout.split()):
        if not name.endswith((".yml", ".yaml")):
            continue
        rel = f"{WORKFLOW_DIR}/{name}"
        shown = subprocess.run(
            ["git", "-C", str(top), "show", f"{base_commit}:{rel}"],
            capture_output=True, text=True, check=False, env=env,
        )  # fmt: skip
        if shown.returncode != 0:
            raise BaseUnavailable(f"cannot read {rel} at {base_commit[:12]}")
        out[rel] = shown.stdout
    return out


def load_workflows(
    top: Path, base_commit: str | None = None
) -> list[tuple[Path, dict[Any, Any]]]:
    """Every parseable workflow file (unparseable ones raise), as committed at *base_commit*.

    Without a base commit the files on disk are read (direct use outside a run).
    """
    out: list[tuple[Path, dict[Any, Any]]] = []
    for rel, text in _workflow_texts(top, base_commit).items():
        doc = yaml.safe_load(text)
        if isinstance(doc, dict):
            out.append((top / rel, doc))
    return out


def changed_workflows(top: Path, base_commit: str) -> list[str]:
    """Workflow files whose working-tree content differs from *base_commit* (incl. deleted)."""
    base = _workflow_texts(top, base_commit)
    now = _workflow_texts(top, None)
    return sorted(rel for rel in {*base, *now} if base.get(rel) != now.get(rel))


def _local_os() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return sys.platform


def _runner_os(runs_on: Any) -> str | None:
    """The OS family a ``runs-on`` value selects, or None when it cannot be told."""
    labels: list[str]
    if isinstance(runs_on, str):
        labels = [runs_on]
    elif isinstance(runs_on, list):
        labels = [str(x) for x in runs_on]
    elif isinstance(runs_on, dict):
        raw = runs_on.get("labels", [])
        labels = [str(x) for x in (raw if isinstance(raw, list) else [raw])]
    else:
        return None
    joined = " ".join(labels).lower()
    for family, keys in (
        ("linux", ("ubuntu", "linux")),
        ("macos", ("macos",)),
        ("windows", ("windows",)),
    ):
        if any(k in joined for k in keys):
            return family
    return None


def expand_matrix(strategy: Any) -> list[dict[str, Any]]:
    """Cartesian product of ``strategy.matrix`` with ``include``/``exclude`` applied."""
    if not isinstance(strategy, dict) or not isinstance(strategy.get("matrix"), dict):
        return [{}]
    matrix = dict(strategy["matrix"])
    include = matrix.pop("include", None) or []
    exclude = matrix.pop("exclude", None) or []
    if any(not isinstance(v, list) for v in matrix.values()):
        raise Unresolvable("matrix built from an expression")
    keys = list(matrix)
    combos: list[dict[str, Any]] = [
        dict(zip(keys, vals, strict=True)) for vals in itertools.product(*matrix.values())
    ]
    if not keys:
        combos = []
    combos = [
        c
        for c in combos
        if not any(
            all(c.get(k) == v for k, v in ex.items()) for ex in exclude if isinstance(ex, dict)
        )
    ]
    for inc in include:
        if not isinstance(inc, dict):
            continue
        matched = False
        for c in combos:
            if all(c.get(k) == v for k, v in inc.items() if k in keys):
                c.update({k: v for k, v in inc.items() if k not in keys})
                matched = True
        if not matched:
            combos.append(dict(inc))
    return combos or [{}]


def _git_out(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def git_toplevel(path: Path) -> Path | None:
    out = _git_out(path, "rev-parse", "--show-toplevel")
    return Path(out) if out else None


def default_branch(top: Path) -> str:
    """The repository's default branch name (``origin/HEAD``), else ``main``."""
    ref = _git_out(top, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    return ref.split("/", 1)[1] if "/" in ref else "main"


def _base_context(top: Path) -> dict[str, Any]:
    head_ref = _git_out(top, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    runner_os = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}.get(_local_os(), "")
    return {
        "github": {
            "event_name": "pull_request",
            "ref": "refs/pull/0/merge",
            "ref_name": "0/merge",
            "base_ref": default_branch(top),
            "head_ref": head_ref,
            "workspace": str(top),
            "sha": _git_out(top, "rev-parse", "HEAD"),
            "event": {"pull_request": {"draft": False}},
            "actor": "local",
        },
        "runner": {"os": runner_os, "temp": tempfile.gettempdir()},
        "env": {},
        "matrix": {},
        "inputs": {},
    }


def _resolve_env(raw: Any, context: dict[str, Any]) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    # YAML reads `true`/`yes` as booleans; the runner exposes them as "true"/"false".
    return {
        str(k): ("true" if v else "false") if isinstance(v, bool) else interpolate(str(v), context)
        for k, v in raw.items()
    }


def _check_path(
    top: Path, rel: str, report: _Report, wf_name: str, what: str, blocking: bool
) -> None:
    for entry in (p.strip() for p in rel.splitlines()):
        if not entry or entry.startswith("!"):
            continue
        present = any(top.glob(entry)) if any(c in entry for c in "*?[") else (top / entry).exists()
        if present:
            continue
        message = f"{what} '{entry}' does not exist — the CI step would fail"
        if blocking:
            report.error(wf_name, message, "ci-parity-missing-path")
        else:
            report.warn(wf_name, message, "ci-parity-missing-path")


# --------------------------------------------------------------------------- execution


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``$GITHUB_ENV`` file (``K=V`` and ``K<<DELIM`` heredoc forms)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delim = line.split("<<", 1)
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            out[key.strip()] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value
        i += 1
    return out


# --------------------------------------------------------------------------- environment

# Host variables a CI step may see. Everything else — cloud credentials, API tokens, agent
# sockets — is dropped: GitHub gives untrusted pull-request code none of them, and neither do we.
# Proxy/CA variables are kept because dependency installs need them behind a corporate or
# sandbox proxy. A human can pass more names in ``HW_CI_PARITY_ENV_PASS`` (comma-separated).
_ENV_ALLOW = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ",
        "TMPDIR", "TEMP", "TMP",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO", "PIP_CERT",
        "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "npm_config_cache", "PNPM_HOME",
        "NVM_DIR", "PYENV_ROOT", "JAVA_HOME", "GOPATH", "GOROOT", "CARGO_HOME", "RUSTUP_HOME",
        "PLAYWRIGHT_BROWSERS_PATH",
    }
)  # fmt: skip
ENV_PASS_VAR = "HW_CI_PARITY_ENV_PASS"


# Caches that would otherwise be looked up under HOME. They hold downloads, not credentials, so
# they keep pointing at the user's real caches when the throw-away HOME replaces it.
_HOME_CACHES = {
    "UV_CACHE_DIR": ".cache/uv",
    "UV_PYTHON_INSTALL_DIR": ".local/share/uv/python",
    "npm_config_cache": ".npm",
    "PIP_CACHE_DIR": "Library/Caches/pip" if sys.platform == "darwin" else ".cache/pip",
    "PLAYWRIGHT_BROWSERS_PATH": (
        "Library/Caches/ms-playwright" if sys.platform == "darwin" else ".cache/ms-playwright"
    ),
}


# Never passed through HW_CI_PARITY_ENV_PASS: they would undo the throw-away HOME and the
# non-interactive git/ssh setup below.
_NEVER_PASS = ("HOME", "XDG_CONFIG_HOME", "GIT_", "SSH_", "GNUPGHOME")


def passed_names() -> list[str]:
    """Names a human listed in ``HW_CI_PARITY_ENV_PASS``, minus the forbidden ones."""
    names = {n.strip() for n in os.environ.get(ENV_PASS_VAR, "").split(",") if n.strip()}
    return sorted(n for n in names if not n.startswith(_NEVER_PASS))


def host_env(home: Path | None = None) -> dict[str, str]:
    """The allow-listed subset of the host environment passed to CI steps.

    With *home*, HOME becomes that throw-away directory (no dotfiles, no credentials), caches
    keep their real location, and git/ssh are made non-interactive with no user config,
    credential helper or key.
    """
    extra = set(passed_names())
    env = {
        k: v for k, v in os.environ.items() if k in _ENV_ALLOW or k in extra or k.startswith("LC_")
    }
    if home is None:
        return env
    real_home = Path(os.environ.get("HOME") or Path.home())
    for name, rel in _HOME_CACHES.items():
        if name not in env and (real_home / rel).exists():
            env[name] = str(real_home / rel)
    empty_config = home / ".gitconfig"
    empty_config.touch()
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        GIT_CONFIG_GLOBAL=str(empty_config),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="/usr/bin/false",
        SSH_ASKPASS="/usr/bin/false",
        GIT_SSH_COMMAND="ssh -F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes "
        "-o IdentityFile=/dev/null -o UserKnownHostsFile=/dev/null",
    )
    return env


# --------------------------------------------------------------------------- setup actions

# setup action -> (binary it provides, input naming the requested version, version parts
# compared). An action not listed here — or any other ``actions/setup-*`` — makes its job
# unverifiable, because the tool it would install cannot be assumed on this machine.
_SETUP_TOOLS: dict[str, tuple[str, str | None, int]] = {
    "actions/setup-node": ("node", "node-version", 1),
    "actions/setup-python": ("python3", "python-version", 2),
    "astral-sh/setup-uv": ("uv", None, 0),
    "pnpm/action-setup": ("pnpm", "version", 1),
    "actions/setup-java": ("java", None, 0),
    "actions/setup-go": ("go", None, 0),
}
_NO_SETUP_NEEDED = (
    "actions/checkout",
    "actions/cache",
    "actions/upload-artifact",
    "actions/download-artifact",
)
_VERSION = re.compile(r"(\d+(?:\.\d+)*)")


def _local_version(binary: str) -> str | None:
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _VERSION.search(proc.stdout + proc.stderr)
    return m.group(1) if m else None


def _setup_check(uses: str, with_: dict[Any, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    """Classify a setup action: ("ok"|"mismatch"|"missing"|"unknown", explanation)."""
    name = uses.split("@", 1)[0]
    spec = _SETUP_TOOLS.get(name)
    if spec is None:
        return "unknown", f"'{uses}' installs a toolchain that is not reproduced locally"
    binary, version_input, parts = spec
    if shutil.which(binary) is None:
        return "missing", f"'{uses}' provides '{binary}', which is not installed here"
    wanted_raw = with_.get(version_input) if version_input else None
    if wanted_raw is None or parts == 0:
        return "ok", ""
    try:
        wanted = interpolate(str(wanted_raw), ctx).strip()
    except Unresolvable:
        return "ok", ""
    if not re.fullmatch(r"\d+(\.\d+)*(\.x)?", wanted):
        return "ok", ""  # ranges/aliases (lts/*, >=18): cannot compare honestly
    have = _local_version(binary)
    if have is None:
        return "ok", ""
    want_parts = wanted.removesuffix(".x").split(".")[:parts]
    have_parts = have.split(".")[: len(want_parts)]
    if want_parts != have_parts:
        return "mismatch", f"'{uses}' asks for {binary} {wanted}, this machine has {have}"
    return "ok", ""


# --------------------------------------------------------------------------- workspaces


@contextlib.contextmanager
def job_workspace(top: Path, base_commit: str | None = None) -> Iterator[Path]:
    """A fresh clone of *top* for one job: HEAD plus the uncommitted work, nothing else.

    GitHub gives every job (and matrix entry) its own clean checkout, so installs, build output
    and files one job writes never leak into another — and nothing a CI step does touches the
    user's worktree. It is a clone, not a ``git worktree``: its refs are its own and it has no
    remote, so a step cannot push, tag or rewrite the real repository. The base branch is
    created at *base_commit* so ``git diff <base>...HEAD`` behaves as on the pull request.
    Ignored files (node_modules, .venv, build caches) are not copied, exactly as on a fresh
    runner. Any failure to build it raises ``RuntimeError``.
    """
    tmp = Path(tempfile.mkdtemp(prefix=JOB_PREFIX))
    ws = tmp / "ws"

    def _git(*args: str, cwd: Path = ws, data: bytes | None = None) -> bytes:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args], input=data, capture_output=True, check=False
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()[:300]
            raise RuntimeError(f"cannot build the job's clean checkout (git {args[0]}): {err}")
        return proc.stdout

    try:
        head = _git("rev-parse", "HEAD", cwd=top).decode().strip()
        _git("clone", "--quiet", "--no-checkout", str(top), str(ws), cwd=tmp)
        _git("checkout", "--quiet", "--detach", head)
        _git("remote", "remove", "origin")
        if base_commit is not None:
            _git("branch", "--force", default_branch(top), base_commit)
        diff = _git("diff", "HEAD", "--binary", cwd=top)
        if diff.strip():
            _git("apply", "--binary", "--whitespace=nowarn", data=diff)
        untracked = _git("ls-files", "--others", "--exclude-standard", "-z", cwd=top).decode(
            "utf-8", errors="surrogateescape"
        )
        for rel in filter(None, untracked.split("\0")):
            src, dst = top / rel, ws / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        yield ws
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _nested_in_job(target: Path) -> bool:
    """True only when *target* lies inside the job clone that the marker names.

    The marker is the clone path a running CI-parity job hands its steps. A worker that sets
    the variable by hand gains nothing unless the target really is inside a live job clone.
    """
    marker = os.environ.get(NESTED_ENV, "")
    if not marker:
        return False
    ws = Path(marker)
    tmp_root = Path(tempfile.gettempdir()).resolve()
    try:
        ws = ws.resolve(strict=True)
        target = target.resolve(strict=True)
    except OSError:
        return False
    return (
        ws.name == "ws"
        and ws.parent.name.startswith(JOB_PREFIX)
        and ws.parent.parent == tmp_root
        and (ws == target or ws in target.parents)
    )


# --------------------------------------------------------------------------- jobs

SUCCESS, FAILURE, SKIPPED = "success", "failure", "skipped"


@dataclass
class _Job:
    workflow: str
    job_id: str
    label: str
    spec: dict[Any, Any]
    matrix: dict[str, Any]
    wf_env: Any
    wf_defaults: Any


# A step is a "pytest step" only when its whole script is one pytest invocation — no chaining,
# pipes, substitutions or redirections that could hide another command's failure.
_PYTEST_STEP = re.compile(
    r"(?:(?:uv[ \t]+run(?:[ \t]+--?[\w-]+(?:[= ][\w.=-]+)?)*|poetry[ \t]+run|python3?[ \t]+-m)"
    r"[ \t]+)?pytest(?:[ \t]+[^\s;&|`$<>()]+)*"
)
# Options that stop the session early or fail it for a reason other than the tests themselves:
# with them, the parsed failing ids are not the whole story.
_PYTEST_UNSAFE_OPTION = re.compile(
    r"(?:^|[ \t])(?:-[a-wyzA-Z]*x|--exitfirst|--maxfail\S*|--cov-fail-under\S*|--lf|--last-failed"
    r"|--ff|--failed-first|--sw|--stepwise\S*|-p[ \t]+no:terminal|--deselect\S*)(?=[ \t]|$)"
)
_PYTEST_SUMMARY = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?\.py(?:::\S+)?)(?:\s|$)", re.MULTILINE)
PYTEST_IDS_MARKER = "[pytest-failing-ids @ "


def is_pytest_step(script: str) -> bool:
    """One pytest command on one line, without options that could hide failures."""
    line = script.strip()
    return (
        "\n" not in line
        and "\r" not in line
        and _PYTEST_STEP.fullmatch(line) is not None
        and _PYTEST_UNSAFE_OPTION.search(line) is None
    )


def pytest_failing_ids(finding: Finding) -> list[str] | None:
    """Repo-relative failing test ids recorded on a ``ci-parity-pytest-failed`` finding."""
    if finding.rule_id != "ci-parity-pytest-failed":
        return None
    _, sep, tail = finding.message.rpartition(PYTEST_IDS_MARKER)
    if not sep:
        return None
    where, _, ids = tail.partition("] ")
    prefix = "" if where in ("", ".") else where.rstrip("/") + "/"
    out = [prefix + i for i in ids.split() if i]
    return out or None


def _soft(raw: Any, ctx: dict[str, Any]) -> bool:
    """Evaluate a ``continue-on-error`` value (literal or expression, e.g. matrix.experimental)."""
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.DOTALL)
    try:
        return _truthy(evaluate_expression(m.group(1) if m else text, ctx))
    except Unresolvable:
        return False


def _run_job(
    job: _Job, ws: Path, ctx: dict[str, Any], report: _Report, timeout: float, deadline: float
) -> str:
    """Execute one job's steps in its own workspace; return SUCCESS, FAILURE or SKIPPED."""
    spec = job.spec
    try:
        job_env = _resolve_env(job.wf_env, ctx)
        ctx["env"] = dict(job_env)
        job_env.update(_resolve_env(spec.get("env"), ctx))
    except Unresolvable as exc:
        report.warn(
            job.workflow,
            f"{job.label}: job env not reproducible ({exc}) — skipped",
            "ci-parity-skip",
        )
        return SKIPPED
    soft_job = _soft(spec.get("continue-on-error"), ctx)
    defaults_dir = None
    for defaults in (spec.get("defaults"), job.wf_defaults):
        if isinstance(defaults, dict) and isinstance(defaults.get("run"), dict):
            defaults_dir = defaults_dir or defaults["run"].get("working-directory")

    status = SUCCESS
    toolchain_notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="hw-ci-parity-") as tmp:
        tmpdir = Path(tmp)
        env_file, path_file = tmpdir / "env", tmpdir / "path"
        extra_path: list[str] = []
        home = tmpdir / "home"
        home.mkdir()
        for n, step in enumerate(spec.get("steps") or [], start=1):
            if not isinstance(step, dict):
                continue
            name = str(step.get("name") or step.get("uses") or f"step {n}")
            where = f"{job.label} / {name}"
            ctx["env"] = dict(job_env)
            ctx[STATUS_KEY] = status
            try:
                if not evaluate_condition(step.get("if"), ctx):
                    continue
                step_env = dict(job_env)
                step_env.update(_resolve_env(step.get("env"), ctx))
                ctx["env"] = step_env
                raw_with = step.get("with")
                with_: dict[Any, Any] = raw_with if isinstance(raw_with, dict) else {}
                if "uses" in step:
                    uses = str(step["uses"])
                    cache_path = with_.get("cache-dependency-path")
                    if cache_path:
                        _check_path(
                            ws,
                            interpolate(str(cache_path), ctx),
                            report,
                            job.workflow,
                            f"{where}: cache-dependency-path",
                            True,
                        )
                    if uses.startswith(_NO_SETUP_NEEDED):
                        continue
                    if "setup" in uses.split("@", 1)[0]:
                        verdict, why = _setup_check(uses, with_, ctx)
                        if verdict in ("missing", "unknown"):
                            report.warn(
                                job.workflow,
                                f"{job.label}: {why} — job NOT verified locally",
                                "ci-parity-toolchain",
                            )
                            return SKIPPED
                        if verdict == "mismatch":
                            toolchain_notes.append(why)
                            report.warn(
                                job.workflow,
                                f"{job.label}: {why} — this job ran on the local version; "
                                "its result is NOT conclusive for the requested one",
                                "ci-parity-toolchain",
                            )
                        continue
                    report.warn(
                        job.workflow,
                        f"{where}: action '{uses}' is not reproduced locally",
                        "ci-parity-skip",
                    )
                    continue
                if "run" not in step:
                    continue
                shell = step.get("shell")
                if shell not in (None, "bash", "sh"):
                    report.warn(
                        job.workflow,
                        f"{where}: shell '{shell}' is not reproduced locally",
                        "ci-parity-skip",
                    )
                    continue
                script = interpolate(str(step["run"]), ctx)
                wd = step.get("working-directory") or defaults_dir
                cwd = ws / interpolate(str(wd), ctx) if wd else ws
                soft_step = _soft(step.get("continue-on-error"), ctx)
            except Unresolvable as exc:
                report.warn(
                    job.workflow,
                    f"{where}: not reproducible locally ({exc}) — skipped",
                    "ci-parity-skip",
                )
                continue
            if not cwd.is_dir():
                report.error(
                    job.workflow,
                    f"{where}: working-directory '{cwd.relative_to(ws)}' does not exist",
                    "ci-parity-missing-path",
                )
                status = FAILURE
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report.error(
                    job.workflow,
                    f"{where}: time budget used up before this step — NOT run",
                    "ci-parity-budget",
                )
                return FAILURE
            env = {**host_env(home), **step_env}
            env.update(
                CI="true",
                GITHUB_ACTIONS="true",
                GITHUB_WORKSPACE=str(ws),
                GITHUB_EVENT_NAME="pull_request",
                GITHUB_BASE_REF=str(ctx["github"]["base_ref"]),
                GITHUB_HEAD_REF=str(ctx["github"]["head_ref"]),
                GITHUB_REF=str(ctx["github"]["ref"]),
                GITHUB_SHA=str(ctx["github"]["sha"]),
                RUNNER_OS=str(ctx["runner"]["os"]),
                RUNNER_TEMP=str(tmpdir),
                GITHUB_ENV=str(env_file),
                GITHUB_PATH=str(path_file),
                GITHUB_OUTPUT=str(tmpdir / "output"),
                GITHUB_STEP_SUMMARY=str(tmpdir / "summary"),
                **{NESTED_ENV: str(ws)},
            )
            if extra_path:
                env["PATH"] = os.pathsep.join([*extra_path, env.get("PATH", "")])
            argv = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script]
            if shell == "sh":
                argv = ["sh", "-e", "-c", script]
            outcome = run_subprocess(argv, cwd=cwd, timeout_s=min(timeout, remaining), env=env)
            report.executed += 1
            report.duration_ms += outcome.duration_ms

            job_env.update(_read_env_file(env_file))
            if path_file.exists():
                extra_path = [
                    p for p in path_file.read_text(encoding="utf-8").splitlines() if p.strip()
                ][::-1] + extra_path
                path_file.write_text("", encoding="utf-8")
            env_file.write_text("", encoding="utf-8")

            if outcome.ran and outcome.returncode == 0:
                continue
            tail = (outcome.stdout + "\n" + outcome.stderr).strip()[-OUTPUT_TAIL_CHARS:]
            why = outcome.error or f"exit {outcome.returncode}"
            if soft_step:
                report.warn(
                    job.workflow,
                    f"{where}: failed ({why}), continue-on-error",
                    "ci-parity-soft-fail",
                )
                continue
            if soft_job or toolchain_notes:
                reason = (
                    "job has continue-on-error"
                    if soft_job
                    else "NOT conclusive: " + "; ".join(toolchain_notes)
                )
                report.warn(
                    job.workflow,
                    f"{where}: failed ({why}) — {reason}\n{tail}",
                    "ci-parity-soft-fail",
                )
            else:
                ids = (
                    sorted(set(_PYTEST_SUMMARY.findall(outcome.stdout + "\n" + outcome.stderr)))
                    # exit 1 = "tests failed"; anything else (usage error, crash, coverage) is not
                    if outcome.ran and outcome.returncode == 1 and is_pytest_step(script)
                    else []
                )
                if ids:
                    # Console output is written by the code under test, so the ids are only
                    # trusted if the same step passes once exactly those tests are deselected.
                    left = deadline - time.monotonic()
                    confirm = (
                        run_subprocess(
                            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
                             script + "".join(f" --deselect {shlex.quote(i)}" for i in ids)],
                            cwd=cwd, timeout_s=min(timeout, left), env=env,
                        )  # fmt: skip
                        if left > 0
                        else None
                    )
                    # 0 = the rest passed; 5 = nothing left to run (every test was in ids)
                    if confirm is None or not (confirm.ran and confirm.returncode in (0, 5)):
                        ids = []
                if ids:
                    rel = cwd.relative_to(ws).as_posix()
                    report.error(
                        job.workflow,
                        f"{where}: failed ({why})\n{tail}\n{PYTEST_IDS_MARKER}{rel}] "
                        + " ".join(ids),
                        "ci-parity-pytest-failed",
                    )
                else:
                    report.error(
                        job.workflow, f"{where}: failed ({why})\n{tail}", "ci-parity-step-failed"
                    )
            # Later steps still run when their if: asks for it (always(), failure()).
            status = FAILURE
    if status == FAILURE and (soft_job or toolchain_notes):
        return SUCCESS if soft_job else SKIPPED
    return status


def _job_order(jobs: dict[str, Any]) -> list[str]:
    """Job ids in an order that respects ``needs:`` (cycles fall back to file order)."""
    order: list[str] = []
    pending = list(jobs)
    while pending:
        progressed = False
        for jid in list(pending):
            needs = jobs[jid].get("needs") if isinstance(jobs[jid], dict) else None
            needs_list = [needs] if isinstance(needs, str) else list(needs or [])
            if all(n in order or n not in jobs for n in needs_list):
                order.append(jid)
                pending.remove(jid)
                progressed = True
        if not progressed:
            order.extend(pending)
            break
    return order


def _combine(results: list[str]) -> str:
    if FAILURE in results:
        return FAILURE
    if results and all(r == SKIPPED for r in results):
        return SKIPPED
    return SUCCESS if SUCCESS in results else SKIPPED


def _path_check_only(
    top: Path, doc: dict[Any, Any], spec: dict[Any, Any], ctx: dict[str, Any], report: _Report,
    wf_name: str, label: str,
) -> None:  # fmt: skip
    """Push/deploy workflows are never executed — only the paths they name are checked."""
    try:
        ctx["env"] = _resolve_env(doc.get("env"), ctx)
    except Unresolvable:
        ctx["env"] = {}
    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        raw_with = step.get("with")
        with_: dict[Any, Any] = raw_with if isinstance(raw_with, dict) else {}
        for raw, what in (
            (step.get("working-directory"), "working-directory"),
            (with_.get("cache-dependency-path"), "cache-dependency-path"),
        ):
            if not raw:
                continue
            try:
                _check_path(
                    top, interpolate(str(raw), ctx), report, wf_name, f"{label}: {what}", False
                )
            except Unresolvable:
                pass


def run_parity(
    top: Path,
    *,
    step_timeout_s: float = STEP_TIMEOUT_S,
    base_commit: str | None = None,
    budget_s: float = DEFAULT_BUDGET_S,
) -> _Report:
    """Path-check every workflow and execute the pull-request ones, each job in its own clone.

    With *base_commit*, the workflows are the ones committed there; changed ones are reported.
    """
    report = _Report()
    base = _base_context(top)
    local = _local_os()
    deadline = time.monotonic() + budget_s
    try:
        workflows = load_workflows(top, base_commit)
    except yaml.YAMLError as exc:
        report.error(WORKFLOW_DIR, f"a workflow file is not valid YAML: {exc}", "ci-parity-yaml")
        return report
    except BaseUnavailable as exc:
        report.error(WORKFLOW_DIR, f"{exc} — blocked", "ci-parity-base")
        return report
    if base_commit is not None:
        for rel in changed_workflows(top, base_commit):
            report.warn(
                rel,
                f"changed by this run — the version committed at {base_commit[:12]} was run; "
                "the new one takes effect only once a human merges it",
                "ci-parity-workflow-changed",
            )

    for path, doc in workflows:
        wf_name = str(path.relative_to(top))
        is_pr = "pull_request" in _triggers(doc)
        raw_jobs = doc.get("jobs")
        jobs: dict[str, Any] = raw_jobs if isinstance(raw_jobs, dict) else {}
        outcome: dict[str, str] = {}
        for jid in _job_order(jobs):
            spec = jobs[jid]
            if not isinstance(spec, dict):
                continue
            if "uses" in spec:  # reusable workflow call
                if is_pr:
                    report.pr_jobs += 1
                    report.warn(
                        wf_name,
                        f"{jid}: reusable workflow '{spec['uses']}' is not reproduced",
                        "ci-parity-skip",
                    )
                outcome[jid] = SKIPPED
                continue
            try:
                combos = expand_matrix(spec.get("strategy"))
            except Unresolvable as exc:
                if is_pr:
                    report.pr_jobs += 1
                    report.warn(wf_name, f"{jid}: {exc} — skipped", "ci-parity-skip")
                outcome[jid] = SKIPPED
                continue
            if len(combos) > MAX_MATRIX_COMBOS:
                report.warn(
                    wf_name,
                    f"{jid}: {len(combos)} matrix combinations, "
                    f"running the first {MAX_MATRIX_COMBOS}",
                    "ci-parity-skip",
                )
                combos = combos[:MAX_MATRIX_COMBOS]

            needs = spec.get("needs")
            needs_list = [needs] if isinstance(needs, str) else list(needs or [])
            need_results = [outcome.get(n, SKIPPED) for n in needs_list if n in jobs]
            if FAILURE in need_results:
                job_status = FAILURE
            elif SKIPPED in need_results:
                job_status = SKIPPED
            else:
                job_status = SUCCESS

            results: list[str] = []
            for combo in combos:
                label = f"{jid}" + (
                    f" ({', '.join(str(v) for v in combo.values())})" if combo else ""
                )
                ctx = {**base, "matrix": combo, "env": {}, STATUS_KEY: job_status}
                if not is_pr:
                    _path_check_only(top, doc, spec, ctx, report, wf_name, label)
                    continue
                try:
                    run_it = evaluate_condition(spec.get("if"), ctx)
                except Unresolvable as exc:
                    report.pr_jobs += 1
                    report.warn(
                        wf_name,
                        f"{label}: if-condition not evaluable locally ({exc}) — skipped",
                        "ci-parity-skip",
                    )
                    results.append(SKIPPED)
                    continue
                if not run_it:
                    if job_status != SUCCESS:
                        report.warn(
                            wf_name,
                            f"{label}: skipped because a needed job "
                            + (
                                "failed" if job_status == FAILURE else "was skipped or NOT verified"
                            ),
                            "ci-parity-skip",
                        )
                    results.append(SKIPPED)
                    continue
                try:
                    runs_on = (
                        interpolate(str(spec.get("runs-on")), ctx)
                        if isinstance(spec.get("runs-on"), str)
                        else spec.get("runs-on")
                    )
                except Unresolvable:
                    runs_on = None
                family = _runner_os(runs_on)
                report.pr_jobs += 1
                if family != local:
                    report.warn(
                        wf_name,
                        f"{label}: runs-on {runs_on!r} ({family or 'unknown'}) cannot be "
                        f"reproduced on {local} — NOT verified locally",
                        "ci-parity-os",
                    )
                    results.append(SKIPPED)
                    continue
                if spec.get("services") or spec.get("container"):
                    report.warn(
                        wf_name,
                        f"{label}: needs services/container — NOT verified locally",
                        "ci-parity-skip",
                    )
                    results.append(SKIPPED)
                    continue
                if time.monotonic() >= deadline:
                    report.error(
                        wf_name,
                        f"{label}: time budget of {budget_s:.0f}s used up — job NOT run",
                        "ci-parity-budget",
                    )
                    results.append(FAILURE)
                    continue
                job = _Job(wf_name, jid, label, spec, combo, doc.get("env"), doc.get("defaults"))
                try:
                    with job_workspace(top, base_commit) as ws:
                        ctx["github"] = {**ctx["github"], "workspace": str(ws)}
                        results.append(
                            _run_job(job, ws, ctx, report, step_timeout_s, deadline)
                        )
                except RuntimeError as exc:
                    report.error(wf_name, f"{label}: {exc} — blocked", "ci-parity-workspace")
                    results.append(FAILURE)
            outcome[jid] = _combine(results) if is_pr else SKIPPED
    return report


class CiParityEvaluator(BaseEvaluator):
    """Run the target repository's pull-request CI locally (see module docstring)."""

    kind: ClassVar[CheckKind] = CheckKind.CONSISTENCY
    name: ClassVar[str] = "ci-parity"
    binary: ClassVar[str] = "bash"
    default_timeout_s: ClassVar[float] = STEP_TIMEOUT_S

    def run(self, target_path: Path, *, config: dict[str, Any] | None = None) -> CheckResult:
        def _result(
            details: str, *, passed: bool = True, severity: Severity = Severity.INFO
        ) -> CheckResult:
            return CheckResult(
                kind=self.kind, name=self.name, passed=passed, severity=severity,
                details=details, duration_ms=0,
            )  # fmt: skip

        cfg = config or {}
        if not target_path.exists():
            return _result(
                f"ci-parity: target {target_path} does not exist",
                passed=False, severity=Severity.ERROR,
            )  # fmt: skip
        if _nested_in_job(target_path):
            return _result(
                "ci-parity: nested inside a CI-parity job's own clone — not run again",
                severity=Severity.WARNING,
            )
        top, git_error = gate_declaration.git_top(target_path)
        if git_error:
            return _result(
                f"ci-parity: git failed on the target ({git_error}) — blocked",
                passed=False, severity=Severity.ERROR,
            )  # fmt: skip
        if top is None and cfg.get("base_commit"):
            return _result(
                "ci-parity: the run started from a git repository but the target is no longer "
                "inside one — blocked",
                passed=False, severity=Severity.ERROR,
            )  # fmt: skip
        if top is None:
            return _result(
                "ci-parity: target is not inside a git repository — nothing to mirror",
                severity=Severity.WARNING,
            )
        base_commit: str | None = None
        switch: gate_declaration.GateSwitch | None = None
        switch_error: str | None = None
        if "base_commit" in cfg:
            # Inside a run the engine always passes the base; its absence means it is unknown.
            if not cfg["base_commit"]:
                return _result(
                    "ci-parity: the run has no base commit, so its committed CI cannot be "
                    "read — blocked (commit the repository before `hw plan`)",
                    passed=False, severity=Severity.ERROR,
                )  # fmt: skip
            base_commit = str(cfg["base_commit"])
            switch, switch_error = gate_declaration.read_switch(top, base_commit, self.name)
            if switch is not None and switch.mode == "off":
                return _result(
                    f"ci-parity: turned off by {switch.note()}", severity=Severity.WARNING
                )

        def _number(key: str, default: float) -> float:
            try:
                return float(cfg.get(key) or default)
            except (TypeError, ValueError):
                return default

        report = run_parity(
            top,
            step_timeout_s=_number("ci_parity_step_timeout_s", STEP_TIMEOUT_S),
            base_commit=base_commit,
            budget_s=_number("ci_parity_budget_s", DEFAULT_BUDGET_S),
        )
        if passed_names():
            report.warn(
                ENV_PASS_VAR,
                f"host variables passed to CI steps by a human: {', '.join(passed_names())}",
                "ci-parity-env-pass",
            )
        if switch_error:
            report.warn(
                gate_declaration.DECLARATION_FILE,
                f"declaration ignored — {switch_error}",
                "ci-parity-declaration",
            )
        if report.pr_jobs == 0 and not report.errors and not report.warnings:
            note = "no pull-request job in the committed workflows"
            if not (top / WORKFLOW_DIR).is_dir() and base_commit is None:
                note = f"no {WORKFLOW_DIR} in {top.name}"
            return _result(f"ci-parity: {note} — nothing to mirror")
        if report.pr_jobs and report.executed == 0 and not report.errors:
            unverified = (
                f"ci-parity: none of the {report.pr_jobs} pull-request job(s) could run on this "
                "machine (see findings) — the CI was NOT verified"
            )
            if switch is not None and switch.mode == "allow-unverified":
                report.warn(".", f"{unverified}; accepted by {switch.note()}", "ci-parity-none")
            else:
                report.error(
                    ".",
                    f"{unverified}. Run on a matching machine, or a human commits "
                    f"{gate_declaration.DECLARATION_FILE} with mode allow-unverified before "
                    "the run",
                    "ci-parity-none",
                )

        findings = [*report.errors, *report.warnings]
        summary = f"ci-parity: {report.executed} CI step(s) executed locally"
        if report.errors:
            first = report.errors[0]
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                details=f"{summary}; {len(report.errors)} would fail on the pull request. "
                f"First: [{first.file}] {first.message}",
                duration_ms=report.duration_ms,
                findings=findings,
            )
        if report.warnings:
            return CheckResult(
                kind=self.kind,
                name=self.name,
                passed=True,
                severity=Severity.WARNING,
                details=f"{summary}; {len(report.warnings)} part(s) NOT verified locally "
                "(see findings) — CI may still fail there",
                duration_ms=report.duration_ms,
                findings=findings,
            )
        return CheckResult(
            kind=self.kind,
            name=self.name,
            passed=True,
            details=f"{summary}; all passed",
            duration_ms=report.duration_ms,
            findings=findings,
        )


__all__ = [
    "BaseUnavailable",
    "CiParityEvaluator",
    "Unresolvable",
    "evaluate_condition",
    "evaluate_expression",
    "expand_matrix",
    "changed_workflows",
    "default_branch",
    "git_toplevel",
    "interpolate",
    "is_pytest_step",
    "pytest_failing_ids",
    "load_workflows",
    "run_parity",
]
