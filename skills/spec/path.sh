#!/bin/bash
# Print the spec path for the current repo and branch. An existing spec wins; otherwise prefer
# the repo's shared .git/agents/ (outside the working tree) and fall back to $TMPDIR when a sandbox
# makes .git read-only. Shared, not per-worktree: removing a worktree deletes its own git dir, and
# the spec and reports must survive the hand-off to the main checkout.
set -euo pipefail
common="$(git rev-parse --path-format=absolute --git-common-dir)"
repo="$("$HOME/.agents/bin/repo-name")"
name="spec-$repo-$(git branch --show-current | tr '/' '-').md"
in_git="$common/agents/$name"
in_tmp="${TMPDIR:-/tmp}/agents-specs/$name"
# Where specs lived before they moved to the shared git dir.
legacy="$(git rev-parse --absolute-git-dir)/agents/spec-$(basename "$(git rev-parse --show-toplevel)")-$(git branch --show-current | tr '/' '-').md"
for candidate in "$in_git" "$in_tmp" "$legacy"; do
  [ -f "$candidate" ] && { echo "$candidate"; exit 0; }
done
# A spec written before `git branch -m` sits under the old name; move it (and its reports) across.
python3 "$HOME/.agents/hooks/review_stamp.py" follow-renames
for candidate in "$in_git" "$in_tmp"; do
  [ -f "$candidate" ] && { echo "$candidate"; exit 0; }
done
if mkdir -p "$(dirname "$in_git")" 2>/dev/null && [ -w "$(dirname "$in_git")" ]; then
  echo "$in_git"
else
  mkdir -p "$(dirname "$in_tmp")"; echo "$in_tmp"
fi
