"""Derive the attention inbox ("Necesita tu atención") from a snapshot.

Pure: this module reads no file, runs no command and never asks the clock. The caller passes
the current time, so the same snapshot and the same ``now`` always give the same list.

The rules are the table in command-center-spec.md, section 7. Each item describes; nothing here
acts. Every visible word comes from ``text_es``.
Ops items (proposals, hand edits waiting for approval, overdue and blocked tasks) follow the ops
design, Ideas/ops/specs/2026-10-14-notion-to-ops-design.md, section 14.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo
from urllib.parse import quote

from hyperwilfred_core.command_center import text_es as es
from hyperwilfred_core.command_center.collect_closed import closed_run_ids
from hyperwilfred_core.command_center.collect_runs import distinct_runs
from hyperwilfred_core.command_center.model import (
    OPS_BLOCKED_STATUS,
    OPS_CLOSED_STATUSES,
    AttentionItem,
    AttentionSubject,
    Ops,
    PendingGate,
    Project,
    Run,
    Snapshot,
    Source,
)
from hyperwilfred_core.command_center.paths import is_inside_run_store
from hyperwilfred_core.command_center.run_detail import parse_time

# command-center-spec.md, section 7: a failed gate older than this leaves the inbox, and an
# "in progress" run with no event for longer than this is flagged as stale.
RECENT_DAYS = 14
# command-center-spec.md, section 7: uncommitted changes count as unpushed work only when the
# repository's last commit is older than this (younger ones are probably work in progress).
UNCOMMITTED_DAYS = 7

_RISK_ORDER = {"alto": 0, "medio": 1, "bajo": 2}
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def _local(moment: datetime, tz: tzinfo | None) -> str:
    """A moment as ``YYYY-MM-DD HH:MM`` in ``tz`` (the Mac's own zone when ``tz`` is None)."""
    try:
        return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, ValueError, OSError):
        return moment.strftime("%Y-%m-%d %H:%M UTC")


def _days(moment: datetime, now: datetime) -> int:
    try:
        return max(0, (now - moment).days)
    except OverflowError:
        return 0


def _run_dir(run: Run) -> str:
    return run.store_path.rstrip("/") + "/" + run.run_id


def _run_sources(run: Run, *fields: str) -> list[Source]:
    picked = [s for s in run.sources if s.field in fields]
    return picked or [Source(kind="file", path=_run_dir(run))]


def _project_name(run: Run, projects: dict[str, Project]) -> str | None:
    project = projects.get(run.project_id or "")
    return project.name if project else run.project_id


def _last_activity(run: Run) -> datetime | None:
    """The newest of the last event and the state's ``updated_at``; None when neither is a date."""
    moments = [
        moment
        for moment in (
            parse_time(run.last_event.timestamp if run.last_event else None),
            parse_time(run.updated_at),
        )
        if moment is not None
    ]
    return max(moments) if moments else None


class _Draft:
    """An item plus the moment its situation started, used only for ordering (oldest first)."""

    def __init__(self, item: AttentionItem, since: datetime | None) -> None:
        self.item = item
        self.since = since

    def key(self) -> tuple[int, datetime, datetime, str]:
        deadline = parse_time(self.item.deadline) or _FAR_FUTURE
        since = self.since or _FAR_FUTURE
        return (_RISK_ORDER[self.item.risk], deadline, since, self.item.id)


def _gate_item(
    run: Run, gates: list[PendingGate], project: str | None, now: datetime, tz: tzinfo | None
) -> _Draft:
    windows = [(g, parse_time(g.challenge_expires_at)) for g in gates]
    live = [g for g, expiry in windows if expiry is not None and expiry >= now]
    unknown = [g for g, expiry in windows if expiry is None and g.expired is not True]
    actions = [gate.action or gate.gate_id for gate in gates]
    ids = [gate.gate_id for gate in gates]
    since = _last_activity(run)
    subject = AttentionSubject(type="run", id=run.run_id)
    sources = _run_sources(run, "pending_gates")
    if live:
        # Only a known, still-open approval window makes an item "alto" (section 7).
        deadline = min(e for g, e in windows if g in live and e is not None)
        local = _local(deadline, tz)
        item = AttentionItem(
            id=f"gate:{run.run_id}",
            kind="gate",
            risk="alto",
            deadline=deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
            title_es=es.att_gate_title(project, run.run_id, actions),
            why_es=es.att_gate_why(actions, ids),
            default_es=es.att_gate_default(local),
            if_ignored_es=es.att_gate_ignored(local),
            subject=subject,
            sources=sources,
        )
        return _Draft(item, since)
    if unknown:
        # No readable approval window: nothing proves a deadline is running, so not "alto".
        item = AttentionItem(
            id=f"gate:{run.run_id}",
            kind="gate",
            risk="medio",
            title_es=es.att_gate_title(project, run.run_id, actions),
            why_es=es.att_gate_why(actions, ids) + " " + es.ATT_GATE_NO_WINDOW,
            default_es=es.att_gate_default(None),
            if_ignored_es=es.att_gate_ignored(None),
            subject=subject,
            sources=sources,
        )
        return _Draft(item, since)
    expiries = [e for g in gates if (e := parse_time(g.challenge_expires_at)) is not None]
    expired_at = _local(max(expiries), tz) if expiries else None
    item = AttentionItem(
        id=f"gate:{run.run_id}",
        kind="gate",
        risk="medio",
        title_es=es.att_gate_expired_title(project, run.run_id, actions),
        why_es=es.att_gate_expired_why(expired_at),
        default_es=es.ATT_GATE_EXPIRED_DEFAULT,
        if_ignored_es=es.att_gate_expired_ignored(expired_at),
        subject=subject,
        sources=sources,
    )
    return _Draft(item, max(expiries) if expiries else since)


def _gates_of_all_copies(runs: list[Run]) -> dict[str, tuple[list[PendingGate], list[Source]]]:
    """Pending gates of every copy of each run id, readable or not, one per gate id.

    Section 7 depends only on the gate files, so a gate in an older copy or next to a corrupt
    ``state.json`` still counts.
    """
    found: dict[str, tuple[list[PendingGate], list[Source]]] = {}
    for run in runs:
        if not run.pending_gates:
            continue
        gates, sources = found.setdefault(run.run_id, ([], []))
        for gate in run.pending_gates:
            if all(g.gate_id != gate.gate_id for g in gates):
                gates.append(gate)
        sources.extend(s for s in _run_sources(run, "pending_gates") if s not in sources)
    return found


def _run_items(
    runs: list[Run], projects: dict[str, Project], now: datetime, tz: tzinfo | None
) -> list[_Draft]:
    drafts: list[_Draft] = []
    recent = timedelta(days=RECENT_DAYS)
    gated = _gates_of_all_copies(runs)
    closed = closed_run_ids(runs)
    for run in distinct_runs(runs):  # one item per run id, never one per copy
        project = _project_name(run, projects)
        if run.run_id in gated:  # a run with a pending gate gives exactly one item
            gates, sources = gated[run.run_id]
            draft = _gate_item(run, gates, project, now, tz)
            draft.item.sources = sources
            # A closed run is silent, except while an approval window is still open: a
            # running deadline is never hidden by the register.
            if run.run_id not in closed or draft.item.risk == "alto":
                drafts.append(draft)
            continue
        if run.run_id in closed:
            continue  # closed by the owner; shown on the page in the "closed" group only
        if not run.readable:
            continue  # its reading problem is already an ``unreadable`` item
        moved = parse_time(run.updated_at)
        if run.status == "awaiting_human":
            since = _last_activity(run)
            drafts.append(
                _Draft(
                    AttentionItem(
                        id=f"awaiting_human:{run.run_id}",
                        kind="awaiting_human",
                        risk="medio",
                        title_es=es.att_awaiting_title(project, run.run_id),
                        why_es=es.ATT_AWAITING_WHY,
                        default_es=es.ATT_AWAITING_DEFAULT,
                        if_ignored_es=es.att_awaiting_ignored(
                            _local(since, tz) if since else None,
                            _days(since, now) if since else None,
                        ),
                        subject=AttentionSubject(type="run", id=run.run_id),
                        sources=_run_sources(run, "status", "last_event"),
                    ),
                    since,
                )
            )
        elif run.status == "gate_failed":
            # Unknown date: shown, because "recent" cannot be ruled out.
            if moved is not None and now - moved > recent:
                continue
            leaves = _local(moved + recent, tz) if moved is not None else None
            step = es.STEP_LABELS.get(run.current_step or "", run.current_step)
            drafts.append(
                _Draft(
                    AttentionItem(
                        id=f"gate_failed:{run.run_id}",
                        kind="gate_failed",
                        risk="medio",
                        title_es=es.att_gate_failed_title(project, run.run_id),
                        why_es=es.att_gate_failed_why(step),
                        default_es=es.ATT_GATE_FAILED_DEFAULT,
                        if_ignored_es=es.att_gate_failed_ignored(leaves, RECENT_DAYS),
                        subject=AttentionSubject(type="run", id=run.run_id),
                        sources=_run_sources(run, "status", "quality", "last_event"),
                    ),
                    moved,
                )
            )
        elif run.status == "in_progress":
            since = _last_activity(run)
            if since is None or now - since <= recent:
                continue  # no date: "stale" cannot be shown to be true
            drafts.append(
                _Draft(
                    AttentionItem(
                        id=f"stale_run:{run.run_id}",
                        kind="stale_run",
                        risk="bajo",
                        title_es=es.att_stale_title(project, run.run_id, _days(since, now)),
                        why_es=es.att_stale_why(_local(since, tz)),
                        default_es=es.ATT_STALE_DEFAULT,
                        if_ignored_es=es.ATT_STALE_IGNORED,
                        subject=AttentionSubject(type="run", id=run.run_id),
                        sources=_run_sources(run, "last_event", "updated_at"),
                    ),
                    since,
                )
            )
    return drafts


def _project_items(projects: list[Project], now: datetime, tz: tzinfo | None) -> list[_Draft]:
    drafts: list[_Draft] = []
    old = timedelta(days=UNCOMMITTED_DAYS)
    for project in projects:
        # Section 7 names repositories; worktrees appear (collapsed) under them in "Proyectos".
        if project.kind != "repository" or is_inside_run_store(project.path):
            continue
        reasons: list[str] = []
        fields: list[str] = []
        if project.ahead:
            reasons.append(es.att_ahead(project.ahead))
            fields.append("ahead")
        if project.branches_without_upstream:
            reasons.append(es.att_branches(project.branches_without_upstream))
            fields.append("branches_without_upstream")
        last_commit = parse_time(project.last_commit_at)
        if project.uncommitted_paths and (last_commit is None or now - last_commit > old):
            reasons.append(
                es.att_uncommitted(
                    project.uncommitted_paths,
                    _local(last_commit, tz) if last_commit else None,
                    _days(last_commit, now) if last_commit else None,
                    has_commits=project.has_commits is not False,
                )
            )
            fields += ["uncommitted_paths", "last_commit_at"]
        if not reasons:
            continue
        sources = [s for s in project.sources if s.field in fields] or [
            Source(kind="file", path=project.path)
        ]
        drafts.append(
            _Draft(
                AttentionItem(
                    id=f"unpushed_work:{project.id}",
                    kind="unpushed_work",
                    risk="medio",
                    title_es=es.att_unpushed_title(project.name),
                    why_es=es.att_unpushed_why(reasons),
                    default_es=es.ATT_UNPUSHED_DEFAULT,
                    if_ignored_es=es.ATT_UNPUSHED_IGNORED,
                    subject=AttentionSubject(type="project", id=project.id),
                    sources=sources,
                ),
                last_commit,
            )
        )
    return drafts


def _problem_subject(path: str, snapshot: Snapshot) -> AttentionSubject:
    """What an unreadable file belongs to: a run store, the memory tree, or a project."""
    root = snapshot.memory.root.rstrip("/") + "/" if snapshot.memory is not None else None
    if is_inside_run_store(path):
        inside = path.split("/.hw/", 1)[1].split("/", 1)[0]
        if inside:
            return AttentionSubject(type="run", id=inside)
    if root is not None and (path + "/").startswith(root):
        return AttentionSubject(type="memory", id=path)
    return AttentionSubject(type="project", id=path)


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _stem(path: str) -> str:
    """``sales/acme.md`` -> ``acme``: what a person calls that file."""
    return _basename(path).rsplit(".", 1)[0]


def _ops_sources(ops: Ops, relative: str, *, web: bool = True) -> list[Source]:
    """The local file, then its GitHub copy when the export gave a valid address."""
    sources = [Source(kind="file", path=ops.root.rstrip("/") + "/" + relative)]
    if web and ops.github_blob:
        sources.append(Source(kind="url", path=ops.github_blob + quote(relative, safe="/")))
    return sources


def _due(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _unapproved_item(ops: Ops, path: str) -> AttentionItem:
    """A hand edit that waits for ``ops approve --file``: sales/schema data, or a task's
    hand-typed authorization. GitHub still holds the approved version: local file only."""
    if path.startswith("tasks/"):
        title = es.att_ops_authorization_title(path)
        why, ignored = es.ATT_OPS_AUTHORIZATION_WHY, es.ATT_OPS_AUTHORIZATION_IGNORED
        default = es.att_ops_authorization_default(path)
    else:
        title = es.att_ops_unapproved_title(_stem(path))
        why, ignored = es.ATT_OPS_UNAPPROVED_WHY, es.ATT_OPS_UNAPPROVED_IGNORED
        default = es.att_ops_unapproved_default(path)
    return AttentionItem(
        id=f"ops_pending_proposal:{path}",
        kind="ops_pending_proposal",
        risk="alto",
        title_es=title,
        why_es=why,
        default_es=default,
        if_ignored_es=ignored,
        subject=AttentionSubject(type="ops", id=path),
        sources=_ops_sources(ops, path, web=False),
    )


def _proposal_items(ops: Ops, tz: tzinfo | None) -> list[_Draft]:
    """Changes that only the owner can approve (ops spec section 14: alto)."""
    drafts: list[_Draft] = []
    for proposal in ops.proposals or []:
        created = parse_time(proposal.created_at)
        when = _local(created, tz) if created else None
        item = AttentionItem(
            id=f"ops_pending_proposal:{proposal.path}",
            kind="ops_pending_proposal",
            risk="alto",
            title_es=es.att_ops_proposal_title(
                proposal.id, proposal.kind, _stem(proposal.target), proposal.fields
            ),
            why_es=es.att_ops_proposal_why(proposal.author, when),
            default_es=es.att_ops_proposal_default(proposal.id),
            if_ignored_es=es.ATT_OPS_PROPOSAL_IGNORED,
            subject=AttentionSubject(type="ops", id=proposal.path),
            sources=_ops_sources(ops, proposal.path),
        )
        drafts.append(_Draft(item, created))
    for path in ops.unapproved_edits or []:
        drafts.append(_Draft(_unapproved_item(ops, path), None))
    return drafts


def _task_items(ops: Ops, now: datetime, tz: tzinfo | None) -> list[_Draft]:
    """One item per open task at most: overdue (medio) wins over blocked (bajo)."""
    drafts: list[_Draft] = []
    today = now.astimezone(tz).date()  # the calendar day on the owner's Mac
    for task in ops.open_tasks or []:
        if task.status in OPS_CLOSED_STATUSES:
            continue
        project = ", ".join(task.projects) or None
        blocked = task.status == OPS_BLOCKED_STATUS
        subject = AttentionSubject(type="ops", id=task.path)
        ends = _due(task.due_end) or _due(task.due)  # a range is late only after its end
        if ends is not None and ends < today:
            late = (today - ends).days
            status = es.OPS_TASK_STATUS.get(task.status, task.status)
            item = AttentionItem(
                id=f"ops_overdue_task:{task.path}",
                kind="ops_overdue_task",
                risk="medio",
                title_es=es.att_ops_overdue_title(project, task.id, task.title, late),
                why_es=es.att_ops_overdue_why(
                    ends.isoformat(), status, task.blocked_by if blocked else None
                ),
                default_es=es.ATT_OPS_OVERDUE_DEFAULT,
                if_ignored_es=es.ATT_OPS_OVERDUE_IGNORED,
                subject=subject,
                sources=_ops_sources(ops, task.path),
            )
            drafts.append(_Draft(item, datetime(ends.year, ends.month, ends.day, tzinfo=UTC)))
        elif blocked:
            item = AttentionItem(
                id=f"ops_blocked_task:{task.path}",
                kind="ops_blocked_task",
                risk="bajo",
                title_es=es.att_ops_blocked_title(project, task.id, task.title),
                why_es=es.att_ops_blocked_why(task.blocked_by),
                default_es=es.ATT_OPS_BLOCKED_DEFAULT,
                if_ignored_es=es.ATT_OPS_BLOCKED_IGNORED,
                subject=subject,
                sources=_ops_sources(ops, task.path),
            )
            drafts.append(_Draft(item, None))
    return drafts


def derive_attention(
    snapshot: Snapshot, now: datetime, *, tz: tzinfo | None = None
) -> list[AttentionItem]:
    """Every situation in ``snapshot`` that needs the owner, most urgent first.

    Order: risk (alto, medio, bajo), then deadline (earliest first, none last), then age (the
    situation that started earliest first). ``tz`` changes how times are written in the Spanish
    sentences and which calendar day counts as "today" for ops due dates; it defaults to the
    Mac's own zone.
    """
    # A naive time is read as UTC, the same way stored times are (``parse_time``).
    now = (now.replace(tzinfo=UTC) if now.tzinfo is None else now).astimezone(UTC)
    projects = {p.id: p for p in snapshot.projects}
    drafts = _run_items(snapshot.runs, projects, now, tz)
    drafts += _project_items(snapshot.projects, now, tz)
    if snapshot.ops is not None:
        drafts += _proposal_items(snapshot.ops, tz)
        drafts += _task_items(snapshot.ops, now, tz)
    for problem in snapshot.problems:
        drafts.append(
            _Draft(
                AttentionItem(
                    id=f"unreadable:{problem.path}:{problem.what}",
                    kind="unreadable",
                    risk="bajo",
                    title_es=es.att_unreadable_title(_basename(problem.path)),
                    why_es=es.att_unreadable_why(problem.what, problem.reason),
                    default_es=es.ATT_UNREADABLE_DEFAULT,
                    if_ignored_es=es.ATT_UNREADABLE_IGNORED,
                    subject=_problem_subject(problem.path, snapshot),
                    sources=[Source(kind="file", path=problem.path)],
                ),
                None,
            )
        )
    problem_paths = {problem.path for problem in snapshot.problems}
    if snapshot.memory is not None:
        for note in snapshot.memory.malformed:
            if note.problem == "unreadable" and note.path in problem_paths:
                continue  # already listed once, as an unreadable file
            drafts.append(
                _Draft(
                    AttentionItem(
                        id=f"memory_malformed:{note.path}",
                        kind="memory_malformed",
                        risk="bajo",
                        title_es=es.att_memory_title(_basename(note.path)),
                        why_es=es.att_memory_why(note.problem),
                        default_es=es.ATT_MEMORY_DEFAULT,
                        if_ignored_es=es.ATT_MEMORY_IGNORED,
                        subject=AttentionSubject(type="memory", id=note.path),
                        sources=[Source(kind="file", path=note.path)],
                    ),
                    None,
                )
            )
    return [draft.item for draft in sorted(drafts, key=_Draft.key)]
