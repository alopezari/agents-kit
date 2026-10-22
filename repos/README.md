# Personal per-repo overlays

One directory per repository, named after the repo's main checkout directory (e.g. `my-plugin` for `~/Projects/my-plugin`): `~/.agents/bin/repo-name` prints it, and it is the same from any worktree of that repo, whatever the worktree's directory is called. Nothing here is committed to the repositories.

- `notes.md`: context agents read before working in that repo (commands, traps, conventions I care about). Referenced from `~/.agents/AGENTS.md`.
- `ship.md` (optional): how the repo deploys, checks and rolls back, and whether it is installed on this machine, under the headings `## Deploy`, `## Smoke checks`, `## Rollback` and `## Installed on this machine`. The `kit-ship` skill follows it after the merge, and drafts it the first time.
- `verify` (executable, optional): run from the repo root by the Stop hook (`~/.agents/hooks/stop_checks.py`) after a session edits files. Exit non-zero to make the agent keep working. Print `ran: <check>` for every check that ran: a green run with none counts as having checked nothing, and the flow says so instead of treating it as a pass. Keep it under 10 minutes. Most use `_shared/verify_changed.py`: semgrep, phpcs and PHPStan on changed lines, plus the unit suite compared against `phpunit-baseline.txt`, so pre-existing failures never block.
- `review.md` (optional): the review guide the review's reviewers apply in that repo, and that wins over the kit's defaults where they conflict: what failure looks like there ("docs that lie", "a config that misroutes"), paths to skip, a nit cap. Keep it short; the good ones are a handful of lines.
- `packs` (optional): which of triage's framework packs apply here, one per line: `wordpress` turns the WordPress pack on in a repo that doesn't look like WordPress, `-wordpress` turns it off in one that does; `#` starts a comment. It wins over detection, and triage stops on a name it doesn't know.
- `wp-cli` (executable, optional): forwards its arguments to WP-CLI inside the repo's local stack; `bin/wp-query-profile` needs it.

Without a `verify` overlay, the Stop hook runs `_shared/verify_auto.py`: it detects the stack from the changed files and runs the project's own linters and the tests related to the change (ESLint and Vitest/Jest; `php -l`, PHPCS, PHPStan and PHPUnit; Ruff and pytest; gofmt, go vet and go test; cargo check; ShellCheck). A tool that isn't installed or configured is reported as `skipped:`, never as a pass. Write an overlay when a repo needs more: a Docker stack, a baseline of known failures, or project-specific rules.

"The change" is everything since the branch left the default branch, committed or not, plus untracked files.

## Profiles

Overlays for work repositories usually live in a private profile repo, not here. `~/.agents/install.sh --profile <dir>` links the profile's `repos/*` into this directory; see the main README.
