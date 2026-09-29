---
name: spec
description: Turn an issue or request into a short spec with checkable acceptance criteria before building, so the work and the self-review are judged against what was actually asked. Use when starting work on a Linear or GitHub issue, or on any request bigger than a small, unambiguous change.
---

# Spec

Most expensive agent mistakes come from building the wrong thing well: a requirement read too literally, an edge case nobody named, an "obvious" scope that wasn't. A spec pins down *what* done means before any code exists. It is not an implementation plan; the approach is yours to choose while building.

## 1. Gather

- **The issue:** title, description, comments, linked PRs and discussion threads.
  - GitHub: `gh issue view <n> --comments`.
  - Other trackers (Linear, Jira…): the configured MCP server's read tools, e.g. `mcp__linear__get_issue` and `list_comments`.
- **Read-only:** never comment on or update the issue while writing a spec.
- **The code it touches:** enough to know which surfaces are involved (callers, CLI and cron entry points, caches, public hooks), not to design the change.
- **The repo notes:** `~/.agents/repos/<repo>/notes.md` often names the traps this kind of change hits.

## 2. Write the spec

Save it where the self-review will find it, outside the working tree (the script handles sandboxes where `.git` is read-only):

```bash
spec="$(~/.agents/skills/spec/path.sh)"
```

Use this shape and keep it under a page:

```markdown
# <issue id>: <title>

Goal: <one or two sentences on the user-visible outcome, in the issue's terms>

## Acceptance criteria
1. <observable behavior> — verify: <test, command, or manual check>
2. ...

## Out of scope
- <things a reader might expect but this change won't do>

## Assumptions
- <routine calls you made, stated so they can be corrected>

## Open questions
- <only questions whose answer would change the work>
```

What makes criteria useful:

- **Each one is checkable by a test, a command or a named manual step.** "Works correctly" is not a criterion; "a standalone Compose v2 binary is accepted, v1 is rejected with install steps" is.
- **They cover the edges the issue implies but doesn't spell out:** other entry points (CLI, cron, REST), existing data and settings, empty and failure states, backwards compatibility.
- **Three to six of them.** More usually means the issue should be split; say so.

Check the shape, and fix what it reports before going on:

```bash
~/.agents/skills/spec/lint.py
```

## 3. Map the change

The defects reviewers catch most are a rule enforced on one path and not another, and a cache that doesn't vary with a new input. Written rules don't prevent them; a table built before the code does. Add this section to the spec, each item found with a code search, not from memory:

```markdown
## Change map
- Ways in: <every entry point that reaches the behavior: callers, CLI, cron, REST, admin UI, retries, preview vs real run> — <file:line>
- Derived data: <every cache, transient, index or stored value that reads a new or changed input> — <file:line>, or "none: searched <what>"
- Failures: <each external call, query or write the change adds, and what the user sees when it fails> — <file:line>
```

Build so that every item is handled, and add a criterion when an item needs its own check. The self-review starts its tables from this map.

## 4. Get a second reading

The model that wrote the spec shares its blind spots, and a misread requirement is built well and then passes a review judged against the same spec. Have the other model family read it against the request. Pipe in the request as you gathered it: the issue, its comments and the linked PRs and discussion threads that shaped it, not the issue alone.

```bash
prompt="$({ gh issue view <n> --comments && cat <notes on linked PRs and threads>; } | ~/.agents/skills/spec/review-prompt.sh)"
```

For a Linear issue, or a request that only exists in the conversation, write all of it to a file in your scratch directory and redirect that in instead. If the script fails, fix what it says (usually no spec yet) rather than sending the reviewer an empty prompt. Then:

- **Running on a Claude model →** Codex: `codex exec --ephemeral --skip-git-repo-check -s read-only - <<<"$prompt"`
- **Running on an OpenAI model →** Claude: `claude -p --allowedTools "Read,Grep,Glob" --disallowedTools "Edit,Write" <<<"$prompt"`

Verify each finding as the self-review does: **confirmed** (the request says so, quoted) → fix the spec; **rejected** → one line on why; **uncertain** → an open question for the user. Then run the lint again, and log the reading so the monthly job can weigh it:

```bash
~/.agents/bin/quality-log lens spec --findings <N> --confirmed <M> --secs <S> --model <codex|claude> --criteria <number of acceptance criteria> --map-items <lines under Change map>
```

If the other CLI isn't installed or fails, say so when you show the spec and go on without it.

## 5. Resolve before building

- **Open questions that change the work:** ask the user, or the issue author through the user, before building. Everything else becomes an assumption in the spec, and you proceed.
- **The request looks wrong or costlier than its author realized:** say so in a sentence, then follow the user's decision.
- **Show the user the goal and the criteria** in a few lines, so a misread requirement gets caught now, not in review.

## Afterwards

The `self-review` skill reads this spec. Its Correctness lens checks every acceptance criterion against the diff, and every change in the diff against the criteria, and the PR description can list them as what was verified. If the scope changes while building, update the spec first.
