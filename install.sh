#!/usr/bin/env bash
# Install this kit's skills and the local-ci script for your user.
#
#   ./install.sh             install (never overwrites anything that already exists)
#   ./install.sh --dry-run   print what would happen, change nothing
#   ./install.sh --update    replace skills previously installed by this script with this version
#
# What it does:
#   1. copies each folder of skills/ into ~/.agents/skills/<name>   (Codex reads this folder)
#   2. links ~/.claude/skills/<name> -> ~/.agents/skills/<name>      (Claude Code reads this one)
#   3. copies bin/local-ci to ~/.local/bin/local-ci
# Third-party tools are separate: third-party/install.sh. Wilfred is separate: see wilfred/README.md.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
AGENTS_SKILLS="${AGENTS_SKILLS_DIR:-$HOME/.agents/skills}"
CLAUDE_SKILLS="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
BIN="${LOCAL_BIN_DIR:-$HOME/.local/bin}"
MARK=".installed-by-agentic-os"
DRY=0
UPDATE=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --update) UPDATE=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

do_() { echo "  \$ $*"; [ "$DRY" -eq 1 ] || "$@"; }

echo "Skills -> $AGENTS_SKILLS (linked into $CLAUDE_SKILLS)"
do_ mkdir -p "$AGENTS_SKILLS" "$CLAUDE_SKILLS"
for src in "$HERE"/skills/*/; do
  name="$(basename "$src")"
  dest="$AGENTS_SKILLS/$name"
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    if [ "$UPDATE" -eq 1 ] && [ -f "$dest/$MARK" ]; then
      do_ rsync -a --delete "$src" "$dest/"
      do_ touch "$dest/$MARK"
      echo "  updated  $name"
    else
      echo "  skipped  $name (already exists at $dest; use --update only for copies this script made)"
    fi
  else
    do_ cp -R "$src" "$dest"
    do_ touch "$dest/$MARK"
    echo "  added    $name"
  fi
  link="$CLAUDE_SKILLS/$name"
  if [ ! -e "$link" ] && [ ! -L "$link" ]; then
    do_ ln -s "$dest" "$link"
  fi
done

echo "local-ci -> $BIN/local-ci"
do_ mkdir -p "$BIN"
if [ -e "$BIN/local-ci" ] && [ "$UPDATE" -eq 0 ]; then
  echo "  skipped  local-ci (already exists; use --update to replace)"
else
  do_ cp "$HERE/bin/local-ci" "$BIN/local-ci"
  do_ chmod +x "$BIN/local-ci"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  note: add $BIN to your PATH to run local-ci from anywhere" ;; esac

echo
echo "Done. Next: copy template/project/ into a project and edit its AGENTS.md (see docs/guide.md)."
