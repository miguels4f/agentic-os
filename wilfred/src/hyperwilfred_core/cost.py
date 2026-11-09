"""Per-run cost accounting — wall-clock from the run ledger, tokens from the runtime transcript.

Before this module Wilfred recorded no tokens and no time per run (``RunEvent.cost_usd`` /
``duration_ms`` existed but were always 0), so a harness comparison could not answer "which one
is cheaper". The engine still never calls an LLM, so it cannot *meter* tokens itself; what it can
do deterministically is:

* **Wall-clock** — derived from the timestamps the engine already writes to ``events.jsonl``
  (first → last event, plus a span per stage). No new instrumentation, nothing to forget.
* **Tokens** — read from the runtime's own transcript after the fact. For Claude Code that is the
  session JSONL under ``~/.claude/projects/<slug>/<session_id>.jsonl`` PLUS every sub-agent
  transcript under ``<session_id>/subagents/*.jsonl``. Sub-agents matter: in the pilot runs they
  were 42-56% of all tokens, and a tracker that reads only the main transcript (ECC's
  ``cost-tracker`` hook does) silently under-counts by that much.

Counting rules (each one a correctness bug seen in the wild if skipped):

* one API response spans several JSONL lines that repeat the same ``usage`` → count each
  ``message.id`` ONCE (line-summing inflates totals ~2.5-3x);
* only entries whose timestamp falls inside the run's window (``state.created_at`` → record
  time) are attributed to the run, so chat before ``hw plan`` in the same session is excluded;
* USD is deliberately NOT computed: prices change and a stale rate table silently lies. Tokens
  are recorded per model and per token class, which is what a price table needs later.

Honest limits: two runs driven concurrently from ONE session cannot be separated (their windows
overlap); a Codex runtime writes a different transcript format and is not parsed yet.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

USAGE_FILE = "usage.json"
TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


class CostError(RuntimeError):
    """A cost-accounting precondition failed (surfaces as CLI exit 2)."""


# --------------------------------------------------------------------------- transcripts


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def find_session_transcript(session_id: str, projects_dir: Path | None = None) -> Path:
    """Locate ``<projects_dir>/*/<session_id>.jsonl`` for a Claude Code session id."""
    if not _SESSION_ID.match(session_id):
        raise CostError(f"not a valid session id: {session_id!r}")
    base = projects_dir or default_projects_dir()
    hits = sorted(base.glob(f"*/{session_id}.jsonl"))
    if not hits:
        raise CostError(f"no transcript for session {session_id!r} under {base}")
    if len(hits) > 1:
        raise CostError(f"session {session_id!r} is ambiguous: {[str(h) for h in hits]}")
    return hits[0]


def transcript_files(transcript: Path) -> list[Path]:
    """The main transcript plus its sub-agent transcripts (``<stem>/subagents/*.jsonl``)."""
    if not transcript.is_file():
        raise CostError(f"transcript not found: {transcript}")
    subagents = transcript.parent / transcript.stem / "subagents"
    return [transcript, *sorted(subagents.glob("*.jsonl"))]


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _empty() -> dict[str, int]:
    return {f: 0 for f in TOKEN_FIELDS} | {"messages": 0}


def _add(bucket: dict[str, int], usage: dict) -> None:
    for f in TOKEN_FIELDS:
        value = usage.get(f)
        bucket[f] += value if isinstance(value, int) and value > 0 else 0
    bucket["messages"] += 1


def summarize(
    files: Iterable[Path], *, since: datetime | None = None, until: datetime | None = None
) -> dict:
    """Token totals for assistant messages in *files* inside ``[since, until]``.

    Deduplicated by ``message.id`` (last line wins — the final usage of a streamed response).
    Split by model and by role (``main`` vs ``subagent``, from the entry's ``isSidechain`` flag).
    Lines that are not JSON, not assistant messages, or carry no usage are skipped.
    """
    by_id: dict[str, tuple[str, str, dict]] = {}
    synthetic = 0
    for path in files:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                    continue
                usage = message["usage"]
                ts = _parse_ts(entry.get("timestamp"))
                if since is not None and (ts is None or ts < since):
                    continue
                if until is not None and (ts is None or ts > until):
                    continue
                msg_id = message.get("id")
                if not isinstance(msg_id, str) or not msg_id:
                    synthetic += 1
                    msg_id = f"__no_id_{synthetic}"
                role = "subagent" if entry.get("isSidechain") else "main"
                model = str(message.get("model") or "unknown")
                by_id[msg_id] = (role, model, usage)

    totals = _empty()
    by_model: dict[str, dict[str, int]] = {}
    by_role: dict[str, dict[str, int]] = {"main": _empty(), "subagent": _empty()}
    for role, model, usage in by_id.values():
        _add(totals, usage)
        _add(by_model.setdefault(model, _empty()), usage)
        _add(by_role[role], usage)
    return {"totals": totals, "by_model": by_model, "by_role": by_role}


def _fingerprint(path: Path) -> dict:
    data = path.read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def record_usage(
    *, run_dir: Path, run_created_at: datetime, transcripts: list[Path], now: datetime | None = None
) -> dict:
    """Summarize *transcripts* (+ their sub-agents) over the run window → the ``usage.json`` body.

    Re-running it recomputes from the transcripts (idempotent snapshot, not an accumulator), so a
    later call after more work simply supersedes the earlier one.
    """
    if not transcripts:
        raise CostError("give at least one --transcript or --session-id")
    until = now or datetime.now(UTC)
    files: list[Path] = []
    for t in transcripts:
        for f in transcript_files(t):
            if f not in files:
                files.append(f)
    summary = summarize(files, since=run_created_at, until=until)
    return {
        "run_id": run_dir.name,
        "runtime": "claude-code",
        "window": {"since": run_created_at.isoformat(), "until": until.isoformat()},
        "sources": [_fingerprint(f) for f in files],
        **summary,
    }


# --------------------------------------------------------------------------- wall-clock


def wall_clock(events: list[dict]) -> dict:
    """Run and per-stage wall-clock spans (seconds) from ``events.jsonl`` rows.

    ``total_s`` (first → last event) includes time the run sat waiting for a human. On the real
    pilot runs that was most of it (one run: 16 h total, 11 h of it an open ship gate), so the
    number to compare between harnesses is ``active_s`` = total minus ``human_wait_s`` (every
    interval from an ``awaiting_human`` event to the next event). ``max_gap_s`` exposes the
    remaining long silences (rate-limit pauses, an idle session) that no ledger can classify.
    A stage span is first → last event carrying that stage.
    """
    stamped: list[tuple[str, datetime, str]] = []
    for e in events:
        ts = _parse_ts(e.get("timestamp"))
        if ts is not None:
            stamped.append((str(e.get("stage", "")), ts, str(e.get("status", ""))))
    if not stamped:
        return {"first_event": None, "last_event": None, "total_s": None, "active_s": None,
                "human_wait_s": None, "max_gap_s": None, "stages": {}}
    stamped.sort(key=lambda row: row[1])
    first, last = stamped[0][1], stamped[-1][1]
    human_wait = 0.0
    max_gap = 0.0
    for (_, prev_ts, prev_status), (_, ts, _) in zip(stamped, stamped[1:], strict=False):
        gap = (ts - prev_ts).total_seconds()
        max_gap = max(max_gap, gap)
        if prev_status == "awaiting_human":
            human_wait += gap
    total = (last - first).total_seconds()
    stages: dict[str, tuple[datetime, datetime]] = {}
    for stage, ts, _ in stamped:
        start, end = stages.get(stage, (ts, ts))
        stages[stage] = (min(start, ts), max(end, ts))
    return {
        "first_event": first.isoformat(),
        "last_event": last.isoformat(),
        "total_s": round(total, 3),
        "active_s": round(total - human_wait, 3),
        "human_wait_s": round(human_wait, 3),
        "max_gap_s": round(max_gap, 3),
        "stages": {
            s: round((end - start).total_seconds(), 3) for s, (start, end) in stages.items()
        },
    }


def read_events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def cost_report(run_dir: Path) -> dict:
    """Wall-clock (always) + tokens (when ``hw record-usage`` has been run) for one run."""
    usage_path = run_dir / USAGE_FILE
    usage = json.loads(usage_path.read_text(encoding="utf-8")) if usage_path.exists() else None
    return {
        "run_id": run_dir.name,
        "wall_clock": wall_clock(read_events(run_dir)),
        "tokens": None if usage is None else {
            "totals": usage["totals"],
            "by_role": usage["by_role"],
            "by_model": usage["by_model"],
            "window": usage["window"],
            "sources": len(usage["sources"]),
        },
        "note": None if usage is not None else (
            "tokens not recorded — run `hw record-usage --run-id <RUN_ID> --session-id "
            "<CLAUDE_CODE_SESSION_ID>` from the session that drove the run"
        ),
    }


# --------------------------------------------------------------------------- hw commands


def hw_record_usage(
    *,
    repo_root: Path,
    run_id: str,
    transcripts: list[Path],
    session_ids: list[str],
    projects_dir: Path | None = None,
) -> dict:
    """``hw record-usage`` — write ``.hw/<run_id>/usage.json`` + one ledger event."""
    from hyperwilfred_core.engine import RunPaths, load_state
    from hyperwilfred_core.run_store import RunEventStatus, RunStore, atomic_write_text

    paths = RunPaths(repo_root, run_id)
    state = load_state(paths)
    resolved = [*transcripts, *(find_session_transcript(s, projects_dir) for s in session_ids)]
    usage = record_usage(
        run_dir=paths.run_dir, run_created_at=state.created_at, transcripts=resolved
    )
    atomic_write_text(paths.artifact(USAGE_FILE), json.dumps(usage, indent=2) + "\n")
    t = usage["totals"]
    RunStore(paths.run_dir).append_event(
        run_id=run_id,
        stage="cost",
        status=RunEventStatus.running,
        message=f"Usage recorded: {t['messages']} message(s), in={t['input_tokens']} "
        f"out={t['output_tokens']} cache_write={t['cache_creation_input_tokens']} "
        f"cache_read={t['cache_read_input_tokens']} "
        f"(subagent share of messages: {usage['by_role']['subagent']['messages']})",
        artifacts=[USAGE_FILE],
    )
    return {"run_id": run_id, "usage": usage}


def hw_cost_report(*, repo_root: Path, run_id: str) -> dict:
    """``hw cost-report`` — wall-clock + recorded tokens for one run (read-only)."""
    from hyperwilfred_core.engine import RunPaths, load_state

    paths = RunPaths(repo_root, run_id)
    load_state(paths)  # the run must exist
    return cost_report(paths.run_dir)


__all__ = [
    "USAGE_FILE",
    "CostError",
    "cost_report",
    "find_session_transcript",
    "hw_cost_report",
    "hw_record_usage",
    "record_usage",
    "summarize",
    "transcript_files",
    "wall_clock",
]
