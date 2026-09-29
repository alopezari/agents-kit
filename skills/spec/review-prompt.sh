#!/bin/bash
# Print a self-contained prompt for a second reading of the spec: the brief, the request (read from stdin) and the
# current branch's spec, so a reviewer from the other model family needs no tool access.
# Usage: gh issue view 12 --comments | review-prompt.sh | codex exec --ephemeral --skip-git-repo-check -s read-only -
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
request=$(cat)
[ -n "${request//[[:space:]]/}" ] || { echo "review-prompt.sh: pass the request (issue and comments) on stdin" >&2; exit 2; }
spec="$("$here/path.sh")"
[ -f "$spec" ] || { echo "review-prompt.sh: no spec for this branch at $spec; write it first" >&2; exit 2; }

cat <<'EOF'
You are reading a spec before any code is written. An agent will build exactly what it says, and a self-review will
judge the change against its acceptance criteria, so a misread here is built well and passes review. Find where the
spec would make the agent build the wrong thing. Compare it with the request below and report:

- Requirements in the request, its comments or linked discussion that the spec misreads, narrows or leaves out.
- Edges the request implies but the spec doesn't cover: other entry points (CLI, cron, REST, admin UI), existing data
  and settings, empty and failure states, backwards compatibility.
- Criteria that can't be checked as written, or whose verify step would still pass if the behavior were wrong.
- Scope the spec adds that the request didn't ask for.
- Assumptions that should be open questions, because the answer would change the work.

For each, quote the line of the request or spec it's about, say what's wrong and what the spec should say instead,
with a confidence (high/medium/low); low-confidence findings are welcome, because they are verified afterwards. Don't
propose an implementation. If nothing is wrong, say so in one line.
EOF
printf '\n## Request\n\n%s\n\n## Spec\n\n' "$request"
cat "$spec"
