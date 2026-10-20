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
When the message arrives RETURN_GAP_HOURS or more after the newest conversation entry in the transcript's tail, the
hook tells the agent to run `~/.agents/bin/reports brief` and open its reply with Status, What it does and How sure.
It runs no report itself, and a tail it can't read only leaves that out (logged as return-gap-unreadable).
"""
import datetime
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
RETURN_GAP_HOURS = 8  # longer than a lunch or a meeting; the "is it done?" questions came after nights and weekends
RETURN_TAIL_BYTES = 64 * 1024  # smaller than the approvals' tail: this one is read on every message
# Codex events that mark the conversation itself; task_started and the like are written as the new message arrives.
CODEX_CONVERSATION_EVENTS = {"task_complete", "agent_message", "user_message", "turn_aborted"}
RETURN_BRIEF = (
    "The user is back after {hours:.0f} hours away. Before answering, run `~/.agents/bin/reports brief` from the "
    "checkout of the branch this conversation was about (`~/.agents/bin/reports --branch <branch> brief` when that "
    "checkout is gone or detached), and open your reply with **Status**, **What it does** and **How sure** for that "
    "task, as AGENTS.md's Communication section describes them. When the brief fails or finds no reports, say the "
    "status is unknown, and why.")
# Written without accents, as plain() leaves text. An offer's verb opens its phrase ("¿la creo?", "¿quieres que la
# cree?", "¿puedo crearla?", "want me to create…?"); a verb that offers a read as often ("abro", "open", "make", "log",
# "dejo") is left out, and so are orders that as often revise the draft ("cambia", "add", "edit"): those approve nothing.
SPANISH_OFFERS = {  # infinitive: first person present, first person subjunctive
    "crear": ("creo", "cree"), "actualizar": ("actualizo", "actualice"), "anadir": ("anado", "anada"),
    "publicar": ("publico", "publique"), "comentar": ("comento", "comente"), "cerrar": ("cierro", "cierre"),
    "reabrir": ("reabro", "reabra"), "mover": ("muevo", "mueva"), "asignar": ("asigno", "asigne"),
    "archivar": ("archivo", "archive"), "subir": ("subo", "suba"), "registrar": ("registro", "registre"),
    "apuntar": ("apunto", "apunte"), "editar": ("edito", "edite"), "cambiar": ("cambio", "cambie"),
    "borrar": ("borro", "borre"), "eliminar": ("elimino", "elimine"), "escribir": ("escribo", "escriba"),
    "marcar": ("marco", "marque"), "enlazar": ("enlazo", "enlace"), "adjuntar": ("adjunto", "adjunte")}
NOUNS = ("archivo", "cambio", "registro")  # as often nouns: "¿Archivo de Linear, lo reviso?"
ENGLISH_OFFERS = ("create", "file", "add", "post", "update", "comment", "close", "reopen", "move", "assign", "archive",
                  "submit", "edit", "change", "delete", "remove", "write", "mark", "attach", "rename", "label", "raise")
SPANISH_ORDERS = ("crea", "publica", "sube", "registra", "apunta", "actualiza", "comenta", "cierra", "reabre",
                  "asigna", "archiva", "borra", "elimina", "adjunta", "enlaza", "procede")
ENGLISH_ORDERS = ("create", "file", "post", "submit", "update", "comment", "close", "reopen", "assign", "archive",
                  "delete", "attach", "proceed")
CLITICS = r"(?:(?:me|te|se|nos|le|les)(?:la|lo|las|los)?|la|lo|las|los)?"
PRONOUN = r"(?:(?:te|se|me) )?(?:(?:la|lo|las|los|le|les) )?"
WRITE_OFFER = re.compile(
    r"(?:(?:so|ok|okay|then|great|entonces|vale|bueno|y|and) )?(?:"
    rf"(?:quieres|prefieres|deseas|te parece bien|te parece) que {PRONOUN}"
    rf"(?:{'|'.join(subj for _, subj in SPANISH_OFFERS.values())})"
    rf"|{PRONOUN}(?:{'|'.join(present for present, _ in SPANISH_OFFERS.values() if present not in NOUNS)})(?! que\b)"
    rf"|{PRONOUN}(?:{'|'.join(verb[:-2] + {'ar': 'amos', 'er': 'emos', 'ir': 'imos'}[verb[-2:]] for verb in SPANISH_OFFERS)})"
    rf"|(?:puedo|podria|debo|deberia|me dejas|voy a|vamos a) (?:{'|'.join(SPANISH_OFFERS)}){CLITICS}"
    r"|(?:(?:do you |would you )?(?:want|like) me to|should i|shall i|can i|could i|may i|i can|i could|let me) "
    rf"(?:go ahead and |just |now )?(?:{'|'.join(ENGLISH_OFFERS)}))\b")
YES_WORDS = (r"si|sip|yes|yep|yeah|yup|ok|okay|okey|vale|claro|dale|venga|adelante|perfecto|perfect|genial|great|sure|"
             r"bien|good|hazlo|do it|go ahead|go for it|please do|de acuerdo|por supuesto|of course|sounds good|"
             r"let'?s do it|absolutely|exacto|correcto|procede|proceed")
FILLERS = r"bueno|pues|please|por favor|gracias|thanks|thank you|m+|u+h?m+|oh|ah|y|and|entonces|so|then|ahora|now"
# The first clause is nothing but a yes: "si" with more after it is an if ("Si puedes, primero…").
YES = re.compile(rf"(?:(?:{FILLERS}) )*(?:{YES_WORDS})(?: (?:{YES_WORDS}|{FILLERS}))*")
WRITE_ORDER = re.compile(rf"(?:(?:{FILLERS}|{YES_WORDS}) )*"
                         rf"(?:(?:{'|'.join(SPANISH_ORDERS)}){CLITICS}|(?:{'|'.join(ENGLISH_ORDERS)}))\b")
# A clause that only says no ("sí, pero no", "claro que no", "ok, mejor no"), unlike a "no" in a detail ("pero no
# urgente"), and the words that hold, retract or redirect wherever they are.
REFUSAL_CLAUSE = re.compile(rf"(?:(?:{FILLERS}|{YES_WORDS}|pero|but|que|mejor|ya|hace falta) )*"
                            rf"(?:no|not|nope|nah|stop|para)(?: (?:{FILLERS}|hace falta|ahora|now|todavia|yet))*")
HOLD = re.compile(
    r"\?|^\W*(?:no|nope|nah|stop)\b|\b(?:no (?:la|lo|las|los|le|les|me|te|se|en)\b|no (?:crees|hagas|toques|publiques|"
    r"subas|sigas)\b|don'?t|do not|not yet|not now|todavia|aun no|ahora no|mas tarde|manana|later|tomorrow|esper\w*|"
    r"wait|hold|cancel\w*|dejalo|olvidalo|forget|never ?mind|skip|let'?s not|primero|antes|first|before|ensena\w*|"
    r"muestra\w*|show me|let me see|dejame ver|that'?s all|eso es todo|nada mas|instead|en vez|en lugar)\b")


def plain(text):
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)).lower()


def accepts(prompt):
    """The user's message says yes, or tells the agent to write: its first clause is nothing but a yes, or one of its
    clauses opens with an order to write ("En el equipo WCCOM, créala"). A question back, a refusal, a hold or a
    "show me first" accepts nothing, though a "no" in a detail doesn't refuse ("Sí, créala, pero no urgente")."""
    said = plain(prompt).replace("’", "'").strip()
    clauses = [" ".join(re.findall(r"[\w']+", clause)) for clause in re.split(r"[,.;:!¡\n]", said)]
    clauses = [clause for clause in clauses if clause]
    if not clauses or HOLD.search(said) or any(REFUSAL_CLAUSE.fullmatch(clause) for clause in clauses):
        return False
    return bool(YES.fullmatch(clauses[0]) or any(WRITE_ORDER.match(clause) for clause in clauses))


def last_agent_message(transcript, prompt):
    """The agent's last message before the user's `prompt`, from the tail of a Claude Code transcript or a Codex
    rollout, or None when the tail doesn't show one: the turn ended on a tool call, an older message of the user's
    comes first, the message is longer than the tail. Raises OSError, and ValueError on a corrupt tail."""
    with open(transcript, "rb") as fh:
        skipped = max(0, os.fstat(fh.fileno()).st_size - TRANSCRIPT_TAIL_BYTES)
        fh.seek(skipped)
        tail = fh.read(TRANSCRIPT_TAIL_BYTES)
    if skipped:
        tail = tail.partition(b"\n")[2]  # the line the tail cut into
    try:
        lines = tail.decode().splitlines()
    except UnicodeDecodeError:
        raise ValueError("the transcript isn't UTF-8") from None
    texts, passed_prompt = [], False

    def current_prompt(text):
        nonlocal passed_prompt
        if texts or passed_prompt or not isinstance(text, str) or text.strip() != prompt.strip():
            return False
        passed_prompt = True  # this message, already written
        return True

    for number, line in enumerate(reversed(lines)):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            raise ValueError(f"line {len(lines) - number} of the tail doesn't parse") from None
        if not isinstance(entry, dict):
            raise ValueError(f"line {len(lines) - number} of the tail isn't an entry")
        event = entry.get("payload")
        if entry.get("type") == "event_msg":  # Codex
            if not isinstance(event, dict):
                return None
            if event.get("type") == "task_complete":
                said = event.get("last_agent_message")
                return said if isinstance(said, str) and not texts else None
            if event.get("type") == "turn_aborted" or (event.get("type") == "user_message"
                                                        and not current_prompt(event.get("message"))):
                return None
            continue
        if entry.get("type") not in ("user", "assistant") or entry.get("isSidechain") or entry.get("isMeta"):
            continue
        content = entry["message"].get("content") if isinstance(entry.get("message"), dict) else None
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(blocks, list) or (entry["type"] == "user" and not blocks):
            return None  # a message this can't read could be anything, an older message of the user's too
        for block in reversed(blocks):
            kind = block.get("type") if isinstance(block, dict) else None
            if entry["type"] == "assistant" and kind == "text":
                texts.insert(0, str(block.get("text") or ""))
            elif entry["type"] == "assistant" and kind in ("thinking", "redacted_thinking"):
                continue
            elif not (entry["type"] == "user" and kind == "text" and current_prompt(block.get("text"))):
                # a tool call or its result, or the user's message that opened the agent's turn
                return "\n\n".join(texts) if texts else None
    return None if skipped else "\n\n".join(texts) or None  # the line the tail cut into could be more of this run


def offered_services(message, services):
    """The services the agent's message ends offering to write to: its last sentence is a question, a clause of
    which opens with an offer to write, naming them; with a "no" or an alternative in it, it offers nothing."""
    text = (message or "").rstrip(" \t\n*_)")
    if not text.endswith("?"):
        return []
    body = text[:-1]
    ends = [match.end() for match in re.finditer(r"\n[ \t]*\n|[.!?]\s", body)]  # a paragraph or a sentence
    question = body[ends[-1] if ends else 0:]
    said = plain(question).replace("’", "'")
    said = said[said.rfind("¿") + 1:]
    if re.search(r"\b(?:no|not|nunca|never|o|or)\b|n't\b", said) \
            or not any(WRITE_OFFER.match(clause.strip(" \t*_#>-")) for clause in re.split(r"[,:;\n]", said)):
        return []
    def named(text):
        return sorted(s for s in services if re.search(rf"(?<!\w){re.escape(s)}(?!\w)", text, re.I))
    # "Como Linear está caído, ¿la creo en GitHub?" offers GitHub; "En Linear, ¿la creo?" still offers Linear.
    return named(question[question.rfind("¿") + 1:]) or named(question)


def hours_away(transcript, prompt):
    """Hours from the newest conversation entry in the transcript's tail, other than the user's `prompt` already
    written, until now; None when the tail holds no such entry. Harness entries (attachments, hook output) don't
    count: they can be written as the prompt arrives. Raises OSError, and ValueError on a corrupt tail."""
    with open(transcript, "rb") as fh:
        skipped = max(0, os.fstat(fh.fileno()).st_size - RETURN_TAIL_BYTES)
        fh.seek(skipped)
        tail = fh.read(RETURN_TAIL_BYTES)
    if skipped:
        tail = tail.partition(b"\n")[2]  # the line the tail cut into
    try:
        lines = tail.decode().splitlines()
    except UnicodeDecodeError:
        raise ValueError("the transcript isn't UTF-8") from None
    for number, line in enumerate(reversed(lines)):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            raise ValueError(f"line {len(lines) - number} of the tail doesn't parse") from None
        if not isinstance(entry, dict):
            raise ValueError(f"line {len(lines) - number} of the tail isn't an entry")
        event = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        if entry.get("type") == "event_msg":  # Codex
            if event.get("type") not in CODEX_CONVERSATION_EVENTS or (
                    event.get("type") == "user_message" and str(event.get("message") or "").strip() == prompt.strip()):
                continue
        elif entry.get("type") in ("user", "assistant") and not entry.get("isSidechain") and not entry.get("isMeta"):
            content = entry["message"].get("content") if isinstance(entry.get("message"), dict) else None
            texts = [content] if isinstance(content, str) else [
                b.get("text") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
            if entry["type"] == "user" and any(isinstance(t, str) and t.strip() == prompt.strip() for t in texts):
                continue
        else:
            continue
        try:
            at = datetime.datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            continue  # a time with no zone could be hours off either way
        return (time.time() - at.timestamp()) / 3600
    return None


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
        except (OSError, ValueError) as error:
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
    told = []
    if switched:
        log("prompt_approvals", "phase-switch", payload, switched)
        told.append(switched)
    if transcript:
        try:
            hours = hours_away(str(transcript), prompt)
        except (OSError, ValueError) as error:
            hours = None
            log("prompt_approvals", "return-gap-unreadable", payload, str(error))
        if hours is not None and hours >= RETURN_GAP_HOURS:
            log("prompt_approvals", "return-brief", payload, f"{hours:.1f}h")
            told.append(RETURN_BRIEF.format(hours=hours))
    if told:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": "\n\n".join(told)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
