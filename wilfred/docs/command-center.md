# Command center

A read-only, local status page for a workspace of projects. It answers four questions without
opening any other tool: which projects exist and in what state, which Wilfred runs are running,
finished, stuck or failed, what is waiting for the owner, and where each fact comes from.

It shows; it does not act. There is no server, no background process, no button and no form.
The page text is in Spanish (it is read by the owner); code and documents are in English.

Specification: a private specification (not included in this kit).
Items from the ops repository: `Ideas/ops/specs/2026-10-14-notion-to-ops-design.md`, section 14.

## Commands

```text
hw command-center snapshot --workspace <dir> [--memory <dir>] [--out-dir <dir>] [--ops-bin <path>]
hw command-center render   [--out-dir <dir>]
hw command-center build    --workspace <dir> [--memory <dir>] [--out-dir <dir>] [--ops-bin <path>]
```

- `snapshot` reads the workspace (repositories, worktrees, run stores) and the shared memory
  index, and writes `snapshot.json`.
- `render` reads `<out-dir>/snapshot.json`, validates it against the snapshot model
  (`wilfred-command-center-snapshot/v1`) and writes `<out-dir>/index.html`. A missing or invalid
  snapshot exits with code 2 and a JSON error; nothing is written.
- `build` runs `snapshot` then `render`. This is the command to use day to day.

Each command prints one JSON object with the paths it wrote, like every other `hw` command.
`--workspace` is required and never defaulted. `--memory` defaults to `~/.agents/memory`. `--ops-bin` defaults to `~/.local/bin/ops` and is used only when the workspace holds an `ops` folder (see "Tasks and sales from the ops repository").

Typical use on the owner's Mac:

```bash
hw command-center build --workspace ~/code
open "$HOME/Library/Application Support/Wilfred/command-center/index.html"
```

## Where output goes

`--out-dir` defaults to `~/Library/Application Support/Wilfred/command-center/`, created with
mode 700; both files are written with mode 600, atomically (temporary file, fsync, rename).
The output directory is refused (exit 2, nothing written) when it lies inside the workspace,
inside the memory directory, inside any git repository, or inside any project or worktree the
snapshot lists. `render` applies the same checks using the workspace and projects named in the
snapshot it reads.

## The page

One self-contained HTML file. Sections, in order: summary, the attention inbox (below), runs grouped by state (awaiting a human decision, blocked by a failed check, in
progress, finished, then any other stored state, then unreadable), projects (those with
unfinished work first; worktrees created inside run stores collapsed), shared memory, read
problems, and where the page comes from.

- Every displayed fact is followed by a link to its source: a `file://` link to the file, or,
  for facts read with git, a link whose text is the git command that produced the value (the
  full command, with its safety options, is in the link's tooltip). Items read from the ops
  repository also link to the file's copy on GitHub (`https://github.com/...`, the only web
  address the page can contain; it is a plain link and nothing is loaded from it).
- A run present in several run stores is shown once, with "también presente en" and the other
  stores.
- A warning appears when the snapshot is more than 24 hours old at render time.
- Every string from the snapshot is escaped with `html.escape`; run messages and branch names
  are untrusted input.
- No external resource of any kind: inline CSS; one short inline script that only adds a text
  filter. A Content-Security-Policy meta tag allows that script by its SHA-256 hash and nothing
  else, and blocks images, forms and any network load. With scripts disabled every piece of
  content is still visible; only the filter is missing.
- All Spanish text lives in `command_center/text_es.py`; `render.py` contains only ASCII.

## Attention inbox ("Necesita tu atención")

`attention.py` derives the list from the snapshot alone. It is a pure function: it reads no
file, runs no command and receives the current time as a parameter. `build` and `snapshot`
store the result in `snapshot.json` under `attention`; `render` shows it as stored. The inbox
describes; it has no button and executes nothing.

| Kind | Condition | Risk |
|---|---|---|
| `gate` | a run (any copy of its run id, readable or not) has a `<gate>.pending` marker and no consumed decision | `alto` if an approval window is known and still open (deadline = earliest open expiry); `medio` if every window expired, or if no window can be read |
| `awaiting_human` | run status `awaiting_human` and no pending gate | `medio` |
| `gate_failed` | run status `gate_failed`, no pending gate, `updated_at` at most 14 days ago (an unknown date is shown, because "recent" cannot be ruled out) | `medio` |
| `stale_run` | run status `in_progress`, no pending gate, newest of last event and `updated_at` more than 14 days ago (an undated run is not called stale) | `bajo` |
| `unpushed_work` | a repository (not a worktree, not inside a run store) with commits ahead of its upstream, a branch without upstream, or uncommitted paths while the last commit is more than 7 days old | `medio` |
| `unreadable` | any `problems` entry | `bajo` |
| `memory_malformed` | a memory note without name, description or a recognised type (an unreadable note is listed once, as `unreadable`) | `bajo` |
| `ops_pending_proposal` | a pending proposal in `<workspace>/ops/proposals/`, or a hand edit that `ops status` lists in `unapproved_edits`: a file under `sales/` or `schema/`, or a task file whose authorization (`authorized_by`, `authorized_at`) was typed by hand | `alto` (no deadline: nothing expires, but the change is stopped until the owner runs `ops approve`) |
| `ops_overdue_task` | an ops task whose `due_end` (or `due`, when there is no range) is before today's date on this Mac, and whose status is not Complete or Deferred | `medio` |
| `ops_blocked_task` | an ops task with status Blocked that is not overdue | `bajo` |

Thresholds are the constants `RECENT_DAYS = 14` and `UNCOMMITTED_DAYS = 7` in `attention.py`.
A run gives at most one item (a pending gate wins); duplicated runs give one item. An ops task gives at most one item about its state (overdue wins over blocked, and the overdue text names the blocker); a hand-typed authorization of the same task is a separate `alto` item, because it asks for a different decision. A run listed
in the closed-runs register (below) gives no item, except a `gate` item of risk `alto`.

### Closed-runs register

`closed-runs.json` in the output directory lists runs the owner closed: archived elsewhere, or kept
in place as evidence (for example the Gate 0 runs under `remediation-20260929/op`, which must not
be moved). `snapshot` and `build` read it; nothing in the package writes it.

```json
{
  "schema_version": "wilfred-closed-runs/v1",
  "runs": [
    {"path": "/abs/project/.hw/<run>", "reason": "plain sentence", "closed_on": "2026-10-13"}
  ]
}
```

- `path` is the run directory, a direct child of a `.hw` store; `reason` is required;
  `closed_on` is optional text.
- A listed run stays on the page in the collapsed "Cerrados" group, with its reason and a link to
  the register. Its directory is not touched.
- It leaves the attention inbox, unless it has a gate whose approval window is still open: a
  running deadline is never hidden.
- A run id counts as closed only when every copy of it is listed; one open copy keeps it open.
- A missing register closes nothing. A register that cannot be read, or that has another
  `schema_version`, closes nothing and is reported as a read problem. A bad entry is skipped and
  reported; the other entries still apply. Mistakes therefore show more, never less.

What each risk level means on the page (the text is in `text_es.RISK_MEANING`):

- `alto` (alta): an approval of the owner is pending and the work stays stopped until he
  decides: a Wilfred approval window that is still open, or, in the ops repository, a sales or
  schema change or a hand-typed task authorization that only he can approve (`ops approve` in
  his own Terminal).
- `medio` (media): something is stopped or could be lost, but no approval deadline is running.
- `bajo` (baja): worth tidying; nothing stops or is lost by waiting.

Order: risk, then deadline (earliest first, none last), then age (oldest situation first). The
page shows the first 12 items and puts the rest behind a "y N más" line. An empty inbox says
"Nada necesita tu atención ahora mismo" with the time of the snapshot. Each item states why a
person is needed, the deadline if any, a proposed default, what happens if nobody acts, and
links to its sources.

### Daily routine

```bash
hw command-center build --workspace ~/code
open "$HOME/Library/Application Support/Wilfred/command-center/index.html"
```

## Tasks and sales from the ops repository

When the workspace holds a folder named `ops` (the git repository that replaced Notion for
tasks, projects and the sales pipeline), `snapshot` runs the owner's `ops` command twice, with
`--root` naming that folder (ops takes its options after the subcommand):

    ops export --json --root <workspace>/ops      (report ops-export/v1)
    ops status --json --root <workspace>/ops      (report ops-status/v1)

- The program is `--ops-bin`, default `~/.local/bin/ops`. Without an `ops` folder nothing runs.
  A folder without an executable program, a failure, a timeout (30 seconds), more than 16 MiB
  of output, a report that is not JSON or not the expected version: each becomes one read
  problem, and the snapshot is still written.
- The export must name `<workspace>/ops` as its `root` again; otherwise none of its data is
  shown and the status report is not read, so a differently configured `ops` can never put
  another repository's tasks on this page.
- `snapshot.json` keeps only what the inbox needs, under `ops`: open tasks that are blocked or
  dated (id, file, title, status, dates, blocker, projects), pending proposals (id, file, kind,
  target file, the NAMES of the fields they change, author, date) and the paths of hand edits
  waiting for `ops approve --file` (`unapproved_edits`: sales or schema files, and task files
  with a hand-typed authorization). No sales record, contact, proposal value or proposal reason
  is copied. A list is `null` when it could not be read; that is never shown as "none".
- Files `ops` itself cannot read become read problems (at most five are named; the rest are
  counted in one line). Validation errors (`check_errors`) and sync conflicts are left to
  mission-control.
- Each ops item links to the local file and, when the export gives a valid
  `https://github.com/<owner>/<repo>/blob/<branch>/` address, to its copy on GitHub. A hand edit
  waiting for approval links only to the local file: GitHub still holds the approved version.
- The page only ever links to `https://github.com/`; any other web address in a snapshot is
  ignored and rendered without a link.

## Read-only guarantee and how it is tested

- The collectors only read. Git is called through an allowlist of read subcommands with
  optional locks disabled, never through a shell (`gitread.py`).
- The only other program the command center starts is the owner's `ops`, through
  `opsread.py`: absolute path, argument list, exactly `export --json` and `status --json`
  followed by `--root <workspace>/ops`, no standard input, a clean environment (inherited
  `GIT_*` and `PYTHON*` variables dropped; `GIT_OPTIONAL_LOCKS=0`; `GIT_TERMINAL_PROMPT=0`;
  `GIT_NO_LAZY_FETCH=1`; `PYTHONIOENCODING=utf-8`; `PYTHONDONTWRITEBYTECODE=1`, so no bytecode cache appears next to the tool's code). `ops`
  itself guarantees that both commands are read-only and offline (no lock, no fetch, nothing
  written under `.git/`, `.ops/` or `board/`) and runs its git with optional locks and
  fsmonitor off. This is a deliberate exception to "the command center runs nothing a
  repository provides": the `ops` code lives in the workspace, and only the program named by
  `--ops-bin` is ever run. The import guard admits `subprocess` in exactly `gitread.py` and
  `opsread.py`; the read-only proof also covers a workspace with an `ops` repository and a
  stand-in `ops` program.
- `snapshot.py` is the only module of the package that writes, and only into the output
  directory. `render.py` is pure: it returns the page as text.
- `tests/test_command_center_readonly.py` fingerprints a fixture workspace (every file's hash,
  size, mode and mtime, every directory's listing and mtime, including `.git`) and the memory
  tree before and after `snapshot`, `build` and `render`, and requires them identical. A negative
  control proves the fingerprint detects a plain `git status`. An import scanner forbids network
  and engine modules (only `urllib.parse` is allowed from `urllib`), and a call scanner forbids
  shells and any write call outside `snapshot.py`.

## "Unknown" cost

No run store records token usage today, and the engine's per-event cost defaults to `0.0`.
The command center therefore reports cost as `unknown` unless the run directory holds a
`usage.json`. The page then says "Coste: desconocido (no se registró el consumo)" and never shows
a number or zero for it. When `usage.json` exists, the page shows the recorded token count.
Money is never computed.

## Limits

Deliberately left out (specification section 10): control actions, live refresh,
authentication, multi-user access, charts, cost in currency, Agent Foundry projects, and any
dependency on the canonical event model of roadmap Phase 1. The page is a snapshot: it is only
as fresh as the last `build`. The inbox sentences (dates, "hace N días") are written when the
snapshot is taken; the countdown next to a deadline is computed when the page is rendered.
