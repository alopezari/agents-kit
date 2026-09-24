// Pi adapter for ~/.agents: runs the same hook scripts Claude Code and Codex use,
// translating Pi's extension events into their JSON contract (stdin in, stdout decision out).
// Installed as a symlink in ~/.pi/agent/extensions/ by ~/.agents/install.sh.
// @ts-nocheck
import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

const HOOKS = join(homedir(), ".agents", "hooks");

function runHook(script: string, payload: Record<string, unknown>): any {
  const result = spawnSync("python3", [join(HOOKS, script)], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    timeout: 660_000,
    env: { ...process.env, AGENTS_HARNESS: "pi" },
  });
  const out = (result.stdout || "").trim();
  if (!out) return null;
  try {
    return JSON.parse(out);
  } catch {
    return null; // A broken hook must not break the session.
  }
}

export default function (pi) {
  // Mirrors Claude Code's stop_hook_active: one automatic continuation per user turn.
  let continuedThisTurn = false;

  const base = (ctx) => ({
    session_id: ctx.sessionManager?.getSessionId?.() ?? `pi-${process.pid}`,
    cwd: ctx.cwd,
  });

  pi.on("input", async (event) => {
    if (event.source !== "extension") continuedThisTurn = false;
  });

  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName !== "bash") return;
    const decision = runHook("guard_bash.py", {
      ...base(ctx),
      tool_name: "Bash",
      tool_input: { command: event.input.command },
    });
    const out = decision?.hookSpecificOutput;
    if (out?.permissionDecision === "deny") {
      return { block: true, reason: out.permissionDecisionReason };
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    if (event.toolName !== "edit" && event.toolName !== "write") return;
    const decision = runHook("post_edit.py", {
      ...base(ctx),
      tool_name: event.toolName === "edit" ? "Edit" : "Write",
      tool_input: { file_path: event.input.path },
    });
    if (decision?.decision === "block") {
      return { content: [...(event.content ?? []), { type: "text", text: `\n\n${decision.reason}` }] };
    }
  });

  // agent_before_settle is the last point that can still continue the run (agent_settled only notifies).
  // Handlers replace the entry list, so keep what earlier extensions proposed.
  pi.on("agent_before_settle", async (event, ctx) => {
    if (event.outcome !== "completed") return;
    const decision = runHook("stop_checks.py", { ...base(ctx), stop_hook_active: continuedThisTurn });
    if (decision?.systemMessage) ctx.ui?.notify?.(decision.systemMessage, "warning");
    if (decision?.decision !== "block" || continuedThisTurn) return;
    continuedThisTurn = true;
    return {
      entries: [
        ...event.entries,
        { type: "custom_message", customType: "agents-kit-stop-check", content: decision.reason, display: true },
      ],
      continue: true,
    };
  });
}
