# Personal per-repo overlays

One directory per repository, named after the repo's main checkout directory (e.g. `my-plugin` for `~/Projects/my-plugin`): `~/.agents/bin/repo-name` prints it, and it is the same from any worktree of that repo, whatever the worktree's directory is called. Nothing here is committed to the repositories.

- `notes.md`: context agents read before working in that repo (commands, traps, conventions I care about). Referenced from `~/.agents/AGENTS.md`.
- `verify` (executable, optional): run from the repo root by the Stop hook (`~/.agents/hooks/stop_checks.py`) after a session edits files. Exit non-zero to make the agent keep working. Keep it under 10 minutes. Most use `_shared/verify_changed.py`: semgrep, phpcs and PHPStan on changed lines, plus the unit suite compared against `phpunit-baseline.txt`, so pre-existing failures never block.

Without a `verify` overlay, the Stop hook runs `_shared/verify_auto.py`: it detects the stack from the changed files and runs the project's own linters and the tests related to the change (ESLint and Vitest/Jest; `php -l`, PHPCS, PHPStan and PHPUnit; Ruff and pytest; gofmt, go vet and go test; cargo check; ShellCheck). A tool that isn't installed or configured is reported as `skipped:`, never as a pass. Write an overlay when a repo needs more: a Docker stack, a baseline of known failures, or project-specific rules.

"The change" is everything since the branch left the default branch, committed or not, plus untracked files.

## Profiles

Overlays for work repositories usually live in a private profile repo, not here. `~/.agents/install.sh --profile <dir>` links the profile's `repos/*` into this directory; see the main README.
