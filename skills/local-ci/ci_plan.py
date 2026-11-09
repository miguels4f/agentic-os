# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""Print what GitHub Actions would run for a commit, so it can be replayed locally.

Usage:
  uv run --script ci_plan.py <repo-or-worktree> [--ref HEAD] [--base origin/main] [--event pull_request]

Reads the workflow files as committed at --ref (not the working tree), decides which
workflows the event would trigger (branch and path filters against the diff base...ref),
and prints, per job: condition, matrix, services, env, and every step (setup actions and
shell commands with their working directory). It runs nothing.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

import yaml

DEPLOY_HINT = re.compile(r"deploy|release|publish|pages|terraform|aws|docker push", re.I)


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def glob_to_re(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def matches_any(path: str, patterns: list[str]) -> bool:
    keep = False
    for p in patterns:
        neg = p.startswith("!")
        if glob_to_re(p[1:] if neg else p).match(path):
            keep = not neg
    return keep


def triggers(doc: dict) -> dict:
    on = doc.get("on", doc.get(True))  # YAML 1.1 parses a bare `on:` key as True
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {e: {} for e in on}
    return {k: (v or {}) for k, v in (on or {}).items()}


def runs_for(event_cfg: dict, branch: str, changed: list[str]) -> tuple[bool, str]:
    branches = event_cfg.get("branches")
    if branches and not matches_any(branch, [str(b) for b in branches]):
        return False, f"branch filter {branches} excludes {branch}"
    if event_cfg.get("branches-ignore") and matches_any(branch, event_cfg["branches-ignore"]):
        return False, f"branches-ignore excludes {branch}"
    paths = event_cfg.get("paths")
    if paths and not any(matches_any(f, paths) for f in changed):
        return False, f"no changed file matches paths {paths}"
    ignore = event_cfg.get("paths-ignore")
    if ignore and changed and all(matches_any(f, ignore) for f in changed):
        return False, f"every changed file matches paths-ignore {ignore}"
    return True, "triggered"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--event", default="pull_request")
    a = ap.parse_args()

    sha = git(a.repo, "rev-parse", "--short=10", a.ref).strip()
    base_branch = a.base.split("/", 1)[-1]
    changed = [f for f in git(a.repo, "diff", "--name-only", f"{a.base}...{a.ref}").splitlines() if f]
    files = [
        f
        for f in git(a.repo, "ls-tree", "-r", "--name-only", a.ref, ".github/workflows/").splitlines()
        if f.endswith((".yml", ".yaml"))
    ]
    print(f"# CI plan for {sha} (event {a.event}, base {a.base}, {len(changed)} changed files)\n")
    if not files:
        print("NO WORKFLOWS: this repository has no GitHub Actions. Use the repo's documented checks "
              "(AGENTS.md / CLAUDE.md / Makefile / package.json scripts / pyproject).")
        return 0

    # For pull_request the branch filter applies to the target branch; for push, to the source branch.
    filter_branch = base_branch if a.event.startswith("pull_request") else git(
        a.repo, "rev-parse", "--abbrev-ref", a.ref).strip()
    for path in files:
        doc = yaml.safe_load(git(a.repo, "show", f"{a.ref}:{path}")) or {}
        trig = triggers(doc)
        name = doc.get("name", path)
        if a.event not in trig:
            print(f"## SKIP {path} ({name}): not triggered by {a.event} (triggers: {', '.join(map(str, trig))})\n")
            continue
        ok, why = runs_for(trig[a.event], filter_branch, changed)
        if not ok:
            print(f"## SKIP {path} ({name}): {why}\n")
            continue
        print(f"## RUN {path} — {name}")
        if doc.get("env"):
            print(f"workflow env: {doc['env']}")
        for job_id, job in (doc.get("jobs") or {}).items():
            text = yaml.safe_dump(job)
            flags = []
            if "secrets." in text:
                flags.append("USES SECRETS")
            if DEPLOY_HINT.search(f"{job_id} {job.get('name', '')}"):
                flags.append("LOOKS LIKE DEPLOY")
            if job.get("uses"):
                flags.append(f"REUSABLE WORKFLOW {job['uses']}")
            print(f"\n### job `{job_id}` — {job.get('name', job_id)}" + (f"  [{'; '.join(flags)}]" if flags else ""))
            for key in ("if", "needs", "runs-on", "env", "services"):
                if job.get(key):
                    print(f"- {key}: {job[key]}")
            if job.get("strategy", {}).get("matrix"):
                print(f"- matrix: {job['strategy']['matrix']}")
            defaults_wd = (job.get("defaults") or {}).get("run", {}).get("working-directory")
            for n, step in enumerate(job.get("steps") or [], 1):
                label = step.get("name", "")
                cond = f" (if: {step['if']})" if step.get("if") else ""
                if step.get("uses"):
                    print(f"  {n}. uses {step['uses']} {step.get('with', '')} {label}{cond}")
                else:
                    wd = step.get("working-directory", defaults_wd) or "."
                    env = f" env={step['env']}" if step.get("env") else ""
                    print(f"  {n}. run in `{wd}`{env} — {label}{cond}")
                    for line in str(step.get("run", "")).rstrip().splitlines():
                        print(f"       {line}")
        print()
    print("Changed files:\n" + "\n".join(f"  {f}" for f in changed[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
