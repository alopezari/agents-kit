---
name: kit-validate
description: Validate a change end to end before it goes to review. Runs the full unit suite (in Docker if the repo needs it), exercises every acceptance criterion against the local environment with temporary scripts covering positive and negative cases, and writes a step-by-step checks guide for whatever it couldn't run: optional steps the user can run before the merge, and the steps after it. Use for behavior changes before opening a PR, or when asked to test a change.
effort: high
---

# Validate

Unit tests prove the pieces. This skill proves the behavior: the change does what the spec says in a running system, and fails safely when it should fail. It ends with evidence, not with "should work".

When `~/.agents/bin/phase switches` lists `validate`, the user switched this phase off: say so in a line and skip it, unless they asked for it by name. With `verify` listed, skip step 0's verify check (still wait for the review it names, unless that is off too), and run the whole unit suite in step 3 yourself: no verify stamp covers it. With `review` listed, don't wait for it or re-run it (step 0 and step 5). With `audit` listed, leave out the Impeccable audit in the design checks.

## 0. Start from a green verify

`verify` must be green before anything here; there's no point exercising a running system with a unit failure already known. The stop hook runs it after every edit and stamps the change it passed on. So check `python3 ~/.agents/hooks/review_stamp.py check --kind verify` first. If only `check --kind verify-empty` passes, verify was green without checking anything (its tools are missing or it found nothing it knows how to check): don't run it again; run the tests related to the change yourself and treat their output as this step's evidence, naming them. When neither passes, run `python3 ~/.agents/hooks/stop_checks.py verify`: it runs the repo's verify as the stop hook does, saves its report and stamps a pass. A verify stamp can't be written by hand. If it's red, fix it and stop there. The stop hook saves each verify run, with what ran, to `$(~/.agents/bin/reports path verify)`; it is the evidence for this step. The review's cross-model pass may still be running: run checks meanwhile, but write this skill's stamp and save the checks guide (step 6) only after it has finished, its fixes are in and the review's re-checks (its step 4, as many rounds as `~/.agents/bin/phase review-rounds` says) are stamped; re-run the checks those fixes touch.

## 1. Plan the checks

Start from the spec (`~/.agents/skills/kit-spec/path.sh`) or, without one, from the stated goal. When there is a spec, run `~/.agents/skills/kit-spec/lint.py` first: a criterion with no real `verify:` leaves nothing to derive checks from, so fix the spec before planning. The bar is the coverage you'd need to ship with confidence, not a number of cases. For each acceptance criterion, derive checks from these sources until each one is covered:

- **Every way in:** each entry point that reaches the behavior (UI, REST, CLI, cron, webhook), taken from the review paths table.
- **Every input class and boundary:** valid, empty, missing, zero, maximum, just past the limit, duplicates, unusual dates.
- **Every failure mode:** bad or tampered input, missing permission, wrong secret, a dependency down or slow, partial failure ("nothing persisted when it fails").
- **Every derived value:** caches and stored data that should change, or stay, when the input changes, taken from the derived-data table.
- **What must not change:** neighbouring behavior the change could break (regression checks).

A simple criterion may need two checks; a risky one may need fifteen. Say what you deliberately left uncovered and why; that residual risk is part of the report.

Run `~/.agents/bin/triage` too: its **Tests** list says which test types this change needs beyond e2e (query budget, accessibility, property-based, mutation, migration round-trip, visual check for style-only changes, design check for UI changes), each with the signal behind it. Stay within its time budget.

Place each check where it can really run:

| Where | Use for |
|---|---|
| Unit | Logic already covered by tests; list it, don't re-test it |
| Local | Anything the local stack can reproduce: endpoints, CLI, cron, admin UI, data changes |
| Local, after the merge | What needs the merged code installed on this machine (a scheduled job or a tool installed from the default branch). Run it from the branch first when that touches nothing shared (a temporary install, a copy of the state); what really needs the merge goes in the guide as your steps after the merge (step 6), run when the user asks for `ship` |
| Only the user | Staging, production, shared infrastructure, third-party integrations, scale, accounts or devices you don't have |

Group the checks into blocks (A. endpoint, B. engine, C. public page, D. wp-admin, E. CLI…) and number them. Show the plan to the user before running it when it's large.

## 2. Bring up the local environment

1. Read `~/.agents/repos/<repo>/notes.md`. If it already has a local-environment recipe, follow it.
2. If it doesn't, work it out from the Makefile, the docker-compose files, the README and the repo's AGENTS.md. Bring the stack up and confirm it answers (e.g. `curl -s -o /dev/null -w "%{http_code}" <url>`). **Then write the recipe into the notes:** start command, URL and port, container names, how to run WP-CLI and PHPUnit inside. The next run skips the discovery.
3. Know what the stack is serving. A bind-mounted stack serves the checkout it was started from, not your worktree. To serve a worktree, add a temporary `docker-compose.override.yml` remounting it, only if no override exists already. Start it with the line `# agents: temporary override` and recreate the containers (`docker compose up -d`). Reverting is part of the cleanup in step 5: delete the file and run `docker compose up -d` again, so the stack serves the primary checkout. Deleting the file alone leaves the old mounts in place. The stop hook blocks while a marked override is still there.
4. **If the repo has no `~/.agents/repos/<repo>/verify` yet,** create one from `~/.agents/repos/_shared/verify_changed.py` with the commands you found. Use `--requires-container` and `--related-tests` for Docker suites. Then run it with `--refresh-baseline` on a clean tree.

Never run deploys, `sync_db`, SSH to shared hosts, or commands that reset the local database without asking. The guard blocks most of them anyway.

## 3. Run the full unit suite

Where `verify` already runs the whole unit suite (the repo notes say so), the verify stamp from step 0 covers this step: don't run it again, and keep its report as this step's evidence with `cp "$(~/.agents/bin/reports path verify)" "$(~/.agents/bin/reports path evidence)/unit-suite-verify.md"`. That report keeps the last 6,000 characters of the output; when a check rests on earlier lines, run the suite through `bin/evidence` instead. Elsewhere, run the whole suite once, including what `verify` skips (Docker suites, slow groups when the change touches them). Compare against `~/.agents/repos/<repo>/phpunit-baseline.txt`: only new failures count.

## 4. Run the local checks with temporary scripts

Every check leaves evidence the user can open instead of re-running it, in the branch's evidence directory: `EV="$(~/.agents/bin/reports path evidence)"`. Run each check through `~/.agents/bin/evidence <id> <command> [args...]`: it prints the output and keeps the exit code, and saves `$EV/<id>.txt` with the command, directory, time, exit code and full output. It takes argv, so a pipe or a script piped to a container goes in `bash -c '...'`. For a UI state, save a screenshot as `$EV/<id>-<state>.png`. A unit-test run behind a PASS is evidence too: run it through `bin/evidence`.

In Claude Code with the kit plugin, hand the runs to `kit:validate-runner`, which runs on Sonnet: give it each check's id and command, with the scratch directory for its scripts. It reports what each printed, with no verdict; you judge each one against what you expected, from its evidence file. In Codex, or with the plugin off, run them yourself.

Make every check repeatable in one command, because a fix sends you back to re-run it. A check that needs setup (a throwaway worktree or clone, seeded data, a fake HOME) is a script outside the tree that sets up, runs and cleans up after itself, and is passed whole to `bin/evidence`. One that only works while a setup from an earlier command is still around fails, or saves the wrong output, once that setup is gone.

- **PHP inside WordPress:** pipe a throwaway script to `wp eval-file -` inside the container, so nothing lands in the working tree (the script must start with `<?php`). Give it a small helper and one line per check:
  ```php
  function check( $id, $ok, $detail = '' ) { echo ( $ok ? 'PASS ' : 'FAIL ' ) . "$id $detail\n"; }
  ```
  Call REST routes with `rest_do_request()` and read secrets with the app's own accessors, never by echoing them.
- **HTTP and auth:** bash with `curl` and a `chk` helper that prints `PASS`/`FAIL` with expected vs actual. Pipe to `jq` to project only the fields you assert on.
- **CLI:** run both from source and the built artifact when both ship, and diff their output. Check exit codes, including the error ones.
- **UI:** an A/B test between Playwright CLI and agent-browser is running until 2026-11-05, so don't pick the tool yourself:
  1. Run `~/.agents/bin/browse assign` once per validation run. It prints the tool assigned to this run.
  2. Send every browser command through `~/.agents/bin/browse <command>`, using the assigned tool's syntax (`playwright-cli --help` or `agent-browser --help`). Log in, assert on the DOM with explicit selectors and expected values, and take a screenshot of every state a check asserts on, saved to `$EV/<id>-<state>.png`.
  3. End with `~/.agents/bin/browse finish --checks <N> --passed <P> --tool-issues <K> --notes "<what the tool made hard, if anything>"`.

  Don't use the Playwright MCP for validation while the test runs, so the data stays comparable. You choose the selectors and judge the results, so don't add an AI browser layer (Stagehand and the like) on top: it adds nondeterminism and a second model to a check that has to be reproducible.

**Advanced tests, only when triage selects them:**
- **Query budget:** `~/.agents/bin/wp-query-profile` runs a REST route or PHP snippet inside the local WordPress and reports the query count and repeated query patterns (N+1). Run it on the affected endpoint or code path with a realistic number of items. Repeated patterns that grow with the item count are findings.
- **Accessibility:** `~/.agents/bin/a11y-check <url>...` runs axe on the pages the change touches and lists serious and critical violations. Compare with the same pages before the change when a violation looks pre-existing.
- **Design:** for each page the change touches, in the state it touches (logged in, after the interaction), numbered D1, D2… so each keeps its own evidence:
  1. Screenshot it at 1280×800 and 390×844 (`$EV/D1-desktop.png`, `$EV/D1-phone.png` for the first page).
  2. Scan its URL with Impeccable's detector at both sizes. It writes nothing in the repository; read every finding whatever the exit code (0 can still list advisories), and any code other than 0 or 2 means the page wasn't scanned, a NOT RUN:
     ```bash
     ~/.agents/bin/evidence D1a ~/.agents/skills/impeccable/scripts/impeccable detect --json --viewport 1280x800 <url>
     ~/.agents/bin/evidence D1b ~/.agents/skills/impeccable/scripts/impeccable detect --json --viewport 390x844 <url>
     ```
     The scan opens the URL fresh and scans whatever answers: a login page, a 404 or the state before an interaction pass for the page itself. Check its findings are about the page you meant, say which states it couldn't reach, and judge those from the screenshots.
  3. Run the `impeccable` skill's `audit` on what changed (a workflow the skill describes, not a CLI command), with both screenshots and both scans as input, and save its report as `$EV/D1-audit.md`.

  Confirmed findings get fixed in this change or listed in the report with why they stay.
- **Property-based:** for the parser, calculation or comparison functions triage names, write a few properties (round-trip, ordering, idempotence, bounds) and check them over generated inputs with a loop in a throwaway script or the repo's PBT library. Keep a property as a real test only if it found something or pins an important invariant.
- **Mutation (high risk only):** mutate the changed lines (flip conditions, off-by-one bounds, remove calls) and check the tests catch each one: with Infection `--git-diff-lines` when the repo has a coverage driver, otherwise by hand for the 5–10 riskiest lines. Treat surviving mutants as missing tests, not as noise. Time-box it to 10 minutes.
- **Regex worst-case timing:** for each line triage names, time the whole regex through the call that uses it (anchors, flags and the rest of the pattern included; a fragment can be fast where the whole is exponential) on a long input that almost matches (many repetitions of the repeated part, then a character that fails the match), growing the input until the time is clear: linear is fine, doubling per few characters is a finding. Run each attempt in a subprocess with a timeout, since a match can't be interrupted from inside; hitting the timeout is a finding. Keep the timing as a test with a bound well under the caller's timeout.
- **Migration round-trip:** run the migration up, down and up again on a copy of local data, and check the data and schema match.

**Data rules:**
- Seed synthetic records with obviously fake IDs (e.g. 999999). Never run a bulk operation over real local data to set up a case. A "replace list" seed once marked 1,686 real extensions inactive.
- Record any option, flag or user you change, and restore or delete it at the end.

**Secrets:**
- Export them without printing them.
- Never request response headers (`curl -D-`, `-v`) on a URL that carries a secret.
- If a secret reaches the transcript anyway, say so and ask the user to rotate it.

## 5. Report and clean up

Report a table per block, `# | Check | Case (+/−) | Result | Evidence`. Result is PASS, FAIL or NOT RUN. Evidence is what you observed, in a few words (the status code, the output line, the query count), plus a link to its file by its absolute path, `$EV` written out: `[output](/Users/me/repo/.git/agents/evidence-repo-feat~x/A1.txt)`, and `![login page](/Users/me/repo/.git/agents/evidence-repo-feat~x/C2-login.png)` for a screenshot, so it shows in the report. Absolute, so the link opens with a click from the terminal as well as from the report. "Works" is not evidence, and a check without evidence counts as NOT RUN: the validate stamp refuses a PASS or FAIL row whose Evidence cell names no saved file. Then list:
- what was only unit-tested;
- what failed and was fixed in the same change;
- what couldn't run locally, and why.

Save the report to `$(~/.agents/bin/reports path validation)`.

Log every test type that ran, so the monthly job can drop the ones that never find anything. `<type>` is one lowercase word: `unit`, `e2e`, `integration`, `browser`, `query-budget`, `accessibility`, `property-based`, `mutation`, `migration-round-trip`, `visual` or `design`, not triage's longer label:

```bash
~/.agents/bin/quality-log test <type> --issues <N> --secs <S> --notes "<what it found>"
```

Record the validation. Opening a PR is blocked for behavior changes until this stamp matches the current change. If validating led to code changes, run the kit-review skill's same-model re-check (its step 4, item 1) again first, with no new cross-model round, so both stamps cover the final code:

```bash
python3 ~/.agents/hooks/review_stamp.py write --kind validate
```

Then clean up in one go: temporary scripts and `/tmp` files. Keep the evidence directory: it is the report's proof. Revert the override (delete it, then `docker compose up -d`), restore the state you recorded, and confirm with `git status --short` that the working tree only holds the change itself.

## 6. Checks guide for what you couldn't run

The guide holds the checks that couldn't run in step 4. What needs an environment, an account or access you don't have is the user's; what runs on this machine after the merge is yours, when the user asks for `ship`. Nothing waits for it: the PR lists these steps as not run, and the user decides whether to run some before merging or to merge as is. When nothing was left out there is no guide. Give the user a step-by-step guide they can follow without you and without guessing:

- **Two parts, split at the merge.** `## Before the merge (optional)` holds what only the user can reach before the merge (another device, an account, a third-party service, an environment a profile's skill sets up), as steps `### M1.`, `### M2.`…, ending with "Put the environment back as it was" when a step changed one. `## After the merge` holds what needs the production deploy (a production dry run, a backfill, a scoring run), as steps `### P1.`…, and the local checks that need the merged code installed on this machine; the `kit-ship` skill runs them when the user asks for it. Each P step's **Where** says who runs it: `you`, or `the agent, on this machine`. Leave out a part with no steps.
- **Step 0 lists everything needed up front:** access and accounts (by name, never secret values), VPN, proxy or tunnel, tools and versions, the branch or build to deploy, what the deploy overwrites, how long the whole guide takes, and a `export VAR=...` block that sets every value later steps reuse (site URL, IDs, branch). It includes `export EVIDENCE_DIR="$(cd <the user's checkout> && ~/.agents/bin/reports path evidence)" && echo "$EVIDENCE_DIR"`, with the checkout's real path, whose Expected is a directory ending in `evidence-<repo>-<branch key>` (an empty value makes `bin/evidence` refuse to run), so every step saves its evidence next to the reports from whichever directory it runs in. Computed there, not pasted as a path: the reports move out of `$TMPDIR` once `.git` is writable.
- **Every step has four parts:**
  - **Why:** one line on what it proves.
  - **Where:** which machine, terminal, directory or browser page, logged in as whom.
  - **Run:** a fenced block that works when pasted as is. Fill in real values (URLs, IDs, branch and file names). When a value can only be known at run time, give the command that prints it and store it in a variable. No `<placeholders>`, no "adjust as needed". Each command whose output proves the step runs as `~/.agents/bin/evidence M3 <command>` (the step's id; `M3a`, `M3b` when a step has several, since each id is one file; `bash -c '...'` for a pipe), so its output is saved without the user copying anything. A browser step names the screenshot to save, `"$EVIDENCE_DIR/M4-<state>.png"`.
  - **Expected:** the exact output, status code, UI text or log line, and **If not:** what it means and what to do next (retry, collect this output, stop and tell whom).
- **Check every command you can** before handing it over: run it locally, or with a dry-run or `--help` for its flags, so a typo can't waste the user's time on a shared environment.
- **Negative steps too,** including one that proves a positive result isn't a false positive: remove the new behavior's trigger and confirm the result changes.
- **Warnings where they bite.** For example, a deploy that includes uncommitted work or overwrites a shared environment, or commands that would launch real runs. Use fake inputs (e.g. version `99.0.0-rc.1`) to exercise guards without real side effects.
- **Anything that changes shared configuration** (repo variables, feature flags, production settings) is marked as the user's step, never run by you.
- **Cleanup keeps the evidence.** "Put the environment back" undoes deploys and data, never `$EVIDENCE_DIR`: it is the results' proof.

Give the guide in chat in the user's language, and save an English copy to `$(~/.agents/bin/reports path checks-guide)`.

**When the user runs steps and says so,** read each step's evidence in `$EVIDENCE_DIR` and judge it against the step's Expected yourself; the user's word settles only what left no file. Append a `## Results (<date>)` table to the guide, one row per step run (`| Step | Result | Evidence |`), with the observation that decided it and a link to each evidence file by its absolute path. A failure is a finding: fix it (verify, the kit-review skill's same-model re-check, with no new cross-model round, and this skill's stamp again), update the guide, and tell the user which steps to run again.
