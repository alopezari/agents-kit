# agents-kit

One setup for every coding agent you use (Claude Code, Codex, Pi): the same instructions, skills, guardrails and checks, whichever harness runs the work. Agents are told to work like a staff engineer, and hooks check that they did: irreversible and outward-facing commands stop and wait for you, every turn that edits code is verified, and a pull request can't be opened until the exact change has been self-reviewed.

**[docs/framework.md](docs/framework.md)** is the full reference: every hook, guard rule, check, skill, tool and scheduled job, with diagrams. It is generated from the code on every commit, so it describes what the kit does now.

macOS only for now (the scheduled jobs use launchd).

## Install

```bash
git clone https://github.com/alopezari/agents-kit.git ~/.agents
~/.agents/install.sh            # wire every installed harness (safe to re-run)
~/.agents/install.sh --doctor   # report what's missing, change nothing
~/.agents/tests/run.sh          # regression suite (passes once --doctor is clean)
```

The kit must live at `~/.agents`. The installer:

- installs missing required and recommended programs from `deps.txt` through Homebrew;
- links `AGENTS.md` as each harness's global instructions and the skills into each harness;
- registers the hooks next to any hooks already there;
- clones the third-party skills in `skills.external` and installs the Node dependencies of `tools/` and `site/`;
- sets the Claude Code status line and enables the kit's git hooks (they keep `docs/framework.md` current);
- installs the scheduled jobs;
- when git uses a per-host proxy, offers to link `bin/gh` into `/usr/local/bin` (asks for your password).

It also adds the kit's baseline harness settings (no fast mode, effort defaults) wherever a key is missing, without overwriting one you set. Files it replaces are backed up under `backups/`.

What it doesn't do, on a new machine:

1. Install or log in to the harnesses (`claude`, `codex`, `pi`) and `gh`. Install them before running `install.sh`, which only wires the harnesses it finds.
2. Trust the Codex hooks. Open Codex once and approve them; `install.sh --doctor` warns until you do.
3. Install harness plugins or MCP servers. Add the ones you use yourself, or keep their setup in a profile.

Requirements: [`deps.txt`](deps.txt) lists every program the kit runs. `install.sh` installs the missing required ones (`python3`, `git`, `jq`, `node`, `gh`) and recommended ones (`semgrep`, `gitleaks`) through Homebrew, and says how to install the optional ones, each needed by one feature. `install.sh --doctor` reports what is missing, and `tests/run.sh` expects it to report nothing.

## Make it yours

- **Instructions:** edit `AGENTS.md`. It is plain Markdown and every harness reads the same file.
- **A repository:** add `repos/<checkout-directory-name>/notes.md` with the context agents should read there. Add an executable `verify` when the automatic checks aren't enough. Nothing is written into the repository itself. See [repos/README.md](repos/README.md).
- **A profile:** keep anything tied to one employer or client in a separate private repository and layer it in with `install.sh --profile <dir>`. It can hold repo overlays, work-only skills, research, MCP write rules and the repositories to learn from. The core stays generic and shareable.

## Hook contract

Every hook is a small program with one contract: a JSON payload on stdin, a JSON decision on stdout. Claude Code and Codex share this contract natively. Other harnesses need an adapter that translates their events into it.

**Payload fields used:** `tool_name`, `tool_input.command`, `tool_input.file_path` (or `notebook_path`, or the file list in Codex's `apply_patch` input), `cwd`, `session_id`, `stop_hook_active`, and `turn_id` to tell Codex apart in the log.

**Decisions:**
- Deny a command: `{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": ...}}`.
- Block after an edit or at stop: `{"decision": "block", "reason": ...}`, optionally with `systemMessage` for the user.

## Adding a harness

1. Point its global instructions file at `AGENTS.md`, and its skills directory at `skills/` if it has one.
2. Write `adapters/<harness>/` to translate its events into the payloads above. `adapters/pi/agents-kit.ts` is the reference:
   - before a command → `guard_bash.py`;
   - after an edit → `post_edit.py`;
   - at idle or stop → `stop_checks.py`, with a guard so the stop check continues the agent only once per user turn.
3. Add a section to `install.sh` and run `install.sh --doctor`. Then test with a harmless blocked command in a scratch repo.
4. Teach `bin/docs` where the new harness registers its hooks (`HARNESS_OF_SETTINGS` and `harness_hooks`), then commit: the pre-commit hook regenerates `docs/framework.md`.

## Not in git

`logs/`, `backups/`, `research/`, `approvals/`, `monitors/state/`, `review-mining/runs/`, `review-mining/baseline.json` and `usage/*.json` hold local, possibly private data; `site/dist/` is the built website. Profiles are linked in and never committed here.

## License

MIT, see [LICENSE](LICENSE).
