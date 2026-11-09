"""Read the detail of one run: events, quality, pending gates and cost state.

Everything is located from the run DIRECTORY, never from text stored inside a file. Files are
read as plain JSON (no engine model), a missing file is a fact rather than a problem, and
"unknown" is never reported as zero.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyperwilfred_core.command_center.model import (
    Cost,
    LastEvent,
    PendingGate,
    Problem,
    QualitySummary,
    Source,
)

EVENTS_NAME = "events.jsonl"
USAGE_NAME = "usage.json"
GATES_NAME = "gates"
MAX_EVENTS_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 500
MAX_TOKENS = 10**15
MAX_COUNT = 10**6


def parse_time(value: object) -> datetime | None:
    """An ISO 8601 text as an aware UTC datetime, or None when it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)  # the engine writes aware times; assume UTC
        return moment.astimezone(UTC)
    except (ValueError, OverflowError):  # overflow: a boundary date shifted by its offset
        return None


def _read_bytes(path: Path, limit: int) -> tuple[bytes | None, str | None, bool]:
    """Return (content, reason, missing). A missing file is not an error."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None, None, True
    except OSError as exc:
        return None, f"unreadable: {exc}", False
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None, "not a regular file", False
        if info.st_size > limit:
            return None, f"larger than {limit} bytes", False
        return path.read_bytes(), None, False
    except OSError as exc:
        return None, f"unreadable: {exc}", False


def _read_object(path: Path) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Return (JSON object, reason, missing)."""
    content, reason, missing = _read_bytes(path, MAX_JSON_BYTES)
    if content is None:
        return None, reason, missing
    try:
        loaded = json.loads(content.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        return None, f"cannot be read as JSON: {exc}", False
    if not isinstance(loaded, dict):
        return None, f"expected a JSON object, found {type(loaded).__name__}", False
    return loaded, None, False


def _optional_text(value: object) -> str | None:
    """Untrusted text from a file, shortened; anything that is not text becomes None."""
    if not isinstance(value, str):
        return None
    return value if len(value) <= MAX_TEXT_CHARS else value[:MAX_TEXT_CHARS] + "…"


def _typed(
    raw: dict[str, Any], key: str, kind: type, label: str, path: Path, problems: list[Problem]
) -> Any:
    """``raw[key]`` when it has the expected type; None (plus a problem) for another type."""
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, kind) and not (kind is int and isinstance(value, bool)):
        if not isinstance(value, int) or 0 <= value <= MAX_COUNT:
            return value
        problems.append(
            Problem(path=str(path), what=f"field {label}.{key}", reason="number out of range")
        )
        return None
    problems.append(
        Problem(
            path=str(path),
            what=f"field {label}.{key}",
            reason=f"expected {kind.__name__}, found {type(value).__name__}",
        )
    )
    return None


def read_events(
    run_dir: Path, problems: list[Problem]
) -> tuple[int | None, LastEvent | None, list[Source]]:
    """``event_count`` (valid lines), ``last_event`` (greatest timestamp) and their source.

    No events file: 0 events and no last event. An unreadable file: the count is unknown
    (None). Corrupt lines are skipped, reading continues, and the file gets ONE problem entry.
    """
    path = run_dir / EVENTS_NAME
    content, reason, missing = _read_bytes(path, MAX_EVENTS_BYTES)
    if missing:
        return 0, None, []
    if content is None:
        problems.append(Problem(path=str(path), what="run events", reason=reason or ""))
        return None, None, []
    count = 0
    untimed = 0
    corrupt: list[int] = []
    latest: dict[str, Any] | None = None
    latest_time: datetime | None = None
    for number, raw in enumerate(content.split(b"\n"), start=1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            corrupt.append(number)
            continue
        if not isinstance(event, dict):
            corrupt.append(number)
            continue
        count += 1
        moment = parse_time(event.get("timestamp"))
        # Later lines win ties, and an event without a usable time only counts as "last"
        # while no timed event has been seen.
        if moment is not None:
            if latest_time is None or moment >= latest_time:
                latest, latest_time = event, moment
        else:
            untimed += 1
            if latest_time is None:
                latest = event
    if corrupt:
        shown = ", ".join(str(n) for n in corrupt[:5]) + (", …" if len(corrupt) > 5 else "")
        problems.append(
            Problem(
                path=str(path),
                what="run events",
                reason=f"{len(corrupt)} line(s) are not valid JSON objects (line {shown}); "
                "they are not counted",
            )
        )
    if untimed:
        problems.append(
            Problem(
                path=str(path),
                what="run events",
                reason=f"{untimed} event(s) have no usable timestamp; they are counted but "
                "cannot be ordered",
            )
        )
    sources = [
        Source(kind="file", path=str(path), field="event_count"),
        Source(kind="file", path=str(path), field="last_event"),
    ]
    if latest is None:
        return count, None, sources[:1]
    last = LastEvent(
        timestamp=_optional_text(latest.get("timestamp")),
        stage=_optional_text(latest.get("stage")),
        status=_optional_text(latest.get("status")),
        message=_optional_text(latest.get("message")),
    )
    return count, last, sources


def read_quality(
    state: dict[str, Any], state_path: Path, problems: list[Problem]
) -> QualitySummary | None:
    """The quality block of ``state.json``; absent or null means the run has not reached it."""
    raw = state.get("quality")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append(
            Problem(
                path=str(state_path),
                what="field quality",
                reason=f"expected an object, found {type(raw).__name__}",
            )
        )
        return None

    override = raw.get("independent_review_override")
    return QualitySummary(
        phase=_optional_text(_typed(raw, "phase", str, "quality", state_path, problems)),
        iteration=_typed(raw, "iteration", int, "quality", state_path, problems),
        max_iterations=_typed(raw, "max_iterations", int, "quality", state_path, problems),
        passed=_typed(raw, "passed", bool, "quality", state_path, problems),
        override_present=isinstance(override, dict) and bool(override),
    )


def _is_plain_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def read_pending_gates(
    run_dir: Path, now: datetime, problems: list[Problem]
) -> tuple[list[PendingGate], list[Source]]:
    """Gates still waiting for a human: a ``.pending`` marker and no consumed decision.

    Decision files are never opened (signed tokens are not needed) and nothing is written.
    """
    gates_dir = run_dir / GATES_NAME
    if os.path.islink(gates_dir):
        problems.append(
            Problem(path=str(gates_dir), what="run gates", reason="symbolic link; not followed")
        )
        return [], []
    if not os.path.isdir(gates_dir):
        if os.path.lexists(gates_dir):
            problems.append(
                Problem(path=str(gates_dir), what="run gates", reason="not a directory")
            )
        return [], []
    try:
        names = sorted(os.listdir(gates_dir))
    except OSError as exc:
        problems.append(
            Problem(path=str(gates_dir), what="run gates", reason=f"cannot be listed: {exc}")
        )
        return [], []
    present = set(names)
    gates: list[PendingGate] = []
    sources: list[Source] = []
    for name in names:
        if not name.endswith(".pending"):
            continue
        gate_id = name.removesuffix(".pending")
        marker = gates_dir / name
        # ``x.decision.pending`` would make "<id>.json" the signed decision file, which is
        # never opened; such a marker (or one that is not a plain file) is not a gate.
        if not gate_id or gate_id.endswith((".decision", ".challenge")):
            problems.append(
                Problem(path=str(marker), what="gate", reason="not a valid pending marker name")
            )
            continue
        if not _is_plain_file(marker):
            problems.append(
                Problem(path=str(marker), what="gate", reason="pending marker is not a plain file")
            )
            continue
        if f"{gate_id}.decision.consumed" in present:
            continue
        sources.append(Source(kind="file", path=str(gates_dir / name), field="pending_gates"))
        kind: str | None = None
        action: str | None = None
        gate_path = gates_dir / f"{gate_id}.json"
        gate, reason, missing = _read_object(gate_path)
        if gate is not None:
            kind, action = _optional_text(gate.get("kind")), _optional_text(gate.get("action"))
            sources.append(Source(kind="file", path=str(gate_path), field="pending_gates"))
        else:
            problems.append(
                Problem(
                    path=str(gate_path),
                    what="gate",
                    reason="pending marker without a gate file" if missing else reason or "",
                )
            )
        expires_at: str | None = None
        expired: bool | None = None
        challenge_path = gates_dir / f"{gate_id}.challenge.json"
        challenge, reason, missing = _read_object(challenge_path)
        if challenge is not None:
            expiry = parse_time(challenge.get("expires_at"))
            if expiry is None:
                problems.append(
                    Problem(
                        path=str(challenge_path),
                        what="gate challenge",
                        reason="expires_at is missing or not a date",
                    )
                )
            else:
                expires_at = _optional_text(challenge.get("expires_at"))
                expired = expiry < now
                sources.append(
                    Source(kind="file", path=str(challenge_path), field="pending_gates")
                )
        elif not missing:
            problems.append(
                Problem(path=str(challenge_path), what="gate challenge", reason=reason or "")
            )
        gates.append(
            PendingGate(
                gate_id=_optional_text(gate_id) or gate_id,
                kind=kind,
                action=action,
                challenge_expires_at=expires_at,
                expired=expired,
            )
        )
    return gates, sources


def read_cost(run_dir: Path, problems: list[Problem]) -> tuple[Cost, list[Source]]:
    """``recorded`` with the token totals of ``usage.json``; otherwise ``unknown``, never 0."""
    path = run_dir / USAGE_NAME
    usage, reason, missing = _read_object(path)
    if usage is None:
        if not missing:
            problems.append(Problem(path=str(path), what="run usage", reason=reason or ""))
        return Cost(), []
    totals = usage.get("totals")
    valid = isinstance(totals, dict) and bool(totals) and all(
        isinstance(key, str)
        and len(key) <= MAX_TEXT_CHARS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_TOKENS
        for key, value in totals.items()
    )
    if not valid:
        problems.append(
            Problem(
                path=str(path),
                what="run usage",
                reason="totals is missing or is not an object of non-negative whole numbers",
            )
        )
        return Cost(), []
    assert isinstance(totals, dict)
    return (
        Cost(state="recorded", tokens=dict(totals)),
        [Source(kind="file", path=str(path), field="cost")],
    )
