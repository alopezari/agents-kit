# Changelog

Each pull request adds a line under Unreleased. A release moves those lines under a new version, sets `VERSION` to it and tags the commit `v<version>`. Versions follow [semantic versioning](https://semver.org): a minor version while the kit is below 1.0, a major one for anything that breaks an installed profile.

## [Unreleased]

## [0.3.0] - 2026-09-26

- The status line's flow segment names the branch (#1).
- CI runs the test suite on macOS for every pull request (#2).
- Profiles can list private terms that the kit's commits and pull requests must never contain (#3).
- `uninstall.sh` removes what `install.sh` wired in, and only that (#4).
- `examples/sample-profile` has one of each extension point, to copy as the start of your own profile (#6).

## [0.2.0] - 2026-09-25

- `deps.txt` declares every program the kit runs. `install.sh` asks before installing missing ones, checks minimum versions and merges each profile's dependencies.
- Install with one line: clone, then run `install.sh`.
- A public landing page and docs site, built from the kit's sources.
- The monthly analysis measures escapes and cost per merged PR against a baseline from before the kit.
- The phase shows the furthest step done, and verify lints only files under `--cwd`.

## [0.1.0] - 2026-09-24

- One setup for Claude Code, Codex and Pi: shared instructions, skills for the path of a change (spec, self-review, validate, create-pr, follow-pr, ship), the shell guard, review stamps, verify on stop, the status line's flow phase, and the harness baseline settings.
- `install.sh` wires it into each installed harness, and `--doctor` reports without changing anything.
