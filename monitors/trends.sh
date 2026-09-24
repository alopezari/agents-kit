#!/bin/bash
# Mid-month trends scan: what changed in the harnesses, models and tools, and what the kit could adopt or retire.
# Proposes only; the model gets read and web tools, no write or shell access, and the script saves its answer.
#   ~/.agents/monitors/trends.sh
set -euo pipefail
K="$HOME/.agents"
MONTH=$(date +%Y-%m)
OUT="$K/research/opportunities/$MONTH.md"
PREV=$(ls "$K"/research/opportunities/*.md 2>/dev/null | grep -v "$MONTH" | tail -1 || true)
mkdir -p "$(dirname "$OUT")"

versions=$(cat "$K/monitors/state/versions.txt" 2>/dev/null || echo "unknown")
context=$(cat "$K"/profiles/*/PROFILE.md 2>/dev/null || echo "No profile; judge relevance for general software work.")
claude_model=$(jq -r '"\(.model // "default") advisor=\(.advisorModel // "none")"' "$HOME/.claude/settings.json" 2>/dev/null)
codex_model=$(grep -E '^(model|model_reasoning_effort) *=' "$HOME/.codex/config.toml" 2>/dev/null | tr '\n' ' ')
pi_model=$(jq -r '"\(.defaultModel // .model // "?") thinking=\(.defaultThinkingLevel // "?")"' "$HOME/.pi/agent/settings.json" 2>/dev/null)

prompt=$(cat <<PROMPT
You are scouting for changes that matter to a personal coding-agent kit. Today is $(date +%F).

The kit lives in $K (read README.md first, then AGENTS.md, skills/*/SKILL.md, hooks/, bin/, install.sh as needed).
It runs the same instructions, skills and hooks on Claude Code, Codex CLI and Pi.
Work context from the user's profiles:
$context

Installed versions:
$versions
Models: Claude Code $claude_model; Codex $codex_model; Pi $pi_model.
Deliberate choices and their evidence are in research/ (e.g. routing-analysis-*.md for models and effort); flag a change against one only when new evidence contradicts it.
Previous scan: ${PREV:-none}. Read it and do not repeat items unless something changed.

Search the web (official changelogs, release notes and docs first; then engineering blogs and discussions) for the last ~6 weeks:
1. Claude Code, Codex CLI and Pi releases: hook events or payload changes, new settings, skills/plugins changes,
   subagent or review features, anything that breaks or duplicates what hooks/ or install.sh does.
2. Model releases and deprecations from Anthropic and OpenAI relevant to coding agents; changes to effort or advisor behavior.
3. Tool releases: semgrep, gitleaks, Playwright CLI, agent-browser, PHPStan, phpcs/WPCS, axe-core.
4. Practice: new, evidenced techniques for agentic coding quality (review, testing, verification, context management).
   Prefer sources with data over opinion.

Write the report in English, as Markdown, and print it as your final answer and nothing else:
- "# Opportunities $MONTH", then a 3-line summary.
- "## Ranked opportunities": a table (rank, change, source link, what it means for the kit, effort S/M/L, value H/M/L).
  Rank by value over effort. Include kit parts that a native feature could now replace (removing code is a win).
  Before listing an item, search the kit's current files for it: if the kit already does it, leave it out of the table.
- "## Already in the kit": one line per candidate you left out for that reason, with the file:line that shows it.
- "## Breaking or risky changes": anything that could make a hook, adapter or skill silently stop working, with the file it affects.
- "## New models": if a new model shipped for any harness, recommend re-running the prompt audit
  (/claude-api prompt-audit on AGENTS.md and skills/) and say what to test.
- "## Watch list": things too early to adopt.
Every claim needs a link. Mark anything you could not confirm. Propose; never edit files.
PROMPT
)

cd "$K"
claude -p "$prompt" --safe-mode --no-session-persistence --model opus \
  --allowedTools "Read" "Grep" "Glob" "WebSearch" "WebFetch" \
  --disallowedTools "Bash" "Edit" "Write" "NotebookEdit" \
  > "$OUT.tmp" 2> "$K/monitors/state/trends.err.log" || true

if grep -q "^# Opportunities" "$OUT.tmp" 2>/dev/null; then
  mv "$OUT.tmp" "$OUT"
  osascript -e "display notification \"Opportunities ready: ~/.agents/research/opportunities/$MONTH.md\" with title \"Agent kit trends\"" 2>/dev/null || true
else
  osascript -e 'display notification "Trends scan failed; see ~/.agents/monitors/state/trends.err.log" with title "Agent kit trends"' 2>/dev/null || true
  exit 1
fi
