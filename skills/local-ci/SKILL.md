---
name: local-ci
description: Use in any session (scheduled or not) that pushes code, opens or updates a pull request, or merges, while GitHub Actions cannot be relied on — minutes or spending limit exhausted ("The job was not started because recent account payments have failed or your spending limit needs to be increased"), GitHub outage, or a repository with no CI. Replays what GitHub Actions would run, on the exact commit, through an independent subagent that never sees the task or the author's claims, and produces a PASS/FAIL evidence file. Do NOT use for docs-only sessions that change no repository files, or read-only reviews.
---

# Local CI — replace GitHub Actions with an independent local check

When hosted CI is unavailable (minutes or spending limit exhausted, an outage, or no workflows at all), every
session that changes a repository runs this check before it declares a pull request ready or merges. The check is
done by a separate subagent that knows nothing about the task, so it cannot be talked into a green result.

Companion script: `bin/local-ci` in this kit (install it as `~/.local/bin/local-ci`). It runs a repository's
checks locally (from the repository's own `.local-ci.sh`, or auto-detected for Python and Node projects), works as
a pre-push hook, and can be the `Runner` for a repository in the table below. Run `local-ci --help` for details.

## Rules

1. **Gate, not advice.** The verdict comes from a fresh subagent (below). The author session may not override,
   reinterpret or skip a FAIL. It may fix the cause and run the whole check again (at most two fix rounds); after
   that it stops, leaves the PR unmerged and reports the failure.
2. **Exact commit.** Commit first. The check runs on a clean detached worktree of that SHA, never on the author's
   working tree. If anything is committed or merged into the branch afterwards (including a merge of `origin/main`),
   the evidence is void: run the check again.
3. **Real GitHub results still count.** A GitHub check that actually ran and failed blocks. Only the billing message
   quoted in the description (or a check that never started because of a GitHub outage) is replaced by this check.
4. **Never weaken anything to get green**: do not edit tests, workflows, guardrail scripts, thresholds or skip lists
   to make the check pass unless that edit is the task itself and the user approved it.
5. **No paid APIs, no secrets.** Jobs that need secrets, deploy, publish or call paid providers are listed as
   `NOT REPLAYED (reason)`, never faked. If such a job guards something the PR changes, say so in the report.

## How to run it (author session)

0. **Optional specialist review (before the runner).** List the changed files:
   `git -C <REPO> diff --name-only <BASE>...<SHA>`. For each matching area, run the matching read-only review:
   - login, sessions, roles, tenant isolation, API keys, client-exposed environment variables, tool-enabled LLM
     calls, database migrations → a read-only security reviewer subagent (it may read and run read-only commands,
     never edit);
   - booking, checkout, payment, refund, balance or ledger code → skill `money-flow-integrity`;
   - user-facing UI → skills `accessibility-audit` and `ui-finish-gate`.

   Skip this step when no file matches. Run it separately from the CI runner: never pass the review, its findings
   or the task to the runner subagent. A **BLOCKER** finding stops the merge exactly like a FAIL (same fix-round
   rules as Rule 1: fix, commit, run step 0 and the whole check again). Once the runner has written the evidence
   file, append a `## Specialist review` section to it (after the runner's content, first line untouched) with
   each review run and its findings; non-blocking findings are listed there only.
1. Commit and push your branch. Note: main checkout path `<REPO>`, commit `<SHA>` (`git rev-parse HEAD`),
   base `<BASE>` (normally `origin/main`, or the branch a stacked PR targets), PR number if any, and `<SKILL_DIR>`
   (the folder holding this SKILL.md and `ci_plan.py`).
2. Look up `<RUNNER>` in the table below (or "derive from workflows"). Resolve the temp folder with
   `echo "$TMPDIR"` and write `<WORKTREE>` as the literal absolute path `<that folder, no trailing slash>/local-ci-<SHA>`
   (for example `/var/folders/ab/cd/T/local-ci-1234abc`). Fill that written-out path into the template. Write every
   path out in full: no variables in a `cd`, because safety hooks and reviewers cannot check a path they cannot see.
3. Launch ONE fresh subagent (Agent tool, `subagent_type: general-purpose`, foreground) with exactly the prompt
   template below, filled in. Give it nothing else: no task description, no report, no PR text, no opinion about
   whether the change is correct. That isolation is the point: it cannot be biased toward the author's version.
   - Codex session or no subagent tool: run `claude -p "<filled template>" --permission-mode bypassPermissions`
     from a shell if available; otherwise run the steps yourself and mark the evidence `NOT INDEPENDENT`.
4. Read the evidence file it writes. Merge or mark the PR ready only if its first line is `LOCAL CI: PASS` and its
   `Specialist review` section (if any) has no open BLOCKER.
5. Post the evidence file's content as a PR comment titled `Local CI (GitHub Actions unavailable)` and cite its
   path, SHA and verdict in your session report.

## Subagent prompt template

```text
You are an independent CI runner. You replace GitHub Actions, which is unavailable. You know nothing about why
this change was made, and you must not try to find out: do not read pull request descriptions, commit messages
beyond the subject line, session reports, task prompts, PROGRESS files or chat logs. Your only job is to run the
repository's checks on one exact commit and report the truth.

Repository (main checkout): <REPO>
Commit to check: <SHA>
Base to diff against: <BASE>
Runner: <RUNNER>
Worktree path: <WORKTREE>
CI plan script: <SKILL_DIR>/ci_plan.py
Evidence file to write: ~/.agents/local-ci-runs/<REPO_NAME>/<SHA>.md  (create the folder)

Steps:
1. `git -C <REPO> fetch -q origin`. Create your own clean worktree:
   `git -C <REPO> worktree add --detach <WORKTREE> <SHA>`. Work only there. Never modify tracked
   files, never commit, push, comment, merge or open PRs. Never print .env files or secrets.
   Shell commands: start every command that runs a repository script with `cd <WORKTREE> && `, the path
   written out in full exactly as given above. No variable may appear in a `cd` (not `cd "$TMPDIR/…"`,
   `cd "$WT"` or `cd "$REPO"`). If a safety hook refuses a command, stop and report `blocked: <message>`;
   never look for another route.
2. Print the CI plan: `uv run -q --script <SKILL_DIR>/ci_plan.py <WORKTREE> --ref <SHA> --base <BASE>`.
3. If Runner is a script: run it as documented (pick free ports with `lsof -iTCP:<port> -sTCP:LISTEN` before
   using one). Then compare it with the plan from step 2: every job the plan says GitHub would run must be covered
   by the script, by the extra commands listed in Runner, or listed as NOT REPLAYED with a reason. Run any job that
   is missing yourself.
   If Runner says "derive from workflows": replay every RUN job of the plan in order: translate setup actions to
   the local equivalent (setup-node/setup-python/setup-uv/pnpm: use the stated version if installed, otherwise
   the closest installed one and note the difference; services such as postgres: start a throwaway local
   instance on a free port), run each `run:` block in its working directory with the job's env, and expand
   matrix entries. `${{ github.workspace }}` is your worktree.
   If the plan says NO WORKFLOWS: the required checks are the Runner commands; if Runner lists none, the checks
   the repo documents (AGENTS.md/CLAUDE.md Commands section, Makefile test targets, package.json
   test/lint/type-check/build scripts, or the repo's `.local-ci.sh`). Tools that are only configured (e.g.
   ruff/mypy in pyproject) but not documented as checks: run them too, report them as INFORMATIONAL; they do not
   decide the verdict.
4. Do not fix anything. A failure is a result. Re-run a failing step once only if the log points to something
   environmental (port in use, network timeout); report both attempts.
   For every failing required step, run the same step on the base commit in a second detached worktree at
   `<WORKTREE>-base` (same rule: path written out in full in every `cd`). If it
   fails there with the same errors (same tests / same messages, no new ones), mark it PRE-EXISTING; if the
   commit adds failures the base does not have, it is a FAIL.
5. Write the evidence file. First line exactly `LOCAL CI: PASS` or `LOCAL CI: FAIL` (FAIL if any required step
   has a failure that is not PRE-EXISTING, or any required job could not run for a reason other than
   secrets/deploy). PRE-EXISTING failures go in their own section so the user sees them. Then: SHA, base, date,
   tool versions used, a table job · step · PASS/FAIL/NOT REPLAYED · duration · key numbers (tests passed/failed/
   skipped), and for each FAIL the last 30 lines of its log. Note any difference from GitHub (OS, versions).
6. Remove your worktrees (`git -C <REPO> worktree remove --force <WORKTREE>`, and `<WORKTREE>-base` if you made it) and stop any database
   or server you started.
7. Your final message: the evidence file path and its first line, nothing else.
```

## Runners

Add one row per repository that needs something other than "derive from workflows". Keep it short: what to run
and what its output must end with.

| Repository | Runner |
|---|---|
| `<owner>/<repo>` (example: has a `.local-ci.sh`, or a plain Python or Node project) | Script: `local-ci --full <WORKTREE>` (the `bin/local-ci` companion); must end `RESULT: ALL GREEN`. Then compare with the plan as step 3 says. |
| `<owner>/<repo>` (example: needs a database) | Script: `bash <SKILL_DIR>/runners/<repo>.sh <WORKTREE> <free PostgreSQL port>`; must end `RESULT: ALL GREEN`. |
| `<owner>/<repo>` (example: no workflows) | No workflows. `cd <WORKTREE> && uv sync --locked && uv run pytest -q`. |
| anything else | Derive from workflows. |

Keep repository-specific runner scripts in `runners/` next to this file, never inside a scheduled-task folder
(those get archived). A script outside the repository is a stopgap while hosted CI is down; the long-term fix is
to restore hosted CI or move the checks into the repository (for example as its `.local-ci.sh`).
