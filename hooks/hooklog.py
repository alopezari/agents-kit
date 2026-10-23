"""Append one JSON line per hook decision to ~/.agents/logs/hooks.jsonl (in AGENTS_LOG_DIR instead when set: tests).

Logging must never break a hook, so every failure here is swallowed. Secret values in the detail are redacted first.
"""
import json
import os
import re
import time

LOG = os.path.join(os.environ.get("AGENTS_LOG_DIR") or os.path.expanduser("~/.agents/logs"), "hooks.jsonl")


REDACTED = "[redacted]"
_VALUE = r"""(?:"[^"]*"|'[^']*'|[^\s'"]+)"""  # quoted whole, or up to the next space
SECRETS = [  # (pattern, replacement): the name or flag is kept, so the log still says what was there
    (re.compile(r"\b([A-Z0-9_]*(?:PASS|PWD|TOKEN|SECRET|KEY|AUTH)[A-Z0-9_]*=)" + _VALUE), r"\1" + REDACTED),
    (re.compile(r"(--(?:password|token|secret|api-key)(?:=|\s+))" + _VALUE), r"\1" + REDACTED),
    (re.compile(r"(\bmysql\w*\b[^;&|\n]*?\s-p)[^\s'\"]+"), r"\1" + REDACTED),
    (re.compile(r"(\bAuthorization:\s*(?:\w+\s+)?)[^\s'\"]+", re.I), r"\1" + REDACTED),
    (re.compile(r"(\b[a-z][\w+.-]*://[^\s/:@'\"]*:)[^\s/@'\"]+(?=@)", re.I), r"\1" + REDACTED),
    # Token shapes, by prefix and a minimum length: a cut token must still match.
    (re.compile(r"\b(?:gh[pousr]_|github_pat_|xox[abposr]-|sk-|AKIA|ASIA)[\w-]{8,}"), REDACTED),
]


def redact(text, keep):
    """The first `keep` characters of text, its secrets redacted before the cut so a secret across it leaves no part."""
    text = text[:keep + 200]  # bounds the patterns' work on a long command; a secret is shorter than the margin
    for pattern, replacement in SECRETS:
        text = pattern.sub(replacement, text)
    return text[:keep]


def harness(payload):
    return os.environ.get("AGENTS_HARNESS") or ("codex" if payload.get("turn_id") else "claude-code")


def session_harness():
    """The harness of the session running this tool, from its environment (a tool gets no hook payload), or None."""
    # Codex before Claude Code: a Codex run started from Claude Code inherits CLAUDECODE.
    return os.environ.get("AGENTS_HARNESS") or ("codex" if os.environ.get("CODEX_THREAD_ID") else
                                                "claude-code" if os.environ.get("CLAUDECODE") else None)


def log(hook, decision, payload, detail):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "hook": hook,
                "decision": decision,
                "session": payload.get("session_id"),
                "cwd": payload.get("cwd"),
                "harness": harness(payload),
                "detail": redact(detail, 500),
                **({"suite_run": os.environ["AGENTS_SUITE_RUN"]} if os.environ.get("AGENTS_SUITE_RUN") else {}),
            }) + "\n")
    except Exception:
        pass
