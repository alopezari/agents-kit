---
name: self-review
description: Adversarial multi-lens review of your own diff before it goes to a human, using focused reviewers and a second model family, then verifying every finding before acting on it. Use before opening or updating a pull request, before declaring a non-trivial change done, or when asked to review the current branch.
effort: high
---

# Self-review

Review your own change the way a skeptical senior reviewer would, before a human spends time on it. The goal is to find real defects; step 3 separates them from false alarms.

Two things make self-review work:

1. **Focus.** One reviewer checking everything finds the obvious. Several reviewers, each with one lens, find more.
2. **Independence.** The author's model shares the author's blind spots. A reviewer from another model family, and evidence from running the code, catch what the author can't.

## 1. Scope the diff

- Base: the PR's target branch, else `trunk`/`main`/`master`/`develop`, whichever exists. Use `git merge-base HEAD <base>`.
- Review committed and uncommitted changes together: `git diff <merge-base>` plus untracked files.
- Goal: if `~/.agents/skills/spec/path.sh` points at an existing file, that spec's goal and acceptance criteria are the goal every reviewer gets. Otherwise, state the goal in one or two sentences.
- Run `~/.agents/bin/triage` in the checkout. It prints the risk tier, the lenses to run with the signal behind each, how much to send cross-model, and a time budget. Run what it selects. Add a lens it missed only when you can name the risk, and say so in the report.

## 2. Run the lenses

Lens definitions are in `lenses.md` next to this file. Triage picks the lenses; the tier decides who runs them:

| Tier | Reviewer |
|---|---|
| Low | Yourself, one pass |
| Standard | One subagent per selected lens, in parallel, plus one cross-model pass on Correctness |
| High | One subagent per selected lens, plus a cross-model pass per lens |

Stay within triage's time budget. If a lens would need much longer, review the riskiest part and say what you left out.

Give each reviewer: the lens text, the base ref, the task or PR goal in one or two sentences, and this instruction: *report every issue you suspect, each with file:line, the failing scenario, a severity and a confidence (high/medium/low); low-confidence findings are welcome, because step 3 verifies them.* Reviewers are read-only and never edit files.

The Correctness lens always runs, at every risk level, and its tables go into the final report. Also run the repo's personal checks when they exist: `~/.agents/repos/<repo>/verify`.

### Cross-model reviewer

Use the other model family from the model you're running on. The harness doesn't decide it; Pi, for example, can run either family.

- **Running on a Claude model →** Codex. Pass the whole change in the prompt; Codex's own command runner can fail in non-interactive runs, and a reviewer that can't read the code returns nothing:
  ```bash
  ~/.agents/skills/self-review/bundle.sh <base> Correctness "<goal>" \
    | codex exec --ephemeral --skip-git-repo-check -s read-only -
  ```
  Run it once per lens you send cross-model. For a very large diff, split it by directory and review the riskiest parts.
- **Running on an OpenAI model (Codex, or Pi on GPT) →** Claude:
  ```bash
  claude -p "<lens text + goal + evidence instruction>. Review: git diff <merge-base>" \
    --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)" \
    --disallowedTools "Edit,Write"
  ```

If the other CLI isn't installed or fails, say so in the report and continue with same-model reviewers. Don't skip the review.

## 3. Verify every finding

Reviewers produce false positives, and a fix for a non-bug is a new bug. For each finding:

1. Reproduce it: read the code path end to end, or better, write or run a test that fails.
2. Give it exactly one verdict, each with evidence you observed, not an argument:
   - **Confirmed**: a failing test or command output, or the file:line trace that reaches the defect.
   - **Rejected**: the file:line where it's already handled, or a test or command that shows the scenario can't happen.
   - **Uncertain**: the concern, and the evidence that would settle it.

   A verdict without evidence is uncertain. Forcing this choice beats adding reviewers ([arXiv 2608.18167](https://arxiv.org/abs/2608.18167)).
3. Fix confirmed findings with the smallest change, and add the failing test when there is one.
4. Put uncertain findings in front of the human; don't silently fix or drop them.

Findings reported by two independent reviewers, or by both model families, deserve extra attention but still need verification.

## 4. Re-check and report

After fixes, re-run the Correctness lens on the new diff only. The stop hook re-runs `verify` when you finish; run it by hand only when you need its output before continuing. Then record the review; opening a PR is blocked until the stamp matches the current change, and any later edit invalidates it:

```bash
python3 ~/.agents/hooks/review_stamp.py write
```

Log every lens that ran, confirmed meaning a finding that survived step 3. The monthly job uses this to retire lenses that never pay off:

```bash
~/.agents/bin/quality-log lens <name> --findings <N> --confirmed <M> --secs <S> --model <claude|codex>
```

Save the report to `$(~/.agents/bin/reports path review)`. Put it in the final message too (see AGENTS.md → Communication). Use this shape; it can go straight into the PR's validation notes:

```
Self-review (risk: standard; lenses: correctness, tests, maintainability, security; cross-model: codex)
Spec table, paths table and derived-data table (from the Correctness lens)
Fixed:     <finding> — <file:line> — evidence: <failing test or output before the fix> — <test that now covers it>
Rejected:  <finding> — evidence: <file:line where it's handled, or the command that disproves it>
Open:      <finding> — <what's uncertain> — <evidence that would settle it>
Not run:   <lens or reviewer skipped, and why>
```
