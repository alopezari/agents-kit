---
name: self-review
description: Adversarial multi-lens review of your own diff before it goes to a human, using focused reviewers and a second model family, then verifying every finding before acting on it. Use before opening or updating a pull request, before declaring a non-trivial change done, or when asked to review the current branch.
effort: high
---

# Self-review

Review your own change the way a skeptical senior reviewer would, before a human spends time on it. The goal is to find real defects; step 3 separates them from false alarms.

When `~/.agents/bin/phase switches` lists `self-review`, the user switched this phase off: say so in a line and skip it, unless they asked for it by name. With `second-model` listed, or the phase of the reviewer you'd call (`codex-review` when you run on a Claude model, `claude-review` on an OpenAI model), run no cross-model reviewer, whatever the tier (triage then says `cross-model: none (<that phase> switched off)`): the same-model lenses only, `cross-model: switched off` in the report's first line and `Not run: cross-model, <that phase> switched off by the user` in the report. When triage can't tell which harness runs it, it adds `(unless the reviewer is off: <phases>)`: check the phase of the reviewer you'd call yourself.

Two things make self-review work:

1. **Focus.** One reviewer checking everything finds the obvious. Several reviewers, each with one lens, find more.
2. **Independence.** The author's model shares the author's blind spots. A reviewer from another model family, and evidence from running the code, catch what the author can't.

## 1. Scope the diff

- Base: `python3 ~/.agents/hooks/review_stamp.py base` prints it: the branch recorded in `branch.<name>.agentsBase`, else the nearest pushed branch this one is stacked on, else the default branch. It is the base the stamps and triage measure against, so the review covers the same change the PR gate checks. Use `git merge-base HEAD <base>`.
- Review committed and uncommitted changes together: `git diff <merge-base>` plus untracked files.
- Goal: if `~/.agents/skills/spec/path.sh` points at an existing file, that spec's goal and acceptance criteria are the goal every reviewer gets. Otherwise, state the goal in one or two sentences.
- For a check that stops something (a guard, validation, permission, rate limit), the goal says who it stops: an honest mistake, a careless model or user, or a deliberate attacker. Without it, reviewers report bypasses the check was never meant to stop, and each round goes to rejecting them.
- Run `~/.agents/bin/triage` in the checkout. It prints the risk tier, the lenses to run with the signal behind each, how much to send cross-model, and a time budget. Run what it selects. Add a lens it missed only when you can name the risk, and say so in the report.

## 2. Run the lenses

Lens definitions are in `lenses.md` next to this file. Triage picks the lenses; the tier decides who runs them:

| Tier | Reviewer |
|---|---|
| Low | Yourself, one pass, with the review guide below |
| Standard | One subagent per selected lens, in parallel, plus one cross-model pass on Correctness |
| High | One subagent per selected lens, plus a cross-model pass per lens |

Stay within triage's time budget. If a lens would need much longer, review the riskiest part and say what you left out.

Give each reviewer: the lens text, the base ref, the task or PR goal in one or two sentences, the spec's path when there is one (its Change map is where the Correctness tables start), and this instruction: *report every issue you suspect, each with file:line, the failing scenario, a severity and a confidence (high/medium/low); low-confidence findings are welcome, because step 3 verifies them. Severity is one of: Important (a defect the change introduces, or makes reachable), Nit (a minor point; at most three), or Pre-existing (already on the base branch, and neither introduced nor newly reached by the change). Report a defect as a class: search the change for every other place with the same pattern and list each file:line under that one finding.* Reviewers are read-only and never edit files.

A repository can have a review guide, `~/.agents/repos/$(~/.agents/bin/repo-name)/review.md`, in the user's overlay next to `notes.md`: what failure looks like there, what to skip, a different nit cap. Every reviewer applies it, and it wins over these instructions where they conflict. `bundle.sh` includes it and the `kit:review-*` agents read it themselves; give a general subagent its path, and read it yourself on the Low tier.

In Claude Code with the kit plugin, each lens is an agent type: spawn `kit:review-<lens>`, with triage's name for the lens (`kit:review-correctness`, `kit:review-ux-a11y-i18n`). It already holds the lens text, has no edit tools and leaves out CLAUDE.md, so give it the rest of the list above. `bundle.sh` below takes the lens's heading instead (`Correctness`, `"Performance and scale"`). Where that type isn't listed (Codex, or the plugin off), give a general subagent the lens text too.

The Correctness lens always runs, at every risk level, and its tables go into the final report. Also run the repo's personal checks when they exist, with `python3 ~/.agents/hooks/stop_checks.py verify`, which runs `~/.agents/repos/<repo>/verify` and stamps a pass.

### Cross-model reviewer

Skipped when `second-model`, or the reviewer's own phase, is switched off (see the top). Otherwise, use the other model family from the model you're running on. Commit the change first: step 4's re-check uses that commit as its base.

- **Running on a Claude model →** Codex. Pass the whole change in the prompt; Codex's own command runner can fail in non-interactive runs, and a reviewer that can't read the code returns nothing. A pass takes about ten minutes, so start it as a background command (in Claude Code, `run_in_background`) as soon as the diff is ready, and run the same-model lenses meanwhile; for behavior changes, start validate's local checks too. When it finishes, read only its `## Found` section (its tables and findings); the rest is its working, and every line read stays in context. No section means the pass failed: read the error file and report it as a failed reviewer, not as no findings:
  ```bash
  out=$(mktemp "${TMPDIR:-/tmp}/codex-review.XXXXXX")   # background command:
  ~/.agents/skills/self-review/bundle.sh <base> Correctness "<goal>" \
    | codex exec --ephemeral --skip-git-repo-check -s read-only - > "$out" 2> "$out.err"
  awk '/^## Found$/ { section = "" } { section = section $0 "\n" } END { if (section ~ /^## Found/) printf "%s", section }' "$out"
  ```
  Run it once per lens you send cross-model. For a very large diff, split it by directory and review the riskiest parts.
- **Running on an OpenAI model (Codex) →** Claude, from the same bundle, so it gets the same instructions:
  ```bash
  ~/.agents/skills/self-review/bundle.sh <base> Correctness "<goal>" \
    | claude -p --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)" \
      --disallowedTools "Edit,Write"
  ```

If the other CLI isn't installed or fails, say so in the report and continue with same-model reviewers. Don't skip the review.

## 3. Verify every finding

Reviewers produce false positives, and a fix for a non-bug is a new bug. For each finding:

1. Reproduce it: read the code path end to end, or better, write or run a test that fails.
2. Give it exactly one verdict, each with evidence you observed, not an argument:
   - **Confirmed**: a failing test or command output, or the file:line trace that reaches the defect.
   - **Rejected**: the file:line where it's already handled, or a test or command that shows the scenario can't happen. A bypass only an actor outside the check's stated threat model would write is rejected with that reason.
   - **Uncertain**: the concern, and the evidence that would settle it.

   A verdict without evidence is uncertain. Forcing this choice beats adding reviewers ([arXiv 2608.18167](https://arxiv.org/abs/2608.18167)).

   Check the severity the same way: a Pre-existing finding is one the base branch already has (`git show <merge-base>:<file>`) and the change doesn't make reachable; otherwise it keeps the reviewer's Important or Nit.
3. Fix confirmed Important findings with the smallest change, and add the failing test when there is one. Fix at most five confirmed Nits, the ones worth a reader's time, unless the review guide sets another cap, and drop the rest from the report. A confirmed Pre-existing finding is not fixed in this change: it goes on the report's `Pre-existing:` line and under **Found**, and stays Pre-existing in later rounds.
4. Put uncertain findings in front of the human; don't silently fix or drop them.

Findings reported by two independent reviewers, or by both model families, deserve extra attention but still need verification.

## 4. Re-check and report

After fixes, run the user's number of re-check rounds, then stop. Every cross-model pass finds something narrower than the last, so rounds never come back clean; the cap is a fixed number, not a clean pass. Read it with `~/.agents/bin/phase review-rounds`: it prints the number and where it's set, `1 (default)` when the user set nothing, and only the user sets it. When it exits 1, the store is unreadable: say so in the report and run 1 round. At `0`, skip items 1 and 2: the review ends with the first pass and its fixes. Each round runs items 1 and 2, then verifies and fixes as in item 3; the fixes of one round are what the next round's cross-model re-check reviews, and the last round's go to no reviewer. Before each round, write the report's Fixed, Rejected, Open and Pre-existing lines so far, each with its evidence, and the Nits dropped past the cap, to a file in your scratch directory, rewritten each round, so its re-checks don't raise them again:

1. **Same model, whole change.** Re-run the Correctness lens yourself on the whole change, with the same base, naming the fixes in the goal and holding the decided findings in front of you: report only a new instance at another location, or a regression a fix introduced. A fix can break what the first pass cleared, and this cheap pass is what sees it. For a very large change, split it into batches that together cover all of it, as in step 2.
2. **Cross-model, the fixes only.** When a cross-model pass ran in step 2, send one Correctness re-check to the other model family, scoped to the fixes and the code they touch: never the whole change again, even on the High tier. Commit the change before the first cross-model pass, so its commit marks what that pass reviewed; `bundle.sh` adds every untracked file whatever the base, so new files left uncommitted would come back in full. Then run step 2's command with that commit as the base, naming in the goal the findings the fixes address and the PR's base branch, the one Pre-existing is judged against; its merge-base with HEAD is that commit, so the bundle holds the fixes with their surrounding lines:
   ```bash
   ~/.agents/skills/self-review/bundle.sh <commit the previous pass reviewed> Correctness "<goal; fixes for: …>" <decided-findings file>
   ```
   Running on an OpenAI model, pipe that same bundle into step 2's `claude -p` command. In a later round, commit before it too, and use the commit the previous round's cross-model re-check reviewed.
3. **No round past the cap.** Verify the re-check's findings as in step 3. Fix only those confirmed, Important and cheap, each with a test; after the last round, don't send those fixes back to a reviewer. A confirmed Pre-existing finding goes on the `Pre-existing:` line as in step 3, and a confirmed Nit is dropped; everything else goes under Open in the report for the human to decide at PR review. A finding that would need another round to settle is a sign the change needs a different design, not another pass: say so under Open.

The stop hook re-runs `verify` when you finish; run it by hand (`python3 ~/.agents/hooks/stop_checks.py verify`) only when you need its output before continuing. Then record the review; opening a PR is blocked until the stamp matches the current change, and any later edit invalidates it:

```bash
python3 ~/.agents/hooks/review_stamp.py write
```

Log every lens that ran, the cross-model passes included, confirmed meaning a finding that survived step 3. The monthly job uses this to retire lenses that never pay off, and the outcomes to measure each reviewer's noise: give every finding exactly one, by what became of it, not by the severity the reviewer gave it. `fixed`: confirmed and fixed. `wrong`: rejected, the scenario can't happen. `intentional`: rejected, the behavior is deliberate or outside the check's threat model. `minor`: confirmed, not worth acting on (a Nit dropped, past the cap or from a re-check). `pre-existing`: confirmed Pre-existing. `open`: confirmed and not minor, left Open for the human. `uncertain`: left uncertain, even on the report's Open line. They add up to the findings, and fixed, minor, pre-existing and open to the confirmed ones; leave out the ones at 0, and with no findings give `--outcomes fixed=0`:

```bash
~/.agents/bin/quality-log lens <name> --findings <N> --confirmed <M> --secs <S> --model <claude|codex> \
  --outcomes fixed=<N>,wrong=<N>,intentional=<N>,minor=<N>,pre-existing=<N>,open=<N>,uncertain=<N>
```

Save the report to `$(~/.agents/bin/reports path review)`. Put it in the final message too (see AGENTS.md → Communication). Use this shape; it can go straight into the PR's validation notes:

```
Self-review (risk: standard; lenses: correctness, tests, maintainability, security; cross-model: codex; re-check rounds: 1 (default))
Spec table, paths table and derived-data table (from the Correctness lens)
Fixed:     <finding> — <file:line> — evidence: <failing test or output before the fix> — <test that now covers it>
Rejected:  <finding> — evidence: <file:line where it's handled, or the command that disproves it>
Pre-existing: <finding> — <file:line> — evidence: <the line on the base branch> (also under Found in the final message)
Open:      <finding> — <uncertain | confirmed, not fixed after the last re-check: why | needs another round: the design question> — <evidence that would settle it, or the decision for the human>
Not run:   <lens or reviewer skipped, and why>
```
