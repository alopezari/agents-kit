---
name: ship
description: Follow a pull request from creation to production. Hands the branch over to the user when it needs manual staging tests (removing the agent's worktree), watches CI and fixes real failures, verifies and answers review comments, and after the user merges and deploys, runs the production checks and closes the loop. Use right after `gh pr create`, or when asked to follow a PR through.
---

# Ship

Opening the PR is the middle of the job, not the end. This skill takes it the rest of the way. The merge is always the user's: the shell guard blocks `gh pr merge`, and that stays.

Start with the PR number and the paths you will need after the worktree is gone:

```bash
gh pr view --json number,url,headRefName,baseRefName
~/.agents/bin/reports    # spec, verify, review, validation and staging guide for this branch
```

## 1. Hand the branch over for staging tests

Do this right after `gh pr create` when both are true:

- the session works in a linked worktree: `git rev-parse --git-dir` differs from `git rev-parse --git-common-dir`;
- the change needs manual tests: `$(~/.agents/bin/reports path staging-guide)` exists.

A branch can be checked out in only one worktree, so the user can't switch to it until the worktree is gone. Before removing it, prove nothing is lost:

1. `git status --porcelain` prints nothing. Untracked files would be deleted with the worktree; commit them or ask.
2. The branch is pushed: `git rev-parse HEAD` equals `git rev-parse @{upstream}`.
3. Find the main checkout: the first `worktree` line of `git worktree list --porcelain`.

Then, from the main checkout (the worktree directory is about to disappear, so leave it first):

```bash
cd <main checkout> && git worktree remove <worktree path> && git worktree prune
```

Never add `--force`, and never switch the main checkout's branch yourself: the user may have work there. The spec and reports live in the repository's shared `.git/agents/`, so they survive. If a tool created the worktree for its session (Xirp, for example), say so, so the user can archive that session too.

Give the user the hand-off in chat, filling in real values:

1. `cd <main checkout> && git status --short`. **Expected:** nothing. If it lists files, commit or stash them first.
2. `git switch <branch> && git pull --ff-only`.
3. `~/.agents/bin/reports`: the staging guide is at the end. Follow it from step 0.

From here the user owns the checkout. Anything that needs a code change (CI, review) is proposed to the user, not written into their checkout, unless they ask you to.

Without a worktree, or without manual tests, skip this step.

## 2. CI

Wait for the checks, bounded: poll `gh pr checks <n>` about every minute for at most 30 minutes. macOS has no `timeout`, so use your harness's background or timeout mechanism rather than an unbounded `--watch`. Still pending after that: report which checks are pending and stop waiting.

For each failing check:

1. Read the failure: `gh run view <run-id> --log-failed | tail -200`.
2. Decide, with evidence:
   - **Caused by the change**: the failing test or lint touches changed code, or fails the same way locally. Fix it on the branch, let verify run, run self-review again if the fix changes behavior, push.
   - **Not caused by the change**: it fails the same way on the base branch (`gh run list --branch <base> --workflow <name> --limit 5`) or in code the PR doesn't touch. Re-run it once (`gh run rerun <run-id> --failed`). If it fails again, report it with the evidence; don't touch unrelated code in this PR.
3. Never skip, loosen or disable a check to make it pass.

## 3. Review comments

Read every comment, from people and bots:

```bash
gh pr view <n> --comments
gh api repos/{owner}/{repo}/pulls/<n>/comments --paginate
```

Verify each one exactly like a self-review finding (Confirmed / Rejected / Uncertain, each with evidence). Fix the confirmed ones as in step 2. Draft a short reply for every comment, saying what was fixed, or why not, with the evidence. Posting is speaking for the user: show the drafts and post only what they approve.

## 4. Ready to merge

Tell the user it is ready when CI is green, the staging guide passed, and every comment has a fix or an approved reply. List anything still open. They merge and deploy.

## 5. After the deploy

When the user says it is deployed, run the production part of the staging guide, read-only only: status checks, logs, dashboards and queries that change nothing. Anything that writes to production is the user's step, written out as in the validate skill's step 6. If something looks wrong, say so at once and give the rollback: `gh pr revert <n>` opens a revert PR (check `gh pr revert --help` exists in their gh version first; otherwise `git revert -m 1 <merge commit>` on a new branch).

## 6. Close the loop

- **Issue:** draft the update (what shipped, the PR link, how it was verified). Writing to a tracker needs the user's approval; the MCP guard enforces it.
- **Repo notes:** a trap that cost time in this PR (a flaky check, a missing setup step) goes into `~/.agents/repos/<repo>/notes.md`, or better, into a check.
- **Local branch:** after the merge, offer `git branch -d <branch>` (lowercase `-d` refuses to delete unmerged work).

PR outcomes (merged, closed, reverted) reach the monthly analysis on their own; nothing to log here.

## Report

```
Ship: <PR url>
Hand-off:  worktree <path> removed; user switches with `git switch <branch>` in <main checkout> | not needed (<why>)
CI:        <check>: pass | fixed (<cause>, <commit>) | not caused by the change (<evidence>) | still pending after 30 min
Review:    <comment> — Confirmed/Rejected/Uncertain — <evidence> — <fix or drafted reply>
Deploy:    <production checks run and their results> | not deployed yet
Open:      <what still needs the user>
```
