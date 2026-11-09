#!/usr/bin/env bash
# Install the third-party tools this kit recommends, each from its OWN source.
# Nothing here is copied into this repository: every tool is fetched from its authors.
#
# Usage:
#   third-party/install.sh --list              show the tools and how each would be installed
#   third-party/install.sh                     install all of them (asks before each one)
#   third-party/install.sh -y                  install all without asking
#   third-party/install.sh superpowers caveman install only these (names from --list)
#   third-party/install.sh --dry-run [...]     print the commands without running them
#
# How each tool is installed:
#   * Claude Code plugin marketplaces:  claude plugin marketplace add <owner/repo>
#                                       claude plugin install <plugin>@<marketplace>
#   * Codex plugin marketplaces:        codex plugin marketplace add <owner/repo>
#                                       codex plugin add <plugin>@<marketplace>
#   * Plain skill folders:              a shallow git clone into ~/.local/share/agentic-os/third-party/
#                                       and a symlink into ~/.agents/skills/ (read by Codex) and
#                                       ~/.claude/skills/ (read by Claude Code)
# Only the agent apps you actually have installed (claude, codex) are configured.
#
# Read each project's README before installing: plugins and skills run with your permissions,
# and some (ECC, caveman) add hooks that run on every session.
set -uo pipefail

SHARE="${AGENTIC_OS_THIRD_PARTY_DIR:-$HOME/.local/share/agentic-os/third-party}"
YES=0
DRY=0
LIST=0
WANT=()
for a in "$@"; do
  case "$a" in
    -y|--yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    --list) LIST=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    -*) echo "unknown option: $a" >&2; exit 2 ;;
    *) WANT+=("$a") ;;
  esac
done

HAVE_CLAUDE=0; command -v claude >/dev/null 2>&1 && HAVE_CLAUDE=1
HAVE_CODEX=0;  command -v codex  >/dev/null 2>&1 && HAVE_CODEX=1

# name | what it is | Claude marketplace repo | Claude plugin@marketplace | Codex marketplace repo | Codex plugin@marketplace | skill repo | skill path(s) inside repo | extra
TOOLS=(
  "superpowers|Process skills: brainstorm, plan, TDD, debugging, code review (MIT)|obra/superpowers|superpowers@superpowers-dev|obra/superpowers|superpowers@superpowers-dev||||"
  "caveman|Terse-output mode that cuts tokens; adds a session hook (Apache-2.0)|JuliusBrussee/caveman|caveman@caveman||||skills/caveman|"
  "impeccable|Frontend design critique, polish and audit skills (Apache-2.0)|pbakaus/impeccable|impeccable@impeccable|||||see https://github.com/pbakaus/impeccable#installation for Codex"
  "frontend-design|Anthropic's skill for distinctive, production-grade UI (Apache-2.0)||frontend-design@claude-plugins-official|||https://github.com/anthropics/skills|skills/frontend-design|"
  "floorplan|Plain-language maps of a codebase in a local viewer (MIT)|haydenschmitty/floorplan|floorplan@floorplan|haydenschmitty/floorplan|floorplan@floorplan||||"
  "hyperresearch|Deep, source-grounded research pipeline + hpr CLI (MIT)|jordan-gibbs/hyperresearch|hyperresearch@hyperresearch|jordan-gibbs/hyperresearch|hyperresearch@hyperresearch|||uv tool install hyperresearch"
  "codex-plugin-cc|Use Codex from inside Claude Code: second opinions, rescue (Apache-2.0)|openai/codex-plugin-cc|codex@openai-codex||||||"
  "ecc|Everything Claude Code: large set of agents, skills, hooks and rules (MIT)|affaan-m/ECC|ecc@ecc|affaan-m/ECC|ecc@ecc||||"
  "shaping-skills|Shape Up style shaping + breadboarding skills (no licence: linked, never copied)||||||https://github.com/rjs/shaping-skills|shaping breadboarding breadboard-reflection|"
  "pdf-to-markdown|Convert whole PDFs to Markdown for agent context (no repo licence: linked)||||||https://github.com/aliceisjustplaying/claude-resources-monorepo|skills/pdf-to-markdown|then follow skills/pdf-to-markdown/README.md to create its .venv"
)

run() {
  echo "  \$ $*"
  [ "$DRY" -eq 1 ] && return 0
  "$@"
}

link_skill() {
  local src="$1" name; name="$(basename "$src")"
  for dir in "$HOME/.agents/skills" "$HOME/.claude/skills"; do
    run mkdir -p "$dir"
    if [ -e "$dir/$name" ] && [ ! -L "$dir/$name" ]; then
      echo "  skip: $dir/$name already exists and is not a link (not touching it)"
      continue
    fi
    if [ -L "$dir/$name" ] && [ "$(readlink "$dir/$name")" != "$src" ]; then
      echo "  skip: $dir/$name already links somewhere else (not touching it)"
      continue
    fi
    run ln -sfn "$src" "$dir/$name"
  done
}

install_one() {
  local name="$1" desc="$2" cmkt="$3" cplug="$4" xmkt="$5" xplug="$6" repo="$7" paths="$8" extra="$9"
  echo
  echo "== $name — $desc"
  if [ "$LIST" -eq 0 ] && [ "$YES" -eq 0 ] && [ "$DRY" -eq 0 ]; then
    read -r -p "Install $name? [y/N] " ans
    case "$ans" in y|Y|yes) ;; *) echo "  skipped"; return ;; esac
  fi
  if [ "$LIST" -eq 1 ]; then DRY=1; fi
  if [ -n "$cplug" ]; then
    if [ "$HAVE_CLAUDE" -eq 1 ] || [ "$LIST" -eq 1 ]; then
      [ -n "$cmkt" ] && run claude plugin marketplace add "$cmkt"
      run claude plugin install "$cplug"
    else
      echo "  (Claude Code not found: skipping its plugin)"
    fi
  fi
  if [ -n "$xplug" ]; then
    if [ "$HAVE_CODEX" -eq 1 ] || [ "$LIST" -eq 1 ]; then
      run codex plugin marketplace add "$xmkt"
      run codex plugin add "$xplug"
    else
      echo "  (Codex not found: skipping its plugin)"
    fi
  fi
  if [ -n "$repo" ]; then
    local dest="$SHARE/$name"
    if [ -d "$dest/.git" ]; then
      run git -C "$dest" pull -q --ff-only
    else
      run mkdir -p "$SHARE"
      run git clone -q --depth 1 "$repo" "$dest"
    fi
    local p
    for p in $paths; do link_skill "$dest/$p"; done
  fi
  if [ -n "$extra" ]; then
    case "$extra" in
      "uv tool install "*) run uv tool install "${extra#uv tool install }" ;;
      *) echo "  note: $extra" ;;
    esac
  fi
}

for row in "${TOOLS[@]}"; do
  IFS='|' read -r name desc cmkt cplug xmkt xplug repo paths extra <<< "$row"
  if [ "${#WANT[@]}" -gt 0 ]; then
    printf '%s\n' "${WANT[@]}" | grep -qx "$name" || continue
  fi
  install_one "$name" "$desc" "$cmkt" "$cplug" "$xmkt" "$xplug" "$repo" "$paths" "$extra"
done
echo
echo "Done. Restart Claude Code / Codex so they pick up new plugins and skills."
