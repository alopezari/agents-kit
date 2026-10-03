// What each command and agent type of the mod offers, and what Codex has instead: bin/docs prints it in
// docs/framework.md, and tests/flow.test.ts fails when the mod registers a command or agent, or draws a button,
// this list doesn't name.
// Kept as one JSON value after the `=`, which bin/docs reads without running JavaScript.
export const FEATURES = [
  {
    "command": "flow",
    "description": "Show where this branch is in the kit's flow, and run verify without a turn",
    "capabilities": [
      {"name": "Phase and reports", "codex": "`bin/reports brief`"},
      {"name": "Once the session's own checkout is detached (validate frees the branch that way), the branch in the main checkout", "codex": "the commands of this list, run from the main checkout"},
      {"name": "Stamps (verify, self-review, validate, staging), current or not, and a verify that checked nothing", "codex": "`python3 ~/.agents/hooks/review_stamp.py check --kind <kind>`"},
      {"name": "The last verify report's ran/skipped/warning/error lines", "codex": "`cat \"$(~/.agents/bin/reports path verify)\"`"},
      {"name": "CI of the pushed HEAD", "codex": "`bin/ci-wait --once --no-log`"},
      {"name": "Context use", "codex": "nothing (Codex shows its own)"},
      {"name": "Run verify button, without a turn, on the checkout the pane shows", "codex": "`python3 ~/.agents/hooks/stop_checks.py verify` in a terminal"},
      {"name": "Refresh button, and a refresh after each turn while the pane is open", "codex": "running the commands above again"},
      {"name": "The staging guide's steps before the merge, each with its latest result, who gave it, and its evidence files", "codex": "`bin/staging steps --json`"},
      {"name": "Pass button on each step before the merge, recorded as the user's verdict", "codex": "`bin/staging mark <step> PASS --by user` in a terminal (the shell guard refuses it from the agent)"},
      {"name": "Fail button on each step before the merge, recorded as the user's verdict", "codex": "`bin/staging mark <step> FAIL --by user` in a terminal"}
    ]
  },
  {
    "agentPrefix": "review",
    "description": "One reviewer agent type per self-review lens",
    "capabilities": [
      {"name": "One for each lens `bin/triage --lens-briefs` prints: its brief from lenses.md, no edit tools, no CLAUDE.md block", "codex": "a subagent given the lens text from `skills/self-review/lenses.md`"}
    ]
  }
]
