# Changelog

Each pull request adds a line under Unreleased; CI checks it, unless the pull request is labeled "no changelog" because nothing changes for someone using the kit. A release moves those lines under a new version, sets `VERSION` to it and tags the commit `v<version>`. Versions follow [semantic versioning](https://semver.org): a minor version while the kit is below 1.0, a major one for anything that breaks an installed profile. 0.1.0 and 0.2.0 group the work before versioning started, by day, and have no tags.

## [Unreleased]

- Asking for Linear, or another service a profile guards, in your message approves the agent's writes to it until your next message; a write you didn't ask for still needs your approval (#23).

- The status line shows "PR open" for a pull request opened without follow-pr, instead of an earlier step (#21).
- On an open pull request, the status line lists the checks that no longer cover the change, as in "PR open · redo self-review" (#22).
- The spec skill gets a second reading from the other model family and a lint for its shape; the self-review flags changes no criterion asks for, follow-pr logs misread requirements as spec escapes, lens runs logged before a branch rename still count for the PR, and a spec written in a sandbox moves out of `$TMPDIR` once `.git` is writable (#24).
- A verify that exits 0 without running any check no longer counts as a pass: the status line says "verify checked nothing", and validate and create-pr ask for the tests run instead. The shell guard recognises the kit's own PRs when `~/.agents` is a worktree (#25).
- The spec skill maps the change before building (ways in, derived data, failures), and the self-review starts its tables from that map. A command the shell guard blocks now says that nothing in it ran, so steps chained into it aren't taken as done (#26).
- Specs are built test first: each criterion's test is seen failing before the code, and the self-review asks for that output. `install.sh --doctor`, and with it the weekly health check, warns when Codex no longer runs, naming a dangling link. The shell guard blocks `rm -rf ~` when the home directory is reached through a symlink, and the kit's hook tests no longer touch your `~/.agents` (#27).
- triage names each dependency a change adds (package.json, composer.json, requirements, go.mod, Cargo.toml, deps.txt) and sends it to the Maintainability lens. Specs record the decisions of one-way doors in `## Decisions`, which the lint checks and the PR description reuses. The stop hook asks once about each changed code file the spec's Change map doesn't name, and the syntax check after editing a Python file no longer writes bytecode into the repo (#28).
- `bin/ci-wait` waits for the CI of the commit you pushed and prints the failing lines of a failed job; follow-pr uses it. After a turn that pushed, the stop hook asks the agent about that commit's CI when it failed, is still running or can't be read. A Python syntax error after an edit reaches the agent as plain text, not a colour-coded traceback (#29).
- In Codex, the shell guard only lets a PR command through when it first `cd`s to an absolute path: Codex doesn't pass a command's workdir to hooks (openai/codex#33986), so the review gate could judge the wrong checkout.

## [0.4.0] - 2026-09-28

- triage no longer reads a git checkout as a payments signal (#7).
- The shell guard no longer blocks commands that only mention `gh pr create` in quoted text or heredocs (#8).
- Monthly review mining runs the model with no shell or network, writing only its run folder and this month's proposal (#9).
- Branches like `feature/x` and `feature-x` no longer share a spec, reports and stamps; files saved under the old name move on first use (#10).
- `install.sh` warns when profiles, or a profile and the kit, use the same name for a skill, overlay, doc or MCP server (#12).
- The install test can no longer write into a real program it runs, and fails if one changes (#14).
- The shell guard no longer blocks text that only mentions a substitution in single quotes or quoted heredocs, and gates `eval` and `sh -c` (#15).
- CI checks that each pull request adds a line here, unless it's labeled "no changelog" (#16).
- Monthly review mining checks each semgrep rule the model drafts against its own examples and adds the results to the proposal (#17).
- The private-terms check also reads pull requests opened through `eval` or `sh -c`, and a `cd` that fails no longer crashes the shell guard, which let the command run (#20).

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
