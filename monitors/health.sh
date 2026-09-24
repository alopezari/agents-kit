#!/bin/bash
# Weekly framework health check. Deterministic, no model calls.
# Writes research/health/<date>.md and notifies only when something needs attention.
#   ~/.agents/monitors/health.sh [--quiet]
set -uo pipefail
K="$HOME/.agents"
STATE="$K/monitors/state"
REPORT="$K/research/health/$(date +%F).md"
mkdir -p "$STATE" "$(dirname "$REPORT")"
problems=()
notes=()

days_since() { echo $(( ( $(date +%s) - $(stat -f %m "$1") ) / 86400 )); }

# 1. Regression suite (hooks, semgrep rules, Pi adapter, triage, install doctor).
suite=$("$K/tests/run.sh" 2>&1); suite_rc=$?
[ $suite_rc = 0 ] || problems+=("Regression suite failed: $(grep -E '^FAIL|  warn ' <<<"$suite" | head -5 | tr '\n' ';')")

# 2. Harness and tool versions: a new version can change the hook contract or add a native feature.
current=$(for c in claude codex pi semgrep gitleaks playwright-cli agent-browser php docker; do
  printf '%s %s\n' "$c" "$($c --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?[^ ]*' | head -1)"; done)
if [ -f "$STATE/versions.txt" ]; then
  changed=$(diff <(sort "$STATE/versions.txt") <(sort <<<"$current") | grep '^>' | cut -c3-)
  [ -n "$changed" ] && notes+=("Versions changed since last week: $(tr '\n' ';' <<<"$changed")the suite above ran against them.")
fi
echo "$current" > "$STATE/versions.txt"
missing=$(awk '$2 == "" {print $1}' <<<"$current" | tr '\n' ' ')
[ -n "$missing" ] && problems+=("Tools not found on PATH: $missing")

# 3. PHPUnit baselines: a stale baseline hides new failures or blocks on fixed ones.
for b in "$K"/repos/*/phpunit-baseline.txt; do
  age=$(days_since "$b")
  [ "$age" -gt 45 ] && notes+=("$(basename "$(dirname "$b")") PHPUnit baseline is $age days old; refresh with its verify --refresh-baseline.")
done

# 4. Scheduled jobs actually ran.
last=$(cat "$K/review-mining/last-success" 2>/dev/null)
if [ -n "$last" ]; then
  age=$(( ( $(date +%s) - $(date -j -u -f %Y-%m-%dT%H:%M:%SZ "$last" +%s) ) / 86400 ))
  [ "$age" -gt 40 ] && problems+=("Monthly review mining last succeeded $age days ago.")
elif [ "$(date +%d)" -gt 7 ] && [ -d "$K/review-mining/runs/$(date +%Y-%m)" ]; then
  problems+=("Monthly review mining ran this month but never produced a proposal.")
fi
for job in review-mining health trends; do
  launchctl list "com.$(id -un).agents-$job" >/dev/null 2>&1 || problems+=("launchd job com.$(id -un).agents-$job is not loaded; run ~/.agents/install.sh.")
done
[ -s "$K/monitors/state/review-mining.err.log" ] && [ "$(days_since "$K/monitors/state/review-mining.err.log")" -lt 8 ] \
  && notes+=("review-mining wrote to its error log this week: $(tail -1 "$K/monitors/state/review-mining.err.log")")

# 5. Leftovers that should never persist.
[ -e "$K/approvals/linear" ] && [ "$(days_since "$K/approvals/linear")" -ge 1 ] && notes+=("A Linear approval file is older than a day; it is expired but can be removed.")
stale_overrides=$(find "$HOME/Projects" -maxdepth 4 -name 'docker-compose.override.yml' -exec grep -l 'agents: temporary override' {} + 2>/dev/null)
[ -n "$stale_overrides" ] && problems+=("Marked temporary docker overrides left behind: $(tr '\n' ' ' <<<"$stale_overrides")")
old_tmp=$(find "${TMPDIR:-/tmp}/agent-hooks" -type f -mtime +14 2>/dev/null | wc -l | tr -d ' ')
[ "$old_tmp" -gt 0 ] && find "${TMPDIR:-/tmp}/agent-hooks" -type f -mtime +14 -delete 2>/dev/null && notes+=("Removed $old_tmp hook session files older than 14 days.")

# 5b. Cost settings drift: fast modes bill 2-2.5x for the same work.
[ "$(jq -r '.fastMode // false' "$HOME/.claude/settings.json")" = true ] && problems+=("Claude Code fastMode is on in ~/.claude/settings.json.")
grep -qE '^service_tier *= *"(fast|priority)"' "$HOME/.codex/config.toml" && problems+=("Codex service_tier is set to a fast tier.")
grep -qE '^fast_mode *= *false' "$HOME/.codex/config.toml" || problems+=("Codex [features] fast_mode is not false.")
grep -qE '^model_reasoning_effort *= *"ultra"' "$HOME/.codex/config.toml" && notes+=("Codex default effort is ultra, which spawns subagents on every task.")

# 6. Log growth.
for f in "$K"/logs/*.jsonl; do
  kb=$(( $(stat -f %z "$f") / 1024 ))
  [ "$kb" -gt 20480 ] && notes+=("$(basename "$f") is ${kb} KB; consider rotating it.")
done

# 7. Hook activity this week (context for the monthly analysis, never a problem by itself).
since=$(date -u -v-7d +%Y-%m-%dT%H:%M:%S)
activity=$(jq -r --arg s "$since" 'select(.ts >= $s) | "\(.hook) \(.decision)"' "$K/logs/hooks.jsonl" 2>/dev/null | sort | uniq -c | sort -rn)

{
  echo "# Framework health $(date +%F)"
  echo
  echo "## Needs attention"
  [ ${#problems[@]} = 0 ] && echo "Nothing." || printf -- '- %s\n' "${problems[@]}"
  echo
  echo "## Notes"
  [ ${#notes[@]} = 0 ] && echo "Nothing." || printf -- '- %s\n' "${notes[@]}"
  echo
  echo "## Hook activity, last 7 days"
  echo '```'; echo "${activity:-none}"; echo '```'
  echo
  echo "## Regression suite"
  echo '```'; echo "$suite" | grep -E '^(ok|FAIL|==|ALL|FAILURES|[0-9]+/)'; echo '```'
} > "$REPORT"

if [ ${#problems[@]} -gt 0 ]; then
  osascript -e "display notification \"${#problems[@]} problem(s); see ~/.agents/research/health/$(date +%F).md\" with title \"Agent kit health\"" 2>/dev/null || true
  exit 1
fi
[ "${1:-}" = --quiet ] || echo "Healthy. Report: $REPORT"
