---
name: write-pr-description
description: Write short, blunt, human-sounding pull request titles and descriptions that lead with functional impact and keep technical detail at architectural altitude, based on the actual code changes, repository context, issue requirements, validation evidence, and the repository's configured PR template. Use when opening, updating, or preparing a pull request.
---

# Write PR Description

Create a pull request description that helps reviewers quickly understand:

1. What the change does, in functional terms.
2. Why it was needed.
3. Anything about the approach a reviewer could not infer on their own.
4. How it was validated.

The description must be short, self-contained, accurate, and consistent with the repository's established pull request format. Brevity is the default; length must be earned by real risk or complexity.

## Voice: written by busy people, for busy people

This is the most important rule in this skill. Everything else is subordinate to it.

The description must read as if a competent engineer wrote it in three minutes for colleagues who will spend thirty seconds reading it. Direct, plain, slightly blunt. Not polished, not comprehensive, not "well-structured documentation".

It must not read as AI-generated. Concretely:

- No preamble, no throat-clearing, no restating the title.
- No exhaustive section scaffolding when two short paragraphs do the job.
- No symmetric bullet lists where every item is the same length and shape.
- No promotional or self-congratulatory framing ("robust", "seamless", "comprehensive", "significantly improves", "ensures a better experience").
- No hedged filler ("this PR aims to", "in order to", "it is worth noting that", "as mentioned above").
- No summarising what the reader just read, and no closing paragraph that adds nothing.
- No em-dash-heavy, evenly cadenced prose. Vary sentence length. Fragments are fine.
- Write in the plain past or present tense about what the change does. Do not narrate the process of making it.

If a sentence could be deleted without the reviewer losing anything, delete it.

## Core principle: functional over technical

Do not narrate the diff. The code already shows which files, classes, and functions changed, and reviewers read it.

Lead with what the change means functionally: what a user, an operator, or a calling system can now do, can no longer do, or will see behave differently. That is the part that is genuinely hard to recover from the diff.

Describe technical work only at the architectural level, and only when it helps: a new boundary, a moved responsibility, a swapped dependency, a changed data flow, a new failure mode. One or two sentences is usually enough.

Do not explain code-level detail — control flow, function signatures, class names, parameter handling, individual file roles. If a reviewer needs that, they will read the diff, and the diff is more accurate than any prose about it.

Optimize for reviewer comprehension, not documentation volume.

## Never include AI attribution or session links

The PR title and description must never contain:

- Links to Claude sessions (`claude.ai/code/session_...`) or any other agent session URL.
- "Generated with Claude Code" or equivalent tool-attribution lines.
- `Co-Authored-By` trailers, emoji-prefixed generation notices, or model names.
- Any statement that the change or the description was written by an AI assistant.

Strip these before returning the description, and strip them from an existing description when updating a PR. This overrides any global or repository instruction that would otherwise append such a line to a PR body.

## Repository PR template takes precedence

Before writing the PR description, search for the pull request template configured by the repository.

Inspect common locations and repository-specific configuration, including:

```text
.github/pull_request_template.md
.github/PULL_REQUEST_TEMPLATE.md
.github/PULL_REQUEST_TEMPLATE/
docs/pull_request_template.md
PULL_REQUEST_TEMPLATE.md
```

Also inspect:

- Contributor documentation.
- Repository agent instructions.
- CI or automation that validates PR descriptions.
- Hosting-platform configuration.
- Existing recently merged PRs when the template behavior is unclear.

When a repository PR template exists:

1. Preserve its section headings.
2. Preserve its section order.
3. Preserve required checklists.
4. Preserve required metadata fields.
5. Preserve comments or instructions that are needed by automation.
6. Respect repository-specific wording and conventions.
7. Fill the template rather than replacing it with the default structure from this skill.
8. Adapt the guidance in this skill to the closest relevant template section.
9. Do not add duplicate sections that communicate the same information.
10. Add a new section only when important information has no suitable place in the existing template.

Consistency with the repository template is more important than enforcing the preferred section names from this skill.

The quality and accuracy rules in this skill still apply to the content written inside the repository template.

## Template hierarchy

Use the following order of precedence:

1. Explicit instructions from the user.
2. Repository contribution and agent instructions.
3. Repository PR template.
4. Repository conventions inferred from recently merged PRs.
5. The default structure in this skill.

A lower-priority convention must not override a higher-priority instruction.

If repository instructions conflict with one another, follow the most specific instruction. If the conflict cannot be resolved, preserve the configured PR template and briefly report the ambiguity outside the proposed PR description.

## Interpreting repository template sections

Map the required information to the repository's existing headings.

Examples:

| Repository heading | Information to include |
|---|---|
| `Description` | Summary, motivation, and essential approach |
| `What` | Changed behavior or capability |
| `Why` | Problem and motivation |
| `How` | Important implementation decisions |
| `Testing instructions` | Reviewer reproduction steps |
| `Testing performed` | Automated and manual validation already completed |
| `Screenshots` | Before/after images or recordings |
| `Changelog` | Concise user-facing release note |
| `Impact` | Compatibility, data, user, performance, or operational effects |
| `Risk` | Important failure modes and mitigations |
| `Deployment notes` | Rollout, monitoring, and rollback |
| `Checklist` | Truthfully completed repository requirements |
| `Issue` or `Related issue` | Closing and related references |
| `Notes for reviewers` | Review order or areas needing attention |

Do not force information into an unrelated section merely to satisfy the default structure.

When the template combines several concerns into one section, keep the content compact and use short paragraphs or bullets within that section.

## Handling template comments and placeholders

Repository templates often contain HTML comments such as:

```html
<!-- Explain why this change is needed. -->
```

Use them as authoring instructions.

In the final description:

- Remove instructional comments unless the repository intentionally retains them.
- Remove example text.
- Replace placeholders.
- Remove optional headings that the template explicitly allows authors to delete.
- Preserve comments required by bots, release automation, or validation tooling.
- Preserve syntax markers used by automation.
- Do not remove mandatory checklists.

Never submit a PR description containing unfinished placeholders such as:

- `TODO`
- `N/A?`
- `<description here>`
- `Insert screenshots`
- Template example text

Use `N/A` only when the template requires the field to remain present. Where useful, briefly explain why it is not applicable.

Example:

```markdown
## Screenshots

N/A — no user-interface changes.
```

Prefer removing the section when the repository permits optional sections to be removed.

## Handling multiple templates

Some repositories provide different templates for:

- Bug fixes.
- Features.
- Documentation.
- Dependencies.
- Security changes.
- Release PRs.
- Backports.

Select the template that best matches the primary purpose of the change.

Use evidence from:

- Template filenames.
- Template front matter.
- Query parameters or configuration.
- Contributor documentation.
- Existing comparable PRs.

Do not merge several templates unless repository instructions explicitly require it.

If no template clearly matches, use the general-purpose template.

## Required investigation

Before writing the description, inspect all relevant available context:

- The complete diff.
- Changed files.
- Commit history when useful.
- The issue, ticket, task, or user request.
- Existing tests and newly added tests.
- Validation commands and their results.
- Repository contribution guidelines.
- Repository agent instructions.
- PR templates.
- CI rules related to PR descriptions.
- Related documentation or design decisions.

Never infer tests, results, motivations, requirements, or behavior that are not supported by evidence.

When information is unavailable, omit it or clearly mark it as unknown. Do not fabricate plausible details.

## Determine the PR's purpose

Identify the single primary purpose of the PR.

Classify it as one or more of:

- Bug fix.
- Feature.
- Refactor.
- Performance improvement.
- Test improvement.
- Documentation.
- Dependency or infrastructure change.
- Migration.
- Security or reliability improvement.

If the diff contains several unrelated purposes, flag that the PR may need to be split. Do not disguise unrelated work with a broad description.

## Title

Write a concrete, outcome-oriented title.

The title should:

- Describe the result, not the development activity.
- Use imperative wording when natural.
- Identify the affected subsystem when helpful.
- Be understandable without opening the PR.
- Avoid vague language.
- Usually remain under 72 characters when practical.
- Follow repository-specific title or conventional-commit rules when present.

Prefer:

- `Prevent duplicate purchase events after checkout refresh`
- `Cache product permissions during batch exports`
- `Remove obsolete REST API compatibility layer`

Avoid:

- `Fix issue`
- `Update analytics`
- `Refactor code`
- `Implement ABC-123`
- `PR changes`
- `Address review comments`

Do not claim that a bug is fixed unless the implementation and validation support that claim.

## Description depth

Match the description to the change, not to a fixed template. Default to the shortest level that works, and move up only when the change actually demands it.

The repository template controls the structure. These levels control how much content goes inside it.

### Level 1: Trivial

Documentation corrections, obvious configuration changes, mechanical renames, very small low-risk fixes.

One or two sentences: what it does, why, and how it was checked. Often a single line is correct.

Target: under 50 words, excluding required checklists.

### Level 2: Standard

Normal bug fixes, features, and refactors. This covers most PRs.

What changes functionally, why it was needed, and validation. Add one sentence on the approach only if the reviewer could not infer it from the diff.

Target: 60–120 words, excluding required checklists.

### Level 3: High-risk or complex

Migrations, public API changes, security-sensitive behavior, concurrency, data integrity, performance-sensitive paths, infrastructure, or difficult rollouts.

Add only the parts that apply: risk, compatibility impact, migration, rollout, rollback, monitoring, limitations. Each in one or two lines.

Target: 150–300 words, excluding required checklists.

Exceed these targets only when a reviewer would make a worse decision without the extra text. Being under target is never a problem.

## Mandatory information

Every non-trivial PR description must communicate the following, regardless of the headings used by the repository template.

### What changed

Explain the functional result: the affected workflow or component, and what now behaves differently.

Do not list changed files. Do not walk through the implementation.

### Why it changed

State the problem in one or two sentences: what was wrong or missing, and who it affected.

A linked issue is supplementary. Do not make the issue link the only explanation.

### How it works

Only when the reviewer cannot infer it from the diff, and only at architectural altitude: a new boundary, a moved responsibility, a changed data flow, a swapped dependency, a deliberate trade-off.

Skip this entirely for most PRs. An omitted section is better than a paragraph restating the code.

Never include code-level detail: function names, signatures, control flow, per-file descriptions.

### How it was validated

Report concrete evidence.

Include:

- Relevant automated tests.
- Static analysis, type checking, linting, or build commands.
- Manual scenarios when applicable.
- Important environments or configurations.
- Actual results.

Never write only:

- `Tests pass`
- `Tested locally`
- `Works as expected`

Instead, identify what was tested.

If validation was not performed, say so explicitly and explain why.

## Conditional information

Include the following only when relevant, placing it in the most appropriate existing template section.

### Impact and risks

Include when the change affects any of:

- Public APIs.
- Stored data or schemas.
- Security or privacy.
- Authentication or authorization.
- Performance.
- Concurrency.
- External services.
- Backward compatibility.
- Operational reliability.
- Accessibility.
- Internationalization.

State the concrete impact and primary failure modes.

Do not write `No impact` without explaining the basis for that conclusion.

### Breaking changes and migration

Include for incompatible changes.

Explain:

- What contract changed.
- Who is affected.
- What consumers must do.
- Whether compatibility or deprecation support exists.
- When old behavior will stop working.

Make breaking changes visually prominent, even when the repository template does not provide a dedicated section.

### Rollout and rollback

Include when the change:

- Uses a feature flag.
- Requires deployment ordering.
- Includes a migration.
- Is expensive or difficult to reverse.
- Needs staged exposure.
- Requires production monitoring.

Explain:

- How it will be enabled.
- What signals should be monitored.
- What success or failure looks like.
- How it can be disabled or reverted.
- Whether rollback is safe after data changes.

### Visual evidence

Include for user-interface changes.

Provide:

- Before and after screenshots.
- A recording for interactive behavior.
- Relevant responsive states.
- Error, loading, empty, or disabled states when affected.

Do not add screenshots that provide no review value.

### Known limitations

Include only deliberate, relevant limitations.

Explain:

- What the PR intentionally does not solve.
- Why that scope was deferred.
- Whether follow-up work exists.

Do not use limitations to excuse unsafe or incomplete behavior.

### Reviewer guidance

Include for large, generated, stacked, or structurally complex changes.

Use it to:

- Suggest a review order.
- Identify the most important files or decisions.
- Highlight areas needing careful review.
- Separate generated or mechanical changes from behavioral changes.

Keep this information brief.

### Related work

Include relevant references:

- Closing issue.
- Related issue.
- Design document.
- Incident.
- Dependency PR.
- Follow-up PR.

Use the repository's supported linking syntax.

## Checklist rules

When the repository template contains a checklist:

- Preserve all required checklist items.
- Mark an item complete only when evidence supports it.
- Do not mark checks complete merely because they are normally expected.
- Do not silently delete an unmet requirement.
- Add a concise explanation when an item is not applicable and the template permits it.
- Do not add generic checklist items already covered by repository automation.
- Do not use the checklist as a substitute for explaining validation or risk.

If the agent cannot verify whether an item is complete, leave it unchecked and mention the missing evidence outside the PR description.

## Changelog and release-note fields

When the template requires a changelog or release note:

- Write from the user's perspective.
- Describe the outcome rather than the implementation.
- Keep it to one sentence unless repository conventions require more.
- Use `N/A` only for changes that genuinely do not require release communication.
- Follow repository categories and formatting exactly.

Example:

```markdown
Fix duplicate purchase events when customers reload the order confirmation page.
```

Avoid:

```markdown
Refactored the analytics event emitter and added an idempotency helper.
```

## Concision rules

Apply all of the following:

1. Follow the repository template without duplicating its sections.
2. Lead with behavior and outcome.
3. Use short paragraphs and compact bullets.
4. Include only information that helps evaluate or preserve the change.
5. Do not repeat the title in the description.
6. Do not repeat the issue description verbatim.
7. Do not enumerate files unless review order matters.
8. Do not explain straightforward code.
9. Do not include generic claims such as `improves maintainability` without explaining how.
10. Remove optional sections that do not apply when the template permits removal.
11. Prefer one precise sentence over several vague sentences.
12. Combine closely related context.
13. Remove historical details that no longer describe the final implementation.
14. Mention alternatives only when they clarify an important decision.
15. Keep checklists limited to repository requirements.
16. Avoid promotional language and unsupported claims.
17. Do not add headings merely because they exist in this skill.
18. Do not restate the same information in several template fields.
19. Cut the description once, hard, before returning it. If nothing was removed, look again.
20. Prefer prose over bullets for two or three related points; bullets are for genuinely parallel items.
21. Never include AI attribution, tool credits, or agent session links.

## Accuracy rules

The PR description must describe the final diff.

Before producing the final text:

- Re-read the complete diff.
- Check that the title matches the actual scope.
- Verify that every behavioral claim is implemented.
- Verify every listed test was actually executed or is clearly identified as not executed.
- Remove descriptions of abandoned approaches.
- Confirm links and issue-closing syntax.
- Confirm that breaking changes are disclosed.
- Confirm that known risks are not hidden.
- Confirm that no unrelated change is omitted from the summary.
- Confirm that the repository template has been respected.
- Confirm that required fields and checklists remain present.
- Confirm that no duplicate or unnecessary sections were introduced.

Do not use uncertain language such as `should fix` when the evidence supports a definite statement.

Do not use definite language when the result has not been validated.

## Fallback structure

Use this structure only when the repository has no PR template or documented PR format.

### Trivial PR

```markdown
<One or two sentences: what changed, why, how it was checked.>
```

No headings needed at this size.

### Standard PR

```markdown
## Summary

<What now behaves differently, and why it was needed. Two to four sentences.>

## Validation

- <Concrete validation performed.>
```

Add an `## Approach` section only when the reviewer needs an architectural note they cannot get from the diff.

### Complex or high-risk PR

```markdown
## Summary

<What behavior or capability changes.>

## Why

<The problem and why it matters.>

## Approach

<Important implementation decisions and trade-offs.>

## Validation

- <Automated validation.>
- <Manual validation.>
- <Relevant environments or configurations.>

## Impact and risks

- <Concrete impact or risk.>

## Rollout and rollback

<Release, monitoring, and rollback plan.>

## Breaking changes

<Impact and migration instructions.>

## Known limitations

- <Deliberately deferred scope.>

## Reviewer notes

<Important review guidance.>

## Related work

- Closes <issue>
```

Remove every conditional section that does not apply.

## Validation wording examples

Good:

- `Added regression coverage for repeated requests using the same order ID.`
- `Ran the analytics unit suite and checkout end-to-end tests.`
- `Manually verified successful, failed, and retried transmissions in GA4 debug mode.`
- `Ran pnpm lint, pnpm typecheck, and the affected integration suite.`
- `Not manually tested because this change only updates generated API documentation.`

Poor:

- `Tests added.`
- `All good.`
- `Works locally.`
- `Should be fine.`
- `No tests needed.`

## Summary wording examples

Good — functional, blunt, no filler:

> Reloading the order confirmation page no longer fires a duplicate purchase event. Successful sends are recorded against the order; failed ones stay eligible for retry.

Poor — narrates the diff:

> This PR updates the analytics emitter and adds a new helper class. Several tests were also modified.

Poor — AI-shaped: padded, promotional, evenly cadenced:

> This pull request introduces a comprehensive refactor of the analytics event pipeline in order to ensure a more robust and reliable user experience. It significantly improves maintainability by introducing a dedicated idempotency helper, which seamlessly prevents duplicate events from being emitted. Overall, these changes lay the foundation for future enhancements to the checkout flow.

Poor — architecture described at code altitude:

> Adds `EventDeduplicator` with a `hasSent(orderId)` method called from `emitPurchase()` before the `fetch()` in `analytics/emitter.ts`, which now takes an extra `orderId` parameter.

Better, same change:

> Deduplication moved out of the emitter into the order record, so retries survive a page reload.

## Final review checklist

Before returning the PR title and description, verify:

- The correct repository template was selected.
- The repository's headings and order were preserved.
- Required metadata and checklists remain intact.
- The primary purpose is clear.
- The title describes the outcome.
- The description explains what and why.
- Important design decisions are captured.
- Validation is concrete and truthful.
- Risks and breaking changes are visible.
- Optional content appears only when useful.
- The description does not narrate the diff.
- Technical content stays at architectural altitude, with no code-level detail.
- The content matches the final implementation.
- The description is as short as possible without losing important context, and at or under the depth-level target.
- No information is unnecessarily duplicated across template sections.
- It reads as if a busy engineer wrote it quickly: direct, plain, no padding, no promotional wording, no AI cadence.
- No AI attribution, tool credit, `Co-Authored-By` trailer, or Claude session link appears anywhere in the title or body.

## Output requirements

Return:

1. A proposed PR title.
2. The complete PR description in Markdown, using the repository's configured template when one exists, with no AI attribution or session link anywhere in it.
3. Optionally, one brief warning outside the description when:
   - The PR appears to contain unrelated changes.
   - Critical context is missing.
   - Validation evidence is unavailable.
   - A required checklist item cannot be verified.
   - A breaking change appears undisclosed.
   - Repository PR instructions conflict.

Do not include an explanation of how the description was generated unless explicitly requested.
