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

## 3. Resolve before building

- **Open questions that change the work:** ask the user, or the issue author through the user, before building. Everything else becomes an assumption in the spec, and you proceed.
- **The request looks wrong or costlier than its author realized:** say so in a sentence, then follow the user's decision.
- **Show the user the goal and the criteria** in a few lines, so a misread requirement gets caught now, not in review.

## Afterwards

The `self-review` skill reads this spec. Its Correctness lens checks every acceptance criterion against the diff, and the PR description can list them as what was verified. If the scope changes while building, update the spec first.
