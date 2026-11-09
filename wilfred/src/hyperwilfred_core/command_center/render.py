"""Turn a snapshot into one self-contained ``index.html`` (pure: returns text, writes nothing).

Hard rules this module enforces (command-center-spec.md, sections 1 and 3):

* Every string from the snapshot is untrusted and goes through ``html.escape``.
* Every displayed fact is followed by a link to its source: a ``file://`` link to the file, for
  git facts a link whose text is the git command that produced the value, and for ops items
  also an ``https://github.com/`` link to the file's copy on GitHub (a plain link: nothing is
  loaded).
* No external resource of any kind: inline CSS, a few lines of inline script (text filter only)
  pinned by a Content-Security-Policy hash. The page is complete with scripts disabled.
* No Spanish text here: every visible word comes from ``text_es``.
"""

from __future__ import annotations

import base64
import hashlib
import html
import shlex
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import quote

from hyperwilfred_core.command_center import text_es as es
from hyperwilfred_core.command_center.collect_closed import closed_run_ids
from hyperwilfred_core.command_center.collect_runs import distinct_runs
from hyperwilfred_core.command_center.model import (
    AttentionItem,
    Problem,
    Project,
    Run,
    Snapshot,
    Source,
    Worktree,
)
from hyperwilfred_core.command_center.paths import is_inside_run_store
from hyperwilfred_core.command_center.run_detail import parse_time

PAGE_NAME = "index.html"
STALE_AFTER_HOURS = 24
# Stored run status -> page group, in page order (prompt S2.3, section "Trabajos").
GROUP_ORDER = (
    "awaiting_human",
    "gate_failed",
    "in_progress",
    "completed",
    "failed",
    "created",
    "other",
    "unreadable",
    "closed",  # listed in the closed-runs register; shown collapsed, last
)
SHORT_HASH = 12
# A run "in progress" with no activity for this long is flagged on its card (same threshold
# as the stale_run rule of the specification, section 7).
INACTIVE_DAYS = 14
# Prompt S2.4: the inbox shows this many items; the rest sit behind a "y N mas" line.
ATTENTION_SHOWN = 12

_CSS = """
:root{color-scheme:light dark;--fg:#1d2433;--muted:#5b6475;--bg:#fbfbfd;--card:#fff;
--line:#d9dde5;--warn:#8a4b00;--warnbg:#fff4e0;--todo:#9a2d1f;--ok:#22663a}
@media (prefers-color-scheme:dark){:root{--fg:#e6e9ef;--muted:#a3abba;--bg:#161a22;
--card:#1e232d;--line:#363d4b;--warn:#ffcf8a;--warnbg:#3a2c12;--todo:#ff9b8a;--ok:#8fd6a5}}
*{box-sizing:border-box}
body{margin:0 auto;max-width:72rem;padding:1rem;font:15px/1.5 -apple-system,system-ui,
sans-serif;color:var(--fg);background:var(--bg);overflow-wrap:anywhere}
h1{font-size:1.6rem;margin:.2rem 0}h2{margin-top:2rem;border-bottom:1px solid var(--line)}
h3{margin:1.2rem 0 .3rem}
.muted,small,.src{color:var(--muted)}small,.src{font-size:.8em}
.src code{font-size:.95em}a{color:inherit}
.warn{background:var(--warnbg);color:var(--warn);padding:.6rem .8rem;border-radius:6px}
.todo{color:var(--todo);font-weight:600}.ok{color:var(--ok)}
article{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:.6rem .9rem;margin:.6rem 0}
article h4{margin:0 0 .3rem;font-size:1rem}
ul.facts{list-style:none;padding:0;margin:0}ul.facts li{margin:.15rem 0}
.att h4{font-size:1.05rem}.risk-alto{color:var(--todo)}
.k{font-weight:600}.k::after{content:": "}
details>summary{cursor:pointer}
code{font-family:ui-monospace,Menlo,monospace}
#filterbox input{width:100%;max-width:30rem;padding:.4rem;font-size:1rem}
"""

_SCRIPT = (
    "(function(){var s=document.getElementById('stale-view');"
    "var g=Number(document.body.getAttribute('data-generated'));"
    "if(s&&g>0&&Date.now()-g>86400000){s.hidden=false;}"
    "var b=document.getElementById('filterbox');var i=b.querySelector('input');"
    "b.hidden=false;i.addEventListener('input',function(){var q=i.value.toLowerCase();"
    "document.querySelectorAll('[data-filter]').forEach(function(e){"
    "e.hidden=q!==''&&e.textContent.toLowerCase().indexOf(q)<0;});"
    "if(q!==''){document.querySelectorAll('details').forEach(function(d){d.open=true;});}"
    "});})();"
)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def file_href(path: str) -> str:
    """A ``file://`` URL for an absolute path; spaces, accents and quotes are percent-encoded."""
    return "file://" + quote(path if path.startswith("/") else "/" + path, safe="/")


def _short_command(source: Source) -> str:
    """The git command without the fixed safety options and the ``-C <path>`` prefix."""
    command = source.command or ""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    for index in range(len(tokens) - 1):
        if tokens[index] == "-C" and tokens[index + 1] == source.path:
            return shlex.join(["git", *tokens[index + 2:]])
    return command


def source_link(source: Source | None) -> str:
    if source is None:
        return ""
    if source.kind == "url":
        # The model admits only https addresses; GitHub only is allowed on the page, checked
        # again here because this is the page.
        if not source.path.startswith("https://github.com/"):
            return ""
        return (
            f' <a class="src" href="{_e(source.path)}" rel="noreferrer"'
            f' title="{_e(es.SOURCE_WEB_HELP)}: {_e(source.path)}">{_e(es.SOURCE_WEB_LINK)}</a>'
        )
    href = _e(file_href(source.path))
    if source.kind == "git" and source.command:
        return (
            f' <a class="src" href="{href}" title="{_e(source.command)}">'
            f"<code>{_e(_short_command(source))}</code></a>"
        )
    return (
        f' <a class="src" href="{href}" title="{_e(es.SOURCE_HELP)}: {_e(source.path)}">'
        f"{_e(es.SOURCE_LINK)}</a>"
    )


def _pick(sources: Iterable[Source], field: str, contains: str | None = None) -> Source | None:
    for source in sources:
        if source.field == field and (contains is None or contains in source.path):
            return source
    return None


def _fact(label: str, value_html: str, source: Source | None) -> str:
    return f'<li><span class="k">{_e(label)}</span>{value_html}{source_link(source)}</li>'


def _moment(value: str | None) -> datetime | None:
    """A stored time, or None when it is missing, malformed or cannot be shown locally."""
    try:
        moment = parse_time(value)
        if moment is not None:
            moment.astimezone()  # boundary dates overflow when shifted to local time
        return moment
    except (OverflowError, ValueError, OSError):
        return None


def _seconds_since(moment: datetime, now: datetime) -> int | None:
    try:
        return int((now - moment).total_seconds())
    except OverflowError:
        return None


def _local(value: str | None) -> str:
    moment = _moment(value)
    if moment is None:
        return es.UNKNOWN if not value else value
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _when(value: str | None, now: datetime) -> str:
    """Local time plus a relative age, as escaped HTML."""
    moment = _moment(value)
    elapsed = _seconds_since(moment, now) if moment is not None else None
    if moment is None or elapsed is None:
        return _e(es.UNKNOWN if not value else value)
    seconds = max(0, elapsed)
    relative = es.age(seconds // 86400, seconds // 3600)
    return f"{_e(_local(value))} <small>({_e(relative)})</small>"


def _deadline(value: str | None, now: datetime) -> str:
    """Local time plus how long is left (or that it already passed), as escaped HTML."""
    moment = _moment(value)
    left = _seconds_since(now, moment) if moment is not None else None
    if moment is None or left is None:
        return _e(es.UNKNOWN if not value else value)
    relative = es.until(left // 86400, left // 3600) if left > 0 else es.DEADLINE_PASSED
    return f"{_e(_local(value))} <small>({_e(relative)})</small>"


def _stored(label: str, stored: str) -> str:
    """A Spanish label with the stored value in small print."""
    return f"{_e(label)} <small>({_e(stored)})</small>"


def _group_of(run: Run, closed: set[str]) -> str:
    if run.run_id in closed:
        return "closed"
    if not run.readable:
        return "unreadable"
    if run.status in GROUP_ORDER and run.status not in ("other", "unreadable", "closed"):
        return run.status
    return "other"


# ---------------------------------------------------------------- page sections


def _header(snapshot: Snapshot, now: datetime) -> str:
    parts = [f"<h1>{_e(es.PAGE_TITLE)}</h1>", f'<p class="muted">{_e(es.PAGE_INTRO)}</p>']
    generated = _moment(snapshot.generated_at)
    elapsed = _seconds_since(generated, now) if generated is not None else None
    parts.append(f"<p>{_e(es.generated_line(_local(snapshot.generated_at)))}</p>")
    if generated is None or elapsed is None:
        parts.append(f'<p class="warn" role="alert">{_e(es.STALE_UNKNOWN)}</p>')
    elif elapsed // 3600 >= STALE_AFTER_HOURS:
        parts.append(
            f'<p class="warn" role="alert">{_e(es.stale_warning(elapsed // 3600))}</p>'
        )
    else:
        # Rendered fresh, but the file may be opened days later: the script reveals this.
        parts.append(f'<p class="warn" role="alert" id="stale-view" hidden>'
                     f"{_e(es.STALE_VIEWED)}</p>")
    parts.append(
        f'<p id="filterbox" hidden><label>{_e(es.FILTER_LABEL)}<br>'
        f'<input type="search" placeholder="{_e(es.FILTER_PLACEHOLDER)}"></label></p>'
    )
    return "\n".join(parts)


def _local_branches(project: Project) -> list[str]:
    """Branches without upstream are a fact of the whole repository, shown on it only."""
    if project.kind != "repository":
        return []
    return project.branches_without_upstream or []


def _has_unfinished(project: Project) -> bool:
    return bool(project.uncommitted_paths or project.ahead or _local_branches(project))


def _git_unknown(project: Project) -> bool:
    """Unknown is not clean: git could not answer one of the facts the clean claim needs."""
    if project.kind == "folder":
        return False
    return (
        project.has_commits is None
        or project.uncommitted_paths is None
        or (project.kind == "repository" and project.branches_without_upstream is None)
    )


def _summary(
    snapshot: Snapshot, runs: list[Run], origin: Source | None, closed: set[str]
) -> str:
    projects = snapshot.projects
    lines = [
        es.summary_repositories(
            sum(p.kind == "repository" for p in projects),
            sum(p.kind == "worktree" for p in projects),
            sum(p.kind == "folder" for p in projects),
        ),
        es.summary_unfinished(
            sum(_has_unfinished(p) for p in projects),
            [p.name for p in _ordered(projects) if _has_unfinished(p)],
        ),
        es.summary_runs(len(runs), len(snapshot.runs)),
    ]
    unknown = sum(_git_unknown(p) for p in projects)
    if unknown:
        lines.insert(2, es.summary_git_unknown(unknown))
    groups: dict[str, int] = defaultdict(int)
    for run in runs:
        groups[_group_of(run, closed)] += 1
    link = source_link(origin)
    items = [f"<li>{_e(line)}{link}</li>" for line in lines]
    for key in GROUP_ORDER:
        if groups.get(key):
            label = es.GROUP_LABELS[key]
            items.append(
                f"<li>{_stored(label, key)}: {groups[key]}{link}</li>"
            )
    open_gates = [run for run in runs if run.pending_gates]
    items.append(
        "<li>"
        + _e(es.summary_gates(len(open_gates), sum(len(r.pending_gates) for r in runs),
                              [r.run_id for r in open_gates]))
        + link
        + "</li>"
    )
    recorded = sum(run.cost.state == "recorded" for run in runs)
    items.append(f"<li>{_e(es.summary_cost(recorded, len(runs)))}{link}</li>")
    items.append(
        f"<li>{_e(es.summary_memory(snapshot.memory.notes if snapshot.memory else None))}"
        f"{link}</li>"
    )
    items.append(f"<li>{_e(es.summary_problems(len(snapshot.problems)))}{link}</li>")
    return _section("resumen", es.SECTION_SUMMARY, "<ul>" + "".join(items) + "</ul>")


def _anchor(item_id: str) -> str:
    """A valid, stable HTML id for an item whose id may hold paths and spaces."""
    return hashlib.sha256(item_id.encode("utf-8", "replace")).hexdigest()[:12]


def _attention_item(item: AttentionItem, now: datetime) -> str:
    risk = (
        f'<p><span class="k risk-{_e(item.risk)}">{_e(es.ATT_RISK)}</span>'
        f"{_e(es.RISK_LABELS[item.risk])} &mdash; {_e(es.RISK_MEANING[item.risk])} "
        f"<small>({_e(es.ATT_KIND_LABELS[item.kind])}; {_e(item.kind)})</small></p>"
    )
    facts = [_fact(es.ATT_WHY, " " + _e(item.why_es), None)]
    if item.deadline is not None:
        facts.append(_fact(es.ATT_DEADLINE, " " + _deadline(item.deadline, now), None))
    facts.append(_fact(es.ATT_DEFAULT, " " + _e(item.default_es), None))
    facts.append(_fact(es.ATT_IF_IGNORED, " " + _e(item.if_ignored_es), None))
    links = "".join(source_link(source) for source in item.sources)
    facts.append(f"<li>{links.strip()}</li>")
    return (
        f'<article data-filter class="att att-{_e(item.risk)}" id="atencion-{_anchor(item.id)}">'
        f"<h4>{_e(item.title_es)}</h4>{risk}"
        f'<ul class="facts">{"".join(facts)}</ul></article>'
    )


def _attention(snapshot: Snapshot, now: datetime) -> str:
    items = snapshot.attention
    if not items:
        body = f"<p><strong>{_e(es.attention_empty(_local(snapshot.generated_at)))}</strong></p>"
        return _section("atencion", es.SECTION_ATTENTION, body)
    parts = [
        f'<p class="muted">{_e(es.ATTENTION_INTRO)}</p>',
        f"<p><strong>{_e(es.attention_count(len(items)))}</strong></p>",
    ]
    parts.extend(_attention_item(item, now) for item in items[:ATTENTION_SHOWN])
    rest = items[ATTENTION_SHOWN:]
    if rest:
        parts.append(
            f"<details><summary>{_e(es.attention_more(len(rest)))}</summary>"
            + "".join(_attention_item(item, now) for item in rest)
            + "</details>"
        )
    return _section("atencion", es.SECTION_ATTENTION, "\n".join(parts))


def _run_dir(run: Run) -> str:
    return run.store_path.rstrip("/") + "/" + run.run_id


def _run_article(run: Run, now: datetime, projects: dict[str, Project]) -> str:
    sources = run.sources
    project = projects.get(run.project_id or "")
    title = es.run_title(run.run_id, project.name if project else run.project_id)
    facts: list[str] = []
    if run.closed is not None:
        facts.append(
            _fact(es.RUN_CLOSED,
                  " " + _e(es.closed_line(run.closed.reason, run.closed.closed_on)),
                  run.closed.source)
        )
    if run.readable:
        facts.append(
            _fact(es.RUN_MODE, " " + _stored(es.MODE_NAMES.get(run.mode, run.mode), run.mode),
                  _pick(sources, "mode"))
        )
        if run.current_step is None:
            step_html = " " + _e(es.RUN_STEP_NONE)
        else:
            stored = run.step_status.get(run.current_step, "")
            step_html = (
                " " + _stored(es.STEP_LABELS.get(run.current_step, run.current_step),
                              run.current_step)
                + ", "
                + _stored(es.STEP_NAMES.get(stored, stored or es.UNKNOWN), stored or es.UNKNOWN)
            )
        facts.append(_fact(es.RUN_STEP, step_html, _pick(sources, "step_status")))
        for gate in run.pending_gates:
            facts.append(
                _fact(
                    es.RUN_GATE,
                    " " + _e(es.gate_line(gate.gate_id, gate.action, gate.expired)),
                    _pick(sources, "pending_gates", f"/{gate.gate_id}.")
                    or _pick(sources, "pending_gates"),
                )
            )
        if run.quality is not None:
            facts.append(
                _fact(
                    es.RUN_QUALITY,
                    " " + _e(es.quality_line(run.quality.passed, run.quality.iteration,
                                             run.quality.max_iterations)),
                    _pick(sources, "quality"),
                )
            )
        facts.append(
            _fact(es.RUN_UPDATED, " " + _when(run.updated_at, now), _pick(sources, "updated_at"))
        )
        moved = _moment(run.updated_at)
        idle = _seconds_since(moved, now) if moved is not None else None
        idle_days = idle // 86400 if idle is not None else 0
        if run.status == "in_progress" and idle_days > INACTIVE_DAYS:
            facts.append(f'<li class="todo">{_e(es.inactive(idle_days))}</li>')
        if run.last_event is not None and run.last_event.message:
            message = f" {es.QUOTE_OPEN}{_e(run.last_event.message)}{es.QUOTE_CLOSE}"
        else:
            message = " " + _e(es.RUN_NO_EVENTS)
        facts.append(_fact(es.RUN_LAST_MESSAGE, message, _pick(sources, "last_event")))
        if run.cost.state == "recorded" and run.cost.tokens is not None:
            cost_text = es.cost_recorded(run.cost.tokens)
            cost_source = _pick(sources, "cost")
            facts.append(f"<li>{_e(cost_text)}{source_link(cost_source)}</li>")
        else:
            folder = Source(kind="file", path=_run_dir(run), field="cost")
            facts.append(
                f"<li>{_e(es.COST_UNKNOWN)} <a class=\"src\" href=\"{_e(file_href(folder.path))}\""
                f" title=\"{_e(es.COST_UNKNOWN_SOURCE_HELP)}\">{_e(es.SOURCE_LINK)}</a></li>"
            )
    facts.append(
        _fact(es.RUN_STORE, f" <code>{_e(_run_dir(run))}</code>",
              _pick(sources, "status") or Source(kind="file", path=_run_dir(run)))
    )
    if run.duplicate_of:
        others = ", ".join(_e(path) for path in run.duplicate_of)
        links = "".join(source_link(Source(kind="file", path=path)) for path in run.duplicate_of)
        facts.append(f"<li><small>{_e(es.RUN_DUPLICATE)}: {others}</small>{links}</li>")
    return (
        f'<article data-filter><h4>{_e(title)}</h4><ul class="facts">'
        + "".join(facts)
        + "</ul></article>"
    )


def _runs(
    runs: list[Run], now: datetime, projects: dict[str, Project], closed: set[str]
) -> str:
    grouped: dict[str, list[Run]] = defaultdict(list)
    for run in runs:
        grouped[_group_of(run, closed)].append(run)
    parts: list[str] = []
    for key in GROUP_ORDER:
        members = grouped.get(key, [])
        if not members and key in ("failed", "created", "other", "closed"):
            continue  # groups the prompt does not name are shown only when they have runs
        members.sort(
            key=lambda r: (_moment(r.updated_at) or datetime.min.replace(tzinfo=UTC)),
            reverse=True,
        )
        group: list[str] = [
            f'<h3 id="grupo-{_e(key)}">{_stored(es.GROUP_LABELS[key], key)} '
            f"&mdash; {len(members)}</h3>",
            f'<p class="muted">{_e(es.GROUP_WHY[key])}</p>',
        ]
        if not members:
            group.append(f"<p>{_e(es.GROUP_EMPTY)}</p>")
        group.extend(_run_article(run, now, projects) for run in members)
        if key == "closed":
            # Closed runs ask for nothing: collapsed, so they do not crowd the open work.
            group = [f"<details><summary>{_e(es.closed_summary(len(members)))}</summary>",
                     *group, "</details>"]
        parts.extend(group)
    return _section("trabajos", es.SECTION_RUNS, "\n".join(parts))


def _project_facts(project: Project, now: datetime) -> list[str]:
    sources = project.sources
    facts: list[str] = []
    if project.kind == "folder":
        return [f"<li>{_e(es.PROJECT_FOLDER)}{source_link(_pick(sources, 'kind'))}</li>"]
    if project.has_commits is None:
        return [f"<li>{_e(es.PROJECT_UNKNOWN_STATE)}{source_link(_pick(sources, 'kind'))}</li>"]
    if not project.has_commits:
        branch_html = " " + _e(es.PROJECT_NO_COMMITS)
    elif project.branch is None:
        branch_html = " " + _e(es.PROJECT_DETACHED)
    else:
        branch_html = f" <code>{_e(project.branch)}</code>"
    facts.append(_fact(es.PROJECT_BRANCH, branch_html, _pick(sources, "branch")))
    if project.head:
        commit_html = (
            f" <code>{_e(project.head[:SHORT_HASH])}</code>, "
            + _when(project.last_commit_at, now)
        )
        facts.append(
            _fact(es.PROJECT_LAST_COMMIT, commit_html,
                  _pick(sources, "last_commit_at") or _pick(sources, "head"))
        )
    todo: list[str] = []
    if project.uncommitted_paths:
        todo.append(_e(es.uncommitted(project.uncommitted_paths))
                    + source_link(_pick(sources, "uncommitted_paths")))
    if project.ahead:
        todo.append(_e(es.ahead(project.ahead)) + source_link(_pick(sources, "ahead")))
    if _local_branches(project):
        todo.append(_e(es.branches_without_upstream(_local_branches(project)))
                    + source_link(_pick(sources, "branches_without_upstream")))
    if todo:
        facts.append(
            f'<li><span class="k todo">{_e(es.PROJECT_UNFINISHED)}</span><ul>'
            + "".join(f"<li>{item}</li>" for item in todo)
            + "</ul></li>"
        )
    elif _git_unknown(project):
        facts.append(f"<li>{_e(es.PROJECT_PARTIAL)}{source_link(_pick(sources, 'kind'))}</li>")
    else:
        facts.append(
            f'<li class="ok">{_e(es.PROJECT_CLEAN)}'
            f"{source_link(_pick(sources, 'uncommitted_paths'))}</li>"
        )
    if project.behind:
        facts.append(f"<li>{_e(es.behind(project.behind))}"
                     f"{source_link(_pick(sources, 'behind'))}</li>")
    if project.remotes:
        remotes = ", ".join(f"<code>{_e(r.url)}</code>" for r in project.remotes)
        facts.append(_fact(es.PROJECT_REMOTES, " " + remotes, _pick(sources, "remotes")))
    elif project.kind == "repository":
        facts.append(
            _fact(es.PROJECT_REMOTES, " " + _e(es.PROJECT_NO_REMOTES), _pick(sources, "remotes"))
        )
    return facts


def _worktree_list(project: Project, children: list[Project]) -> list[str]:
    """Worktrees git lists for ``project`` that are not shown as projects of their own."""
    shown = {child.path for child in children}
    plain: list[Worktree] = []
    internal: list[Worktree] = []
    for worktree in project.worktrees:
        if worktree.path == project.path or worktree.path in shown:
            continue
        (internal if worktree.inside_run_store else plain).append(worktree)
    source = _pick(project.sources, "worktrees")
    items: list[str] = []
    for worktree in plain:
        notes = []
        if not worktree.exists:
            notes.append(es.WORKTREE_MISSING)
        if worktree.prunable:
            notes.append(es.WORKTREE_PRUNABLE)
        label = f"<code>{_e(worktree.path)}</code>"
        if worktree.branch:
            label += f" &mdash; <code>{_e(worktree.branch)}</code>"
        if notes:
            label += f" <small>({_e(', '.join(notes))})</small>"
        items.append(f"<li>{label}{source_link(source)}</li>")
    parts: list[str] = []
    if items:
        parts.append(f"<details><summary>{_e(es.worktrees_summary(len(items)))}</summary>"
                     f"<ul>{''.join(items)}</ul></details>")
    if internal:
        rows = "".join(
            f"<li><code>{_e(w.path)}</code>"
            + (f" <small>({_e(es.WORKTREE_MISSING)})</small>" if not w.exists else "")
            + "</li>"
            for w in internal
        )
        parts.append(f"<details><summary>{_e(es.run_store_worktrees(len(internal)))}"
                     f"{source_link(source)}</summary><ul>{rows}</ul></details>")
    return parts


def _project_article(project: Project, now: datetime, children: list[Project]) -> str:
    facts = _project_facts(project, now)
    header = (
        f'<h4>{_e(project.name)} <small><a href="{_e(file_href(project.path))}">'
        f"{_e(project.path)}</a></small></h4>"
    )
    body = '<ul class="facts">' + "".join(facts) + "</ul>"
    extra = _worktree_list(project, children)
    if children:
        unfinished = [c for c in children if _has_unfinished(c)]
        rows = "".join(_child_article(child, now) for child in _ordered(children))
        extra.append(
            f"<details><summary>{_e(es.worktrees_summary(len(children)))}"
            + (f' &mdash; <span class="todo">{_e(es.children_unfinished(len(unfinished)))}</span>'
               if unfinished else "")
            + f"</summary>{rows}</details>"
        )
    return f"<article data-filter>{header}{body}{''.join(extra)}</article>"


def _child_article(project: Project, now: datetime) -> str:
    facts = _project_facts(project, now)
    return (
        f'<article data-filter><h4>{_e(project.name)} <small><a '
        f'href="{_e(file_href(project.path))}">{_e(project.path)}</a></small></h4>'
        f'<ul class="facts">{"".join(facts)}</ul></article>'
    )


def _ordered(projects: list[Project]) -> list[Project]:
    return sorted(projects, key=lambda p: (not _has_unfinished(p), p.name.casefold(), p.path))


def _projects(snapshot: Snapshot, now: datetime) -> str:
    by_id = {p.id: p for p in snapshot.projects}
    children: dict[str, list[Project]] = defaultdict(list)
    top: list[Project] = []
    for project in snapshot.projects:
        if project.kind == "worktree" and project.parent_repository in by_id:
            children[project.parent_repository or ""].append(project)
        else:
            top.append(project)
    articles = [
        _project_article(p, now, children.get(p.id, []))
        for p in _ordered(top)
        if not (p.kind == "worktree" and is_inside_run_store(p.path))
    ]
    internal = [
        p for p in top if p.kind == "worktree" and is_inside_run_store(p.path)
    ]
    if internal:
        rows = "".join(_child_article(p, now) for p in _ordered(internal))
        articles.append(
            f"<details><summary>{_e(es.run_store_worktrees(len(internal)))}</summary>"
            f"{rows}</details>"
        )
    body = f'<p class="muted">{_e(es.PROJECTS_INTRO)}</p>' + "\n".join(articles)
    return _section("proyectos", es.SECTION_PROJECTS, body)


def _memory(snapshot: Snapshot) -> str:
    memory = snapshot.memory
    if memory is None:
        return _section("memoria", es.SECTION_MEMORY, f"<p>{_e(es.MEMORY_MISSING)}</p>")
    root_source = _pick(memory.sources, "notes") or Source(kind="file", path=memory.root)
    types = ", ".join(
        f"{count} {es.MEMORY_TYPES[name]}"
        for name, count in memory.by_type.model_dump().items()
        if count
    )
    facts = [
        _fact(es.MEMORY_ROOT, f" <code>{_e(memory.root)}</code>", root_source),
        _fact(es.MEMORY_NOTES, f" {memory.notes}" + (f" ({_e(types)})" if types else ""),
              root_source),
    ]
    if memory.index_lines is None:
        index_html = " " + _e(es.MEMORY_INDEX_UNKNOWN)
    else:
        index_html = f" {memory.index_lines}"
    facts.append(_fact(es.MEMORY_INDEX, index_html, _pick(memory.sources, "index_lines")))
    body = f'<p class="muted">{_e(es.MEMORY_INTRO)}</p><ul class="facts">{"".join(facts)}</ul>'
    if memory.malformed:
        rows = "".join(
            f"<li><code>{_e(note.path)}</code>: {_e(note.problem)}"
            f"{source_link(Source(kind='file', path=note.path))}</li>"
            for note in memory.malformed
        )
        body += f"<h3>{_e(es.MEMORY_MALFORMED)} &mdash; {len(memory.malformed)}</h3><ul>{rows}</ul>"
    return _section("memoria", es.SECTION_MEMORY, body)


def _problems(problems: list[Problem]) -> str:
    if not problems:
        return _section("problemas", es.SECTION_PROBLEMS, f"<p>{_e(es.PROBLEMS_NONE)}</p>")
    rows = "".join(
        f"<li><code>{_e(p.path)}</code> &mdash; {_e(p.what)}: {_e(p.reason)}"
        f"{source_link(Source(kind='file', path=p.path))}</li>"
        for p in problems
    )
    body = f'<p class="muted">{_e(es.PROBLEMS_INTRO)}</p><ul>{rows}</ul>'
    return _section("problemas", es.SECTION_PROBLEMS, body)


def _origin(snapshot: Snapshot, origin: Source | None, refresh_args: list[str]) -> str:
    commit = snapshot.generator.product_commit
    facts = [
        _fact(es.ORIGIN_GENERATED, " " + _e(_local(snapshot.generated_at)), origin),
        _fact(es.ORIGIN_WORKSPACE, f" <code>{_e(snapshot.workspace)}</code>",
              Source(kind="file", path=snapshot.workspace)),
        _fact(es.ORIGIN_COMMIT,
              f" <code>{_e(commit[:SHORT_HASH])}</code>" if commit
              else " " + _e(es.ORIGIN_COMMIT_UNKNOWN), origin),
        _fact(es.ORIGIN_VERSION, " " + _e(snapshot.generator.wilfred_version), origin),
    ]
    if origin is not None:
        facts.append(_fact(es.ORIGIN_SNAPSHOT, f" <code>{_e(origin.path)}</code>", origin))
    command = shlex.join(
        ["hw", "command-center", "build", "--workspace", snapshot.workspace, *refresh_args]
    )
    body = (
        f'<ul class="facts">{"".join(facts)}</ul>'
        f"<p>{_e(es.ORIGIN_READ_ONLY)}</p>"
        f"<p>{_e(es.ORIGIN_REFRESH)}<br><code>{_e(command)}</code></p>"
    )
    return _section("origen", es.SECTION_ORIGIN, body)


def _section(anchor: str, title: str, body: str) -> str:
    return f'<section id="{anchor}"><h2>{_e(title)}</h2>\n{body}\n</section>'


def render_page(
    snapshot: Snapshot,
    *,
    now: datetime | None = None,
    snapshot_path: str | None = None,
    refresh_args: list[str] | None = None,
) -> str:
    """The whole page as text. Pure: reads nothing, writes nothing.

    ``refresh_args`` are extra ``build`` options (``--out-dir``, ``--memory``) shown in the
    refresh command when they differ from the defaults.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    origin = Source(kind="file", path=snapshot_path) if snapshot_path else None
    generated = _moment(snapshot.generated_at)
    generated_ms = int(generated.timestamp() * 1000) if generated is not None else 0
    runs = distinct_runs(snapshot.runs)
    closed = closed_run_ids(snapshot.runs)
    projects = {p.id: p for p in snapshot.projects}
    script_hash = base64.b64encode(hashlib.sha256(_SCRIPT.encode("ascii")).digest()).decode()
    policy = (
        "default-src 'none'; style-src 'unsafe-inline'; "
        f"script-src 'sha256-{script_hash}'; img-src 'none'; form-action 'none'; "
        "base-uri 'none'"
    )
    body = "\n".join(
        [
            _header(snapshot, moment),
            _summary(snapshot, runs, origin, closed),
            _attention(snapshot, moment),
            _runs(runs, moment, projects, closed),
            _projects(snapshot, moment),
            _memory(snapshot),
            _problems(snapshot.problems),
            _origin(snapshot, origin, refresh_args or []),
        ]
    )
    return (
        f'<!doctype html>\n<html lang="{es.LANG}">\n<head>\n<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_e(policy)}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f"<title>{_e(es.PAGE_TITLE)}</title>\n<style>{_CSS}</style>\n</head>\n"
        f'<body data-generated="{generated_ms}">\n'
        f"{body}\n<script>{_SCRIPT}</script>\n</body>\n</html>\n"
    )
