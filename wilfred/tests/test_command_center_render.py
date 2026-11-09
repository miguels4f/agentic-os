"""Command center: the page renderer (``render.py``) and its Spanish strings (``text_es.py``)."""

from __future__ import annotations

import base64
import hashlib
import html
import re
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from hyperwilfred_core.command_center import render, text_es
from hyperwilfred_core.command_center.model import (
    Cost,
    LastEvent,
    Memory,
    PendingGate,
    Problem,
    Project,
    Run,
    Snapshot,
    Source,
    Worktree,
)
from hyperwilfred_core.command_center.model import Generator as Gen

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
WS = "/ws"


def _snapshot(**overrides) -> Snapshot:
    data = {
        "generated_at": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "workspace": WS,
        "generator": Gen(wilfred_version="0.1.0", product_commit="a" * 40),
    }
    data.update(overrides)
    return Snapshot(**data)


def _run(run_id: str = "r1", status: str = "in_progress", store: str = "/ws/repo/.hw",
         **fields) -> Run:
    state = f"{store}/{run_id}/state.json"
    sources = [Source(kind="file", path=state, field=f)
               for f in ("mode", "status", "step_status", "updated_at", "quality")]
    sources.append(Source(kind="file", path=f"{store}/{run_id}/events.jsonl", field="last_event"))
    base = {
        "run_id": run_id, "store_path": store, "project_id": "repo", "mode": "build",
        "status": status, "step_status": {"implement": "running"},
        "current_step": "implement", "updated_at": "2026-10-08T10:00:00Z", "sources": sources,
    }
    base.update(fields)
    return Run(**base)


def _repo(**fields) -> Project:
    path = fields.get("path", "/ws/repo")
    git = shlex.join(["git", "--no-optional-locks", "--no-pager", "-c", "core.fsmonitor=false",
                      "-c", "log.showSignature=false", "-C", path, "status", "--porcelain"])
    base = {
        "id": "repo", "name": "repo", "path": "/ws/repo", "kind": "repository",
        "branch": "main", "head": "b" * 40, "has_commits": True, "uncommitted_paths": 0,
        "ahead": 0, "behind": 0, "branches_without_upstream": [], "local_branches": 1,
        "last_commit_at": "2026-10-08T10:00:00+02:00",
        "sources": [Source(kind="git", path=path, field=f, command=git)
                    for f in ("branch", "uncommitted_paths", "ahead", "head", "remotes",
                              "branches_without_upstream", "last_commit_at")],
    }
    base.update(fields)
    return Project(**base)


def _page(snapshot: Snapshot) -> str:
    return render.render_page(snapshot, now=NOW, snapshot_path="/out/snapshot.json")


def _visible_text(page: str) -> str:
    body = re.sub(r"<(script|style)\b.*?</\1>", "", page, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


def test_run_message_with_a_script_tag_is_escaped() -> None:
    hostile = "<script>alert(1)</script>"
    run = _run(last_event=LastEvent(message=hostile, timestamp="2026-10-08T10:00:00Z"))
    page = _page(_snapshot(runs=[run]))
    assert "<script>alert" not in page
    assert html.escape(hostile) in page
    assert page.count("<script>") == 1, "only the page's own filter script"


def test_branch_name_with_quotes_and_ampersand_is_escaped() -> None:
    branch = 'feat/"quoted"&<b>\'x\''
    page = _page(_snapshot(projects=[_repo(branch=branch)]))
    assert branch not in page
    assert html.escape(branch, quote=True) in page


def test_path_with_spaces_and_accents_gives_a_valid_file_link() -> None:
    path = "/Users/me/code/Olimpiadas 2026/año ñandú"
    page = _page(_snapshot(projects=[_repo(path=path, name="año")]))
    href = render.file_href(path)
    assert href.startswith("file:///") and " " not in href
    assert re.fullmatch(r"file://[A-Za-z0-9/%._~-]+", href)
    assert unquote(urlsplit(href).path) == path
    assert f'href="{href}"' in page


def test_unknown_cost_renders_the_sentence_and_no_number() -> None:
    page = _page(_snapshot(runs=[_run(cost=Cost(state="unknown"))]))
    text = _visible_text(page)
    assert text_es.COST_UNKNOWN in text
    for match in re.finditer(r"Coste[^\n]{0,60}", text):
        assert not re.search(r"\d", match.group(0)), match.group(0)


def test_recorded_cost_shows_the_token_count() -> None:
    run = _run(cost=Cost(state="recorded", tokens={"input_tokens": 100, "output_tokens": 20}))
    assert "120" in _visible_text(_page(_snapshot(runs=[run])))


def test_duplicate_run_is_rendered_once_with_the_other_store() -> None:
    first = _run(store="/ws/a/.hw", duplicate_of=["/ws/b/.hw"], updated_at="2026-10-08T09:00:00Z")
    second = _run(store="/ws/b/.hw", duplicate_of=["/ws/a/.hw"], updated_at="2026-10-08T11:00:00Z")
    page = _page(_snapshot(runs=[first, second]))
    assert page.count("<h4>r1 ") == 1
    assert f"{text_es.RUN_DUPLICATE}: /ws/a/.hw" in page


def test_empty_snapshot_renders_every_section_in_order() -> None:
    page = _page(_snapshot())
    titles = [text_es.SECTION_SUMMARY, text_es.SECTION_ATTENTION, text_es.SECTION_RUNS,
              text_es.SECTION_PROJECTS, text_es.SECTION_MEMORY, text_es.SECTION_PROBLEMS,
              text_es.SECTION_ORIGIN]
    positions = [page.index(f"<h2>{html.escape(t)}</h2>") for t in titles]
    assert positions == sorted(positions)
    empty = text_es.attention_empty(render._local(_snapshot().generated_at))
    assert html.escape(empty) in page
    assert html.escape(text_es.PROBLEMS_NONE) in page


def test_page_has_no_external_url_when_the_data_has_none() -> None:
    snapshot = _snapshot(
        projects=[_repo()],
        runs=[_run(), _run("r2", "awaiting_human",
                           pending_gates=[PendingGate(gate_id="ship", expired=True)])],
        memory=Memory(root="/m", notes=2, index_lines=1),
        problems=[Problem(path="/ws/x/state.json", what="run state", reason="corrupt")],
    )
    page = _page(snapshot)
    assert "http://" not in page and "https://" not in page
    assert "//" not in re.sub(r"file:///", "", page.replace("</", "<")), "no protocol-relative URL"
    assert not re.search(r"\b(src|srcset)=", page), "nothing is loaded from anywhere"
    assert "@import" not in page and "url(" not in page


def test_old_snapshot_shows_the_warning_and_a_fresh_one_does_not() -> None:
    old = _snapshot(generated_at=(NOW - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert html.escape(text_es.stale_warning(30)) in _page(old)
    fresh = _page(_snapshot())
    assert re.findall(r'<p class="warn"[^>]*>', fresh) == [
        '<p class="warn" role="alert" id="stale-view" hidden>'
    ], "a fresh page hides the warning; its script reveals it if opened a day later"
    assert html.escape(text_es.STALE_UNKNOWN) in _page(_snapshot(generated_at="not a date"))


def test_render_module_contains_no_non_ascii_letters() -> None:
    source = Path(render.__file__).read_text(encoding="utf-8")
    offenders = [char for char in source if ord(char) > 127]
    assert offenders == [], "Spanish text belongs in text_es.py"


def test_runs_are_grouped_in_the_required_order() -> None:
    runs = [_run("done", "completed"), _run("going", "in_progress"),
            _run("blocked", "gate_failed"), _run("waiting", "awaiting_human"),
            _run("broken", readable=False)]
    page = _page(_snapshot(runs=runs))
    order = ["awaiting_human", "gate_failed", "in_progress", "completed", "unreadable"]
    positions = [page.index(f'id="grupo-{key}"') for key in order]
    assert positions == sorted(positions)
    assert page.index("<h4>waiting") < page.index("<h4>blocked") < page.index("<h4>going")
    # Stored value next to the Spanish label.
    assert f"{text_es.GROUP_AWAITING} <small>(awaiting_human)</small>" in page


def test_unrecognised_status_is_shown_not_dropped() -> None:
    page = _page(_snapshot(runs=[_run("odd", "paused-by-alien")]))
    assert 'id="grupo-other"' in page and "<h4>odd" in page


def test_every_fact_has_a_source_link_and_git_facts_show_the_command() -> None:
    page = _page(_snapshot(projects=[_repo(uncommitted_paths=3)], runs=[_run()]))
    assert "<code>git status --porcelain</code></a>" in page
    for item in re.findall(r'<li><span class="k">.*?</li>', page):
        if text_es.ORIGIN_GENERATED in item or text_es.ORIGIN_COMMIT in item \
                or text_es.ORIGIN_VERSION in item:
            continue  # facts about the page itself, not about the workspace
        assert 'class="src"' in item, item


def test_unfinished_project_is_listed_first_and_summarised() -> None:
    clean = _repo(id="clean", name="aaa-clean", path="/ws/clean")
    dirty = _repo(id="dirty", name="zzz-dirty", path="/ws/dirty", ahead=2,
                  branches_without_upstream=["wip"])
    page = _page(_snapshot(projects=[clean, dirty]))
    assert page.index("zzz-dirty") < page.index("aaa-clean")
    assert html.escape(text_es.summary_unfinished(1, ["zzz-dirty"])) in page
    assert html.escape(text_es.ahead(2)) in page


def test_run_store_worktrees_are_collapsed() -> None:
    inner = Worktree(path="/ws/repo/.hw/r1/worktrees/x", inside_run_store=True, exists=False)
    page = _page(_snapshot(projects=[_repo(worktrees=[inner])]))
    summary = html.escape(text_es.run_store_worktrees(1))
    assert summary in page
    details = page[page.rindex("<details>", 0, page.index(summary)):]
    assert inner.path in details and "<details open" not in page


def test_page_is_complete_without_scripts_and_the_script_is_pinned() -> None:
    page = _page(_snapshot(runs=[_run()]))
    assert 'id="filterbox" hidden' in page, "the filter only appears when scripts run"
    assert "<h4>r1 " in page.split("<script>")[0], "content does not depend on the script"
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', page).group(1)
    assert "default-src &#x27;none&#x27;" in policy and "sha256-" in policy


def test_spanish_text_explains_the_technical_terms() -> None:
    side = _repo(id="side", name="side", path="/ws/side", kind="worktree",
                 parent_repository="repo")
    page = _visible_text(_page(_snapshot(projects=[_repo(), side], runs=[_run()])))
    for term in ("run:", "worktree:", "gate:", "branch:", "commit (una foto"):
        assert term in page, term


def test_old_run_in_progress_is_flagged_and_gate_tells_the_action() -> None:
    old = _run("old", "in_progress", updated_at="2026-09-01T10:00:00Z")
    waiting = _run("wait", "awaiting_human",
                   pending_gates=[PendingGate(gate_id="ship", action="Merge x", expired=True)])
    page = _page(_snapshot(runs=[old, waiting]))
    assert html.escape(text_es.inactive(38)) in page
    assert html.escape(text_es.gate_line("ship", "Merge x", True)) in page


def test_git_link_text_is_the_short_command_even_for_quoted_paths() -> None:
    path = "/ws/Mi Work's #space ñ/proj? a"
    page = _page(_snapshot(projects=[_repo(path=path, name="odd", uncommitted_paths=1)]))
    assert "<code>git status --porcelain</code></a>" in page
    assert html.escape(shlex.quote(path)) in page, "the full command stays in the tooltip"


def test_hostile_paths_stay_inside_attributes() -> None:
    path = '/ws/x" onmouseover="alert(1)\'><img src=x>'
    page = _page(_snapshot(projects=[_repo(path=path, name="x")],
                           problems=[Problem(path=path, what="w", reason="r")]))
    assert "<img" not in page and 'onmouseover="' not in page
    for href in re.findall(r'href="([^"]*)"', page):
        assert href.startswith(("file:///", "#")), href


def test_script_hash_in_the_policy_matches_the_emitted_script() -> None:
    page = _page(_snapshot())
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"sha256-{digest}" in page


def test_extreme_and_malformed_dates_render_as_unknown() -> None:
    run = _run(updated_at="0001-01-01T00:00:00+10:00")
    page = _page(_snapshot(generated_at="0001-01-01T00:00:00Z", runs=[run]))
    assert html.escape(text_es.STALE_UNKNOWN) in page


def test_unknown_git_state_is_never_called_clean() -> None:
    blind = _repo(uncommitted_paths=None, ahead=None, branches_without_upstream=None)
    page = _page(_snapshot(projects=[blind]))
    assert html.escape(text_es.PROJECT_CLEAN) not in page
    assert html.escape(text_es.PROJECT_PARTIAL) in page
    assert html.escape(text_es.summary_git_unknown(1)) in page


def test_refresh_command_is_shell_quoted_and_keeps_custom_options() -> None:
    workspace = "/ws/it's; rm -rf ~"
    page = render.render_page(_snapshot(workspace=workspace), now=NOW,
                              refresh_args=["--out-dir", "/o"])
    command = shlex.join(["hw", "command-center", "build", "--workspace", workspace,
                          "--out-dir", "/o"])
    assert html.escape(command) in page


def test_summary_lines_link_to_the_snapshot() -> None:
    page = _page(_snapshot(runs=[_run()]))
    summary = page[page.index('id="resumen"'):page.index('id="atencion"')]
    for item in re.findall(r"<li>.*?</li>", summary):
        assert 'class="src"' in item, item
