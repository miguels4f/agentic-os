"""Human switches for the flaky-test and CI-parity gates, read AS COMMITTED at the run's base.

A gate a worker can switch off is not a gate. The switches therefore live in
``.hw-gates.json`` at the repository root and are read with ``git show <base_commit>:...`` —
the version committed *before* ``hw plan``, exactly like ``.hw-known-failures.json``. An edit
made during the run (uncommitted, or committed after the base) has no effect, and no
environment variable can turn a gate off.

```json
{"version": 1, "gates": {
  "ci-parity": {"mode": "allow-unverified", "reason": "CI runs on ubuntu; this Mac cannot"},
  "flaky-tests": {"mode": "off", "reason": "suite needs the lab database"}
}}
```

* ``off`` — the gate does not run; it reports a visible WARNING naming the file, the commit and
  the reason.
* ``allow-unverified`` (ci-parity only) — when no pull-request CI step could run on this machine
  the gate reports a WARNING instead of blocking. Steps that do run still block on failure.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

DECLARATION_FILE = ".hw-gates.json"
MODES: dict[str, frozenset[str]] = {
    "ci-parity": frozenset({"off", "allow-unverified"}),
    "flaky-tests": frozenset({"off"}),
}


def git_env() -> dict[str, str]:
    """Environment for git reads of base-commit content: no replace refs, no legacy grafts.

    ``git replace`` (or ``.git/info/grafts``) can make ``<base>:file`` show content that was
    never committed there; reads that decide what a gate checks must see the real objects.
    """
    return {
        **os.environ,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": os.devnull,
        # git's messages are parsed (``not a git repository``); keep them untranslated.
        "LC_ALL": "C",
        "LANGUAGE": "C",
    }


def git_top(path: Path) -> tuple[Path | None, str | None]:
    """``(toplevel, None)``, ``(None, None)`` when *path* is not in a repository at all, or
    ``(None, error)`` when git failed for any other reason (the gate must then block)."""
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False, env=git_env(),
    )  # fmt: skip
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()), None
    if "not a git repository" in proc.stderr:
        here = path.resolve()
        broken = next((d for d in (here, *here.parents) if (d / ".git").exists()), None)
        if broken is None:
            return None, None
        return None, f"{broken / '.git'} exists but git cannot read it as a repository"
    return None, (proc.stderr.strip() or f"git rev-parse exited {proc.returncode}")[:300]


@dataclass(frozen=True)
class GateSwitch:
    gate: str
    mode: str
    reason: str
    commit: str

    def note(self) -> str:
        return f"{DECLARATION_FILE} @ {self.commit[:12]}: {self.mode} — {self.reason}"


def read_switch(top: Path, base_commit: str, gate: str) -> tuple[GateSwitch | None, str | None]:
    """``(switch, error)`` for *gate* as committed at *base_commit* in the repo at *top*.

    No file, or no entry for the gate -> ``(None, None)``. An invalid file -> ``(None, error)``:
    the gate then runs normally and the error is shown in its result.
    """
    proc = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(top), "show",
         f"{base_commit}:{DECLARATION_FILE}"],
        capture_output=True,
        text=True,
        check=False,
        env=git_env(),
    )  # fmt: skip
    if proc.returncode != 0:
        return None, None
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, f"{DECLARATION_FILE} at {base_commit[:12]} is not valid JSON: {exc}"
    gates = doc.get("gates") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(gates, dict):
        return None, f"{DECLARATION_FILE} at {base_commit[:12]} must be {{version: 1, gates: {{}}}}"
    entry = gates.get(gate)
    if entry is None:
        return None, None
    if not isinstance(entry, dict):
        return None, f"{DECLARATION_FILE}: entry for {gate!r} must be an object"
    mode, reason = entry.get("mode"), str(entry.get("reason") or "").strip()
    allowed = MODES.get(gate, frozenset())
    if mode not in allowed:
        names = ", ".join(sorted(allowed)) or "none"
        return None, f"{DECLARATION_FILE}: mode {mode!r} for {gate!r} is not one of: {names}"
    if not reason:
        return None, f"{DECLARATION_FILE}: entry for {gate!r} needs a reason"
    return GateSwitch(gate=gate, mode=str(mode), reason=reason, commit=base_commit), None


__all__ = ["DECLARATION_FILE", "MODES", "GateSwitch", "git_env", "git_top", "read_switch"]
