"""Live-demo check — is the slice's promised demo actually achievable on CURRENT data?

Tests written by the author of a feature happily assert whatever state the feature produces —
pilot run B's site test asserted "every per-account action is blocked" as correct while the demo
promised working actions. This check runs a read-only command (or reads adapter evidence) and
records a FINDING whenever the output is empty, unreachable, missing a promised marker or carries
a declared failure marker. Pure code: it reports, it does not judge intent.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from pydantic import BaseModel

from hyperwilfred_core.schemas import LiveDemo

TIMEOUT_S = 120
_MAX_EXCERPT = 400


class LiveDemoResult(BaseModel):
    achievable: bool
    promise: str
    source: str = ""  # "command" | "evidence" | "none"
    finding: str = ""
    output_excerpt: str = ""


def check(*, repo_root: Path, demo: LiveDemo) -> LiveDemoResult:
    output = ""
    source = "none"
    problem = ""
    if demo.command.strip():
        source = "command"
        try:
            proc = subprocess.run(
                shlex.split(demo.command), cwd=repo_root, capture_output=True, text=True,
                timeout=TIMEOUT_S, check=False,
            )
            output = proc.stdout + proc.stderr
            if proc.returncode != 0:
                problem = f"the demo command exited {proc.returncode}"
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            problem = f"the demo command could not run: {exc}"
    elif demo.evidence_path.strip():
        source = "evidence"
        p = Path(demo.evidence_path)
        p = p if p.is_absolute() else repo_root / p
        try:
            output = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            problem = f"the evidence file {demo.evidence_path!r} does not exist or is unreadable"
    else:
        problem = "no live-demo command or evidence was recorded"

    if not problem and not output.strip():
        problem = "the demo produced an empty result"
    if not problem:
        low = output.lower()
        hit = next((m for m in demo.failure_markers if m and m.lower() in low), None)
        if hit:
            problem = f"the output contains the failure marker {hit!r}"
        else:
            absent = [m for m in demo.must_contain if m and m.lower() not in low]
            if absent:
                problem = f"the output lacks the promised marker(s) {absent}"

    if problem:
        return LiveDemoResult(
            achievable=False, promise=demo.promise, source=source,
            finding=f"Live-demo gap: the promised demo ({demo.promise!r}) is not achievable on "
            f"current data — {problem}. Fix the product or the data; do not assert this state "
            "as correct in a test.",
            output_excerpt=output.strip()[:_MAX_EXCERPT],
        )
    return LiveDemoResult(
        achievable=True, promise=demo.promise, source=source,
        output_excerpt=output.strip()[:_MAX_EXCERPT],
    )


__all__ = ["LiveDemoResult", "check"]
