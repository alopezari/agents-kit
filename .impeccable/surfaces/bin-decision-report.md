---
version: 1
slug: "bin-decision-report"
primary_target: "bin/decision-report"
related_targets: []
---

# Decision page (`bin/reports html`)

Scope: one generated, local, static HTML page per branch. Mode: Read. Audience: the kit's owner deciding a merge, who may not know the project; sometimes a colleague the page is forwarded to. The change page has its own stylesheet (`CHANGE_STYLE`); the investigation page keeps the site's palette and its own (`STYLE`) until it moves to the same look.

## Direction contract
THESIS: a map of one decision: is this PR safe to merge, or is something missing that could compromise system stability or the user's experience? The verdict is taken in at a glance, and the flow and the evidence are drawn as diagrams rather than told in prose.
OWN-WORLD: a planning map's world, in a light and a dark theme that follow `prefers-color-scheme`: a near-white `#FCFCFC` or near-black `#0A0A0A` ground, white or `#111111` cards with a 3px coloured top edge, 1px hairlines; blue, teal, amber, violet, rose and lime as category colours; green for passed, red for failed, amber for stale, missing and waiting, blue for the owner's call. Every text and state colour meets WCAG AA in both themes, and no state is told by colour alone: each carries its word, and absence is a dashed or dotted outline.
STORY: the claim, the lead, the verdict with every reason that blocks or needs the owner as a chip, a rail of spec, review, validate and CI ending on "Your call", and three short cards for stability, your experience and undo. Then the change (problem and solution cards, before and after, the map, where a regression could show up), testing, code review, not proven and your decisions.
FIRST VIEWPORT: at 1440×900, the claim, the verdict and its chips, the rail and the three cards. The rail shows every state in a word and a colour (passed, failed, missing, stale, switched off, not needed, pending, unavailable) and never shows skipped or unread work as passed.
FORM: diagrams only where the report holds their data: a test map with one row per layer, each check a chip led by its name and result with its id as a small label; a review matrix of lens by fixed, rejected, open, pre-existing and not run, where missing attribution reads unknown; a regression matrix whose check cell is a chip when the check has a result; before and after paired when the report has a table. A criterion shows by its text, never its number. The agent's long prose folds behind a one-line card; empty optional fields are left out. Matrices scroll inside their box on a narrow screen. No eyebrow labels, no side border wider than 1px, no emoji or unicode icons.
MOTION: only inside a scene figure, on one continuous timeline per figure: every shape, the lit caption and the progress bar share it, moved by per-figure keyframes named through `--k`, never frame by frame. Four radios and two identical copies give play, pause and restart with no script; restart swaps the copies, which starts them at 0. The default state is the whole picture, and reduced motion and print hold it still with the controls and bar gone. A page without a scene carries none of this CSS. A series of images stays a still strip.
RULES: a generated sentence comes only from recorded counts and labels; the agent's prose and judgment are labelled as such; a missing field reads "Not recorded", a missing impact "Impact not recorded". An id a link targets sits inside its `<details>`, never on it, or the link wouldn't open it.

Signature moment: the verdict and its chips, each of which links to the evidence behind it: a test opens to what it did, the case, expected and actual, with its evidence one fold below (the log's meaningful lines with exit code and commit, images, a playable video, the script).

