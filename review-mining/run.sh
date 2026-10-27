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
# No profile, or lists with only comments, is an empty list, not a failure.
repos=$(cat "$HOME"/.agents/profiles/*/review-mining/repos.txt 2>/dev/null | grep -Ev '^[[:space:]]*(#|$)' | sort -u || true)
[ -n "$repos" ] && while read -r repo; do
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
done <<<"$repos"

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
# Removed first: a failed rerun this month must leave no summary rather than the previous run's numbers.
rm -f "$RUN/test-types.json"
python3 "$BASE/test_types.py" "$RUN/test-types.json" > "$RUN/test-types.log" 2>&1 || true
# Replay the escaped bugs against both reviewers: up to 10 bug and 10 clean cases each, about one review pass per case.
# The two models run side by side: one after the other can take hours before the analysis starts.
for model in codex claude; do
  { python3 "$BASE/eval.py" run --model "$model" --limit 10 || echo "eval.py run --model $model failed with exit $?"; } \
    > "$RUN/evals-$model.txt" 2>&1 &
done
wait
cat "$RUN/evals-codex.txt" "$RUN/evals-claude.txt" > "$RUN/evals.txt"

cat > "$RUN/prompt.md" <<EOF
You are updating the evidence behind a set of coding-agent instructions.

Inputs (read them from disk):
- $RUN/comments.jsonl: $COUNT human review comments from GitHub since $SINCE (id, repo, pr, path, reviewer, body, hunk).
- $RUN/agent-prs.txt: "repo pr" lines for PRs whose description says an AI agent wrote them.
- $BASE/taxonomy.md: category codes.
- $HOME/.agents/AGENTS.md: the current global instructions.
- $HOME/.agents/research/review-findings.md: the previous analysis, when a profile provides one.
- $HOME/.agents/logs/hooks.jsonl: every hook block/nudge (hook, decision, session, cwd, detail).
- $HOME/.agents/logs/quality.jsonl: one line per review lens or test type run (tier, findings/confirmed or issues, secs, model, for a lens the outcomes of its findings, and for a test "flake": true when it hit a flake), per escape follow-pr logged, per branch rename (kind "rename": name is the old branch, to the new one; not a perspective), and per push to an open PR (kind "pr": name is the PR number, with that change's size, tests touched and deleted, verify verdict, red check, diff coverage and weakened tests; outcomes.json joins the last one before the merge to each PR as its signals).
- $HOME/.agents/logs/build-ab.jsonl: A/B test of the build's model (one assign event per branch: repo, branch, tier, model, harness).
- $RUN/sessions.json: my last 200 agent sessions (harness, model, effort, tokens, advisor_calls, prompts, correction_signals).
- $RUN/sessions-all.json: the same for every session, read for the A/B's builds.

- $HOME/.agents/research/health/*.md: weekly health reports from this month.
- $RUN/evals.txt: this month's review evals, per model and lens: recall over escaped bugs replayed against the
  reviewers, Important findings per clean case, errors and skips, beside the previous run's numbers.
- $RUN/outcomes.json: my merged PRs since the kit started and in a baseline window before it (see $BASE/outcomes.py
  for every field), with per-period summaries; $RUN/outcomes.log has its errors, if any.
- $RUN/test-types.json: quality.jsonl's test rows per test type (see $BASE/test_types.py), with older spellings folded
  into the canonical names and unknown names listed; $RUN/test-types.log has its errors, if any.
$COMMENT_NOTE

Steps:
1. Classify every comment with parallel subagents (model: sonnet), each reading its own range of about 300 lines of comments.jsonl and writing its own output file in $RUN with {"id","cat","agent","rule"} per line, as defined in taxonomy.md. Give each subagent a unique scratch filename. Verify every id is classified exactly once.
2. Aggregate: category counts overall, and separately for comments on agent-authored PRs. Compare with the previous analysis and name what changed.
3. Decide what the evidence supports: new recurring defects, rules that no longer show up, wording that reviewers keep contradicting. A rule needs a pattern across several PRs, not one comment. For every recurring defect, push it down this ladder as far as it goes, and propose the lowest rung that works:
   a. make it impossible (an API or helper the team could adopt; propose, never apply);
   b. a deterministic check: a semgrep rule in $HOME/.agents/repos/_shared/wordpress.semgrep.yml or a repo's verify script under $HOME/.agents/repos/ (write each rule to $RUN/rules/<name>.yml, a synthetic example it must flag to $RUN/rules/<name>.bad.<ext> and one it must not flag to $RUN/rules/<name>.good.<ext>, and put the rule in the proposal as a diff. You can't run semgrep: when you finish, run.sh checks each rule against its examples and adds the results to the proposal. Never edit the kit's files);
   c. a regression or property test the team could add;
   d. required evidence in the review lenses ($HOME/.agents/skills/kit-review/lenses.md);
   e. a sentence in AGENTS.md, only when nothing above fits. Keep AGENTS.md short.
4. From quality.jsonl, add a short "Review and test perspectives" section: per lens and test type, how often triage selected it, findings vs confirmed findings, and time. Take the test-type counts from test-types.json, not from quality.jsonl's raw names: per canonical type, runs, runs with an issue, issues and time (secs covers only its timed_runs: give time per timed run, and how many runs logged none), with flaky runs reported separately (a flake is a test that failed, then passed on a rerun without a code change, and is never an issue: a flaky run counts as a run with an issue only when it also found a real one), and name any unknown types it lists. If test-types.json is missing, say test statistics are unavailable and point at $RUN/test-types.log. The spec lens is the spec's second reading: it runs with every spec, not from triage, so it has no trigger to tighten. Lines named correctness-recheck are the review's re-check rounds, not triage selections: report them apart from correctness. Its lines carry the spec's number of acceptance criteria: set specs outside 3–6 against the rest by C.requirement escapes on the same branch, and propose making the lint fail outside that range only when the gap shows across several PRs. Lines logged since the Change map existed also carry map_items, its number of items (0 for none): compare confirmed Correctness findings per PR with a map against PRs without one, joining a spec logged on a session branch to its PR through the rename lines, and leave out lines without the field. Lens lines with a "shared" field measure the cross-model pass: from those lines only, per lens and per reviewer role (harness claude-code with model codex is Codex as the second reader; harness codex with model claude the reverse), give each family's confirmed findings the other didn't report (confirmed minus shared) and minutes per such finding, writing "none unique" instead of dividing by zero; a line without the field is unknown overlap, never zero. Lens lines logged since outcomes existed carry an outcomes object (not the merged-PR outcomes of outcomes.json), what became of each finding (fixed, wrong, intentional, minor, pre-existing, open, uncertain): per lens and model, over the lines that carry it, give the noise rate (the sum of wrong plus intentional over the sum of findings), the pre-existing share and the minor share over the same sum, n/a when that sum is 0, and how many lines had no outcomes. A lens or model whose noise stays high across several PRs gets a proposed fix to its questions in lenses.md; a high pre-existing share means reviewers read old code instead of the change. Recommend dropping or merging a perspective with no confirmed findings over ~20 runs, and tightening a trigger (in ~/.agents/bin/triage) that fires often with nothing found. Propose these as diffs.
   Also from quality.jsonl, add a short "Spec risk predictions" section: per branch, its rows' spec_tier and diff_tier (rows without a spec_tier had no prediction), how often the diff came out above the spec, by tier pair, with example branches. A spec "low" under a "standard" diff is expected for a small behavior change; a diff "high" under a lower spec is a missed prediction. When one pair keeps recurring, propose a change to the tier rules in kit-spec's SKILL.md.
   From build-ab.jsonl and sessions-all.json, add a short "Build model A/B" section: the first build of a change goes to
   kit:build-<tier> on Sonnet or Opus, effort held at the tier's value. Per tier and assigned arm: builds assigned;
   builds found in $RUN/sessions-all.json, every session rather than the last 200 (each session's "builds", joined to an assignment by the build's repo and branch,
   through the rename lines in quality.jsonl; grouped by the assignment's tier, noting a build whose agent type names
   another); and builds whose transcript model differs from the
   arm, listed and left out of the comparison. For the rest, per build: tokens by type and the extractor's usd, advisor calls included
   (null means a call the price table can't price: another model, fast mode or US-only inference; say so rather than
   counting it as 0), and, joined by repo and branch through
   the renames, the confirmed review findings on that branch (quality.jsonl lens lines), its escapes and its merge
   outcome (outcomes.json). A branch with no review lines, no follow-pr run or no PR yet is unobserved for that
   measure, never zero: give those counts apart. The test ends on 2026-11-13. Under 5 compared builds per arm in a
   tier, report that tier as inconclusive with its counts; otherwise say whether Opus's extra dollars per build bought
   fewer findings and escapes, as evidence, not proof, and propose the tier's model for the build agent.
   From hooks.jsonl, add a short "Hooks" section: which rules fired, how often, which look like false positives (the agent or the user worked around them) and which caught real problems; propose removing or narrowing noisy rules. Count the browser-MCP lines (decision "browser-mcp") apart: sessions that drove a browser through an MCP instead of agent-browser, and which of them look like validation.
   From sessions.json, add a short "Models and effort" section: sessions per model/effort, how often the advisor (Fable) was consulted and whether sessions that used it had fewer correction signals, how sessions that used effort "auto" compare, and which tasks look over- or under-provisioned. Compare cost per completed task, never per token. Treat it as evidence, not proof.
   Add an "Outcomes" section from outcomes.json: merged PRs since the kit per model and effort (from the sessions
   joined to each PR), with tokens and hours to merge per PR. This is the closest measure of finished work; set it
   against tokens per session in sessions.json. Small samples are evidence, not proof.
   Add an "Attention" section from outcomes.json's sessions_joined.attention and each PR's sessions.attention: the
   user's share of a merged PR (prompts, the agent's questions, corrections, wait_minutes idle until each reply,
   reply_minutes with each gap capped at 15 minutes, a guess at the user's time rather than a measurement), baseline
   against this period (since_kit covers the window field, not every PR since the kit), always with the complete,
   partial and unknown PR counts, as means and medians cover complete PRs only; lead with the medians, as one
   overnight wait skews a mean. Name the PRs and phases that took the most
   prompts and wait, and what in them asked for the user (a question, a correction, a report to read). Hours to PR
   and to merge are the cycle time to set it against. Few complete PRs make this evidence, not proof.
   Add a "Signals" section from since_kit.signals in outcomes.json: per split (size band by lines added plus deleted,
   tier, red check, diff coverage, weakened tests, tests deleted, tests touched), each bucket's settled PRs and its fix
   candidates, revert candidates and confirmed escapes with their rates, and the PRs without signals apart, never as
   clean ones. It covers signals_window, which starts FOLLOW_UP_DAYS before window to take in the PRs still pending at
   the previous run. Judge fix and revert candidates as the Escapes section does before reading a rate as trouble. A
   bucket under ~10 PRs proves nothing: give its counts and say so instead of drawing a conclusion; name a signal as
   predicting trouble only when the gap holds across buckets with enough PRs, and say which signal to watch next.
   Add an "Escapes" section from outcomes.json, set against its baseline:
   - At the PR stage: the escapes follow-pr logged (CI failures the change caused, review comments by people and bots)
     per merged PR, by category and verdict. For each confirmed escape, name the lens that should have caught it and
     whether it ran on that branch. A lens that ran and still let its category through gets a proposed fix to its
     questions in lenses.md, or for the spec lens in $HOME/.agents/skills/kit-spec/review-prompt.sh; a lens that never
     catches anything is a candidate to drop (weigh it with quality.jsonl).
     Rejected comments per bot measure that bot's noise.
   - After merge: judge every follow_ups candidate from its title and shared files. Count it only when it plausibly
     fixes or reverts the original PR (not a lint sweep, a rename across the codebase, or a word that only looks like
     "fix"), and list the ones you count with their URLs. Separately, for the PRs follow-pr handled: a PR with no
     escapes that later got a real follow-up means review missed something; say what, if the titles show it.
   - Before and after: compare the per-PR numbers (review threads, changes requested, red pushes, commits after the
     first review, real follow-ups) between baseline and since_kit, with the PR counts. Few PRs since the kit make
     any difference noise; say so instead of drawing a conclusion. The baseline counts every PR; the kit numbers can't
     beat it by excluding hard ones.
   Add a "Review evals" section from evals.txt: per model and lens, recall and Important findings per clean case
   beside the previous run's, and whether the cases were the same. When they were and recall fell or false alarms rose,
   name the changes to lenses.md, bundle.sh or the models since the previous run and propose reverting the likely
   cause. Then name up to three merged PRs from outcomes.json with no escapes and follow_ups settled with none you
   counted, as clean-case candidates, each with the command for a person to fill in and run (never add cases yourself):
   \`$BASE/eval.py add --checkout <the repo's checkout> --sha <merge commit> --base <its first parent> --lens Correctness --goal "<PR title>" --clean\`.
   Add a "Cost per merged PR" section from the sessions joined to PRs: tokens (input+output; cache reads separately),
   wall-clock minutes (idle included), hours from the first session to the PR and from the PR to the merge, per
   period and per model and effort where sessions.json has them. Compare cost per merged PR, never per session.
   Add a "Cost per phase" section from sessions_joined.per_phase and each PR's sessions.phases: per phase of the flow
   (build, spec, self-review, validate, create-pr, follow-pr, ship), output tokens, cache reads and writes, and active
   minutes (gaps over 15 minutes left out) per merged PR, next to the escapes that phase let through. Point at the
   phase that costs most for what it catches. Phases come from Claude Code transcripts only; say how many PRs had them.
   Add a "Framework friction" section: where the kit itself slowed work down or was bypassed. Count, with example sessions:
   blocks the agent worked around or the user overrode (a deny followed by the same intent in another form), verify or
   review steps reported as NOT RUN and why, time per tier against the triage budgets (low <5 min, standard <=20,
   high <=45), user corrections right after a kit step (spec, review, validate, a hook), and repeated health-report
   problems. For each, propose the smallest fix: narrow a rule, fix a tool, change a budget, or drop a step.
   End with "What works": the kit steps with evidence of catching real problems, so they are not removed by mistake.
5. Write $PROPOSAL in English: a short summary, the numbers, the evidence for each proposed change with 2-3 example comment ids, and the proposed change as a unified diff against $HOME/.agents/AGENTS.md. If nothing is worth changing, say so.

Do not edit AGENTS.md or any file outside $RUN and $PROPOSAL. Do not call GitHub or any other remote service. Never name reviewers.
EOF

cd "$RUN"
mkdir -p "$(dirname "$PROPOSAL")"
# The comments are other people's text, so the model gets no network and no shell: even an allowed command like
# semgrep can fetch rules or rewrite files. --restricted keeps file tools inside the working directories, and
# dontAsk refuses anything the rules don't allow. It reads the kit; it writes only this run and the proposal.
previous=$(cksum "$PROPOSAL" 2>/dev/null || true)
rm -rf "$RUN/rules"  # rules from an earlier run this month would be checked as this run's
claude -p "$(cat "$RUN/prompt.md")" --safe-mode --restricted --strict-mcp-config --no-session-persistence --model opus \
  --add-dir "$HOME/.agents" --tools "Read,Grep,Glob,Write,Edit,Agent" --permission-mode dontAsk \
  --allowedTools "Edit(/$RUN/**)" "Edit(/$PROPOSAL)" \
  > "$RUN/claude.log" 2>&1

# The model can't run code, so its semgrep rules are checked here: each must flag its .bad example and nothing
# in its .good one. The rules come from a model that read other people's text, so semgrep runs with the network
# denied by the macOS sandbox and a time limit.
offline_semgrep() {
  sandbox-exec -p '(version 1)(allow default)(deny network*)' perl -e 'alarm shift; exec @ARGV' 30 \
    semgrep --metrics=off --disable-version-check --timeout 30 --json "$@"
}
check_rules() {
  local rule name bad good example count=0
  printf '\n## Semgrep rules checked by run.sh\n\n'
  if ! command -v semgrep >/dev/null; then echo "semgrep is not installed, so no rule was checked."; return; fi
  for rule in "$RUN"/rules/*.yml; do
    [ -f "$rule" ] || continue
    count=$((count + 1)); [ $count -gt 20 ] && { echo "- More than 20 rules; the rest weren't checked."; break; }
    name=$(basename "$rule" .yml)
    bad=""; good=""
    for example in "$RUN/rules/$name".bad.*; do [ -f "$example" ] && bad=$example; done
    for example in "$RUN/rules/$name".good.*; do [ -f "$example" ] && good=$example; done
    if [ -z "$bad" ] || [ -z "$good" ]; then echo "- $name: NO EXAMPLE (needs $name.bad.* and $name.good.*)"; continue; fi
    echo "- $name: $(rule_result "$rule" "$bad" "$good")"
  done
  [ $count -gt 0 ] || echo "No rules were drafted."
}

rule_result() {  # <rule> <bad example> <good example>
  local out example bad_hits good_hits
  for example in "$2" "$3"; do
    out=$(offline_semgrep --config "$1" "$example" 2>/dev/null || true)
    if ! jq -e '.errors | length == 0' <<<"$out" >/dev/null 2>&1; then
      echo "ERROR ($(jq -r '.errors[0].message // "semgrep failed or took over 30s"' <<<"$out" 2>/dev/null | head -1 | cut -c1-120))"
      return
    fi
    if ! jq -e --arg f "/$(basename "$example")" 'any(.paths.scanned[]; ("/" + .) | endswith($f))' <<<"$out" >/dev/null; then
      echo "ERROR (semgrep skipped $(basename "$example"); use an extension its language scans)"; return
    fi
    if [ "$example" = "$2" ]; then bad_hits=$(jq '.results | length' <<<"$out"); else good_hits=$(jq '.results | length' <<<"$out"); fi
  done
  if [ "$bad_hits" -gt 0 ] && [ "$good_hits" = 0 ]; then echo "PASS (flags the bad example, not the good one)"
  elif [ "$bad_hits" = 0 ]; then echo "FAIL (misses its bad example)"
  else echo "FAIL (flags its good example $good_hits times)"; fi
}

# A proposal left by an earlier run this month is not this run's success.
if [ -s "$PROPOSAL" ] && [ "$(cksum "$PROPOSAL")" != "$previous" ]; then
  check_rules >> "$PROPOSAL"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$SINCE_FILE"
  osascript -e "display notification \"Proposal ready: ~/.agents/research/proposals/$MONTH.md\" with title \"Agent review mining\"" || true
else
  osascript -e 'display notification "Run failed; see ~/.agents/review-mining/runs" with title "Agent review mining"' || true
  exit 1
fi
