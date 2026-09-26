---
name: release-notes
description: Draft release notes for example-plugin from the merged pull requests since the last tag. Use when asked for release notes, a changelog entry or what changed since the last release.
---

# Release notes

1. Find the last tag: `git describe --tags --abbrev=0`.
2. List what merged since: `gh pr list --state merged --limit 500 --search "merged:>=$(git log -1 --format=%cs <tag>)" --json number,title,labels`.
3. Group by label (`feature`, `fix`, anything else under "Other"), one line per pull request, written for users: what changed for them, not how.
4. Show the draft; the user publishes it.
