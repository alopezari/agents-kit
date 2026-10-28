#!/bin/bash
# Print a self-contained review prompt: lens brief + goal + the full change (committed and
# uncommitted vs the merge-base, plus untracked files), so a reviewer needs no tool access.
# With the repo's review guide (~/.agents/repos/<repo>/review.md) when there is one, and, on a re-check, the findings
# earlier rounds decided (the report's lines), so the reviewer doesn't raise them again. The Maintainability prompt also
# holds bin/structure-facts' output.
# Usage: bundle.sh <base-ref> <lens-name> "<goal>" [<decided-file>]   e.g. bundle.sh origin/trunk Correctness "Require Compose v2"
set -euo pipefail
base=$1 lens=$2 goal=$3 decided=${4-}
here=$(cd "$(dirname "$0")" && pwd)
merge_base=$(git merge-base HEAD "$base")
guide="$HOME/.agents/repos/$("$HOME/.agents/bin/repo-name")/review.md"
# Read both before printing anything: a prompt missing either would raise again what the user already settled.
guide_text=""
if [ -e "$guide" ]; then guide_text=$(cat "$guide") || { echo "bundle.sh: can't read the review guide $guide" >&2; exit 1; }; fi
decided_text=""
if [ $# -ge 4 ] && [ -z "$decided" ]; then echo "bundle.sh: no decided-findings file: the 4th argument is empty" >&2; exit 1; fi
if [ -n "$decided" ]; then decided_text=$(cat "$decided") || { echo "bundle.sh: can't read the decided findings $decided" >&2; exit 1; }; fi
facts=""
if [ "$lens" = Maintainability ]; then
  facts=$("$here/../../bin/structure-facts" "$base" 2>&1) || facts="not measured: structure facts: structure-facts failed: $facts"
fi

echo "You are reviewing a code change. Goal of the change: $goal"
spec="$("$here/../kit-spec/path.sh")"
if [ -f "$spec" ]; then echo; echo "The change was built against this spec:"; echo; cat "$spec"; fi
echo
awk -v lens="## $lens" '$0==lens{on=1;next} /^## /{on=0} on' "$here/lenses.md"
if [ -n "$guide_text" ]; then
  echo
  echo "The repository's review guide says what failure looks like here and what to skip. Apply it; where it conflicts with the instructions below (a nit cap, say), the guide wins:"
  echo
  echo "$guide_text"
fi
if [ -n "$decided_text" ]; then
  echo
  echo "These findings were already decided in an earlier round, each with its verdict and evidence. Don't report them again, nor a known finding that is still there. Report only a new instance at another location, or a regression a later fix introduced:"
  echo
  echo "$decided_text"
fi
if [ -n "$facts" ]; then
  echo
  echo "Measured structure facts for this change, from ~/.agents/bin/structure-facts. Read them as the Maintainability brief above says:"
  echo
  echo "$facts"
fi
echo
echo "Report every issue you suspect, each with file:line, the failing scenario, a severity and a confidence (high/medium/low); low-confidence findings are welcome, because they are verified afterwards. Severity is one of: Important (a defect the change introduces, or makes reachable), Nit (a minor point; at most three), or Pre-existing (already on the base branch, and neither introduced nor newly reached by the change). Report a defect as a class: search the change for every other place with the same pattern and list each file:line under that one finding. If the diff lacks context you need, say which file and why instead of guessing. End with a final line reading exactly \"## Found\", then your tables and every finding below it: only that section is read."
echo
echo '```diff'
git diff -U15 "$merge_base"
git ls-files --others --exclude-standard | while read -r f; do git diff --no-index -U15 /dev/null "$f" || true; done
echo '```'
