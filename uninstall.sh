#!/bin/bash
# Unwire ~/.agents from every harness: what install.sh added that still points at the kit. Safe to re-run.
#   ./uninstall.sh                list what it would remove, then ask
#   ./uninstall.sh --yes          remove without asking
# Kept: settings install.sh only filled in where they were missing (now ordinary settings), programs
# installed through Homebrew, and the kit itself with its logs, research and profile links.
set -euo pipefail

KIT="$HOME/.agents"
YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
# Outside the kit, so deleting ~/.agents afterwards doesn't take the backups with it.
BACKUP="$HOME/.agents-uninstall-backups/$(date +%Y%m%d-%H%M%S)"
APPLY=0; planned=0
todo() { if [ $APPLY = 1 ]; then printf '  done  %s\n' "$*"; else printf '  -     %s\n' "$*"; planned=$((planned + 1)); fi; }
warn() { [ $APPLY = 1 ] || printf '  warn  %s\n' "$*"; }
backup() { [ $APPLY = 1 ] && [ -f "$1" ] && { mkdir -p "$BACKUP"; local dir; dir=$(basename "$(dirname "$1")"); cp "$1" "$BACKUP/${dir#.}-$(basename "$1")"; } || true; }

# unlink_kit <path>: remove a symlink into the kit; anything else there is the user's.
unlink_kit() {
  local target; target=$(readlink "$1" 2>/dev/null) || return 0
  case "$target" in "$KIT"/*) ;; *) return 0 ;; esac
  [ $APPLY = 1 ] && rm "$1"
  todo "$1 (link to ${target#"$KIT"/})"
}

unlink_skills() { for s in "$1"/*; do unlink_kit "$s"; done; }

# unhook <hooks file>: drop the kit's hook commands; other hooks, and the rest of the file, stay.
KIT_HOOK='python3 $HOME/.agents/hooks/'
unhook() {
  local file=$1 count
  count=$(jq --arg k "$KIT_HOOK" '[.hooks[]?[]?.hooks[]?.command // "" | select(startswith($k))] | length' "$file" 2>/dev/null) || return 0
  [ "$count" -gt 0 ] || return 0
  if [ $APPLY = 1 ]; then
    backup "$file"
    local tmp; tmp=$(mktemp)
    jq --arg k "$KIT_HOOK" '.hooks |= (map_values(map(.hooks |= map(select((.command // "") | startswith($k) | not)))
      | map(select(.hooks | length > 0))) | with_entries(select(.value | length > 0)))' "$file" > "$tmp" && mv "$tmp" "$file"
  fi
  todo "$file: $count kit hooks"
}

unwire() {
  echo "Claude Code"
  unlink_kit "$HOME/.claude/CLAUDE.md"
  unlink_skills "$HOME/.claude/skills"
  S="$HOME/.claude/settings.json"
  [ -f "$S" ] && unhook "$S"
  line="$KIT/adapters/claude/statusline.sh"
  current=$(jq -r '.statusLine.command // ""' "$S" 2>/dev/null || true)
  if [ "$current" = "$line" ]; then
    if [ $APPLY = 1 ]; then backup "$S"; tmp=$(mktemp); jq 'del(.statusLine)' "$S" > "$tmp" && mv "$tmp" "$S"; fi
    todo "$S: status line"
  elif [ -n "$current" ] && grep -qF "$line" "$current" 2>/dev/null; then
    warn "your status line ($current) runs the kit's; edit it by hand"
  fi

  echo "Codex"
  unlink_kit "$HOME/.codex/AGENTS.md"
  unlink_skills "$HOME/.codex/skills"
  for profile in deep light; do unlink_kit "$HOME/.codex/$profile.config.toml"; done
  [ -f "$HOME/.codex/hooks.json" ] && unhook "$HOME/.codex/hooks.json"

  echo "Pi"
  unlink_kit "$HOME/.pi/agent/AGENTS.md"
  unlink_kit "$HOME/.pi/agent/extensions/agents-kit.ts"

  echo "Scheduled jobs"
  # Job labels are per user, not per HOME: a test HOME must not unload the real jobs.
  if [ -n "${AGENTS_SKIP_LAUNCHD:-}" ]; then [ $APPLY = 1 ] || echo "  skipped (AGENTS_SKIP_LAUNCHD is set)"
  else
    for tpl in "$KIT"/launchd/*.plist; do
      label="com.$(id -un).$(basename "$tpl" .plist)"; dest="$HOME/Library/LaunchAgents/$label.plist"
      [ -f "$dest" ] || continue
      if [ $APPLY = 1 ]; then launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true; rm "$dest"; fi
      todo "$label (unloaded, $dest removed)"
    done
  fi

  if [ "$(readlink -f "$(command -v gh 2>/dev/null)" 2>/dev/null)" = "$(readlink -f "$KIT/bin/gh")" ]; then
    echo "gh wrapper"; wrapper=$(command -v gh)
    # Root's directory: only a person at a terminal can give sudo its password.
    if [ $APPLY = 0 ]; then todo "$wrapper (link to bin/gh; needs sudo)"
    elif [ -t 0 ] && sudo rm "$wrapper"; then todo "$wrapper"
    else printf '  warn  %s still links to the kit; run: sudo rm %s\n' "$wrapper" "$wrapper"; fi
  fi
}

unwire
if [ $planned = 0 ]; then echo "Nothing of the kit is wired in."; exit 0; fi
answer=n
if [ $YES = 1 ]; then answer=y
elif [ -t 0 ]; then read -r -p "Remove these $planned items? [y/N] " answer
else echo "Nothing removed: run uninstall.sh in a terminal to be asked, or with --yes."; exit 0; fi
[[ $answer == [Yy]* ]] || { echo "Nothing removed."; exit 0; }

APPLY=1
unwire
[ -d "$BACKUP" ] && echo "Settings files before the change: $BACKUP"
echo "Kept: the settings install.sh filled in where they were missing, and the programs it installed with Homebrew."
[ -d "$KIT/backups" ] && echo "Files install.sh replaced with links were saved in $KIT/backups; copy back any you want."
echo "Codex may ask you to approve your remaining hooks again. The kit is still at $KIT; delete it when you no longer need it."
