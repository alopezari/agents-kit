#!/bin/bash
# Print the spec path for the current repo and branch. An existing spec wins; otherwise prefer
# .git/agents/ (outside the working tree) and fall back to $TMPDIR when a sandbox makes .git read-only.
set -euo pipefail
name="spec-$(basename "$(git rev-parse --show-toplevel)")-$(git branch --show-current | tr '/' '-').md"
in_git="$(git rev-parse --absolute-git-dir)/agents/$name"
in_tmp="${TMPDIR:-/tmp}/agents-specs/$name"
for candidate in "$in_git" "$in_tmp"; do
  [ -f "$candidate" ] && { echo "$candidate"; exit 0; }
done
if mkdir -p "$(dirname "$in_git")" 2>/dev/null && [ -w "$(dirname "$in_git")" ]; then
  echo "$in_git"
else
  mkdir -p "$(dirname "$in_tmp")"; echo "$in_tmp"
fi
