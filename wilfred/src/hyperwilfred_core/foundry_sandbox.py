"""Fail-closed OS boundary for generated Python, independent of Python audit hooks."""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path


def sandbox_command(root: Path) -> list[str]:
    """No host home/auth, writes, sockets, or child executables are permitted."""
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("Why: enforced macOS sandbox is unavailable. "
                           "Unlock: run on a supported macOS host; isolation is mandatory.")
    python = Path(sys.executable).resolve()
    # python.org framework builds ship bin/python3.x as a stub that re-spawns Python.app,
    # an exec the profile denies. Start the real interpreter directly instead.
    app = Path(sys.base_prefix).resolve() / "Resources/Python.app/Contents/MacOS/Python"
    if app.is_file():
        python = app
    readable = [str(root), str(Path(sys.base_prefix).resolve()), "/System/Library", "/usr/lib"]
    paths = " ".join(f"(subpath {json.dumps(path)})" for path in readable)
    profile = (
        '(version 1) (deny default) (allow sysctl-read) (allow mach-lookup) '
        '(allow signal (target self)) '
        f'(allow process-exec (literal {json.dumps(str(python))})) '
        f'(allow file-read* {paths} (literal "/dev/null") (literal "/dev/urandom")) '
        # dyld opens the root DIRECTORY itself. require-all + literal does not grant descendants.
        '(allow file-read-data (require-all (literal "/") (vnode-type DIRECTORY))) '
        '(allow file-read-metadata)'
    )
    return ["/usr/bin/sandbox-exec", "-p", profile, str(python), "-I", str(root / "runner.py")]
