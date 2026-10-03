# agents-kit

One setup for every coding agent you use (Claude Code, Codex, Pi): the same instructions, skills, guardrails and checks, whichever harness runs the work. Agents are told to work like a staff engineer, and hooks check that they did: irreversible and outward-facing commands stop and wait for you, every turn that edits code is verified, and a pull request can't be opened until the exact change has been self-reviewed.

**[docs/framework.md](docs/framework.md)** is the full reference: every hook, guard rule, check, skill, tool and scheduled job, with diagrams. It is generated from the code on every commit, so it describes what the kit does now.

macOS only for now (the scheduled jobs use launchd).

## Install

```bash
git clone https://github.com/alopezari/agents-kit.git ~/.agents && ~/.agents/install.sh
```

`install.sh` wires every installed harness and is safe to re-run. To check the setup afterwards:

```bash
~/.agents/install.sh --doctor   # report what's missing, change nothing
~/.agents/tests/run.sh          # regression suite (passes once --doctor is clean)
```

The kit must live at `~/.agents`. The installer:

- offers to install missing required and recommended programs from `deps.txt` through Homebrew, asking once (`--yes` skips the question; without a terminal it only reports them);
- links `AGENTS.md` as each harness's global instructions and the skills into each harness;
- registers the hooks next to any hooks already there;
- fetches the third-party skills in `skills.external`, a pinned one at its commit into `vendor/`, and installs the Node dependencies of `tools/` and `site/`;
- sets the Claude Code status line and enables the kit's git hooks (they keep `docs/framework.md` current);
- installs the scheduled jobs;
- when git uses a per-host proxy, offers to link `bin/gh` into `/usr/local/bin` (asks for your password).

It also adds the kit's baseline harness settings (no fast mode, effort defaults) wherever a key is missing, without overwriting one you set. Files it replaces are backed up under `backups/`.

To remove it, `~/.agents/uninstall.sh` lists every link, hook, status line and scheduled job that still points at the kit, asks, and removes them (`--yes` skips the question). Your own hooks and settings stay, and so do the baseline settings and the programs installed through Homebrew. It backs up the settings files it edits to `~/.agents-uninstall-backups/`, and leaves `~/.agents` itself for you to delete.

What it doesn't do, on a new machine:

1. Install or log in to the harnesses (`claude`, `codex`, `pi`) and `gh`. Install them before running `install.sh`, which only wires the harnesses it finds.
2. Trust the Codex hooks. Open Codex once and approve them; `install.sh --doctor` warns until you do.
3. Install harness plugins or MCP servers. Add the ones you use yourself, or keep their setup in a profile.

Requirements: [`deps.txt`](deps.txt) lists every program the kit runs. `install.sh` offers to install the missing required ones (`python3`, `git`, `jq`, `node`, `gh`) and recommended ones (`semgrep`, `gitleaks`, `php`) through Homebrew, and says how to install the optional ones, each needed by one feature. `install.sh --doctor` reports what is missing, and `tests/run.sh` expects it to report nothing.

## Make it yours

- **Instructions:** edit `AGENTS.md`. It is plain Markdown and every harness reads the same file.
- **A repository:** add `repos/<checkout-directory-name>/notes.md` with the context agents should read there. Add an executable `verify` when the automatic checks aren't enough. Nothing is written into the repository itself. See [repos/README.md](repos/README.md).
- **A profile:** keep anything tied to one employer or client in a separate private repository and layer it in with `install.sh --profile <dir>`. It can hold repo overlays, work-only skills, research, MCP write rules and the repositories to learn from; [`examples/sample-profile/`](examples/sample-profile/) has one of each to start from. The core stays generic and shareable.

## Hook contract

Every hook is a small program with one contract: a JSON payload on stdin, a JSON decision on stdout. Claude Code and Codex share this contract natively. Other harnesses need an adapter that translates their events into it.

**Payload fields used:** `tool_name`, `tool_input.command`, `tool_input.file_path` (or `notebook_path`, or the file list in Codex's `apply_patch` input), `cwd`, `session_id`, `stop_hook_active`, and `turn_id` to tell Codex apart in the log.

**Decisions:**
- Deny a command: `{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": ...}}`.
- Block after an edit or at stop: `{"decision": "block", "reason": ...}`, optionally with `systemMessage` for the user.

## Mods contract

Claude Code also loads the kit as a plugin, `mods/kit/`, whose [mod](https://code.claude.com/docs/en/plugins/mods/overview) runs inside Claude Code: it can draw panes and run commands without a turn. Mods exist only in Claude Code, so the kit keeps working the same with or without them. Every mod follows these rules:

1. **It owns no workflow state.** Specs, stamps, reports and evidence stay in `.git/agents/` and go through `bin/` and `hooks/review_stamp.py`, so a change started in Claude Code can be finished in Codex, and the other way round. What those tools cache or migrate follows their own rules.
2. **It owns no safety.** The Python hooks decide what is blocked, in every harness. A mod can only add to that in Claude Code. A disabled or failed mod leaves the behavior as it was without it, which is what Codex has.
3. **Every capability names what Codex has instead**, or says it has nothing. They are listed in `mods/kit/hooks/features.js`, which `docs/framework.md` prints, and the mod's tests fail when it registers a command the list doesn't name.
4. **The flow works fully with the mod disabled or failed.** `claude --safe-mode` turns off the kit's settings hooks too, so it isn't a supported way to run the kit.
5. **Guarantees go by tier.** A capability that gives Claude Code a stronger guarantee than Codex has records where each fact came from, such as a step the user marked with a button versus one the agent wrote down. A record without that provenance counts as the weaker tier.
6. **It calls the kit's Python instead of reimplementing it**, so one rule never has two implementations that drift apart.

`install.sh` registers `~/.agents` as the `agents-kit` plugin marketplace and installs `kit@agents-kit`. The plugin loads in place, so a pull reaches it at the next session start or `/reload-plugins`. To work on the mod, run `claude --plugin-dir ~/.agents/mods/kit`, which reloads it on save; `tests/run.sh mods` validates and tests it.

## Adding a harness

1. Point its global instructions file at `AGENTS.md`, and its skills directory at `skills/` if it has one.
2. Write `adapters/<harness>/` to translate its events into the payloads above. `adapters/pi/agents-kit.ts` is the reference:
   - before a command → `guard_bash.py`;
   - after an edit → `post_edit.py`;
   - at idle or stop → `stop_checks.py`, with a guard so the stop check continues the agent only once per user turn.
3. Add a section to `install.sh` and run `install.sh --doctor`. Then test with a harmless blocked command in a scratch repo.
4. Teach `bin/docs` where the new harness registers its hooks (`HARNESS_OF_SETTINGS` and `harness_hooks`), then commit: the pre-commit hook regenerates `docs/framework.md`.

## Testing a hook change through a real harness

`tests/` feeds the hooks hand-made payloads. To see a branch's hooks run inside Codex itself, with its real payloads, point `HOME` at a directory whose `.agents` is the branch's checkout. Codex's `hooks.json` runs `python3 $HOME/.agents/hooks/<hook>.py`, and it trusts a hook by that command text, so the branch's code runs through the entries you already approved. `CODEX_HOME` keeps Codex's own settings, login and trust, and `GH_CONFIG_DIR` keeps `gh` logged in; they come before `HOME=` because bash and zsh expand `~` with the `HOME` assigned before it:

```bash
H=$(mktemp -d) && ln -s ~/.agents-worktree-<name> "$H/.agents"   # the branch's checkout
git init -q /tmp/hook-probe && git -C /tmp/hook-probe commit -q --allow-empty -m init
echo 'Run this shell command once and report what happened: <a command the change should block or allow>' \
  | CODEX_HOME=~/.codex GH_CONFIG_DIR=~/.config/gh HOME="$H" \
    codex exec -C /tmp/hook-probe --skip-git-repo-check -s workspace-write --ephemeral -
tail -3 ~/.agents-worktree-<name>/logs/hooks.jsonl
```

The hooks log to the branch checkout's `logs/` (not in git), so the last lines show each decision with `"harness": "codex"`. The checkout has no `repos/<repo>/verify` overlay, so the stop hook falls back to the automatic verify. Use a scratch repo: a hook that fails to block lets the command run.

## Not in git

`logs/`, `backups/`, `research/`, `approvals/`, `monitors/state/`, `review-mining/runs/`, `review-mining/baseline.json` and `usage/*.json` hold local, possibly private data; `site/dist/` is the built website. Profiles are linked in and never committed here.

## License

MIT, see [LICENSE](LICENSE).
