---
name: create-pr
description: Open the pull request once the change is reviewed and validated. Checks that verify, self-review, validate and any staging tests passed for the exact current change, pushes the branch, writes the title and description with the write-pr-description skill, and runs gh pr create. Use when a change is ready for a PR, instead of calling gh pr create directly.
---

# Create PR

The last step before the change leaves your hands. Everything before it produced evidence; this step checks the evidence is there and complete, and turns it into the PR. Then `follow-pr` takes over.

## 1. Check the evidence

Run from the checkout that has the branch. Each line must hold, or the listed step runs first:

| Check | Command | If it fails |
|---|---|---|
| Verify passed on this change | `python3 ~/.agents/hooks/review_stamp.py check --kind verify` | Let the stop hook run verify (or run `~/.agents/repos/<repo>/verify`), fix what it finds |
| Self-review covers this change | `python3 ~/.agents/hooks/review_stamp.py check` | Run the `self-review` skill |
| Validated, for behavior changes | `python3 ~/.agents/hooks/review_stamp.py needs-validate && python3 ~/.agents/hooks/review_stamp.py check --kind validate` | Run the `validate` skill |
| Staging passed, when there is a guide | `~/.agents/bin/reports` shows the staging guide with a `## Results` section, every step PASS | Ask the user for the results (validate, step 7) |

The shell guard enforces the two stamps on `gh pr create`; this table catches the rest before you get there.

One exception: when the repo can only deploy to staging from a PR (its notes say so), open the PR as a draft with the staging results pending, say so in the description, and mark it ready when they pass.

## 2. Push

```bash
git push -u origin "$(git branch --show-current)"
```

## 3. Write the title and description

Run the `write-pr-description` skill. Give it the evidence, so the validation section is concrete: `~/.agents/bin/reports` prints the verify, self-review and validation reports and the staging results. Link the issue with the repository's syntax, from the spec (`~/.agents/skills/spec/path.sh`).

Write the description to a file outside the working tree, for example `"$(dirname "$(~/.agents/skills/spec/path.sh)")/pr-body.md"`.

## 4. Open it

```bash
gh pr create --base <base> --head "$(git branch --show-current)" --title "<title>" --body-file <pr-body.md>
```

Add `--draft` only in the exception from step 1. Print the PR URL.

## 5. Hand over to follow-pr

Continue with the `follow-pr` skill in the same session: CI starts now, and bot reviewers comment within minutes.

## Report

```
PR:        <url> (ready | draft: <why>)
Evidence:  verify <PASS/FAIL> · self-review <stamp ok> · validate <stamp ok | not needed: tests/docs only> · staging <all PASS | none needed | pending: draft>
```
