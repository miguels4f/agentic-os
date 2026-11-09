#!/usr/bin/env python3
"""Search what already exists before building a workflow. Stdlib only, Python 3.9+. Read-only, no network.

  scout.py local <kw...>     your skills + installed plugin skills + account plugin skills
  scout.py directory <kw...> Claude plugin directory cache (listings; no popularity)
  scout.py installs [kw...]  install counts of the official marketplace (warns when stale)
"""
import glob, json, os, re, sys, datetime

HOME = os.path.expanduser("~")
DIR_CACHE = HOME + "/.claude/plugins/plugin-directory-cache-v2.json"
CNT_CACHE = HOME + "/.claude/plugins/install-counts-cache.json"


def age_days(iso):
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - t).days
    except Exception:
        return None


def score(text, kws):
    t = text.lower()
    return sum(1 for k in kws if k in t)


def frontmatter(path):
    try:
        t = open(path, encoding="utf8", errors="ignore").read(4000)
    except Exception:
        return None, ""
    m = re.search(r"^---\n(.*?)\n---", t, re.S)
    fm = m.group(1) if m else ""
    n = re.search(r"^name:\s*(.+)$", fm, re.M)
    d = re.search(r"^description:\s*(.+?)(?=\n\w[\w-]*:|\Z)", fm, re.M | re.S)
    return (n.group(1).strip() if n else os.path.basename(os.path.dirname(path))), (d.group(1).strip().replace("\n", " ") if d else "")


def cmd_local(kws):
    pats = [
        # your own skills: user-level and project-level (shared .agents/ and Claude Code .claude/ layouts)
        HOME + "/.agents/skills/*/SKILL.md",
        HOME + "/.claude/skills/*/SKILL.md",
        os.getcwd() + "/.agents/skills/*/SKILL.md",
        os.getcwd() + "/.claude/skills/*/SKILL.md",
        # skills shipped inside installed Claude Code plugins
        HOME + "/.claude/plugins/cache/*/*/*/skills/*/SKILL.md",
        # plugins enabled on the account in the Claude desktop app (macOS; ignored when absent)
        HOME + "/Library/Application Support/Claude/local-agent-mode-sessions/*/*/rpm/plugin_*/skills/*/SKILL.md",
    ]
    seen, rows = set(), []
    for p in pats:
        for f in glob.glob(p):
            name, desc = frontmatter(f)
            if not name or (name, desc[:40]) in seen:
                continue
            seen.add((name, desc[:40]))
            s = score(name + " " + desc, kws)
            if s:
                where = "ours" if ("/.agents/skills/" in f or "/.claude/skills/" in f) else ("plugin" if "/plugins/cache/" in f else "account-plugin")
                rows.append((s, name, where, desc[:150]))
    for s, n, w, d in sorted(rows, reverse=True)[:25]:
        print("%d | %s [%s] | %s" % (s, n, w, d))
    print("(%d matches; 'ours' first when tied is not guaranteed — read each SKILL.md before deciding)" % len(rows))


def cmd_directory(kws):
    d = json.load(open(DIR_CACHE))
    print("directory fetched %s (%s days old); %d listings; NO popularity data here" % (d.get("fetchedAt"), age_days(d.get("fetchedAt", "")), len(d["listings"])))
    rows = []
    for x in d["listings"]:
        s = score((x.get("name") or "") + " " + (x.get("summary") or ""), kws)
        if s:
            pub = x.get("publisher")
            pub = pub.get("name") if isinstance(pub, dict) else pub
            repo = (((x.get("source") or {}).get("repository")) or {}).get("url", "")
            rev = (((x.get("checks") or {}).get("review")) or {}).get("state", "?")
            rows.append((s, x.get("name"), pub or "", rev, repo, (x.get("summary") or "")[:130]))
    for r in sorted(rows, key=lambda r: -r[0])[:25]:
        print("%d | %s | publisher=%s | review=%s | %s | %s" % r)
    print("(%d matches)" % len(rows))


def cmd_installs(kws):
    d = json.load(open(CNT_CACHE))
    a = age_days(d.get("fetchedAt", ""))
    print("install counts fetched %s (%s days old)%s" % (d.get("fetchedAt"), a, "  ** STALE (>30 days): confirm on skills.sh / the marketplace page **" if a and a > 30 else ""))
    rows = [c for c in d["counts"] if not kws or score(c["plugin"], kws)]
    for c in sorted(rows, key=lambda c: -(c.get("unique_installs") or 0))[:25]:
        print("%8d  %s" % (c.get("unique_installs") or 0, c["plugin"]))


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("local", "directory", "installs"):
        print(__doc__); sys.exit(1)
    kws = [k.lower() for k in sys.argv[2:]]
    {"local": cmd_local, "directory": cmd_directory, "installs": cmd_installs}[sys.argv[1]](kws)
