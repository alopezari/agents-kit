# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
- The kit's owner: an engineer with a QA background who hands tasks to coding agents (Claude Code, Codex) and decides, at the pull request, whether the change merges. They work in a terminal, on macOS, across their own and shared repositories.
- Occasionally a colleague or reviewer the owner forwards a page to by hand. They lack the session's context, so pages read on their own.

## Product Purpose
agents-kit gives every coding agent the same instructions, skills, guardrails and checks, and hooks check the agent followed them: irreversible actions wait for the owner, every edit is verified, and a pull request can't open until the exact change was reviewed and validated. Success is a merge decision the owner can make quickly and trust, because the evidence behind it is on file.

## Positioning
The kit doesn't trust the agent's account of its work: gates read recorded evidence (stamps tied to the exact change, command outputs with their commit, tree and exit code), and reports say "not run", "stale" or "unavailable" rather than implying a pass.

## Operating Context
- The flow: spec → build → review → validate → ship (opens the PR, waits for CI) → the owner decides the merge. The owner decides at the PR and nowhere earlier.
- Reports are Markdown files per branch in `.git/agents/` (spec, verify, review, validation, checks guide, PR body), with evidence files and check scripts in an evidence directory. They are listed by `bin/reports`, and `bin/prune-merged` deletes them after the merge.
- The decision page is read in two passes: about 30 seconds to decide whether it can merge, then the annex only when something doesn't add up.

## Capabilities and Constraints
- Pages generated from the kit's reports are local files: they are never published by the kit, since evidence can hold sensitive data. They make no network requests and run no scripts.
- Generated text comes only from what the reports and spec say; a page never claims more than its sources show.
- Public repository (github.com/alopezari/agents-kit): no private names or terms in committed files.

## Brand Commitments
- Name: agents-kit. The public site (`site/src/`) has an established look; the site is the only incumbent visual surface.
- Voice: plain, short, factual English; results first; uncertainty named, never hidden.

## Evidence on Hand
- Real reports and evidence from past branches under `~/.agents/.git/agents/`; no invented metrics, testimonials or users.

## Product Principles
1. Evidence over assertion: every claim links to what proves it.
2. Absence is visible: missing, stale, not run and unavailable are distinct and shown by name.
3. Decision first, detail on demand.
4. The owner keeps the irreversible calls.

## Accessibility & Inclusion
WCAG 2.2 AA: contrast, keyboard navigation, readable at 200% zoom.
