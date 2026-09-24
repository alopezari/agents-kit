---
name: follow-pr
description: Take an open pull request to ready-to-merge. Handles what is new since the last run: CI failures the change caused and review comments from people and bots, each verified before it is fixed or answered, with review and testing of every fix. Runs once right after create-pr (waiting for CI and bot reviews), then whenever the user asks to follow up on the PR.
---

# Follow PR

From an open PR to "ready to merge". The merge is always the user's: the shell guard blocks `gh pr merge`, and that stays.

Every run handles only what is new: its report keeps each comment it dealt with, so running it again after three days picks up where it left off.

```bash
gh pr view --json number,url,author,headRefName,baseRefName,isDraft,reviewRequests
cat "$(~/.agents/bin/reports path follow-pr)" 2>/dev/null   # what earlier runs handled
```

- **First run** (right after `create-pr`): wait for CI as in section 1, then read the comments. Bot reviewers usually post when their own check finishes, so reading earlier misses them.
- **On-demand runs** (the user asks to follow up): read the current state, don't wait. If checks are still running, say which and handle the rest.

Fixes go on the branch wherever it is checked out. If that is the user's main checkout (after the staging hand-off), say what you're about to change before editing there: they may be in the middle of something.

## 1. CI

`gh pr checks <n>` shows the state. On the first run, and after a push, wait for it bounded: poll about every minute for at most 30 minutes. macOS has no `timeout`, so use your harness's background or timeout mechanism rather than an unbounded `--watch`. Still pending after that: report which checks are pending and move on.

For each failing check:

1. Read the failure: `gh run view <run-id> --log-failed | tail -200`.
2. Decide, with evidence:
   - **Caused by the change**: the failing test or lint touches changed code, or fails the same way locally. Fix it (section 3).
   - **Not caused by the change**: it fails the same way on the base branch (`gh run list --branch <base> --workflow <name> --limit 5`) or in code the PR doesn't touch. Re-run it once (`gh run rerun <run-id> --failed`). If it fails again, report it with the evidence; don't touch unrelated code in this PR.
3. Never skip, loosen or disable a check to make it pass.

## 2. Review comments

Fetch all three kinds, with their ids:

```bash
gh api 'repos/{owner}/{repo}/pulls/<n>/comments' --paginate \
  --jq '.[] | {id, user: .user.login, path, line, body, url: .html_url}'          # inline, on the diff
gh api 'repos/{owner}/{repo}/issues/<n>/comments' --paginate \
  --jq '.[] | {id, user: .user.login, body, url: .html_url}'                      # conversation
gh api 'repos/{owner}/{repo}/pulls/<n>/reviews' --paginate \
  --jq '.[] | select(.body != "") | {id, user: .user.login, state, body, url: .html_url}'   # review summaries
```

Skip the ids already in the report, and comments by the PR author (the user's own replies). For each new one:

1. Verify it like a self-review finding: **Confirmed**, **Rejected** or **Uncertain**, each with evidence.
2. Confirmed: fix it (section 3). Rejected: draft a reply with the evidence. Uncertain: put it in front of the user.
3. Draft a short reply for every comment: what was fixed and where, or why not. Posting speaks for the user: show the drafts and post only what they approve.

## 3. Fixing on an open PR

A fix is a small change of its own, and gets reviewed and tested in proportion:

1. The smallest fix, with a test that fails without it when there is a harness for it.
2. Verify runs at the end of the turn (the stop hook).
3. Self-review, step 4 only: re-run the Correctness lens on the new diff, then `python3 ~/.agents/hooks/review_stamp.py write`.
4. For behavior changes, validate the checks the fix touches, not the whole plan, then `python3 ~/.agents/hooks/review_stamp.py write --kind validate`.
5. If the fix changes something already tested on staging, update the staging guide and ask the user to re-run the affected steps; the PR is not ready until they pass.
6. Push. If the description no longer matches the code, run `write-pr-description` again and `gh pr edit <n> --body-file <pr-body.md>`.

The push starts a new CI run: go back to section 1 and wait for it.

## 4. Record and report

Append this run to `$(~/.agents/bin/reports path follow-pr)`, one line per comment with its id, so the next run skips it:

```
## Run <date time>
CI:      <check>: pass | fixed (<cause>, <commit>) | not caused by the change (<evidence>) | pending
<id> <url> — <user> — Confirmed/Rejected/Uncertain — <evidence> — <fix commit | reply drafted | reply posted | asked the user>
Status:  ready to merge | waiting on <what>
```

It is ready to merge when CI is green, every comment has a fix or an approved reply, no review request is pending, and any staging steps a fix touched passed again. If it was a draft waiting for staging results, it can be marked ready (`gh pr ready <n>`) once they pass. Say what would need another run: checks still running, reviewers who haven't answered. The user merges; when they are about to deploy, the `ship` skill takes over.
