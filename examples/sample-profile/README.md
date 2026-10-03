# Sample profile

A profile layers everything specific to one kind of work (a job, a client, your open source) on top of the kit, from its own repository. This one is a working example with one of each extension point, for a made-up WordPress plugin called `example-plugin`. Copy it into a new repository, replace the contents, and install it:

```bash
mkdir -p ~/profiles && cp -R ~/.agents/examples/sample-profile ~/profiles/work && git -C ~/profiles/work init
~/.agents/install.sh --profile ~/profiles/work
```

Keep the repository private when it holds anything about your employer or clients. `tests/test_install.sh` installs this sample to check that every extension point works.

| Path | What it does | How the kit uses it |
|---|---|---|
| `repos/example-plugin/notes.md` | What an agent should know before working in that repository | linked to `~/.agents/repos/example-plugin/`; `AGENTS.md` sends agents there |
| `repos/example-plugin/verify` | Checks run when an agent stops after editing that repository; exit non-zero to send it back to work | run by the Stop hook from the repository root |
| `repos/example-plugin/rules.semgrep.yml` | A project rule the `verify` applies to changed lines only | read by `verify` |
| `skills/release-notes/` | A skill only this kind of work needs | linked into every harness's skills |
| `research/*` | Studies built from this work's data, one file each | each file linked into `~/.agents/research/` |
| `mcp-writes.json` | Which MCP tool calls write to shared systems; the guard blocks them until you approve that service | read by `hooks/guard_mcp.py` |
| `personal-repos.txt` | Owners whose repositories are yours alone; elsewhere Impeccable's project files wait for your say-so | read by `hooks/design_files.py` |
| `private-terms.txt` | Words that must never reach the public kit (ticket prefixes, internal names) | commits and pull requests to the kit that match are refused |
| `deps.txt` | Programs this work needs, in the core's format | `install.sh` installs or reports them |
| `review-mining/repos.txt`, `hosts.txt` | Repositories whose review comments the monthly job learns from, and extra GitHub hosts | read by `review-mining/` |
| `PROFILE.md` | Context for the monthly trends scan | read by `monitors/trends.sh` |
| `statusline` | An executable that adds a segment to the Claude Code status line | run by the kit's status line with the same JSON on stdin |
| `launchd/sample-weekly.plist`, `weekly.sh` | A scheduled job: every Monday at 9:00 it prints how many turns the after-turn counted | rendered into `~/Library/LaunchAgents/` and loaded by `install.sh`, like the kit's jobs, with `__PROFILE__` for this profile's directory; `uninstall.sh` removes it |
| `after-turn` | An executable that runs after every agent turn; this one counts turns in the overlay's repository | started in the background by the Stop hook with its JSON on stdin; nothing waits for it, and sessions it starts don't start it again |

Everything is optional: leave out what you don't need. Several profiles can be installed at once and their lists add up, but a skill, repo overlay or direct MCP server with the same name in two profiles is taken from only one of them, so keep those names unique.
