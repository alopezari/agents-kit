// Simulates Pi's extension host to exercise ~/.agents/adapters/pi/agents-kit.ts.
import { execSync } from "node:child_process";
import { mkdtempSync, writeFileSync, appendFileSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";

const mod = await import(join(homedir(), ".agents/adapters/pi/agents-kit.ts"));
let failures = 0;
const check = (label, ok) => { console.log(`${ok ? "ok  " : "FAIL"} pi: ${label}`); if (!ok) failures++; };
const handlers: Record<string, Function> = {};
const pi = { on: (ev: string, fn: Function) => (handlers[ev] = fn) };
mod.default(pi);

const repo = mkdtempSync(join(tmpdir(), "pi-sim-"));
// The hooks inherit this: the stop hook records checkouts, and this one isn't real.
const state = mkdtempSync(join(tmpdir(), "pi-sim-state-"));
process.env.AGENTS_STATE_DIR = state;
execSync("git init -q && echo x=1 > a.py && git add -A && git -c user.email=t@t -c user.name=t commit -qm i", { cwd: repo });
const ctx = { cwd: repo, sessionManager: { getSessionId: () => "pi-sim" }, ui: { notify: () => {} } };

const blocked = await handlers.tool_call({ toolName: "bash", input: { command: "git push --force origin x" } }, ctx);
check("guard blocks force push", blocked?.block === true);
const allowed = await handlers.tool_call({ toolName: "bash", input: { command: "git status" } }, ctx);
check("guard allows git status", allowed === undefined);

writeFileSync(join(repo, "b.py"), "def f(:\n");
const edited = await handlers.tool_result({ toolName: "write", input: { path: join(repo, "b.py") }, content: [] }, ctx);
check("syntax error fed back", JSON.stringify(edited?.content ?? []).includes("syntax check failed"));

writeFileSync(join(repo, "b.py"), "import pdb\nbreakpoint()\n");
await handlers.tool_result({ toolName: "edit", input: { path: join(repo, "b.py") }, content: [] }, ctx);
await handlers.input({ source: "interactive" }, ctx);
const settle = (entries = [{ type: "custom", customType: "other-extension" }]) =>
  handlers.agent_before_settle({ outcome: "completed", entries, continue: false, context: { canContinue: false } }, ctx);
const first = await settle();
check("stop check asks to continue", first?.continue === true && JSON.stringify(first.entries).includes("Debug leftover"));
check("earlier extensions' entries kept", first?.entries?.[0]?.customType === "other-extension");
check("only one automatic continuation", (await settle()) === undefined);
await handlers.input({ source: "interactive" }, ctx);
const aborted = await handlers.agent_before_settle({ outcome: "aborted", entries: [], continue: false, context: { canContinue: false } }, ctx);
check("no continuation after an abort", aborted === undefined);
execSync(`rm -rf ${repo} ${state}`);
process.exit(failures ? 1 : 0);
