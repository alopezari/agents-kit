---
version: 1
slug: "bin-decision-report"
primary_target: "bin/decision-report"
related_targets: []
---

# Decision page (`bin/reports html`)

Scope: one generated, local, static HTML page per branch. Mode: Read, with Operate density in section 04. Audience: the kit's owner deciding a merge (glance, then detail); sometimes a colleague the page is forwarded to by hand, who lacks the session. Job: tell the reader what the task must do, what the change does, what it changes for users, what was tested and how, how to test it themselves, and what they must decide. Reference: the user's own decision page for an earlier change (numbered sections, Simplified Technical English, small diagrams, a counts line, a filterable check list, a step-by-step test guide, a decisions list, an identifiers footer). Constraints: no scripts, no network, no web fonts (CSP `default-src 'none'`); the agent's notes are labelled as the agent's, every count comes from the reports; absence (missing, stale, not run, unavailable, not recorded) is named, never implied green. WCAG 2.2 AA, readable at 200% zoom, works at 360px.

## Direction contract
THESIS: a decision post, read top to bottom like the reference: six numbered questions, each answered in plain words first and proven by data right below. It refuses the dashboard of tiles and the wall of collapsed reports.
OWN-WORLD: the site's world: near-black `#0E100F` ground, `#161917` panels, `#2A2E2B` hairlines, bone text, lime `#C6EC5A` only for proven/passed, coral `#FF9B73` for what needs the owner (Open, FAIL, stale, missing, unavailable), dim grey for not run / not recorded. System mono for section numbers, ids, paths, counts, commands and outputs; system sans for prose at a 70ch measure. Square corners, 1px rules, no shadows, no gradients. States are words in mono, colored, never glyphs alone. The agent's notes carry a small "written by the agent" label; data blocks carry their source path.
STORY: the reader learns the task and the change in plain words, sees what it changes for users, checks the counts and every check, can rerun the guide's steps, and ends on the list of what they must decide.
FIRST VIEWPORT: title (branch or PR title) and a one-line purpose; a status strip in mono: PR, CI for the head, verify, review, validate, and "N items to decide" linking to 06 in coral (or "nothing to decide" in lime); then 01 begins with the agent's explanation and its figure.
FORM: candidate 1 of 7 (the reference's numbered decision post), chosen by the user's reference over the dealt roll (seed key 7e9d9faf); the status strip borrows the gate line of candidate 6.
FINISH: unreviewed and unfinished until the finish reviewer returns ship.

Signature moment: section 04's check list filters by result (All / PASS / FAIL / NOT RUN) with CSS radio inputs and `:has()`, and each row opens (`<details>`) to its Expected, observed and evidence; each item in 06 links to its row, highlighted via `:target`. No script.

Unresolved: none.
