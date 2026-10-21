---
name: kit-ship
description: The flow's last phase. Before the merge, it opens the pull request once the change is reviewed and validated (checks that verify, review and validate passed for the exact current change, or that the user switched their phase off, pushes, writes the description with the kit-write-pr-description skill, runs gh pr create) and waits for its CI. After the merge, and only when the user asks, it sees the merged pull request into production with the repo's own steps from its ship.md - a deploy and light verification guide for the user, or, for a repo installed on this machine, the install and checks run by the agent - with the rollback ready, and closes the loop. Use it instead of calling gh pr create directly. Hearing that a PR is merged or deployed is not a request for the part after the merge.
---

# Ship

Ship has two parts, and the pull request decides which one runs. Read its state first. When the user names the PR, or this session opened it, name it, since after the merge the session's directory is detached or on the default branch and has no PR of its own:

```bash
gh pr view <number or head branch> --json number,url,state,mergedAt,mergeCommit,headRefName,baseRefName
```

Otherwise run it without an argument, from the checkout that has the branch.

- **No pull request** (`no pull requests found`): part 1, the whole of it, but only on a branch of its own. On the default branch or a detached HEAD there is nothing to open: ask which PR the user means.
- **Open:** part 1 without opening a second one, from a checkout of its `headRefName`: the main checkout once the user has switched to it, otherwise add a worktree (`git worktree add <path> <headRefName>`), since everything below reads the branch checked out where it runs. First record the PR's `baseRefName` as the base (step 4's last paragraph), so the evidence is measured against what GitHub shows. Then check the evidence (step 1), push the commits GitHub doesn't have yet (step 2), refresh the description when it no longer matches the change (step 3, then `gh pr edit <n> --body-file <body file>` as a command of its own), wait for CI (step 5), free the branch (step 6) and report.
- **Merged:** part 2, only when the user asked for it ("ship", "install", a deploy guide, a production check). When they only say it's merged or deployed, report its status and list what part 2 would do as optional, without running any of it or asking.
- **Closed without a merge:** say so and stop.

When `gh pr view` fails for another reason (GitHub unreadable, not logged in), say so and stop. That is not "no pull request", and opening one could duplicate it.

## Part 1. Open the pull request

The last step before the change leaves your hands. Everything before it produced evidence: this part checks the evidence is there and complete, turns it into the pull request and waits for its CI. Following up on failures and review comments is the `kit-follow-pr` skill's, and it runs only when the user asks for it.

### 1. Check the evidence

Each line must hold, or the listed step runs first:

| Check | Command | If it fails |
|---|---|---|
| Verify passed on this change | `python3 ~/.agents/hooks/review_stamp.py check --kind verify` | Let the stop hook run verify (or run `python3 ~/.agents/hooks/stop_checks.py verify`), fix what it finds. If only `--kind verify-empty` passes, verify checked nothing: the evidence is the tests you ran instead, named in the description, never "verify passed" |
| Review covers this change | `python3 ~/.agents/hooks/review_stamp.py check` | Run the `kit-review` skill |
| Validated, for behavior changes | `python3 ~/.agents/hooks/review_stamp.py needs-validate && python3 ~/.agents/hooks/review_stamp.py check --kind validate` | Run the `kit-validate` skill |

A row whose phase `~/.agents/bin/phase switches` lists (verify, review, validate) is skipped: the user switched it off, and `bin/pr-validation` puts that in the description. The shell guard enforces the two stamps on `gh pr create`; this table catches the rest before you get there.

The stamps measure the change against `python3 ~/.agents/hooks/review_stamp.py base`, and the guard denies a PR whose target differs from it. When the PR should target another branch (a stacked branch whose parent wasn't detected, or the default branch for one that was), record the target first, `git config branch."$(git branch --show-current)".agentsBase <target>`. When the change measured against it differs, the stamps go stale: re-run the kit-review and kit-validate skills on the change it shows.

### 2. Push

```bash
git push -u origin "$(git branch --show-current)"
```

If the push is refused, show the error and stop: nothing new reached GitHub.

### 3. Write the title and description

Run the `kit-write-pr-description` skill. Give it the evidence, so the validation section is concrete: `~/.agents/bin/reports` prints the verify, review and validation reports and the checks guide. Start the validation section from `~/.agents/bin/pr-validation`, which builds it from those reports. Shorten its wording or merge its lines, but keep every verdict and count it prints, and add no result it doesn't show: a count written from memory is how a description ends up claiming more than was tested. Link the issue with the repository's syntax, and give it the spec's `## Decisions` for the approach: a reviewer sees the alternative that was rejected, and why, without asking. Both come from the spec (`~/.agents/skills/kit-spec/path.sh`).

Write the description to the branch's own file, at the path `~/.agents/bin/reports path pr-body` prints. Below, `<body file>` is that path, written out (see step 4).

### 4. Open it

For a pull request to the kit itself (`~/.agents`, which is public), check the title and description for a profile's private terms first. It names each one and the line it's on; rewrite those, then run it again:

```bash
python3 ~/.agents/hooks/private_terms.py pr "<title>" <body file>
```

```bash
gh pr create --base <base> --head "$(git branch --show-current)" --title "<title>" --body-file <body file>
```

A normal pull request, ready for review: not a draft. `<base>` is the branch `review_stamp.py base` prints, without its `origin/`, written out: the guard reads the command's text, so a `$(...)` there names no branch, and for a pull request to the kit no description file either. Run it as a command of its own, after the stamps were written and the description file saved in earlier commands: the guard reads both before anything in the command runs.

If the guard or GitHub refuses it, the branch is pushed with no pull request: say which step the refusal names and run that step, then this one again. Otherwise print the PR URL.

Then record the PR's target as the branch's base, `git config branch.<head branch>.agentsBase <base>`, even when `review_stamp.py base` found it on its own: a parent found by detection is lost once its pushed tip moves past this branch, and later fixes (kit-follow-pr) measure against this base.

### 5. Wait for CI

Unless `~/.agents/bin/phase switches` lists `ci`, wait for the checks of the commit you pushed, in the foreground:

```bash
~/.agents/bin/ci-wait --sha "$(git rev-parse HEAD)"
```

It waits up to 30 minutes and prints each check, with the failing lines of a failed job's log. When your harness caps a command's run time (Claude Code's is 10 minutes), pass a `--timeout` under the cap and run it again while it exits 2, up to 30 minutes in all. Report its result as it is, each exit code by name: 0 passed, 1 failed (name the failing checks), 2 still running after the wait, 3 no checks appeared, 4 GitHub couldn't be read, 5 the repo runs no CI. Only 0 is "CI passed".

On a failure, don't fix it here: report it, and offer the `kit-follow-pr` skill, which works out whether the change caused it and fixes it. Never call the pull request ready to merge: review comments may still come, and that's follow-pr's call.

### 6. Free the branch

When the branch is checked out in a linked worktree (`git rev-parse --git-dir` differs from `git rev-parse --git-common-dir` there), free it once the CI wait is over, so the user can switch to it without removing the worktree by hand. Run it with the session's own directory (the one it started in) as the working directory, since that decides what happens:

```bash
cd <the session's directory> && ~/.agents/bin/free-branch <worktree path>
```

It removes a worktree you created and detaches the one the session runs in (the session's hooks need that directory). It refuses, changing nothing, while anything there isn't committed and pushed, or, to remove it, while it holds ignored files an install or build doesn't bring back (a `.env`, local notes): it lists them, and they're the user's call. Give the user the switch command it prints. From then on the branch's reports are read from a checkout of it: the main checkout once the user has switched, or a worktree added for a fix (follow-pr).

### Report

```
PR:        <url> (opened | already open: <N> commits pushed, description <refreshed | unchanged>)
CI:        passed | failed: <checks> (kit-follow-pr can take it) | still running | no checks | GitHub unreadable | no CI | not waited: ci switched off
Branch:    free (<worktree removed | detached>; `cd <main checkout> && git switch <branch>`) | in the main checkout
Evidence:  verify <PASS | FAIL | checked nothing: <tests run instead>> · review <stamp ok> · validate <stamp ok | not needed: tests/docs only>
```

## Part 2. After the merge

Now it has to work in production. The user deploys and runs every production step: you prepare the guide, the rollback and the follow-up. Never deploy, write to production or SSH into it yourself, unless the repo's ship.md says it is installed on this machine.

### The repo's ship.md

How each repo deploys, what to check after, how to roll back and whether it runs on this machine differ from repo to repo, so they live outside the kit, in `~/.agents/repos/$(~/.agents/bin/repo-name)/ship.md`, beside the repo's notes. It has four sections, each named exactly as here:

- `## Deploy`: the commands, who runs them and where.
- `## Smoke checks`: the read-only checks after every deploy (pages or endpoints, a happy path that changes nothing, the logs or error rates to watch and for how long).
- `## Rollback`: how a deploy is undone, and what data a revert leaves behind.
- `## Installed on this machine`: `no`, or `yes` with the pull and install commands the agent runs here.

These sections decide what this part does: when one disagrees with a step below, ship.md wins.

When the file is missing, or a section is, draft it before anything else. Copy the deploy, check and rollback steps the notes already give (often under a heading starting with `## Deploy`), and leave the notes as they are. Infer the rest from the repo's README, Makefile, package scripts and CI config, marking each inferred step `(inferred: <file>)`. A section none of these support says `unknown`: ask the user for it, never invent it. Copied steps need no confirmation.

On every run, not only the first, a step still marked `(inferred: …)` or a section still `unknown` is shown to the user before anything runs, and runs only once they confirm it. Their confirmation removes the marker in ship.md, so it isn't asked again.

### 1. Deploy and verification guide

Write the guide in the kit-validate skill's step 6 format: numbered steps, each with **Why**, **Where**, a copy-paste **Run** block with real values, **Expected** and **If not**. Step 0 lists access, tunnel or VPN, and an `export` block with the values later steps reuse.

- **Deploy:** ship.md's `## Deploy`.
- **Light verification, read-only:** ship.md's `## Smoke checks`, plus the checks guide's "After the merge" steps (`~/.agents/bin/reports --branch <the PR's head branch>`): they are this change's checks that needed production. Cut anything that writes unless the guide says it's the point (a backfill, a scoring run), and keep their `~/.agents/bin/evidence P1 …` form so the results come back as files.
- **When to roll back:** the exact signal (status code, error line, metric) that means "revert now".

Check every command you can before handing it over: `--help` for flags, a dry run, or the same read-only command against staging.

**When ship.md says the repo is installed on this machine**, there is no production for the user to reach: run its pull and install, then the guide's "After the merge" steps marked as yours, through `~/.agents/bin/evidence` as written, and record the results as in step 3. On the default branch `bin/reports` finds that branch's reports, not the merged PR's: read the guide with `~/.agents/bin/reports --branch <the PR's head branch>`, and `export EVIDENCE_DIR="$(~/.agents/bin/reports --branch <the PR's head branch> path evidence)"` in place of the guide's step 0 export, which on the default branch names that branch's directory. Ask first only when a step runs something real the user didn't plan for (a paid or long run, a write to data outside a copy).

### 2. Rollback, ready to paste

Before the user deploys, give ship.md's `## Rollback` in the same format, with this change's values: the PR number for `gh pr revert <n>` when that's how the repo undoes code, the ref to deploy back to, and the data this change leaves behind (a migration, stored settings) with how to clean it up.

### 3. Close the loop

When the user says the deploy is done, or you ran it, read the verification steps' evidence and append a `## Results after the deploy (<date>)` table to the checks guide, with one row per P step (`| Step | Result | Evidence |`), the observation that decided it and a link to its file by its absolute path. When every step passed:

- **Issue:** draft the update (what shipped, the PR link, how it was verified in production). Writing to a tracker needs the user's approval; the MCP guard enforces it.
- **Repo notes:** a trap that cost time in this change (a flaky check, a missing setup step, a deploy surprise) goes into `~/.agents/repos/<repo>/notes.md`, or better, into a check. A deploy, check or rollback step that turned out wrong goes into ship.md.
- **Worktrees and local branches:** run `~/.agents/bin/prune-merged` from the main checkout. It removes only the worktrees and branches already in origin's default branch that lose nothing, and says why it keeps the rest; pass its kept lines on to the user.

PR outcomes (merged, reverted) reach the monthly analysis on their own; nothing to log here.

### Report

```
Ship:      <PR url>, merged <date>
Steps:     ship.md <used | drafted: <sections inferred or unknown>>
Guide:     deploy + <N> verification steps given to the user | run here (installed on this machine)
Rollback:  <from ship.md, with this change's values> (+ <data left behind, or none>)
Result:    <P steps PASS/FAIL, from their evidence> | waiting for the deploy
Closed:    issue update <drafted/posted> · notes <updated/nothing new> · branch <deleted/kept>
```
