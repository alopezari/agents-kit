---
name: kit-follow-pr
description: "Take an open pull request to ready-to-merge. Handles what is new since the last run: CI failures the change caused and review comments from people and bots, each verified before it is fixed or answered, with review and testing of every fix. Runs only when the user asks to follow up on the PR; kit-ship opens it and waits for its first CI run."
---

# Follow PR

From an open PR to "ready to merge". The merge is always the user's: the shell guard blocks `gh pr merge`, and that stays.

Every run handles only what is new: its report keeps each comment it dealt with, so running it again after three days picks up where it left off.

```bash
gh pr view --json number,url,author,headRefName,baseRefName,isDraft,reviewRequests
cat "$(~/.agents/bin/reports path follow-pr)" 2>/dev/null   # what earlier runs handled
```

Record the PR's target as the branch's base, `git config branch.<headRefName>.agentsBase <baseRefName>`, even when `python3 ~/.agents/hooks/review_stamp.py base` already names it: a parent found by detection is lost once its pushed tip moves past this branch. A PR retargeted on GitHub (its parent merged) then measures the change GitHub shows; when that change differs the stamps go stale, and the fix steps of section 3 bring them back.

It runs only when the user asks to follow up: read the current state, don't wait. If checks are still running, say which and handle the rest. In a new session, start with `~/.agents/bin/reports brief`: the phase and one line per report, so you read in full only the reports you need.

Fixes go on the branch wherever it is checked out. If that is the user's main checkout (the user switched to it), say what you're about to change before editing there: they may be in the middle of something. Once kit-ship has freed the branch, run this whole skill (`gh pr view`, `pr-comments`, the reports, ci-wait) from a checkout of the branch: they read the branch checked out where they run, and a session directory kit-ship detached has none. That is the main checkout once the user has switched to it; otherwise add a worktree, `git worktree add <path> <branch>`, and free it again with `cd <the session's directory> && ~/.agents/bin/free-branch <path>` at the end of the run, after its CI wait and the report.

## 1. CI

After every push, wait for the checks of the commit you pushed, in the foreground, and read the result before saying anything about the PR:

```bash
~/.agents/bin/ci-wait --sha "$(git rev-parse HEAD)"
```

It reads that commit's checks, not whatever `gh pr checks` shows from the previous push, waits up to 30 minutes, and prints each check, with the failing lines of a failed job's log. Exit 0 passed, 1 failed, 2 still running after 30 minutes (report which, and move on), 3 no checks within 5 minutes, or within a shorter `--timeout` (no checks found within the wait: CI that didn't start for this commit, such as a branch or path filter, a pull request not open yet or merge conflicts, or a repo with no CI that ci-wait couldn't prove), 4 GitHub couldn't be read (say so; don't report it as passed), 5 the repo runs no CI (no CI config in the commit and no checks on the default branch, the parent or recent pull requests; it says so at once). When your harness caps a command's run time (Claude Code's is 10 minutes), pass a `--timeout` under the cap and run it again while it exits 2, up to 30 minutes in all. Don't move it to the background: the turn can end before it does, and the stop hook asks about CI still running on a commit you pushed.

For each failing check:

1. Read the failure: ci-wait prints its failing lines; for the whole log, `gh run view <run-id> --log-failed | tail -200`.
2. Decide, with evidence:
   - **Caused by the change**: the failing test or lint touches changed code, or fails the same way locally. Fix it (section 3).
   - **Not caused by the change**: it fails the same way on the base branch (`gh run list --branch <base> --workflow <name> --limit 5`) or in code the PR doesn't touch. Re-run it once (`gh run rerun <run-id> --failed`). If it fails again, report it with the evidence; don't touch unrelated code in this PR.
3. Never skip, loosen or disable a check to make it pass.

## 2. Review comments

Print the comments no earlier run handled, from the branch's checkout:

```bash
~/.agents/bin/pr-comments
```

It reads all three kinds, every page: inline comments on the diff, the conversation, and review summaries. Each comes with its id, author (bots marked), file and line, URL and text. It skips the ids already in the report, the PR author's own replies and reviews without text, and says how many of each it skipped. Exit 4 means GitHub couldn't be read: say so, and don't report "no comments". For each new one:

1. Verify it like a kit-review finding: **Confirmed**, **Rejected** or **Uncertain**, each with evidence.
2. Confirmed: fix it (section 3). Rejected: draft a reply with the evidence. Uncertain: put it in front of the user.
   Before fixing any of them, record each confirmed comment that names a file and line, neither outdated nor a deleted line, as a review eval case: the PR's head, the commit the comment's line refers to, still holds the bug, and the monthly job replays it against the reviewers. Skip it when the lens that should have caught it is `spec`, which has no reviewer to replay:
   ```bash
   head=$(gh pr view --json headRefOid -q .headRefOid)
   ~/.agents/review-mining/eval.py add --checkout . --sha "$head" --base "$(git merge-base "$head" origin/<baseRefName>)" \
     --lens <lens that should have caught it> --goal "<PR title>" --file <path> --line <n> --summary "<the defect in one sentence>"
   ```
3. Draft a short reply for every comment: what was fixed and where, or why not. Posting speaks for the user: show the drafts and post only what they approve.

## 3. Fixing on an open PR

A fix is a small change of its own, and gets reviewed and tested in proportion, leaving out the phases `~/.agents/bin/phase switches` lists:

1. The smallest fix, with a test that fails without it when there is a harness for it.
2. Verify runs at the end of the turn (the stop hook).
3. The kit-review skill's same-model re-check (its step 4, item 1) only: re-run the Correctness lens yourself on the whole change, naming the fix and holding the findings already decided, even when the user set the rounds to 0, then `python3 ~/.agents/hooks/review_stamp.py write`. No new cross-model round: the review spent its re-check rounds, and a pass per fix would restart the loop it caps.
4. For behavior changes, validate the checks the fix touches, not the whole plan, saving their evidence and updating their rows in the validation report as in the kit-validate skill (steps 4 and 5), then `python3 ~/.agents/hooks/review_stamp.py write --kind validate`.
5. Push. If the description no longer matches the code, write it again as the kit-ship skill's step 3 does (`kit:pr-description` in Claude Code with the kit plugin, the kit-write-pr-description skill elsewhere), save the whole new description to the path `~/.agents/bin/reports path pr-body` prints in the branch's checkout (its own file, even when an earlier run left none), then, as a command of its own, `gh pr edit <n> --body-file <body file>` with that path written out: for a pull request to the kit, the guard reads the file before anything in the command runs.

The push starts a new CI run: go back to section 1 and wait for it.

## 4. Record and report

Append this run to `$(~/.agents/bin/reports path follow-pr)`, one line per comment with its id, so the next run skips it:

```
## Run <date time>
CI:      <check>: pass | fixed (<cause>, <commit>) | not caused by the change (<evidence>) | pending
<id> <url> — <user> — Confirmed/Rejected/Uncertain — <evidence> — <fix commit | reply drafted | reply posted | asked the user>
Status:  ready to merge | waiting on <what>
```

Log every CI failure the change caused and every review comment from someone other than the user, once, when you first handle it. Each is something kit-review and kit-validate let through, and the monthly job measures them:

```bash
~/.agents/bin/quality-log escape <ci|review|bot> --verdict <confirmed|rejected|uncertain> \
  --category <code from ~/.agents/review-mining/taxonomy.md> --lens <kit-review lens that should have caught it, or spec> --pr <n>
```

`review` is a person, `bot` an automated reviewer. For CI, the verdict is `confirmed`; the lens is the one whose area the failure falls in (Tests for a failing test, Correctness for a wrong result). A comment saying the change doesn't do what was asked is category `C.requirement` with lens `spec`: the spec's second reading should have caught it. CI failures not caused by the change are not escapes.

It is ready to merge when CI is green, every comment has a fix or an approved reply, and no review request is pending. A fix can make results in the checks guide stale: name the steps it touches, so the user can run them again if they choose. Say what would need another run: checks still running, reviewers who haven't answered. The user merges, and the flow ends there; the `kit-ship` skill's part after the merge runs only when they ask for it (a deploy guide, or the install and post-merge checks of a repo installed on this machine).
