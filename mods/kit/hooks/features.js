// What each command of the mod offers, and what Codex has instead: bin/docs prints it in docs/framework.md,
// and tests/flow.test.ts fails when the mod registers a command, or draws a button, this list doesn't name.
// Kept as one JSON value after the `=`, which bin/docs reads without running JavaScript.
export const FEATURES = [
  {
    "command": "flow",
    "description": "Show where this branch is in the kit's flow, and run verify without a turn",
    "capabilities": [
      {"name": "Phase and reports", "codex": "`bin/reports brief`"},
      {"name": "Stamps (verify, self-review, validate, staging), current or not, and a verify that checked nothing", "codex": "`python3 ~/.agents/hooks/review_stamp.py check --kind <kind>`"},
      {"name": "The last verify report's ran/skipped/warning/error lines", "codex": "`cat \"$(~/.agents/bin/reports path verify)\"`"},
      {"name": "CI of the pushed HEAD", "codex": "`bin/ci-wait --once --no-log`"},
      {"name": "Context use", "codex": "nothing (Codex shows its own)"},
      {"name": "Run verify button, without a turn, on the checkout the session is in", "codex": "`python3 ~/.agents/hooks/stop_checks.py verify` in a terminal"},
      {"name": "Refresh button, and a refresh after each turn while the pane is open", "codex": "running the commands above again"}
    ]
  }
]
