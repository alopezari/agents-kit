---
version: 1
slug: "bin-decision-report"
primary_target: "bin/decision-report"
related_targets: []
---

# Decision page (`bin/reports html`)

Scope: one generated, local, static HTML page per branch. Mode: Read, with Operate density in sections 02 (Tests) and 04 (Merge and ship). Audience: the kit's owner deciding a merge (glance, then detail); sometimes a colleague the page is forwarded to by hand, who lacks the session. Job: tell the reader what the task must do, what the change does, what it changes for users, what was tested and how, how to test it themselves, and what they must decide. Reference: the user's own decision page for an earlier change (numbered sections, Simplified Technical English, small diagrams, a counts line, a filterable check list, a step-by-step test guide, a decisions list, an identifiers footer). Constraints: no scripts, no network, no web fonts (CSP `default-src 'none'`); the agent's notes are labelled as the agent's, every count comes from the reports; absence (missing, stale, not run, unavailable, not recorded) is named, never implied green. WCAG 2.2 AA, readable at 200% zoom, works at 360px.

## Direction contract
THESIS: a verdict, then the proof on demand. One column: the summary answers what the task is, what the change does, how it was reviewed, what was tested at each level and what the owner must decide; below it, numbered folded sections (Coverage, Tests, Review, Merge and ship, Reports) hold every detail, closed unless they hold a failure or a blocker. It refuses the dashboard of tiles and the wall of reports read before the verdict.
OWN-WORLD: the site's world: near-black `#0E100F` ground, `#161917` panels, `#2A2E2B` hairlines, bone text, lime `#C6EC5A` only for proven/passed, coral `#FF9B73` for what needs the owner (Open, FAIL, stale, missing, unavailable), dim grey for not run / not recorded. System mono for section numbers, ids, paths, counts, commands and outputs; system sans for prose at a 70ch measure. Square corners, 1px rules, no shadows, no gradients. States are words in mono, colored, never glyphs alone. The agent's notes carry a small "written by the agent" label; data blocks carry their source path.
STORY: the reader takes the verdict from the summary in about 30 seconds, then opens only what doesn't add up: a criterion's coverage, a check's steps, expected and actual result and evidence, a lens's findings, the ship and rollback steps, a full report.
FIRST VIEWPORT: title and the status strip (the PR and the count of decisions and blockers; CI and the stamps live in GATES only); then the summary in labelled mono rows (TASK, SOLUTION, REVIEW, TESTS, GATES) and the three lists: YOUR DECISIONS in coral with a recommendation each, BLOCKERS the agent owns, WARNINGS; nothing else before them. Approved by the owner as a one-column ASCII preview (2026-10-26).
FORM: candidate 1 of 7 (the reference's numbered decision post), chosen by the user's reference over the dealt roll (seed key 7e9d9faf); the status strip borrows the gate line of candidate 6.
FINISH: unreviewed and unfinished until the finish reviewer returns ship.

Signature moment: the coverage matrix, criteria by test level, each cell a result linking to its check; opening a check shows Steps, Expected, Actual and Evidence (screenshots, a playable video, the log's tail) without script. Section 02's result filter (CSS radios and `:has()`, empty level groups hidden) and `:target` highlights stay.

Unresolved: none.
