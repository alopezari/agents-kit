#!/usr/bin/env python3
"""UserPromptSubmit hook for Claude Code and Codex.

Asking for a write is approving it: when the user's message names a service guard_mcp.py guards
(Linear, or one a profile declares), or one of Impeccable's project files design_files.py guards,
writes to it are allowed until the user's next message. A DROP or TRUNCATE statement guard_bash.py refuses
needs more than a mention: the message opens with the line `allow <statement>`, or does nothing but ask for it in
plain words ("Sí, borra las bases de prueba", "drop the test databases").
A message whose first line is `phase off|on <phase> [branch|repo|global]` switches a flow phase (phase_switches.py),
and the hook tells the agent what is off now; one whose first line is `review-rounds <0-9> [branch|repo|global]`
sets how many re-checks the self-review runs.
A yes to the agent's offer approves too: when the agent's last message ends asking whether to write to a guarded
service, naming it ("¿Quieres que la cree en Linear?"), and the user's message accepts it ("Sí, crea la tarea en
WCCOM…", "dale", "go ahead"), that service is approved until the next message. Only the transcript's tail is read, and
anything it can't show for sure (a missing or corrupt file, a turn that ended on a tool call) approves nothing.
Only the user's own messages reach this hook, so an agent can't grant itself one: it can word the question, but the
user has to accept it. Mentioning the service only to read from it approves writes for that turn too.
The objects guard_mcp.py records a session's approved writes to outlast the user's messages: this hook only deletes
the ones older than guard_mcp.OBJECT_APPROVAL_HOURS.
"""
import json
import os
import re
import shutil
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design_files  # noqa: E402
import guard_mcp  # noqa: E402
from hooklog import log  # noqa: E402

STALE_SECONDS = 24 * 3600
TRANSCRIPT_TAIL_BYTES = 256 * 1024  # this hook runs on every message: a transcript grows to hundreds of MB
# Matched without accents. Spanish in first person ("¿quieres que la cree?", "¿la creo?") or infinitive ("¿puedo
# crearla?"), English after "want me to", "should I".
WRITE_OFFER = re.compile(
    r"\b(?:cre|abr|anad|public|actualic|coment|cierr|muev|asign|archiv|envi|mand|sub|registr|apunt|edit|cambi|borr|"
    r"elimin|guard|pong|dej|escrib|marqu|enlac|adjunt)(?:e|a|o|ar|ir|er)(?:la|lo|las|los|le|les)?\b|"
    r"\b(?:create|open|file|add|post|update|comment|close|move|assign|archive|send|submit|log|edit|change|delete|"
    r"remove|write|mark|link|attach|put|make|set)\b", re.I)
YES = re.compile(r"(?:si|yes|yep|yeah|ok|okay|vale|claro|dale|venga|adelante|go ahead|sure|perfecto|perfect|genial|"
                 r"great|de acuerdo|hazlo|do it|please do|por favor|please)\b", re.I)
WRITE_ORDER = re.compile(
    r"(?:(?:please|por favor|ok|venga|ahora|now|y|and|then|luego) )*"
    r"(?:(?:crea|abre|anade|publica|actualiza|comenta|cierra|mueve|asigna|archiva|envia|manda|sube|registra|apunta|"
    r"edita|pon|mete|haz|escribe|marca)(?:la|lo|las|los|le|les|mela|melo)?|procede|"
    r"create|open|file|add|post|update|comment|close|move|assign|send|submit|log|edit|put|make|do|go|proceed)\b", re.I)
REFUSAL = re.compile(r"\?|^\W*(?:no|nope|nah|mejor no|para|stop)\b|\b(?:no (?:la|lo|las|los|le) \w+|no lo hagas|don'?t|"
                     r"do not|not yet|todavia no|aun no|espera|wait|hold on|cancel\w*)\b", re.I)


def plain(text):
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)).lower()


def accepts(prompt):
    """The user's message says yes, or tells the agent to write: it opens with a yes, or one of its clauses is an
    order to write ("En el equipo WCCOM, créala"). A question back, a refusal or a hold accepts nothing, though a
    "no" inside the details doesn't refuse ("Sí, créala, pero no urgente")."""
    said = plain(prompt).strip()
    if not said or REFUSAL.search(said):
        return False
    return bool(re.match(r"\W*" + YES.pattern, said, re.I)
                or any(WRITE_ORDER.match(clause.strip()) for clause in re.split(r"[,.;:!¡]", said)))


def last_agent_message(transcript, prompt):
    """The agent's last message before the user's `prompt`, from the tail of a Claude Code transcript or a Codex
    rollout, or None when the tail doesn't show it for sure: the turn ended on a tool call, an older message of the
    user's comes first, a line doesn't parse (a corrupt file, or a message cut by the tail). Raises OSError."""
    with open(transcript, "rb") as fh:
        fh.seek(max(0, os.fstat(fh.fileno()).st_size - TRANSCRIPT_TAIL_BYTES))
        lines = fh.read(TRANSCRIPT_TAIL_BYTES).decode(errors="replace").splitlines()
    texts, passed_prompt = [], False
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            return None
        if not isinstance(entry, dict):
            return None
        if entry.get("type") == "event_msg":  # Codex: the turn before this message ended, or the user stopped it
            event = entry.get("payload") or {}
            if event.get("type") == "turn_aborted":
                return None
            if event.get("type") == "task_complete":
                said = event.get("last_agent_message")
                return said if isinstance(said, str) else None
            continue
        if entry.get("type") not in ("user", "assistant") or entry.get("isSidechain") or entry.get("isMeta"):
            continue
        content = (entry.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(blocks, list):
            return None
        for block in reversed(blocks):
            kind = block.get("type") if isinstance(block, dict) else None
            if entry["type"] == "assistant" and kind == "text":
                texts.insert(0, str(block.get("text") or ""))
            elif entry["type"] == "assistant" and kind in ("thinking", "redacted_thinking"):
                continue
            elif entry["type"] == "user" and kind == "text" and not texts and not passed_prompt \
                    and str(block.get("text") or "").strip() == prompt.strip():
                passed_prompt = True  # this message, already written
            else:  # a tool call or its result, or the user's message that opened the agent's turn
                return "\n\n".join(texts) if texts else None
    return "\n\n".join(texts) if texts else None


def offered_services(message, services):
    """The services the message's last sentence offers to write to: a question, with a write verb, naming them."""
    question = re.search(r"[^.!?\n]*\?[\s*_\"'»)]*\Z", message or "")
    if not question or not WRITE_OFFER.search(plain(question.group())):
        return []
    return sorted(s for s in services if re.search(rf"(?<!\w){re.escape(s)}(?!\w)", question.group(), re.I))


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    session = re.sub(r"[^\w-]", "", str(payload.get("session_id") or ""))
    if not session:
        return 0
    shutil.rmtree(os.path.join(guard_mcp.TURN_APPROVALS, session), ignore_errors=True)  # the last turn's
    os.makedirs(guard_mcp.TURN_APPROVALS, exist_ok=True)
    for name in os.listdir(guard_mcp.TURN_APPROVALS):  # sessions long gone
        path = os.path.join(guard_mcp.TURN_APPROVALS, name)
        try:
            if time.time() - os.path.getmtime(path) > STALE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:  # another session's hook removed it first
            pass
    for folder, _, records in os.walk(guard_mcp.SESSION_APPROVALS, topdown=False):
        for name in records:
            path = os.path.join(folder, name)
            try:
                if time.time() - os.path.getmtime(path) > guard_mcp.OBJECT_APPROVAL_HOURS * 3600:
                    os.remove(path)
            except OSError:
                pass
        if folder != guard_mcp.SESSION_APPROVALS:
            try:
                os.rmdir(folder)  # only once it's empty
            except OSError:
                pass
    prompt = str(payload.get("prompt") or "")
    named = sorted(s for s in guard_mcp.guarded_services() if re.search(rf"(?<!\w){re.escape(s)}(?!\w)", prompt, re.I))
    transcript = payload.get("transcript_path")
    if transcript and accepts(prompt):  # only then: most messages read no transcript
        try:
            offered = offered_services(last_agent_message(str(transcript), prompt), guard_mcp.guarded_services())
        except OSError as error:
            offered = []
            log("prompt_approvals", "transcript-unreadable", payload, str(error))
        if offered:
            log("prompt_approvals", "approve-offer", payload, ",".join(offered))
            named = sorted(set(named) | set(offered))
    named += design_files.approval_names(prompt)
    import guard_bash  # after the clearing: if it fails to load, the last turn's approvals are already gone
    named += guard_bash.database_approval_names(prompt)
    if named:
        os.makedirs(os.path.join(guard_mcp.TURN_APPROVALS, session))
        for service in named:
            open(os.path.join(guard_mcp.TURN_APPROVALS, session, service), "w").close()
        log("prompt_approvals", "approve-turn", payload, ",".join(named))
    import phase_switches  # after the clearing, like guard_bash
    switched = phase_switches.apply_phase_line(prompt, payload.get("cwd") or os.getcwd())
    if switched:
        log("prompt_approvals", "phase-switch", payload, switched)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": switched}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
