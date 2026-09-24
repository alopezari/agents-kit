---
name: ship
description: See a merged pull request safely into production. Gives the user a step-by-step deploy and light production verification guide to run themselves, prepares the rollback, and closes the loop (issue update, repo notes, local branch). Use when the user is about to deploy a merged PR or says it is deployed.
---

# Ship

The PR is merged; now it has to work in production. The user deploys and runs every production step: you prepare the guide, the rollback and the follow-up. Never deploy, write to production or SSH into it yourself.

```bash
gh pr view <n> --json number,url,state,mergedAt,mergeCommit,headRefName
```

It must be merged. If it isn't, say so and stop: `follow-pr` is the step before.

## 1. Deploy and verification guide

Write the guide in the validate skill's step 6 format: numbered steps, each with **Why**, **Where**, a copy-paste **Run** block with real values, **Expected** and **If not**. Step 0 lists access, tunnel or VPN, and an `export` block with the values later steps reuse.

- **Deploy:** the repository's deploy steps, from `~/.agents/repos/<repo>/notes.md` or the repo's own docs. If neither says how, ask the user instead of guessing.
- **Light verification, read-only:** a status check of the affected pages or endpoints, one real happy path that changes nothing, the log lines or error rates to watch, and for how long. Take them from the production part of the staging guide (`~/.agents/bin/reports`), and cut anything that writes.
- **When to roll back:** the exact signal (status code, error line, metric) that means "revert now".

Check every command you can before handing it over: `--help` for flags, a dry run, or the same read-only command against staging.

## 2. Rollback, ready to paste

Before the user deploys, give the rollback in the same format:

1. `gh pr revert <n>` opens a revert PR. Review and merge it, then deploy it the same way. When the revert would leave data behind (a migration, stored settings), say what stays and how to clean it up.
2. If the repo deploys a specific ref, the command to deploy the previous one, taken from the notes.

## 3. Close the loop

When the user confirms the deploy went well:

- **Issue:** draft the update (what shipped, the PR link, how it was verified in production). Writing to a tracker needs the user's approval; the MCP guard enforces it.
- **Repo notes:** a trap that cost time in this change (a flaky check, a missing setup step, a deploy surprise) goes into `~/.agents/repos/<repo>/notes.md`, or better, into a check.
- **Local branch:** offer `git branch -d <branch>` (lowercase `-d` refuses to delete unmerged work).

PR outcomes (merged, reverted) reach the monthly analysis on their own; nothing to log here.

## Report

```
Ship:      <PR url>, merged <date>
Guide:     deploy + <N> verification steps given to the user
Rollback:  gh pr revert <n> (+ <data left behind, or none>)
Result:    <what the user reported> | waiting for the deploy
Closed:    issue update <drafted/posted> · notes <updated/nothing new> · branch <deleted/kept>
```
