# Sample profile

A profile layers everything specific to one kind of work (a job, a client, your open source) on top of the kit, from its own repository. This one is a working example with one of each extension point, for a made-up WordPress plugin called `example-plugin`. Copy it into a new repository, replace the contents, and install it:

```bash
cp -R ~/.agents/examples/sample-profile ~/profiles/work && git -C ~/profiles/work init
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
| `private-terms.txt` | Words that must never reach the public kit (ticket prefixes, internal names) | commits and pull requests to the kit that match are refused |
| `deps.txt` | Programs this work needs, in the core's format | `install.sh` installs or reports them |
| `review-mining/repos.txt`, `hosts.txt` | Repositories whose review comments the monthly job learns from, and extra GitHub hosts | read by `review-mining/` |
| `PROFILE.md` | Context for the monthly trends scan | read by `monitors/trends.sh` |
| `statusline` | An executable that adds a segment to the Claude Code status line | run by the kit's status line with the same JSON on stdin |

Everything is optional: leave out what you don't need. Several profiles can be installed at once; their lists add up.
