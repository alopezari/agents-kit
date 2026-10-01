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
| Verify passed on this change | `python3 ~/.agents/hooks/review_stamp.py check --kind verify` | Let the stop hook run verify (or run `python3 ~/.agents/hooks/stop_checks.py verify`), fix what it finds. If only `--kind verify-empty` passes, verify checked nothing: the evidence is the tests you ran instead, named in the description, never "verify passed" |
| Self-review covers this change | `python3 ~/.agents/hooks/review_stamp.py check` | Run the `self-review` skill |
| Validated, for behavior changes | `python3 ~/.agents/hooks/review_stamp.py needs-validate && python3 ~/.agents/hooks/review_stamp.py check --kind validate` | Run the `validate` skill |
| Staging passed for this change, when the guide has steps before the merge | `python3 ~/.agents/hooks/review_stamp.py staging` | Ask the user to run the guide, or the steps the changes since touch, then record the results from its evidence and write the staging stamp (validate, step 7) |

The shell guard enforces the two stamps on `gh pr create`, and the staging results on `gh pr create` without `--draft` and on `gh pr ready` (`python3 ~/.agents/hooks/review_stamp.py staging` says what's missing); this table catches the rest before you get there.

One exception: when the repo can only deploy to staging from a PR (its notes say so), open the PR as a draft with the staging results pending, say so in the description, and mark it ready when they pass.

## 2. Push

```bash
git push -u origin "$(git branch --show-current)"
```

## 3. Write the title and description

Run the `write-pr-description` skill. Give it the evidence, so the validation section is concrete: `~/.agents/bin/reports` prints the verify, self-review and validation reports and the staging results. Start the validation section from `~/.agents/bin/pr-validation`, which builds it from those reports. Shorten its wording or merge its lines, but keep every verdict and count it prints, and add no result it doesn't show: a count written from memory is how a description ends up claiming more than was tested. Link the issue with the repository's syntax, and give it the spec's `## Decisions` for the approach: a reviewer sees the alternative that was rejected, and why, without asking. Both come from the spec (`~/.agents/skills/spec/path.sh`).

Write the description to a file outside the working tree, for example `"$(dirname "$(~/.agents/skills/spec/path.sh)")/pr-body.md"`.

## 4. Open it

For a pull request to the kit itself (`~/.agents`, which is public), check the title and description for a profile's private terms first. It names each one and the line it's on; rewrite those, then run it again:

```bash
python3 ~/.agents/hooks/private_terms.py pr "<title>" <pr-body.md>
```

```bash
gh pr create --base <base> --head "$(git branch --show-current)" --title "<title>" --body-file <pr-body.md>
```

Add `--draft` only in the exception from step 1. Print the PR URL.

## 5. Hand over to follow-pr

Run the `follow-pr` skill in the same session (its first run): it waits for the pushed commit's CI with `~/.agents/bin/ci-wait` and reads the bot reviews that come with it. After that, the user runs it on demand.

## Report

```
PR:        <url> (ready | draft: <why>)
Evidence:  verify <PASS | FAIL | checked nothing: <tests run instead>> · self-review <stamp ok> · validate <stamp ok | not needed: tests/docs only> · staging <all PASS | none needed | pending: draft>
```
