---
name: follow-pr
description: Take an open pull request to ready-to-merge. Watches CI and fixes failures the change caused, verifies every review comment (people and bots) before fixing or answering it, keeps the description true after fixes, and reports when the PR is ready for the user to merge. Use right after create-pr, or when asked to follow up on a PR.
---

# Follow PR

From an open PR to "ready to merge". The merge is always the user's: the shell guard blocks `gh pr merge`, and that stays.

```bash
gh pr view --json number,url,headRefName,baseRefName,isDraft
```

Fixes go on the branch wherever it is checked out. If that is the user's main checkout (after the staging hand-off), say what you're about to change before editing there: they may be in the middle of something.

## 1. CI

Wait for the checks, bounded: poll `gh pr checks <n>` about every minute for at most 30 minutes. macOS has no `timeout`, so use your harness's background or timeout mechanism rather than an unbounded `--watch`. Still pending after that: report which checks are pending and stop waiting.

For each failing check:

1. Read the failure: `gh run view <run-id> --log-failed | tail -200`.
2. Decide, with evidence:
   - **Caused by the change**: the failing test or lint touches changed code, or fails the same way locally. Fix it (section 3).
   - **Not caused by the change**: it fails the same way on the base branch (`gh run list --branch <base> --workflow <name> --limit 5`) or in code the PR doesn't touch. Re-run it once (`gh run rerun <run-id> --failed`). If it fails again, report it with the evidence; don't touch unrelated code in this PR.
3. Never skip, loosen or disable a check to make it pass.

## 2. Review comments

Read every comment, from people and bots:

```bash
gh pr view <n> --comments
gh api repos/{owner}/{repo}/pulls/<n>/comments --paginate
```

Verify each one like a self-review finding: Confirmed, Rejected or Uncertain, each with evidence. Fix the confirmed ones (section 3). Draft a short reply for every comment: what was fixed, or why not, with the evidence. Posting speaks for the user: show the drafts and post only what they approve.

## 3. Fixing on an open PR

1. Make the smallest fix, with a test that fails without it when there is a harness for it.
2. Let verify run. When the fix changes behavior, re-run the self-review re-check (its step 4) and validate the affected checks, so the stamps cover the final code.
3. Push.
4. If the description no longer matches the code, run the `write-pr-description` skill again and update it: `gh pr edit <n> --body-file <pr-body.md>`.

Then go back to section 1: the push starts a new CI run.

## 4. Ready to merge

Report that it is ready when CI is green and every comment has a fix or an approved reply. If it was a draft waiting for staging results, it can be marked ready (`gh pr ready <n>`) once they passed. List anything still open. The user merges; when they are about to deploy, the `ship` skill takes over.

## Report

```
PR:      <url>
CI:      <check>: pass | fixed (<cause>, <commit>) | not caused by the change (<evidence>) | still pending after 30 min
Review:  <comment> — Confirmed/Rejected/Uncertain — <evidence> — <fix or drafted reply>
Status:  ready to merge | waiting on <what>
```
