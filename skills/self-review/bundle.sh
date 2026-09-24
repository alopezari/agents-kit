#!/bin/bash
# Print a self-contained review prompt: lens brief + goal + the full change (committed and
# uncommitted vs the merge-base, plus untracked files), so a reviewer needs no tool access.
# Usage: bundle.sh <base-ref> <lens-name> "<goal>"   e.g. bundle.sh origin/trunk Correctness "Require Compose v2"
set -euo pipefail
base=$1 lens=$2 goal=$3
here=$(cd "$(dirname "$0")" && pwd)
merge_base=$(git merge-base HEAD "$base")

echo "You are reviewing a code change. Goal of the change: $goal"
spec="$("$here/../spec/path.sh")"
if [ -f "$spec" ]; then echo; echo "The change was built against this spec:"; echo; cat "$spec"; fi
echo
awk -v lens="## $lens" '$0==lens{on=1;next} /^## /{on=0} on' "$here/lenses.md"
echo
echo "Report every issue you suspect, each with file:line, the failing scenario, a severity and a confidence (high/medium/low); low-confidence findings are welcome, because they are verified afterwards. If the diff lacks context you need, say which file and why instead of guessing."
echo
echo '```diff'
git diff -U15 "$merge_base"
git ls-files --others --exclude-standard | while read -r f; do git diff --no-index -U15 /dev/null "$f" || true; done
echo '```'
