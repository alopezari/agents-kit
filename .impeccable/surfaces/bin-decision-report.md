---
version: 1
slug: "bin-decision-report"
primary_target: "bin/decision-report"
related_targets: []
---

# Decision page (`bin/reports html`)

Scope: one generated, local, static HTML page per branch. Mode: Read. Audience: the kit's owner deciding a merge, who may not know the project; sometimes a colleague the page is forwarded to. The investigation page shares only the site's palette and keeps its own structure and stylesheet (`STYLE`); the change page has its own (`CHANGE_STYLE`).

## Direction contract
THESIS: a control panel for one question: is this PR safe to merge, or is something missing that could compromise system stability or the user's experience? The page explains the problem, the solution, the testing and the review so that they're very easy to understand, always with the user in mind.
OWN-WORLD: the site's world: near-black `#0E100F` ground, `#161917` panels, `#2A2E2B` hairlines, bone text, lime `#C6EC5A` only for proven and passed, coral `#FF9B73` for what needs the owner (Open, FAIL, stale, missing, unavailable), dim grey for what's recorded as absent.
STORY: the claim, the lead, the verdict, then one sentence each on the problem, stability, your experience, testing, code review, not proven, undo and your decisions. Each sentence links to its section; a section can be read alone, and anything that blocks the merge stays open.
FIRST VIEWPORT: claim title, lead, verdict sentence, a folded "Why this verdict" holding the written rule and every reason, then the panel. No pipe- or dot-separated status tokens outside that fold.
FORM: one column of native `<details>` sections, open by default, with long lists collapsed behind their count. Testing comes before Code review. Tests are grouped by layer, each carrying its layer in words, never by colour alone. Fact sheets for the problem and solution; a project map and a regression table in the solution.
MOTION: only inside a scene figure, on one continuous timeline per figure: every shape, the lit caption and the progress bar share it, moved by per-figure keyframes named through `--k`, never frame by frame. Four radios and two identical copies give play, pause and restart with no script; restart swaps the copies, which starts them at 0. The default state is the whole picture, and reduced motion and print hold it still with the controls and bar gone. A page without a scene carries none of this CSS. A series of images stays a still strip.
RULES: a generated sentence comes only from recorded counts and labels; the agent's prose and judgment are labelled as such; a missing field reads "Not recorded", a missing impact "Impact not recorded". An id a link targets sits inside its `<details>`, never on it, or the link wouldn't open it.

Signature moment: the verdict sentence and its panel, whose every word links to the evidence behind it: a test opens to what it did, the case, expected, actual and its evidence inline (the log's meaningful lines with exit code and commit, images, a playable video).

