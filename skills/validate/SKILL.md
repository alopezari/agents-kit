---
name: validate
description: Validate a change end to end before it goes to review. Runs the full unit suite (in Docker if the repo needs it), exercises every acceptance criterion against the local environment with temporary scripts covering positive and negative cases, writes a step-by-step guide for whatever can only be tested on staging or production, and hands the branch over for those tests. Use for behavior changes before opening a PR, or when asked to test a change.
effort: high
---

# Validate

Unit tests prove the pieces. This skill proves the behavior: the change does what the spec says in a running system, and fails safely when it should fail. It ends with evidence, not with "should work".

## 0. Start from a green verify

`verify` must be green before anything here; there's no point exercising a running system with a unit failure already known. The stop hook runs it after every edit and stamps the change it passed on. So check `python3 ~/.agents/hooks/review_stamp.py check --kind verify` first, and run `~/.agents/repos/<repo>/verify` yourself only when that fails. If it's red, fix it and stop there. The stop hook saves each verify run, with what ran, to `$(~/.agents/bin/reports path verify)`; it is the evidence for this step.

## 1. Plan the checks

Start from the spec (`~/.agents/skills/spec/path.sh`) or, without one, from the stated goal. The bar is the coverage you'd need to ship with confidence, not a number of cases. For each acceptance criterion, derive checks from these sources until each one is covered:

- **Every way in:** each entry point that reaches the behavior (UI, REST, CLI, cron, webhook), taken from the self-review paths table.
- **Every input class and boundary:** valid, empty, missing, zero, maximum, just past the limit, duplicates, unusual dates.
- **Every failure mode:** bad or tampered input, missing permission, wrong secret, a dependency down or slow, partial failure ("nothing persisted when it fails").
- **Every derived value:** caches and stored data that should change, or stay, when the input changes, taken from the derived-data table.
- **What must not change:** neighbouring behavior the change could break (regression checks).

A simple criterion may need two checks; a risky one may need fifteen. Say what you deliberately left uncovered and why; that residual risk is part of the report.

Run `~/.agents/bin/triage` too: its **Tests** list says which test types this change needs beyond e2e (query budget, accessibility, property-based, mutation, migration round-trip, visual check for style-only changes), each with the signal behind it. Stay within its time budget.

Place each check where it can really run:

| Where | Use for |
|---|---|
| Unit | Logic already covered by tests; list it, don't re-test it |
| Local | Anything the local stack can reproduce: endpoints, CLI, cron, admin UI, data changes |
| Staging / production | Real catalogues, shared infrastructure, third-party integrations, scale, things local can't fabricate |

Group the checks into blocks (A. endpoint, B. engine, C. public page, D. wp-admin, E. CLI…) and number them. Show the plan to the user before running it when it's large.

## 2. Bring up the local environment

1. Read `~/.agents/repos/<repo>/notes.md`. If it already has a local-environment recipe, follow it.
2. If it doesn't, work it out from the Makefile, the docker-compose files, the README and the repo's AGENTS.md. Bring the stack up and confirm it answers (e.g. `curl -s -o /dev/null -w "%{http_code}" <url>`). **Then write the recipe into the notes:** start command, URL and port, container names, how to run WP-CLI and PHPUnit inside. The next run skips the discovery.
3. Know what the stack is serving. A bind-mounted stack serves the checkout it was started from, not your worktree. To serve a worktree, add a temporary `docker-compose.override.yml` remounting it, only if no override exists already. Start it with the line `# agents: temporary override` and recreate the containers (`docker compose up -d`). Reverting is part of the cleanup in step 5: delete the file and run `docker compose up -d` again, so the stack serves the primary checkout. Deleting the file alone leaves the old mounts in place. The stop hook blocks while a marked override is still there.
4. **If the repo has no `~/.agents/repos/<repo>/verify` yet,** create one from `~/.agents/repos/_shared/verify_changed.py` with the commands you found. Use `--requires-container` and `--related-tests` for Docker suites. Then run it with `--refresh-baseline` on a clean tree.

Never run deploys, `sync_db`, SSH to shared hosts, or commands that reset the local database without asking. The guard blocks most of them anyway.

## 3. Run the full unit suite

Where `verify` already runs the whole unit suite (the repo notes say so), the verify stamp from step 0 covers this step: don't run it again. Elsewhere, run the whole suite once, including what `verify` skips (Docker suites, slow groups when the change touches them). Compare against `~/.agents/repos/<repo>/phpunit-baseline.txt`: only new failures count.

## 4. Run the local checks with temporary scripts

- **PHP inside WordPress:** pipe a throwaway script to `wp eval-file -` inside the container, so nothing lands in the working tree (the script must start with `<?php`). Give it a small helper and one line per check:
  ```php
  function check( $id, $ok, $detail = '' ) { echo ( $ok ? 'PASS ' : 'FAIL ' ) . "$id $detail\n"; }
  ```
  Call REST routes with `rest_do_request()` and read secrets with the app's own accessors, never by echoing them.
- **HTTP and auth:** bash with `curl` and a `chk` helper that prints `PASS`/`FAIL` with expected vs actual. Pipe to `jq` to project only the fields you assert on.
- **CLI:** run both from source and the built artifact when both ship, and diff their output. Check exit codes, including the error ones.
- **UI:** an A/B test between Playwright CLI and agent-browser is running until 2026-11-05, so don't pick the tool yourself:
  1. Run `~/.agents/bin/browse assign` once per validation run. It prints the tool assigned to this run.
  2. Send every browser command through `~/.agents/bin/browse <command>`, using the assigned tool's syntax (`playwright-cli --help` or `agent-browser --help`). Log in, assert on the DOM with explicit selectors and expected values, and take screenshots of the states that matter, saved outside the working tree.
  3. End with `~/.agents/bin/browse finish --checks <N> --passed <P> --tool-issues <K> --notes "<what the tool made hard, if anything>"`.

  Don't use the Playwright MCP for validation while the test runs, so the data stays comparable. You choose the selectors and judge the results, so don't add an AI browser layer (Stagehand and the like) on top: it adds nondeterminism and a second model to a check that has to be reproducible.

**Advanced tests, only when triage selects them:**
- **Query budget:** `~/.agents/bin/wp-query-profile` runs a REST route or PHP snippet inside the local WordPress and reports the query count and repeated query patterns (N+1). Run it on the affected endpoint or code path with a realistic number of items. Repeated patterns that grow with the item count are findings.
- **Accessibility:** `~/.agents/bin/a11y-check <url>...` runs axe on the pages the change touches and lists serious and critical violations. Compare with the same pages before the change when a violation looks pre-existing.
- **Property-based:** for the parser, calculation or comparison functions triage names, write a few properties (round-trip, ordering, idempotence, bounds) and check them over generated inputs with a loop in a throwaway script or the repo's PBT library. Keep a property as a real test only if it found something or pins an important invariant.
- **Mutation (high risk only):** mutate the changed lines (flip conditions, off-by-one bounds, remove calls) and check the tests catch each one: with Infection `--git-diff-lines` when the repo has a coverage driver, otherwise by hand for the 5–10 riskiest lines. Treat surviving mutants as missing tests, not as noise. Time-box it to 10 minutes.
- **Migration round-trip:** run the migration up, down and up again on a copy of local data, and check the data and schema match.

**Data rules:**
- Seed synthetic records with obviously fake IDs (e.g. 999999). Never run a bulk operation over real local data to set up a case. A "replace list" seed once marked 1,686 real extensions inactive.
- Record any option, flag or user you change, and restore or delete it at the end.

**Secrets:**
- Export them without printing them.
- Never request response headers (`curl -D-`, `-v`) on a URL that carries a secret.
- If a secret reaches the transcript anyway, say so and ask the user to rotate it.

## 5. Report and clean up

Report a table per block, `# | Check | Case (+/−) | Result | Evidence`. Result is PASS, FAIL or NOT RUN. Evidence is what you observed: the command and the relevant output lines, a status code and response excerpt, a query count, or a screenshot path. "Works" is not evidence, and a check without evidence counts as NOT RUN. Then list:
- what was only unit-tested;
- what failed and was fixed in the same change;
- what couldn't run locally, and why.

Save the report to `$(~/.agents/bin/reports path validation)`.

Log every test type that ran, so the monthly job can drop the ones that never find anything:

```bash
~/.agents/bin/quality-log test <type> --issues <N> --secs <S> --notes "<what it found>"
```

Record the validation. Opening a PR is blocked for behavior changes until this stamp matches the current change. If validating led to code changes, run the self-review re-check (its step 4) again first, so both stamps cover the final code:

```bash
python3 ~/.agents/hooks/review_stamp.py write --kind validate
```

Then clean up in one go: temporary scripts, screenshots and `/tmp` files. Revert the override (delete it, then `docker compose up -d`), restore the state you recorded, and confirm with `git status --short` that the working tree only holds the change itself.

## 6. Guide for what only staging or production can test

Give the user a step-by-step guide they can follow without you and without guessing:

- **Numbered sections**, from "0. Before you start" to a final "Put the environment back as it was".
- **Step 0 lists everything needed up front:** access and accounts (by name, never secret values), VPN, proxy or tunnel, tools and versions, the branch or build to deploy, what the deploy overwrites, how long the whole guide takes, and a `export VAR=...` block that sets every value later steps reuse (site URL, IDs, branch).
- **Every step has four parts:**
  - **Why:** one line on what it proves.
  - **Where:** which machine, terminal, directory or browser page, logged in as whom.
  - **Run:** a fenced block that works when pasted as is. Fill in real values (URLs, IDs, branch and file names). When a value can only be known at run time, give the command that prints it and store it in a variable. No `<placeholders>`, no "adjust as needed".
  - **Expected:** the exact output, status code, UI text or log line, and **If not:** what it means and what to do next (retry, collect this output, stop and tell whom).
- **Check every command you can** before handing it over: run it locally, or with a dry-run or `--help` for its flags, so a typo can't waste the user's staging slot.
- **Negative steps too,** including one that proves a positive result isn't a false positive: remove the new behavior's trigger and confirm the result changes.
- **Warnings where they bite.** For example, a staging deploy that includes uncommitted work or overwrites a shared environment, or commands that would launch real runs. Use fake inputs (e.g. version `99.0.0-rc.1`) to exercise guards without real side effects.
- **Anything that changes shared configuration** (repo variables, feature flags, production settings) is marked as the user's step, never run by you.

Give the guide in chat in the user's language, and save an English copy to `$(~/.agents/bin/reports path staging-guide)`.

## 7. Hand the branch over and record the staging results

When there is a staging guide, the PR waits for its results: the PR description then shows real staging evidence, and a staging failure gets fixed before CI and reviewers spend time on the PR. When the change needs no manual tests, skip this step; `create-pr` comes next.

**If you worked in a linked worktree** (`git rev-parse --git-dir` differs from `git rev-parse --git-common-dir`), remove it so the user can check the branch out: a branch can be checked out in only one worktree. First prove nothing is lost:

1. `git status --porcelain` prints nothing. Untracked files would be deleted with the worktree; commit them or ask.
2. The branch is pushed: `git push -u origin <branch>` if it has no upstream yet, then `git rev-parse HEAD` equals `git rev-parse @{upstream}`.
3. The main checkout is the first `worktree` line of `git worktree list --porcelain`.

Then leave the worktree (its directory is about to disappear) and remove it from the main checkout:

```bash
cd <main checkout> && git worktree remove <worktree path> && git worktree prune
```

Never add `--force`, and never switch the main checkout's branch yourself: the user may have work there. The spec, the reports and the review and validate stamps live in the repository's shared `.git/agents/`, so they survive. If a tool created the worktree for its session (Xirp, for example), say so, so the user can archive that session too.

Give the user the hand-off in chat, with real values:

1. `cd <main checkout> && git status --short`. **Expected:** nothing. If it lists files, commit or stash them first.
2. `git switch <branch> && git pull --ff-only`.
3. `~/.agents/bin/reports` prints the staging guide at the end. Follow it from step 0, and tell me the result of each step.

From here the user owns the checkout. Propose fixes instead of editing it, unless they ask you to.

**When the user reports back,** append their results to the staging-guide report under `## Results (<date>)`, one line per step: PASS or FAIL and what they saw. A failure is a finding: fix it (verify, the self-review re-check and this skill's stamp again), update the guide, and ask for the affected steps to be re-run. When every step passed, continue with `create-pr`.
