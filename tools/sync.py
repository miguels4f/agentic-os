#!/usr/bin/env python3
"""Keep this kit in step with a maintainer's private originals, without leaking them.

The kit's skills and Wilfred are curated copies of files that live in a maintainer's private
setup. This tool copies them again, scrubs private names with a PRIVATE replacement map that
never enters the repository, refuses to continue if anything private survives, and proposes
the result as a pull request. It never merges.

Commands (run from anywhere; --root defaults to this repository):
  build    copy sources into the tree (auto files are rewritten; hand-curated files whose
           source changed are listed as "needs port" and left untouched)
  record   accept the current tree as the new baseline (after hand-porting); updates
           tools/sync-state.json
  check    fail if any private term (denylist), key material or a leak found by gitleaks
           remains anywhere in the tree
  weekly   build + check + Wilfred tests in a fresh worktree on a new branch, then push it
           and open a pull request (with --dry-run: everything except push and PR); when
           anything changed it also redraws the kit map
  map      redraw docs/map.html from docs/map-source with the Floorplan plugin

File modes in tools/sync-state.json:
  auto      public file == scrubbed source; rewritten automatically on every build
  curated   public file was edited by hand after scrubbing; a source change is only reported
  excluded  deliberately not published; never re-added
  link      a symbolic link, copied as a link

Private inputs (never committed): ~/.config/agentic-os/private-map.json, override with
AGENTIC_OS_PRIVATE_MAP. See tools/private-map.example.json for its shape.
Standard library only; needs git, gitleaks, uv, node with the Floorplan plugin (for the map)
and (for weekly without --dry-run) gh.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = "tools/sync-config.json"
STATE = "tools/sync-state.json"
PRIVATE_MAP = Path(os.environ.get("AGENTIC_OS_PRIVATE_MAP",
                                  "~/.config/agentic-os/private-map.json")).expanduser()
KEY_BLOCK = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*\n[A-Za-z0-9+/=]{40,}")
FORBIDDEN_NAMES = ("*.env", ".env*", "id_rsa*", "id_ed25519*", "*private-key*.pem",
                   "*.p12", "*.pfx", "*.keystore")


# ----------------------------------------------------------------------------- inputs
def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def private_map() -> dict:
    if not PRIVATE_MAP.exists():
        sys.exit(f"sync: private map not found at {PRIVATE_MAP} (see tools/private-map.example.json)")
    data = load_json(PRIVATE_MAP)
    data["_replacements"] = [(re.compile(p), r) for p, r in data.get("replacements", [])]
    data["_denylist"] = [(re.compile(p, re.I if "i" in f else 0), p)
                         for p, f in data.get("denylist", [])]
    return data


@dataclass(frozen=True)
class Source:
    public: str          # path in this repository
    origin: str          # "<component>:<relative path>", recorded in the state file
    path: Path           # absolute path of the source file on disk
    is_link: bool


def _excluded(rel: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch("/" + rel, "*/" + g) for g in globs)


def _walk(base: Path, globs: list[str]):
    for dirpath, dirnames, filenames in os.walk(base):
        d = Path(dirpath)
        keep = []
        for name in dirnames:
            full = d / name
            rel = full.relative_to(base).as_posix()
            if _excluded(rel + "/x", globs) or _excluded(rel, globs):
                continue
            if full.is_symlink():
                yield rel, full, True
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            full = d / name
            rel = full.relative_to(base).as_posix()
            if not _excluded(rel, globs):
                yield rel, full, full.is_symlink()


def collect_sources(cfg: dict, pmap: dict, scratch: Path) -> list[Source]:
    src = pmap.get("sources", {})
    out: list[Source] = []
    skills_dir = Path(src["skills"]).expanduser()
    for skill in cfg["skills"]:
        base = (skills_dir / skill).resolve()
        if not base.is_dir():
            print(f"WARN  skill source missing: {skill}", file=sys.stderr)
            continue
        for rel, full, link in _walk(base, cfg["skill_exclude"]):
            out.append(Source(f"skills/{skill}/{rel}", f"skills:{skill}/{rel}", full, link))
    for public, path in src.get("extra_files", {}).items():
        p = Path(path).expanduser()
        if p.exists():
            out.append(Source(public, f"extra:{Path(public).name}", p, False))
    w = cfg["wilfred"]
    repo = Path(src["wilfred"]).expanduser()
    export = scratch / "wilfred-export"
    export.mkdir(parents=True, exist_ok=True)
    ref = src.get("wilfred_ref", "HEAD")
    if ref.startswith("origin/"):
        # Read the published branch, not a possibly stale local checkout.
        subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
    archive = subprocess.run(["git", "-C", str(repo), "archive", ref],
                             check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(export)], input=archive, check=True)
    for rel, full, link in _walk(export, w["exclude"]):
        out.append(Source(f"{w['target']}/{rel}", f"wilfred:{rel}", full, link))
    return out


# ----------------------------------------------------------------------------- scrub
def scrub(data: bytes, pmap: dict) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    for pattern, repl in pmap["_replacements"]:
        text = pattern.sub(repl, text)
    return text.encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_bytes(s: Source) -> bytes:
    return os.readlink(s.path).encode() if s.is_link else s.path.read_bytes()


# ----------------------------------------------------------------------------- build
def build(root: Path, cfg: dict, pmap: dict, component: str | None = None) -> dict:
    state_path = root / STATE
    state = load_json(state_path) if state_path.exists() else {}
    report = {"written": [], "added": [], "removed": [], "needs_port": [], "unchanged": 0}
    with tempfile.TemporaryDirectory() as tmp:
        sources = [s for s in collect_sources(cfg, pmap, Path(tmp))
                   if not component or s.origin.startswith(component + ":")]
        seen = set()
        for s in sources:
            seen.add(s.public)
            raw = source_bytes(s)
            entry = state.get(s.public)
            target = root / s.public
            if entry and entry["mode"] == "excluded":
                continue
            if entry and entry["mode"] == "curated":
                if entry["source_sha"] != sha(raw):
                    report["needs_port"].append(s.public)
                else:
                    report["unchanged"] += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if s.is_link:
                link_target = scrub(raw, pmap).decode()
                if target.is_symlink() and os.readlink(target) == link_target:
                    report["unchanged"] += 1
                    continue
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(link_target)
                state[s.public] = {"origin": s.origin, "source_sha": sha(raw), "mode": "link"}
                report["added" if not entry else "written"].append(s.public)
                continue
            new = scrub(raw, pmap)
            if target.exists() and not target.is_symlink() and target.read_bytes() == new:
                report["unchanged"] += 1
                state[s.public] = {"origin": s.origin, "source_sha": sha(raw), "mode": "auto"}
                continue
            target.write_bytes(new)
            if os.access(s.path, os.X_OK) and not s.is_link:
                target.chmod(0o755)
            state[s.public] = {"origin": s.origin, "source_sha": sha(raw), "mode": "auto"}
            report["added" if not entry else "written"].append(s.public)
        for public in sorted(set(state) - seen):
            if state[public]["mode"] == "excluded":
                continue
            if component and not state[public]["origin"].startswith(component + ":"):
                continue
            target = root / public
            if target.exists() or target.is_symlink():
                target.unlink()
            del state[public]
            report["removed"].append(public)
    write_state(state_path, state)
    return report


def record(root: Path, cfg: dict, pmap: dict, only: list[str] | None) -> dict:
    """Accept the tree as baseline: classify each source as auto/curated/excluded/link."""
    state_path = root / STATE
    state = load_json(state_path) if state_path.exists() else {}
    counts = {"auto": 0, "curated": 0, "excluded": 0, "link": 0}
    with tempfile.TemporaryDirectory() as tmp:
        for s in collect_sources(cfg, pmap, Path(tmp)):
            if only and s.public not in only:
                continue
            raw = source_bytes(s)
            target = root / s.public
            if s.is_link:
                mode = "link" if target.is_symlink() else "excluded"
            elif not target.exists():
                mode = "excluded"
            else:
                mode = "auto" if target.read_bytes() == scrub(raw, pmap) else "curated"
            state[s.public] = {"origin": s.origin, "source_sha": sha(raw), "mode": mode}
    for entry in state.values():
        counts[entry["mode"]] += 1
    write_state(state_path, state)
    return counts


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(state.items())), indent=1) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------- map
MAP_SOURCE = Path("docs") / "map-source"
MAP_FILE = Path("docs") / "map.html"


def _version_key(path: Path) -> list:
    return [int(n) if n.isdigit() else 0 for n in path.parts[-4].split(".")]


def find_floorplan(pmap: dict) -> Path | None:
    """Locate the Floorplan plugin's scripts folder (newest installed version)."""
    override = pmap.get("sources", {}).get("floorplan_scripts")
    if override:
        cands = [Path(override).expanduser()]
    else:
        cands = sorted(Path.home().glob(".claude/plugins/cache/floorplan/floorplan/*/skills/floorplan/scripts"),
                       key=_version_key)
    for cand in reversed(cands):
        if (cand / "build.mjs").exists():
            return cand
    return None


def _fill_skill_descriptions(desc_dir: Path, root: Path) -> int:
    """Give new SKILL.md entries the skill's own one-line description."""
    filled = 0
    for jf in sorted(desc_dir.glob("*.json")):
        data = json.loads(jf.read_text(encoding="utf-8"))
        changed = False
        for rel, entry in data.get("files", {}).items():
            if entry.get("description") or not rel.endswith("SKILL.md"):
                continue
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^description:\s*(.+)$", text.split("\n---", 1)[0], re.M)
            if not m:
                continue
            desc = m.group(1).strip().strip("'\"")
            desc = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0][:220]
            entry["description"] = desc
            entry.pop("review", None)
            changed = True
            filled += 1
        if changed:
            jf.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return filled


def redraw_map(root: Path, pmap: dict) -> str:
    """Rebuild docs/map.html from docs/map-source with the Floorplan plugin.

    The map's structure and descriptions live in docs/map-source (reviewed like any other file);
    this only redraws it for the files present now. New files get a skeleton entry; new skills
    take their description from their own SKILL.md. Returns a one-line note for the report.
    """
    scripts = find_floorplan(pmap)
    src = root / MAP_SOURCE
    node = shutil.which("node")
    if not src.joinpath("architecture.json").exists():
        return "map: not redrawn (docs/map-source/architecture.json missing)"
    if not scripts or not node:
        return "map: NOT redrawn (Floorplan plugin or node not found on this computer)"
    fp = root / "floorplan"
    fp.mkdir(exist_ok=True)
    shutil.copy2(src / "architecture.json", fp / "architecture.json")
    desc = fp / "descriptions"
    shutil.rmtree(desc, ignore_errors=True)
    shutil.copytree(src / "descriptions", desc)
    (fp / "recent-work.json").write_text('{ "tasks": [] }\n', encoding="utf-8")
    subprocess.run([node, str(scripts / "build.mjs"), str(root), "--no-claude-md"],
                   capture_output=True, text=True)
    subprocess.run([node, str(scripts / "describe.mjs"), str(root)], capture_output=True, text=True)
    auto = _fill_skill_descriptions(desc, root)
    out = subprocess.run([node, str(scripts / "build.mjs"), str(root), "--artifact", "--no-claude-md"],
                         capture_output=True, text=True)
    if out.returncode != 0 or not (fp / "artifact.html").exists():
        return "map: NOT redrawn (Floorplan build failed: " + (out.stdout + out.stderr).strip()[-300:] + ")"
    shutil.copy2(fp / "artifact.html", root / MAP_FILE)
    shutil.rmtree(src / "descriptions")
    shutil.copytree(desc, src / "descriptions")
    m = re.search(r"DESCRIPTIONS\s+files (\d+)/(\d+)", out.stdout)
    have, total = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    note = f"map: redrawn; {have}/{total} files described"
    if auto:
        note += f" ({auto} new skill description(s) taken from their SKILL.md)"
    if total and have < total:
        note += (f"; {total - have} new file(s) still need a description in "
                 "docs/map-source/descriptions/")
    return note


# ----------------------------------------------------------------------------- check
def check(root: Path, pmap: dict) -> list[str]:
    """Scan what git would publish: tracked files plus new files that are not ignored.

    Local-only files that git ignores (a floorplan map, CLAUDE.local.md, caches) are skipped,
    because they never reach the repository. Outside a git checkout the whole folder is scanned.
    """
    if not (root / ".git").exists():
        return _check_tree(root, pmap)
    listing = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard"], check=True, capture_output=True).stdout
    with tempfile.TemporaryDirectory() as tmp:
        dest_root = Path(tmp)
        for raw in listing.split(b"\0"):
            if not raw:
                continue
            rel = raw.decode("utf-8", "surrogateescape")
            src = root / rel
            if not os.path.lexists(src):
                continue
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest, follow_symlinks=False)
        return _check_tree(dest_root, pmap)


def _check_tree(root: Path, pmap: dict) -> list[str]:
    findings: list[str] = []
    allow = pmap.get("allow", {})
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", ".venv", "node_modules",
                                                        "__pycache__", ".pytest_cache",
                                                        ".mypy_cache", ".ruff_cache"}]
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if rel == ".git":
                continue  # a worktree's .git pointer file: local path, never published
            if any(fnmatch.fnmatch(name, g) for g in FORBIDDEN_NAMES) and \
                    not name.endswith(".example") and name != ".env.example":
                findings.append(f"forbidden file name: {rel}")
            if full.is_symlink():
                data = os.readlink(full).encode()
            else:
                data = full.read_bytes()
            if KEY_BLOCK.search(data):
                findings.append(f"key material marker: {rel}")
            if "outlier" in rel.split("/"):
                findings.append(f"confidential skill folder: {rel}")
            text = data.decode("utf-8", errors="replace")
            for pattern, raw in pmap["_denylist"]:
                if any(re.search(a, rel) for a in allow.get(raw, [])):
                    continue
                m = pattern.search(text)
                if m:
                    line = text.count("\n", 0, m.start()) + 1
                    findings.append(f"private term {raw!r} at {rel}:{line}")
    gl = shutil.which("gitleaks")
    if not gl:
        findings.append("gitleaks not installed: leak scan could not run")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gitleaks.json"
            proc = subprocess.run([gl, "dir", str(root), "--no-banner", "--redact",
                                   "--report-format", "json", "--report-path", str(out)],
                                  capture_output=True, text=True)
            if out.exists():
                for leak in json.loads(out.read_text() or "[]"):
                    findings.append(f"gitleaks {leak['RuleID']}: "
                                    f"{Path(leak['File']).relative_to(root)}:{leak['StartLine']}")
            elif proc.returncode not in (0,):
                findings.append(f"gitleaks failed: {proc.stderr.strip()[:200]}")
    return findings


# ----------------------------------------------------------------------------- weekly
def run(cmd: list[str], cwd: Path, check_rc: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check_rc, capture_output=True, text=True)


def weekly(repo: Path, cfg: dict, pmap: dict, dry_run: bool) -> int:
    today = dt.date.today().isoformat()
    branch = f"sync/{today}"
    run(["git", "fetch", "-q", "origin"], repo)
    base = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo,
               check_rc=False).stdout.strip() or "origin/main"
    work = Path(tempfile.mkdtemp(prefix="agentic-os-sync-"))
    wt = work / "tree"
    run(["git", "worktree", "add", "-q", "-b", branch, str(wt), base], repo)
    try:
        report = build(wt, cfg, pmap)
        print(json.dumps({k: (v if isinstance(v, int) else len(v)) for k, v in report.items()}))
        status = run(["git", "status", "--porcelain"], wt).stdout.strip()
        if not status and not report["needs_port"]:
            print("RESULT: NO CHANGES")
            return 0
        map_note = redraw_map(wt, pmap)
        print(map_note)
        findings = check(wt, pmap)
        if findings:
            print("RESULT: BLOCKED — private content or leaks found; no pull request opened:")
            for f in findings:
                print("  - " + f)
            return 2
        wdir = wt / cfg["wilfred"]["target"]
        tests = subprocess.run("uv sync -q --locked --group dev && uv run -q pytest -q -p no:cacheprovider",
                               shell=True, cwd=wdir, capture_output=True, text=True)
        if tests.returncode != 0:
            print("RESULT: BLOCKED — Wilfred tests fail after scrubbing; no pull request opened:")
            print("\n".join((tests.stdout + tests.stderr).strip().splitlines()[-25:]))
            return 3
        shutil.rmtree(wdir / ".venv", ignore_errors=True)
        body = pr_body(report, tests.stdout.strip().splitlines()[-1:] or ["(no output)"], map_note)
        if not status:
            print("RESULT: NEEDS PORT ONLY — sources of hand-curated files changed:")
            print(body)
            return 0
        run(["git", "add", "-A"], wt)
        run(["git", "commit", "-q", "-m", f"chore(sync): weekly refresh {today}"], wt)
        stat = run(["git", "show", "--stat", "--format=", "HEAD"], wt).stdout
        if dry_run:
            print(stat)
            print(body)
            print(f"RESULT: DRY RUN OK — would push {branch} and open a pull request (nothing pushed)")
            return 0
        run(["git", "push", "-q", "-u", "origin", branch], wt)
        pr = run(["gh", "pr", "create", "--base", base.split("/", 1)[1], "--head", branch,
                  "--title", f"Weekly sync {today}", "--body", body], wt).stdout.strip()
        print(f"RESULT: PULL REQUEST OPENED {pr} (not merged; a maintainer must review it)")
        return 0
    finally:
        run(["git", "worktree", "remove", "--force", str(wt)], repo, check_rc=False)
        if dry_run:
            run(["git", "branch", "-D", branch], repo, check_rc=False)
        shutil.rmtree(work, ignore_errors=True)


def pr_body(report: dict, tests: list[str], map_note: str = "") -> str:
    lines = ["Automated weekly refresh of the kit's skills and Wilfred from the maintainer's originals.",
             "", "Do not merge without reading the diff.", "",
             f"- Updated files: {len(report['written'])}",
             f"- New files: {len(report['added'])}",
             f"- Removed files: {len(report['removed'])}",
             f"- Private-term and leak scan: clean",
             f"- Wilfred tests: {tests[0]}",
             f"- Kit map (docs/map.html): {map_note.removeprefix('map: ') or 'not redrawn'}"]
    if report["needs_port"]:
        lines += ["", "Hand-curated files whose original changed (port the change by hand, then run "
                  "`python3 tools/sync.py record --only <path>`):"]
        lines += [f"- `{p}`" for p in report["needs_port"]]
    for key, title in (("added", "New"), ("removed", "Removed")):
        if report[key]:
            lines += ["", f"{title}:"] + [f"- `{p}`" for p in report[key][:50]]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["build", "record", "check", "weekly", "map"])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--only", nargs="*", help="record: limit to these public paths")
    ap.add_argument("--dry-run", action="store_true", help="weekly: do not push or open a PR")
    ap.add_argument("--component", choices=["skills", "wilfred", "extra"],
                    help="build: only this component")
    args = ap.parse_args()
    root = args.root.resolve()
    cfg = load_json(root / CONFIG) if args.command != "check" else {}
    pmap = private_map()
    if args.command == "build":
        report = build(root, cfg, pmap, args.component)
        print(json.dumps({k: (v if isinstance(v, int) else len(v)) for k, v in report.items()}))
        if report["needs_port"]:
            print("needs port: " + ", ".join(report["needs_port"]))
    elif args.command == "record":
        print(json.dumps(record(root, cfg, pmap, args.only)))
    elif args.command == "check":
        findings = check(root, pmap)
        for f in findings:
            print("FINDING  " + f)
        print("RESULT: CLEAN" if not findings else f"RESULT: {len(findings)} FINDING(S)")
        return 1 if findings else 0
    elif args.command == "map":
        note = redraw_map(root, pmap)
        print(note)
        return 1 if "NOT redrawn" in note else 0
    else:
        return weekly(root, cfg, pmap, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
