#!/bin/bash
# Wire ~/.agents into every installed coding-agent harness. Safe to re-run.
#   ./install.sh                  install or repair
#   ./install.sh --doctor         report only, change nothing
#   ./install.sh --profile <dir>  add a profile (a private repo with repo overlays, skills, research, rules)
#   ./install.sh --yes            install missing requirements without asking
set -euo pipefail

KIT="$HOME/.agents"
DOCTOR=0; NEW_PROFILE=""; YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --doctor) DOCTOR=1 ;;
    --yes) YES=1 ;;
    --profile) NEW_PROFILE="$(cd "$2" && pwd)"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
BACKUP="$KIT/backups/$(date +%Y%m%d-%H%M%S)"
ok()   { printf '  ok    %s\n' "$*"; }
fix()  { printf '  fix   %s\n' "$*"; }
warn() { printf '  warn  %s\n' "$*"; }
info() { printf '  info  %s\n' "$*"; }

backup() { [ -e "$1" ] && [ ! -L "$1" ] && { mkdir -p "$BACKUP"; cp -R "$1" "$BACKUP/"; } || true; }

# link <target> <link path>: make <link path> a symlink to <target>, backing up a real file first.
link() {
  local target=$1 path=$2
  if [ "$(readlink "$path" 2>/dev/null)" = "$target" ]; then ok "$path"; return; fi
  if [ $DOCTOR = 1 ]; then warn "$path should link to $target"; return; fi
  mkdir -p "$(dirname "$path")"
  # A folder keeps its full path under the backups: two harnesses can hold one with the same name.
  if [ -d "$path" ] && [ ! -L "$path" ]; then mkdir -p "$BACKUP$(dirname "$path")"; mv "$path" "$BACKUP$path"; else backup "$path"; rm -f "$path"; fi
  ln -s "$target" "$path"; fix "$path -> $target"
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

version=$(cat "$KIT/VERSION")
echo "agents-kit $version"
echo "Profiles"
# A profile is layered in by symlinks into the kit's git-ignored slots, so every path the hooks,
# skills and AGENTS.md use stays the same with or without it.
if [ -n "$NEW_PROFILE" ]; then
  current=$(readlink "$KIT/profiles/$(basename "$NEW_PROFILE")" 2>/dev/null || true)
  [ -n "$current" ] && [ "$current" != "$NEW_PROFILE" ] \
    && warn "profile $(basename "$NEW_PROFILE") was $current; $NEW_PROFILE replaces it (profiles are named by their directory)"
  link "$NEW_PROFILE" "$KIT/profiles/$(basename "$NEW_PROFILE")"
fi
# Two profiles with the same name for a skill, overlay or doc: the first, alphabetically, keeps it. Linking both
# would re-point the link on every run.
claimed=""
link_profile_entry() {  # <profile name> <entry> <kit path>
  local owner
  owner=$(lookup="$3" awk -F'\t' '$1 == ENVIRON["lookup"] { print $2; exit }' <<<"$claimed")
  if [ -n "$owner" ]; then warn "${3#"$KIT"/} is in profiles $owner and $1; using $owner's"; return; fi
  claimed+="$3	$1"$'\n'
  link "$2" "$3"
}
for profile in "$KIT"/profiles/*/; do
  [ -d "$profile" ] || continue
  profile=${profile%/}; name=$(basename "$profile")
  for entry in "$profile"/repos/*/ "$profile"/skills/*/; do
    [ -d "$entry" ] || continue
    entry=${entry%/}; slot=$(basename "$(dirname "$entry")")
    target="$KIT/$slot/$(basename "$entry")"
    if [ -e "$target" ] && [ ! -L "$target" ]; then warn "$slot/$(basename "$entry") in profile $name has the name of the kit's own; skipped"; continue; fi
    link_profile_entry "$name" "$entry" "$target"
  done
  for doc in "$profile"/research/*; do [ -e "$doc" ] && link_profile_entry "$name" "$doc" "$KIT/research/$(basename "$doc")"; done
done
[ -d "$KIT/profiles" ] || ok "no profiles (add one with --profile <dir>)"
# Until a profile names the user's own owners, design_files.py asks before Impeccable's files in every repository.
if compgen -G "$KIT/profiles/*/personal-repos.txt" >/dev/null; then ok "personal repositories listed (personal-repos.txt)"
else info "no profile has personal-repos.txt: every repository with a remote counts as shared, and Impeccable's PRODUCT.md, DESIGN.md and .impeccable/ wait for your say-so in all of them"; fi
# guard_mcp.py merges every profile's direct MCP servers into one table, so a later profile replaces earlier rules.
python3 - "$KIT" <<'PY' | while read -r line; do warn "$line"; done
import glob, json, os, sys
kit = sys.argv[1]
os.environ["AGENTS_PROFILES_DIR"] = os.devnull
sys.path.insert(0, os.path.join(kit, "hooks"))
import guard_mcp
owner = {server: "the kit" for server in guard_mcp.DIRECT_WRITE}
for path in sorted(glob.glob(os.path.join(kit, "profiles", "*", "mcp-writes.json"))):
    profile = os.path.basename(os.path.dirname(path))
    for server in json.load(open(path)).get("direct", {}):
        if server in owner:
            print(f"MCP server {server} has write rules in {owner[server]} and profile {profile}; {profile}'s replace them")
        owner[server] = f"profile {profile}"
PY

echo "External skills"
# Third-party skills are fetched from their source instead of being copied into the kit. A pinned one,
# <url>@<commit>#<folder>, is checked out in vendor/ at exactly that commit and only its folder is linked in.
pinned_skill() {  # <name> <url> <commit> <folder>
  local name=$1 url=$2 pin=$3 folder=$4 checkout="$KIT/vendor/$1" at="" kept="not installed"
  # Without its own .git, git -C would find the kit's repository and report the kit's HEAD.
  [ -e "$checkout/.git" ] && at=$(git -C "$checkout" rev-parse -q --verify HEAD) && kept="keeping ${at:0:12}"
  if [ $DOCTOR = 1 ]; then
    if [ -z "$at" ]; then warn "$name is missing (from $url)"; return; fi
    local changes; changes=$(git -C "$checkout" status --porcelain)
    [ "$at" = "$pin" ] || warn "$name is at ${at:0:12}, pinned to ${pin:0:12}"
    [ -z "$changes" ] || warn "$name has local changes in vendor/$name"
    [ "$at" != "$pin" ] || [ -n "$changes" ] || ok "$name at ${pin:0:12}"
  elif [ "$at" = "$pin" ]; then ok "$name at ${pin:0:12}"
  else
    # Nothing replaces the current checkout or link until the new commit is known to hold the skill.
    [ -e "$checkout/.git" ] || git init -q "$checkout"
    git -C "$checkout" config remote.origin.url "$url"
    if ! git -C "$checkout" fetch -q --depth 1 origin "$pin" 2>/dev/null; then
      warn "$name: cannot fetch ${pin:0:12} from $url; $kept"; return
    elif [ "$(git -C "$checkout" cat-file -t "$pin:$folder/SKILL.md" 2>/dev/null)" != blob ]; then
      warn "$name: $folder has no SKILL.md at ${pin:0:12}; $kept"; return
    elif ! git -C "$checkout" checkout -q --detach "$pin"; then
      warn "$name: cannot check out ${pin:0:12} in vendor/$name (local changes?); $kept"; return
    fi
    fix "$name checked out at ${pin:0:12} from $url"
  fi
  if [ ! -f "$checkout/$folder/SKILL.md" ]; then warn "$name: $folder has no SKILL.md at ${at:0:12}; not linked"; return; fi
  link "$checkout/$folder" "$KIT/skills/$name"
}
while read -r name source _; do
  case "$name" in ''|'#'*) continue ;; esac
  url=${source%@*} pin=${source##*@} folder=${pin#*#} pin=${pin%%#*}
  # A copied or moved kit keeps links to the old vendor/ path: those are still the kit's, not a profile's.
  if [ -L "$KIT/skills/$name" ] && [[ "$(readlink "$KIT/skills/$name")" != */vendor/"$name"/* ]]; then
    warn "$name is an external skill and a profile's; using the profile's"
  elif [[ "$source" == *@*#* ]]; then pinned_skill "$name" "$url" "$pin" "$folder"
  elif [ -d "$KIT/skills/$name" ]; then ok "$name"
  elif [ $DOCTOR = 1 ]; then warn "$name is missing (from $source)"
  else git clone -q --depth 1 "$source" "$KIT/skills/$name" && fix "$name cloned from $source"; fi
done < "$KIT/skills.external"

echo "Kit repository"
# The pre-commit hook regenerates docs/framework.md, so the reference never lags the code.
if [ ! -e "$KIT/.git" ]; then warn "$KIT is not a git repository; the docs hook needs one"
elif [ "$(git -C "$KIT" config core.hooksPath)" = ".githooks" ]; then ok "git hooks (.githooks)"
elif [ $DOCTOR = 1 ]; then warn "git hooks not enabled (git -C $KIT config core.hooksPath .githooks)"
else git -C "$KIT" config core.hooksPath .githooks && fix "git hooks (.githooks)"; fi

echo "Requirements"
# deps.txt lists every program the kit runs, and profiles add theirs. Missing required and recommended
# programs are installed through Homebrew once the person running this agrees (or passed --yes); optional
# ones serve a single feature, so they are only reported.
installed() {
  case "$1" in
    chrome) [ -d "/Applications/Google Chrome.app" ] ;;
    # The skill's launcher fetches this engine on first use: only its cache counts, since an `impeccable` on PATH
    # may be a launcher that downloads when asked for --version.
    impeccable) [ -x "$HOME/.impeccable/bin/$(cat "$KIT/skills/impeccable/scripts/VERSION" 2>/dev/null)/impeccable" ] ;;
    *) command -v "$1" >/dev/null ;;
  esac
}
install_hint() { case "$1" in brew:*) echo "brew install ${1#brew:}" ;; npm:*) echo "npm install -g ${1#npm:}" ;; *) echo "$1" ;; esac; }
missing=()
older_than() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" != "$2" ]; }
while read -r tier spec how purpose; do
  program=${spec%%>=*}; minimum=${spec#"$program"}; minimum=${minimum#>=}
  if installed "$program"; then
    version=""  # chrome is an app; an impeccable on PATH may be a launcher that downloads, and its cache is pinned
    [ "$program" = impeccable ] || version=$("$program" --version 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+)+' | head -1) || true
    if [ -z "$minimum" ] || [ -z "$version" ] || ! older_than "$version" "$minimum"; then ok "$program${version:+ $version}"; continue; fi
    msg="$program $version is older than $minimum, needed for $purpose: $(install_hint "$how")"
    if [ "$tier" = optional ]; then info "$msg"; else warn "$msg"; fi
    continue
  fi
  if [ "$tier" = optional ]; then info "$program not installed, needed for $purpose: $(install_hint "$how")"; continue; fi
  if [ $DOCTOR = 0 ] && [ "${how%%:*}" = brew ] && command -v brew >/dev/null; then
    missing+=("$program ${how#brew:} $tier $purpose"); continue
  fi
  warn "$program is missing ($tier), needed for $purpose: $(install_hint "$how")"
done < <(cat "$KIT/deps.txt" "$KIT"/profiles/*/deps.txt 2>/dev/null | grep -Ev '^[[:space:]]*(#|$)' | awk '
  # One line per program, in first-seen order, with the strictest tier any list gives it.
  { rank = $1 == "required" ? 3 : $1 == "recommended" ? 2 : 1; p = $2; sub(/>=.*/, "", p) }
  !(p in best) { order[++n] = p }
  rank > best[p] { best[p] = rank; line[p] = $0 }
  END { for (i = 1; i <= n; i++) print line[order[i]] }')
if [ ${#missing[@]} -gt 0 ]; then
  answer=n; asked=""
  if [ $YES = 1 ]; then answer=y
  elif [ -t 0 ]; then
    read -r -p "  Install with Homebrew: $(for m in "${missing[@]}"; do printf '%s ' "${m%% *}"; done)[Y/n] " answer
    answer=${answer:-y}
  else asked=" (run install.sh in a terminal to be asked, or with --yes)"; fi
  for entry in "${missing[@]}"; do
    read -r program formula tier purpose <<<"$entry"
    if [[ $answer == [Yy]* ]] && brew install --quiet "$formula" </dev/null >/dev/null 2>&1 && installed "$program"; then
      fix "$program installed (brew install $formula)"
    else warn "$program is missing ($tier), needed for $purpose: brew install $formula$asked"; fi
  done
fi
for dir in tools/a11y tools/mermaid site; do
  if [ -d "$KIT/$dir/node_modules" ]; then ok "$dir dependencies"
  elif [ $DOCTOR = 1 ]; then warn "$dir dependencies missing (npm install in $dir)"
  elif (cd "$KIT/$dir" && npm install --no-audit --no-fund >/dev/null 2>&1); then fix "$dir dependencies installed"
  else warn "npm install failed in $dir"; fi
done

# gh ignores git's per-host proxies (http.<url>.proxy); bin/gh applies them, so it must come first in PATH.
# /usr/local/bin precedes Homebrew in every shell the harnesses start. It is root's, so the link needs sudo:
# asked for only when a person runs this in a terminal; an agent's run can't answer a password prompt.
if git config --global --get-regexp '^http\..+\.proxy$' >/dev/null 2>&1; then
  wrapper_cmd="sudo ln -sf $KIT/bin/gh /usr/local/bin/gh"
  if [ "$(readlink -f "$(command -v gh)")" = "$(readlink -f "$KIT/bin/gh")" ]; then ok "gh applies git's per-host proxies (bin/gh)"
  elif [ $DOCTOR = 1 ] || [ ! -t 0 ]; then warn "gh ignores git's per-host proxy, so gh hangs on that host. Run: $wrapper_cmd"
  else
    echo "  gh ignores git's per-host proxy; linking bin/gh ahead of it needs your password:"
    $wrapper_cmd && fix "gh applies git's per-host proxies (/usr/local/bin/gh -> bin/gh)" || warn "not linked; run: $wrapper_cmd"
  fi
fi

if command -v claude >/dev/null || [ -d "$HOME/.claude" ]; then
  echo "Claude Code"
  link "$KIT/AGENTS.md" "$HOME/.claude/CLAUDE.md"
  skills "$HOME/.claude/skills"
  S="$HOME/.claude/settings.json"; settings_file "$S" '{}'
  baseline "$S" "$KIT/adapters/claude/settings.baseline.json"
  hook "$S" PreToolUse  "Bash" guard_bash.py 10
  hook "$S" PreToolUse  "mcp__.*" guard_mcp.py 10
  hook "$S" PreToolUse  "Edit|Write|MultiEdit|NotebookEdit" guard_files.py 10
  hook "$S" UserPromptSubmit "" prompt_approvals.py 10
  hook "$S" PostToolUse "Edit|Write|MultiEdit|NotebookEdit" post_edit.py 30
  hook "$S" Stop        "" stop_checks.py 660
  # Tools that add their own status line may point statusLine at a wrapper script that runs ours;
  # that keeps our line, so it counts as installed. Anything else loses it.
  line="$KIT/adapters/claude/statusline.sh"
  current=$(jq -r '.statusLine.command // ""' "$S" 2>/dev/null)
  if [ "$current" = "$line" ] || grep -qF "$line" "$current" 2>/dev/null; then ok "status line"
  elif [ $DOCTOR = 1 ]; then warn "status line neither is nor wraps $line"
  else backup "$S"; tmp=$(mktemp); jq --arg c "$line" '.statusLine = {type: "command", command: $c}' "$S" > "$tmp" && mv "$tmp" "$S"; fix "status line"; fi
fi

if command -v codex >/dev/null || [ -d "$HOME/.codex" ]; then
  echo "Codex"
  if codex_out=$(codex --version 2>&1); then ok "codex runs ($codex_out)"
  else
    if ! command -v codex >/dev/null; then
      codex_out="not found on PATH"
      IFS=: read -ra path_dirs <<<"$PATH"
      for dir in "${path_dirs[@]}"; do
        [ -L "$dir/codex" ] && [ ! -e "$dir/codex" ] && { codex_out+="; $dir/codex links to $(readlink "$dir/codex"), which no longer exists"; break; }
      done
    fi
    warn "codex does not run ($(head -1 <<<"$codex_out")): the self-review's cross-model pass and the spec's second reading need it. Reinstall Codex or relink it."
  fi
  link "$KIT/AGENTS.md" "$HOME/.codex/AGENTS.md"
  skills "$HOME/.codex/skills"
  H="$HOME/.codex/hooks.json"; settings_file "$H" '{"hooks":{}}'
  baseline "$HOME/.codex/config.toml" "$KIT/adapters/codex/config.baseline.toml"
  hook "$H" PreToolUse  "Bash|shell|exec_command|local_shell" guard_bash.py 10
  hook "$H" PreToolUse  "mcp__.*" guard_mcp.py 10
  hook "$H" PreToolUse  "apply_patch|Edit|Write" guard_files.py 10
  hook "$H" UserPromptSubmit "" prompt_approvals.py 10
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
render() {
  sed -e "s#__HOME__#$HOME#g" -e "s#__NODEBIN__#$1#g" -e "s#__LABEL__#$label#g" -e "s#__PROFILE__#$profile_dir#g" "$tpl"
}
# A profile's launchd/*.plist load the same way, with __PROFILE__ for its directory. As with skills, a job name
# stays with the kit, then with the first profile alphabetically.
templates=("$KIT"/launchd/*.plist); profile_dirs=(); job_owners=""
for tpl in "${templates[@]}"; do profile_dirs+=(""); done
for profile in "$KIT"/profiles/*/; do
  [ -n "${AGENTS_SKIP_LAUNCHD:-}" ] && break
  for tpl in "$profile"launchd/*.plist; do
    [ -e "$tpl" ] || continue
    job=$(basename "$tpl"); name=$(basename "$profile")
    owner=$(lookup="$job" awk -F'\t' '$1 == ENVIRON["lookup"] { print $2; exit }' <<<"$job_owners")
    if [ -e "$KIT/launchd/$job" ]; then warn "launchd/$job in profile $name has the name of the kit's own; skipped"; continue; fi
    if [ -n "$owner" ]; then warn "launchd/$job is in profiles $owner and $name; using $owner's"; continue; fi
    job_owners+="$job	$name"$'\n'
    templates+=("$tpl"); profile_dirs+=("${profile%/}")
  done
done
for i in "${!templates[@]}"; do
  [ -n "${AGENTS_SKIP_LAUNCHD:-}" ] && break
  tpl=${templates[$i]} profile_dir=${profile_dirs[$i]}
  label="com.$(id -un).$(basename "$tpl" .plist)"; dest="$HOME/Library/LaunchAgents/$label.plist"
  if [ $DOCTOR = 1 ]; then
    # A job is fine while the node it names is installed, even when the shell runs another nvm version.
    node_line=$(render __NODEBIN__ | grep -F -m1 __NODEBIN__ || true)
    before_node=${node_line%%__NODEBIN__*} after_node=${node_line#*__NODEBIN__}
    job_node=$(grep -F -- "$before_node" "$dest" 2>/dev/null | head -1 || true); job_node=${job_node#"$before_node"}; job_node=${job_node%"$after_node"}
    if [ -n "$job_node" ] && [ -x "$job_node/node" ] && [ "$(render "$job_node")" = "$(cat "$dest")" ] \
      && launchctl list "$label" >/dev/null 2>&1; then ok "$label (node from $job_node)"; continue; fi
    warn "$label is not installed or out of date"; continue
  fi
  rendered=$(render "$nodebin")
  if [ "$rendered" = "$(cat "$dest" 2>/dev/null)" ] && launchctl list "$label" >/dev/null 2>&1; then ok "$label"; continue; fi
  mkdir -p "$KIT/monitors/state" "$(dirname "$dest")"; backup "$dest"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  echo "$rendered" > "$dest"
  launchctl bootstrap "gui/$(id -u)" "$dest" && fix "$label loaded"
done

[ -d "$BACKUP" ] && echo "Backups of replaced files: $BACKUP"
echo "Done."
