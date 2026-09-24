#!/bin/bash
# Wire ~/.agents into every installed coding-agent harness. Safe to re-run.
#   ./install.sh                  install or repair
#   ./install.sh --doctor         report only, change nothing
#   ./install.sh --profile <dir>  add a profile (a private repo with repo overlays, skills, research, rules)
set -euo pipefail

KIT="$HOME/.agents"
DOCTOR=0; NEW_PROFILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --doctor) DOCTOR=1 ;;
    --profile) NEW_PROFILE="$(cd "$2" && pwd)"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
BACKUP="$KIT/backups/$(date +%Y%m%d-%H%M%S)"
ok()   { printf '  ok    %s\n' "$*"; }
fix()  { printf '  fix   %s\n' "$*"; }
warn() { printf '  warn  %s\n' "$*"; }

backup() { [ -e "$1" ] && [ ! -L "$1" ] && { mkdir -p "$BACKUP"; cp -R "$1" "$BACKUP/"; } || true; }

# link <target> <link path>: make <link path> a symlink to <target>, backing up a real file first.
link() {
  local target=$1 path=$2
  if [ "$(readlink "$path" 2>/dev/null)" = "$target" ]; then ok "$path"; return; fi
  if [ $DOCTOR = 1 ]; then warn "$path should link to $target"; return; fi
  mkdir -p "$(dirname "$path")"; backup "$path"; rm -f "$path"; ln -s "$target" "$path"; fix "$path -> $target"
}

# hooks <settings.json> <event> <matcher> <script> <timeout>: one entry per script, other hooks untouched.
hook() {
  local file=$1 event=$2 matcher=$3 script=$4 timeout=$5
  local cmd="python3 \$HOME/.agents/hooks/$script"
  if jq -e --arg ev "$event" --arg c "$cmd" '.hooks[$ev] // [] | map(.hooks[]?.command) | index($c)' "$file" >/dev/null 2>&1; then
    ok "$(basename "$file") $event → $script"; return
  fi
  if [ $DOCTOR = 1 ]; then warn "$(basename "$file") is missing $event → $script"; return; fi
  backup "$file"
  local tmp; tmp=$(mktemp)
  jq --arg ev "$event" --arg m "$matcher" --arg c "$cmd" --argjson to "$timeout" \
    '.hooks[$ev] = ((.hooks[$ev] // []) + [{matcher: $m, hooks: [{type: "command", command: $c, timeout: $to}]}])' \
    "$file" > "$tmp" && mv "$tmp" "$file"
  fix "$(basename "$file") $event → $script"
}

# settings_file <path> <initial json>: create a harness settings file on a machine that has none yet.
settings_file() {
  [ -f "$1" ] && return
  if [ $DOCTOR = 1 ]; then warn "$1 does not exist yet"; return; fi
  mkdir -p "$(dirname "$1")"; echo "$2" > "$1"; fix "$1 created"
}

# baseline <settings file> <kit baseline>: add the kit's recommended settings where a key is missing.
baseline() {
  local check=""; [ $DOCTOR = 1 ] && check=--check
  local out; out=$(python3 "$KIT/adapters/apply_baseline.py" "$1" "$2" $check)
  if [ -z "$out" ]; then ok "$(basename "$1") has the kit's baseline settings"; return; fi
  [ $DOCTOR = 1 ] || backup "$1"
  while read -r line; do if [ $DOCTOR = 1 ]; then warn "$(basename "$1") $line"; else fix "$(basename "$1") $line"; fi; done <<<"$out"
}

skills() {  # link every kit skill into a harness skills dir
  local dir=$1
  for s in "$KIT"/skills/*/; do s=${s%/}; link "$s" "$dir/$(basename "$s")"; done
}

echo "Profiles"
# A profile is layered in by symlinks into the kit's git-ignored slots, so every path the hooks,
# skills and AGENTS.md use stays the same with or without it.
[ -n "$NEW_PROFILE" ] && link "$NEW_PROFILE" "$KIT/profiles/$(basename "$NEW_PROFILE")"
for profile in "$KIT"/profiles/*/; do
  [ -d "$profile" ] || continue
  profile=${profile%/}
  for entry in "$profile"/repos/*/ "$profile"/skills/*/; do
    [ -d "$entry" ] || continue
    entry=${entry%/}; slot=$(basename "$(dirname "$entry")")
    link "$entry" "$KIT/$slot/$(basename "$entry")"
  done
  for doc in "$profile"/research/*; do [ -e "$doc" ] && link "$doc" "$KIT/research/$(basename "$doc")"; done
done
[ -d "$KIT/profiles" ] || ok "no profiles (add one with --profile <dir>)"

echo "External skills"
# Third-party skills are fetched from their source instead of being copied into the kit.
while read -r name url _; do
  case "$name" in ''|'#'*) continue ;; esac
  if [ -d "$KIT/skills/$name" ]; then ok "$name"
  elif [ $DOCTOR = 1 ]; then warn "$name is missing (from $url)"
  else git clone -q --depth 1 "$url" "$KIT/skills/$name" && fix "$name cloned from $url"; fi
done < "$KIT/skills.external"

echo "Kit repository"
# The pre-commit hook regenerates docs/framework.md, so the reference never lags the code.
if [ ! -d "$KIT/.git" ]; then warn "$KIT is not a git repository; the docs hook needs one"
elif [ "$(git -C "$KIT" config core.hooksPath)" = ".githooks" ]; then ok "git hooks (.githooks)"
elif [ $DOCTOR = 1 ]; then warn "git hooks not enabled (git -C $KIT config core.hooksPath .githooks)"
else git -C "$KIT" config core.hooksPath .githooks && fix "git hooks (.githooks)"; fi

echo "Requirements"
for bin in python3 jq git semgrep gitleaks docker node playwright-cli agent-browser; do
  command -v $bin >/dev/null && ok "$bin" || warn "$bin not found"
done
for tool in a11y mermaid; do
  if [ -d "$KIT/tools/$tool/node_modules" ]; then ok "$tool dependencies"
  elif [ $DOCTOR = 1 ]; then warn "$tool dependencies missing (npm install in tools/$tool)"
  else (cd "$KIT/tools/$tool" && npm install --no-audit --no-fund >/dev/null 2>&1) && fix "$tool dependencies installed"; fi
done

if command -v claude >/dev/null || [ -d "$HOME/.claude" ]; then
  echo "Claude Code"
  link "$KIT/AGENTS.md" "$HOME/.claude/CLAUDE.md"
  skills "$HOME/.claude/skills"
  S="$HOME/.claude/settings.json"; settings_file "$S" '{}'
  baseline "$S" "$KIT/adapters/claude/settings.baseline.json"
  hook "$S" PreToolUse  "Bash" guard_bash.py 10
  hook "$S" PreToolUse  "mcp__.*" guard_mcp.py 10
  hook "$S" PostToolUse "Edit|Write|MultiEdit|NotebookEdit" post_edit.py 30
  hook "$S" Stop        "" stop_checks.py 660
  # Xirp rewrites statusLine when it reinstalls its integration; ours runs Xirp's line too.
  line="$KIT/adapters/claude/statusline.sh"
  if [ "$(jq -r '.statusLine.command // ""' "$S" 2>/dev/null)" = "$line" ]; then ok "status line"
  elif [ $DOCTOR = 1 ]; then warn "status line is not $line (Xirp may have reset it)"
  else backup "$S"; tmp=$(mktemp); jq --arg c "$line" '.statusLine = {type: "command", command: $c}' "$S" > "$tmp" && mv "$tmp" "$S"; fix "status line"; fi
fi

if command -v codex >/dev/null || [ -d "$HOME/.codex" ]; then
  echo "Codex"
  link "$KIT/AGENTS.md" "$HOME/.codex/AGENTS.md"
  skills "$HOME/.codex/skills"
  H="$HOME/.codex/hooks.json"; settings_file "$H" '{"hooks":{}}'
  baseline "$HOME/.codex/config.toml" "$KIT/adapters/codex/config.baseline.toml"
  hook "$H" PreToolUse  "Bash|shell|exec_command|local_shell" guard_bash.py 10
  hook "$H" PreToolUse  "mcp__.*" guard_mcp.py 10
  hook "$H" PostToolUse "apply_patch|Edit|Write" post_edit.py 30
  hook "$H" Stop        "" stop_checks.py 660
  # Codex silently skips hooks the user hasn't trusted; trust lives in config.toml [hooks.state].
  jq -r '.hooks | to_entries[] | .key as $e | .value | to_entries[] | .key as $i | .value.hooks | to_entries[]
    | "\($e)\t\($i)\t\(.key)\t\(.value.command)"' "$H" 2>/dev/null | while IFS=$'\t' read -r ev i j cmd; do
    snake=$(sed -E 's/([a-z])([A-Z])/\1_\2/g' <<<"$ev" | tr '[:upper:]' '[:lower:]')
    grep -qF "hooks.json:$snake:$i:$j\"]" "$HOME/.codex/config.toml" 2>/dev/null \
      || warn "codex hook not trusted yet (skipped silently): $ev → ${cmd##*/}. Open Codex and approve it."
  done
  grep -q '^hooks = false' "$HOME/.codex/config.toml" 2>/dev/null \
    && warn "hooks are disabled in ~/.codex/config.toml ([features] hooks = false)" || ok "codex hooks feature enabled"
  for profile in deep light; do link "$KIT/adapters/codex/$profile.config.toml" "$HOME/.codex/$profile.config.toml"; done
  # A symlinked codex binary looks for its code-mode host next to the symlink, not the real binary.
  real=$(readlink "$(command -v codex)" 2>/dev/null || true)
  if [ -n "$real" ] && [ -x "$(dirname "$real")/codex-code-mode-host" ]; then
    link "$(dirname "$real")/codex-code-mode-host" "$(dirname "$(command -v codex)")/codex-code-mode-host"
  fi
fi

if command -v pi >/dev/null || [ -d "$HOME/.pi" ]; then
  echo "Pi"
  link "$KIT/AGENTS.md" "$HOME/.pi/agent/AGENTS.md"
  ok "skills: Pi loads ~/.agents/skills natively"
  link "$KIT/adapters/pi/agents-kit.ts" "$HOME/.pi/agent/extensions/agents-kit.ts"
  baseline "$HOME/.pi/agent/settings.json" "$KIT/adapters/pi/settings.baseline.json"
fi

echo "Scheduled jobs"
# launchd needs real files with absolute paths, so the kit keeps templates and renders them here.
nodebin=$(dirname "$(command -v node 2>/dev/null || echo /usr/local/bin/node)")
# Job labels are per user, not per HOME: a test install must not replace the real jobs.
[ -n "${AGENTS_SKIP_LAUNCHD:-}" ] && ok "skipped (AGENTS_SKIP_LAUNCHD is set)"
for tpl in "$KIT"/launchd/*.plist; do
  [ -n "${AGENTS_SKIP_LAUNCHD:-}" ] && break
  label="com.$(id -un).$(basename "$tpl" .plist)"; dest="$HOME/Library/LaunchAgents/$label.plist"
  rendered=$(sed -e "s#__HOME__#$HOME#g" -e "s#__NODEBIN__#$nodebin#g" -e "s#__LABEL__#$label#g" "$tpl")
  if [ "$rendered" = "$(cat "$dest" 2>/dev/null)" ] && launchctl list "$label" >/dev/null 2>&1; then ok "$label"; continue; fi
  if [ $DOCTOR = 1 ]; then warn "$label is not installed or out of date"; continue; fi
  mkdir -p "$KIT/monitors/state"; backup "$dest"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  echo "$rendered" > "$dest"
  launchctl bootstrap "gui/$(id -u)" "$dest" && fix "$label loaded"
done

[ -d "$BACKUP" ] && echo "Backups of replaced files: $BACKUP"
echo "Done."
