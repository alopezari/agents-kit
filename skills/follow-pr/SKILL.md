---
name: follow-pr
description: "Take an open pull request to ready-to-merge. Handles what is new since the last run: CI failures the change caused and review comments from people and bots, each verified before it is fixed or answered, with review and testing of every fix. Runs once right after create-pr (waiting for CI and bot reviews), then whenever the user asks to follow up on the PR."
---

# Follow PR

From an open PR to "ready to merge". The merge is always the user's: the shell guard blocks `gh pr merge`, and that stays.

Every run handles only what is new: its report keeps each comment it dealt with, so running it again after three days picks up where it left off.

```bash
gh pr view --json number,url,author,headRefName,baseRefName,isDraft,reviewRequests
cat "$(~/.agents/bin/reports path follow-pr)" 2>/dev/null   # what earlier runs handled
```

- **First run** (right after `create-pr`): wait for CI as in section 1, then read the comments. Bot reviewers usually post when their own check finishes, so reading earlier misses them.
- **On-demand runs** (the user asks to follow up): read the current state, don't wait. If checks are still running, say which and handle the rest. In a new session, start with `~/.agents/bin/reports brief`: the phase and one line per report, so you read in full only the reports you need.

Fixes go on the branch wherever it is checked out. If that is the user's main checkout (after the staging hand-off), say what you're about to change before editing there: they may be in the middle of something. If no checkout has it (create-pr freed it), add a worktree for the fix, `git worktree add <path> <branch>`, and free it again with `~/.agents/bin/free-branch <path>` once the fix is pushed. The reports (`reports path follow-pr` and the rest) are keyed by the branch checked out where they run: read them from a checkout of the branch, not from a session directory create-pr detached.

## 1. CI

On the first run, and after every push, wait for the checks of the commit you pushed, in the foreground, and read the result before saying anything about the PR:

```bash
~/.agents/bin/ci-wait --sha "$(git rev-parse HEAD)"
```

It reads that commit's checks, not whatever `gh pr checks` shows from the previous push, waits up to 30 minutes, and prints each check, with the failing lines of a failed job's log. Exit 0 passed, 1 failed, 2 still running after 30 minutes (report which, and move on), 3 no checks within 5 minutes, or within a shorter `--timeout` (the repo runs no CI on this branch), 4 GitHub couldn't be read (say so; don't report it as passed). When your harness caps a command's run time (Claude Code's is 10 minutes), pass a `--timeout` under the cap and run it again while it exits 2, up to 30 minutes in all. Don't move it to the background: the turn can end before it does, and the stop hook asks about CI still running on a commit you pushed.

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

1. Verify it like a self-review finding: **Confirmed**, **Rejected** or **Uncertain**, each with evidence.
2. Confirmed: fix it (section 3). Rejected: draft a reply with the evidence. Uncertain: put it in front of the user.
3. Draft a short reply for every comment: what was fixed and where, or why not. Posting speaks for the user: show the drafts and post only what they approve.

## 3. Fixing on an open PR

A fix is a small change of its own, and gets reviewed and tested in proportion:

1. The smallest fix, with a test that fails without it when there is a harness for it.
2. Verify runs at the end of the turn (the stop hook).
3. Self-review, step 4 only: re-run the Correctness lens on the whole change, naming the fix, then `python3 ~/.agents/hooks/review_stamp.py write`.
4. For behavior changes, validate the checks the fix touches, not the whole plan, saving their evidence and updating their rows in the validation report as in the validate skill (steps 4 and 5), then `python3 ~/.agents/hooks/review_stamp.py write --kind validate`.
5. When the branch has staging results, any fix makes them stale (the status line shows "redo staging"). Ask the user which steps the fix touches, update the guide if it changed what a step checks, and have them re-run those; the PR is not ready until they pass. Record the new round from their evidence and write the staging stamp, as in validate's step 7. A fix no step covers (a test, a comment) needs no re-run, but the user decides that, not you.
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

Log every CI failure the change caused and every review comment from someone other than the user, once, when you first handle it. Each is something self-review and validate let through, and the monthly job measures them:

```bash
~/.agents/bin/quality-log escape <ci|review|bot> --verdict <confirmed|rejected|uncertain> \
  --category <code from ~/.agents/review-mining/taxonomy.md> --lens <self-review lens that should have caught it, or spec> --pr <n>
```

`review` is a person, `bot` an automated reviewer. For CI, the verdict is `confirmed`; the lens is the one whose area the failure falls in (Tests for a failing test, Correctness for a wrong result). A comment saying the change doesn't do what was asked is category `C.requirement` with lens `spec`: the spec's second reading should have caught it. CI failures not caused by the change are not escapes.

It is ready to merge when CI is green, every comment has a fix or an approved reply, no review request is pending, and any staging steps a fix touched passed again. If it was a draft waiting for staging results, it can be marked ready once they pass: `gh pr ready` from the branch's checkout, where the shell guard checks every step before the merge has a PASS with saved evidence. Say what would need another run: checks still running, reviewers who haven't answered. The user merges; when they are about to deploy, the `ship` skill takes over.
