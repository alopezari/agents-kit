#!/bin/bash
# Monthly review mining: fetch new human review comments, have Claude classify
# them and propose AGENTS.md changes as a diff. Never edits AGENTS.md itself.
set -euo pipefail

BASE="$HOME/.agents/review-mining"
MONTH=$(date +%Y-%m)
RUN="$BASE/runs/$MONTH"
PROPOSAL="$HOME/.agents/research/proposals/$MONTH.md"
SINCE_FILE="$BASE/last-success"
SINCE=$(cat "$SINCE_FILE" 2>/dev/null || date -u -v-35d +%Y-%m-%dT%H:%M:%SZ)
MAX_PAGES=20

mkdir -p "$RUN"
: > "$RUN/comments.jsonl"

# Repositories to mine come from the profiles (profiles/<name>/review-mining/repos.txt, one owner/repo per line).
cat "$HOME"/.agents/profiles/*/review-mining/repos.txt 2>/dev/null | grep -v '^\s*#' | grep -v '^\s*$' | sort -u | while read -r repo; do
  for page in $(seq 1 $MAX_PAGES); do
    batch=$(gh api "repos/$repo/pulls/comments?sort=created&direction=desc&since=$SINCE&per_page=100&page=$page") || break
    [ "$(jq length <<<"$batch")" = 0 ] && break
    jq -c --arg repo "$repo" '.[]
      | select(.user.type == "User" and .in_reply_to_id == null and (.body | length) >= 25)
      | select(.user.login | test("\\[bot\\]$|copilot|coderabbit|claude|codex|gemini"; "i") | not)
      | {id: ("c" + (.id|tostring)), repo: $repo, pr: (.pull_request_url | split("/") | last),
         path, reviewer: .user.login, body: (.body | .[0:1200]), hunk: (.diff_hunk | .[-400:])}' <<<"$batch" >> "$RUN/comments.jsonl"
    [ "$(jq length <<<"$batch")" -lt 100 ] && break
  done
done

# Flag PRs whose description says an agent wrote them.
jq -r '"\(.repo) \(.pr)"' "$RUN/comments.jsonl" | sort -u | while read -r repo pr; do
  body=$(gh api "repos/$repo/pulls/$pr" --jq '.body // ""' 2>/dev/null || true)
  if grep -qiE 'generated with \[?claude|co-authored-by: claude|codex|drafted with ai|🤖 generated' <<<"$body"; then
    echo "$repo $pr"
  fi
done > "$RUN/agent-prs.txt"

[ "${FETCH_ONLY:-}" = 1 ] && { wc -l "$RUN/comments.jsonl" "$RUN/agent-prs.txt"; exit 0; }

COUNT=$(wc -l < "$RUN/comments.jsonl" | tr -d ' ')
# Few comments still leave the logs and sessions worth analysing; only the comment steps are skipped.
COMMENT_NOTE=""
[ "$COUNT" -lt 50 ] && COMMENT_NOTE="Only $COUNT comments this month: skip steps 1-3 and say so in the proposal."

python3 "$HOME/.agents/usage/extract_sessions.py" 200 "$RUN/sessions.json" > "$RUN/sessions.log" 2>&1 || true
# Every session, not the 200 above: joining sessions to merged PRs needs the ones before the kit too.
python3 "$HOME/.agents/usage/extract_sessions.py" 100000 "$RUN/sessions-all.json" > /dev/null 2>&1 || true
python3 "$BASE/outcomes.py" "$SINCE" "$RUN/sessions-all.json" "$RUN/outcomes.json" > "$RUN/outcomes.log" 2>&1 || true

cat > "$RUN/prompt.md" <<EOF
You are updating the evidence behind a set of coding-agent instructions.

Inputs (read them from disk):
- $RUN/comments.jsonl: $COUNT human review comments from GitHub since $SINCE (id, repo, pr, path, reviewer, body, hunk).
- $RUN/agent-prs.txt: "repo pr" lines for PRs whose description says an AI agent wrote them.
- $BASE/taxonomy.md: category codes.
- $HOME/.agents/AGENTS.md: the current global instructions.
- $HOME/.agents/research/review-findings.md: the previous analysis, when a profile provides one.
- $HOME/.agents/logs/hooks.jsonl: every hook block/nudge (hook, decision, session, cwd, detail).
- $HOME/.agents/logs/quality.jsonl: one line per review lens or test type run (tier, findings/confirmed or issues, secs, model).
- $HOME/.agents/logs/browser-ab.jsonl: A/B test of browser tools in the validate skill (assign/call/finish events per run).
- $RUN/sessions.json: my last 200 agent sessions (harness, model, effort, tokens, advisor_calls, prompts, correction_signals).

- $HOME/.agents/research/health/*.md: weekly health reports from this month.
- $RUN/outcomes.json: my merged PRs since the kit started and in a baseline window before it (see $BASE/outcomes.py
  for every field), with per-period summaries; $RUN/outcomes.log has its errors, if any.
$COMMENT_NOTE

Steps:
1. Split comments.jsonl into chunks of about 300 lines and classify every comment with parallel subagents (model: sonnet), each writing its own output file in $RUN with {"id","cat","agent","rule"} per line, as defined in taxonomy.md. Give each subagent a unique scratch filename. Verify every id is classified exactly once.
2. Aggregate: category counts overall, and separately for comments on agent-authored PRs. Compare with the previous analysis and name what changed.
3. Decide what the evidence supports: new recurring defects, rules that no longer show up, wording that reviewers keep contradicting. A rule needs a pattern across several PRs, not one comment. For every recurring defect, push it down this ladder as far as it goes, and propose the lowest rung that works:
   a. make it impossible (an API or helper the team could adopt; propose, never apply);
   b. a deterministic check: a semgrep rule in $HOME/.agents/repos/_shared/wordpress.semgrep.yml or a repo's verify script under $HOME/.agents/repos/ (draft the rule in $RUN, test it against a synthetic example, and include it in the proposal as a diff; never edit the kit's files);
   c. a regression or property test the team could add;
   d. required evidence in the self-review lenses ($HOME/.agents/skills/self-review/lenses.md);
   e. a sentence in AGENTS.md, only when nothing above fits. Keep AGENTS.md short.
4. From quality.jsonl, add a short "Review and test perspectives" section: per lens and test type, how often triage selected it, findings vs confirmed findings, and time. Recommend dropping or merging a perspective with no confirmed findings over ~20 runs, and tightening a trigger (in ~/.agents/bin/triage) that fires often with nothing found. Propose these as diffs.
   From browser-ab.jsonl, add a short "Browser A/B" section per tool: runs, commands per run, output bytes per run (context cost), failed calls, wall time, checks passed/completed, tool issues and notes. Also count, from hooks.jsonl (decision "browser-mcp"), sessions that drove a browser through an MCP instead of bin/browse, and which of them look like validation: those runs are missing from the test. The test ends on 2026-11-05 or at 10 finished runs per tool, whichever comes first. Recommend one tool only when both tools have at least 10 finished runs; otherwise report the result as inconclusive, give the counts, and propose a new end date (and, if MCP bypasses explain the gap, how to stop them).
   From hooks.jsonl, add a short "Hooks" section: which rules fired, how often, which look like false positives (the agent or the user worked around them) and which caught real problems; propose removing or narrowing noisy rules.
   From sessions.json, add a short "Models and effort" section: sessions per model/effort, how often the advisor (Fable) was consulted and whether sessions that used it had fewer correction signals, how sessions that used effort "auto" compare, and which tasks look over- or under-provisioned. Compare cost per completed task, never per token. Treat it as evidence, not proof.
   Add an "Outcomes" section from outcomes.json: merged PRs since the kit per model and effort (from the sessions
   joined to each PR), with tokens and hours to merge per PR. This is the closest measure of finished work; set it
   against tokens per session in sessions.json. Small samples are evidence, not proof.
   Add an "Escapes" section from outcomes.json, set against its baseline:
   - At the PR stage: the escapes follow-pr logged (CI failures the change caused, review comments by people and bots)
     per merged PR, by category and verdict. For each confirmed escape, name the lens that should have caught it and
     whether it ran on that branch. A lens that ran and still let its category through gets a proposed fix to its
     questions in lenses.md; a lens that never catches anything is a candidate to drop (weigh it with quality.jsonl).
     Rejected comments per bot measure that bot's noise.
   - After merge: judge every follow_ups candidate from its title and shared files. Count it only when it plausibly
     fixes or reverts the original PR (not a lint sweep, a rename across the codebase, or a word that only looks like
     "fix"), and list the ones you count with their URLs. Separately, for the PRs follow-pr handled: a PR with no
     escapes that later got a real follow-up means review missed something; say what, if the titles show it.
   - Before and after: compare the per-PR numbers (review threads, changes requested, red pushes, commits after the
     first review, real follow-ups) between baseline and since_kit, with the PR counts. Few PRs since the kit make
     any difference noise; say so instead of drawing a conclusion. The baseline counts every PR; the kit numbers can't
     beat it by excluding hard ones.
   Add a "Cost per merged PR" section from the sessions joined to PRs: tokens (input+output; cache reads separately),
   wall-clock minutes (idle included), hours from the first session to the PR and from the PR to the merge, per
   period and per model and effort where sessions.json has them. Compare cost per merged PR, never per session.
   Add a "Framework friction" section: where the kit itself slowed work down or was bypassed. Count, with example sessions:
   blocks the agent worked around or the user overrode (a deny followed by the same intent in another form), verify or
   self-review steps reported as NOT RUN and why, time per tier against the triage budgets (low <5 min, standard <=20,
   high <=45), user corrections right after a kit step (spec, self-review, validate, a hook), and repeated health-report
   problems. For each, propose the smallest fix: narrow a rule, fix a tool, change a budget, or drop a step.
   End with "What works": the kit steps with evidence of catching real problems, so they are not removed by mistake.
5. Write $PROPOSAL in English: a short summary, the numbers, the evidence for each proposed change with 2-3 example comment ids, and the proposed change as a unified diff against $HOME/.agents/AGENTS.md. If nothing is worth changing, say so.

Do not edit AGENTS.md or any file outside $RUN and $PROPOSAL. Do not call GitHub or any other remote service. Never name reviewers.
EOF

cd "$RUN"
claude -p "$(cat "$RUN/prompt.md")" --safe-mode --no-session-persistence --model opus \
  --permission-mode bypassPermissions --add-dir "$HOME/.agents" \
  --disallowedTools "Bash(gh:*)" "Bash(git push:*)" "Bash(curl:*)" "WebFetch" "WebSearch" \
  > "$RUN/claude.log" 2>&1

if [ -s "$PROPOSAL" ]; then
  date -u +%Y-%m-%dT%H:%M:%SZ > "$SINCE_FILE"
  osascript -e "display notification \"Proposal ready: ~/.agents/research/proposals/$MONTH.md\" with title \"Agent review mining\"" || true
else
  osascript -e 'display notification "Run failed; see ~/.agents/review-mining/runs" with title "Agent review mining"' || true
  exit 1
fi
