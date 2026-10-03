---
name: ship
description: See a merged pull request safely into production. Gives the user a step-by-step deploy and light production verification guide to run themselves, or, for a repo installed on this machine, deploys and runs the checks itself; prepares the rollback, and closes the loop (issue update, repo notes, local branch). Use when the user is about to deploy a merged PR, says it is deployed, or says a PR of a repo installed on this machine is merged.
---

# Ship

The PR is merged; now it has to work in production. The user deploys and runs every production step: you prepare the guide, the rollback and the follow-up. Never deploy, write to production or SSH into it yourself.

```bash
gh pr view <n> --json number,url,state,mergedAt,mergeCommit,headRefName
```

It must be merged. If it isn't, say so and stop: `follow-pr` is the step before.

Whatever the deploy, the guide's "After the merge" steps marked as yours are yours to run once it's done. **A repo installed on this machine** (a personal tool or profile whose deploy is a pull and an install here; the repo notes say so) has no production for the user to reach: pull, install and run those steps, through `~/.agents/bin/evidence` as written, then record the results as in section 3. On the default branch `bin/reports` finds that branch's reports, not the merged PR's: read the guide at `$(git rev-parse --path-format=absolute --git-common-dir)/agents/staging-guide-<repo>-<branch key>.md` (the PR's head branch, `/` written `~`), and set `EVIDENCE_DIR` to the `evidence-<repo>-<branch key>` directory beside it. Ask first only when a step runs something real the user didn't plan for (a paid or long run, a write to data outside a copy). Rollback and closing the loop are as below.

## 1. Deploy and verification guide

Write the guide in the validate skill's step 6 format: numbered steps, each with **Why**, **Where**, a copy-paste **Run** block with real values, **Expected** and **If not**. Step 0 lists access, tunnel or VPN, and an `export` block with the values later steps reuse.

- **Deploy:** the repository's deploy steps, from `~/.agents/repos/<repo>/notes.md` or the repo's own docs. If neither says how, ask the user instead of guessing.
- **Light verification, read-only:** a status check of the affected pages or endpoints, one real happy path that changes nothing, the log lines or error rates to watch, and for how long. Start from the staging guide's "After the merge" steps (`~/.agents/bin/reports`): they are the checks that needed production. Cut anything that writes unless the guide says it's the point (a backfill, a scoring run), and keep their `~/.agents/bin/evidence P1 …` form so the results come back as files.
- **When to roll back:** the exact signal (status code, error line, metric) that means "revert now".

Check every command you can before handing it over: `--help` for flags, a dry run, or the same read-only command against staging.

## 2. Rollback, ready to paste

Before the user deploys, give the rollback in the same format:

1. `gh pr revert <n>` opens a revert PR. Review and merge it, then deploy it the same way. When the revert would leave data behind (a migration, stored settings), say what stays and how to clean it up.
2. If the repo deploys a specific ref, the command to deploy the previous one, taken from the notes.

## 3. Close the loop

When the user says the deploy is done, or you ran it, read the verification steps' evidence and append a `## Results after the deploy (<date>)` table to the staging guide, in validate's step 7 shape (`| Step | Result | Evidence |`, P1, P2…). When every step passed:

- **Issue:** draft the update (what shipped, the PR link, how it was verified in production). Writing to a tracker needs the user's approval; the MCP guard enforces it.
- **Repo notes:** a trap that cost time in this change (a flaky check, a missing setup step, a deploy surprise) goes into `~/.agents/repos/<repo>/notes.md`, or better, into a check.
- **Local branch:** offer `git branch -d <branch>` (lowercase `-d` refuses to delete unmerged work).

PR outcomes (merged, reverted) reach the monthly analysis on their own; nothing to log here.

## Report

```
Ship:      <PR url>, merged <date>
Guide:     deploy + <N> verification steps given to the user
Rollback:  gh pr revert <n> (+ <data left behind, or none>)
Result:    <P steps PASS/FAIL, from their evidence> | waiting for the deploy
Closed:    issue update <drafted/posted> · notes <updated/nothing new> · branch <deleted/kept>
```
