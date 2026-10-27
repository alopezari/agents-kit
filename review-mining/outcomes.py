#!/usr/bin/env python3
"""What happened to my merged pull requests, before the kit and since, for the monthly job.

Usage: outcomes.py SINCE_ISO SESSIONS.json OUTPUT.json

Per merged PR, from GitHub (github.com plus any host a profile lists in review-mining/hosts.txt):
  review_threads      inline review threads started by a person other than me
  changes_requested   reviews by a person that requested changes
  red_pushes          commits pushed after the PR opened whose checks ended red
  follow_ups          candidate reverts and fix PRs: merged within FOLLOW_UP_DAYS, touching the same files,
                      with a fix/revert word in the title ("pending" until that window has passed). A title
                      match is not proof (a lint sweep, a project called "bug-bash"), so the monthly job
                      judges each candidate before counting it as an escape.
  sessions            agent sessions on the PR's branch in its repo (by origin remote): count, input+output tokens, wall-clock minutes
                      (idle time included), and hours from the first session to opening the PR and from
                      opening to merging; before the kit, only where transcripts still exist. "phases":
                      tokens, active minutes and the user's share per phase of the flow (Claude Code sessions
                      only). "attention": the user's share summed over those phases (see USER_SHARE), with
                      coverage "complete", or "partial" when a joined session has no phases (Codex, or a Claude
                      session that never named a branch) or predates the share, so its share is unknown
Since the kit, also:
  escapes             follow-pr's logged CI failures and review comments (logs/quality.jsonl, kind "escape"),
                      each with whether the lens that should have caught it ran on that branch, under any of the
                      earlier names its "rename" entries record
  signals             the PR's last "pr" line in logs/quality.jsonl before the merge (bin/quality-log pr: size, tests,
                      verify, red check, diff coverage, weakened tests), matched by host, owner/repo and number; null
                      when kit-ship and follow-pr never recorded one

Each summary's "signals" block sets those signals against the PRs' settled outcomes (fix and revert candidates,
confirmed escapes), counted once per PR, per bucket of each split (size band, tier, red check, coverage, weakened
tests, tests deleted, tests touched), with PRs without signals apart. Since the
kit it reads PRs merged from FOLLOW_UP_DAYS before SINCE (signals_window), so a PR still pending at last month's run
is settled in this one; every other summary and the PR list keep to PRs merged since SINCE (window).

The baseline window (BASELINE_MONTHS before KIT_START) never changes once its follow-up windows have
passed, so it is cached in baseline.json and fetched again only while any of its PRs is pending.
"""
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "hooks"))
from review_stamp import quality_log  # noqa: E402

KIT_START = "2026-09-24"
BASELINE_MONTHS = 6
FOLLOW_UP_DAYS = 14
# Larger batches make GitHub's GraphQL time out (a follow-up search with files is the heaviest part).
DETAIL_BATCH = 10
FOLLOW_UP_BATCH = 5
SEARCH_LIMIT = 1000
GH = os.path.expanduser("~/.agents/bin/gh")
QUALITY_LOG = quality_log()
BASELINE_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json")
BOT = re.compile(r"\[bot\]$|copilot|coderabbit|claude|codex|gemini|github-actions|dependabot", re.I)
# The user's share of a phase, from usage/extract_sessions.py's no_spending: prompts (typed commands too), the
# agent's questions, corrections, minutes idle until each reply, and those gaps capped at its ACTIVE_GAP_MINUTES each.
USER_SHARE = ("prompts", "questions", "corrections", "wait_minutes", "reply_minutes")
FOLLOW_UP_TITLE = re.compile(r"\b(revert|fix|hotfix|regression|bug)", re.I)
# Files nearly every PR touches; sharing only these doesn't make a later PR a follow-up.
INCIDENTAL_FILE = re.compile(r"(^|/)(changelog[^/]*|readme[^/]*|package-lock\.json|composer\.lock|pnpm-lock\.yaml|yarn\.lock)$"
                             r"|(^|/)changelog\.d/", re.I)

PR_FIELDS = """title url createdAt mergedAt headRefName author { login }
  reviewThreads(first: 100) { nodes { comments(first: 1) { nodes { author { login } } } } }
  reviews(first: 100) { nodes { state submittedAt author { login } } }
  commits(last: 100) { nodes { commit { committedDate statusCheckRollup { state } } } }
  files(first: 100) { nodes { path } }"""


def github_hosts():
    """github.com, plus the hosts profiles add (a company's GitHub Enterprise), one per line."""
    hosts = ["github.com"]
    for path in sorted(glob.glob(os.path.expanduser("~/.agents/profiles/*/review-mining/hosts.txt"))):
        hosts += [line.strip() for line in open(path) if line.strip() and not line.startswith("#")]
    return list(dict.fromkeys(hosts))


def gh(host, *args):
    out = subprocess.run([GH, *args], capture_output=True, text=True, timeout=180, env={**os.environ, "GH_HOST": host})
    if out.returncode != 0 and not out.stdout.strip():
        raise RuntimeError(f"gh {' '.join(args[:2])} on {host}: {out.stderr.strip()[:300]}")
    return out.stdout


def graphql(host, query):
    """(data, errors); a batch that fails outright comes back as an error, so the other batches still run."""
    try:
        data = json.loads(gh(host, "api", "graphql", "-f", f"query={query}"))
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        return {}, [f"batch failed: {str(error)[:300]}"]
    return data.get("data") or {}, [e.get("message", "") for e in data.get("errors") or []]


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def my_merged_prs(host, start, end):
    raw = gh(host, "search", "prs", "--author=@me", "--merged", "--merged-at", f"{start}..{end}",
             "--limit", str(SEARCH_LIMIT), "--json", "repository,number")
    found = [(pr["repository"]["nameWithOwner"], pr["number"]) for pr in json.loads(raw or "[]")]
    if len(found) == SEARCH_LIMIT:
        print(f"warning: {host} {start}..{end} hit the {SEARCH_LIMIT} search limit; older PRs are missing", file=sys.stderr)
    return found


def pr_metrics(me, node):
    opened = parse_time(node["createdAt"])
    people = [r for r in node["reviews"]["nodes"] if r["author"] and r["author"]["login"] != me
              and not BOT.search(r["author"]["login"])]
    first_review = min((parse_time(r["submittedAt"]) for r in people if r["submittedAt"]), default=None)
    commits = [c["commit"] for c in node["commits"]["nodes"]]
    thread_authors = [(t["comments"]["nodes"] or [{}])[0].get("author") or {} for t in node["reviewThreads"]["nodes"]]
    return {
        "review_threads": sum(1 for a in thread_authors if a.get("login") not in (None, me) and not BOT.search(a["login"])),
        "changes_requested": sum(1 for r in people if r["state"] == "CHANGES_REQUESTED"),
        "red_pushes": sum(1 for c in commits if parse_time(c["committedDate"]) > opened
                          and (c["statusCheckRollup"] or {}).get("state") in ("FAILURE", "ERROR")),
        "commits_after_first_review": sum(1 for c in commits if first_review and parse_time(c["committedDate"]) > first_review),
    }


def pr_details(host, me, prs, errors):
    """One GraphQL query per DETAIL_BATCH PRs; a failing batch is recorded and the rest continue."""
    details = []
    for i in range(0, len(prs), DETAIL_BATCH):
        batch = prs[i:i + DETAIL_BATCH]
        parts = [f'p{j}: repository(owner: "{repo.split("/")[0]}", name: "{repo.split("/")[1]}") '
                 f"{{ pullRequest(number: {number}) {{ {PR_FIELDS} }} }}" for j, (repo, number) in enumerate(batch)]
        data, batch_errors = graphql(host, "{ " + "\n".join(parts) + " }")
        errors += [f"{host} details: {e}" for e in batch_errors]
        for j, (repo, number) in enumerate(batch):
            node = ((data.get(f"p{j}") or {}).get("pullRequest"))
            if not node:
                continue
            details.append({"host": host, "repo": repo, "number": number, "url": node["url"], "title": node["title"],
                            "branch": node["headRefName"], "opened": node["createdAt"], "merged": node["mergedAt"],
                            "files": [f["path"] for f in node["files"]["nodes"]], **pr_metrics(me, node)})
    return details


def add_follow_ups(host, prs, now, errors):
    ready = []
    for pr in prs:
        if parse_time(pr["merged"]) + timedelta(days=FOLLOW_UP_DAYS) > now:
            pr["follow_ups"] = "pending"
        else:
            ready.append(pr)
    for i in range(0, len(ready), FOLLOW_UP_BATCH):
        batch = ready[i:i + FOLLOW_UP_BATCH]
        parts = []
        for j, pr in enumerate(batch):
            start = parse_time(pr["merged"])
            window = f"{start:%Y-%m-%dT%H:%M:%SZ}..{start + timedelta(days=FOLLOW_UP_DAYS):%Y-%m-%dT%H:%M:%SZ}"
            query = f"repo:{pr['repo']} is:pr is:merged merged:{window} fix OR revert OR hotfix OR regression OR bug"
            parts.append(f'f{j}: search(query: "{query}", type: ISSUE, first: 50) '
                         "{ nodes { ... on PullRequest { number title url files(first: 100) { nodes { path } } } } }")
        data, batch_errors = graphql(host, "{ " + "\n".join(parts) + " }")
        errors += [f"{host} follow-ups: {e}" for e in batch_errors]
        for j, pr in enumerate(batch):
            if f"f{j}" not in data:
                pr["follow_ups"] = "unknown"
                continue
            mine = {f for f in pr["files"] if not INCIDENTAL_FILE.search(f)}
            pr["follow_ups"] = [
                {"kind": "revert" if later["title"].lower().startswith("revert") else "fix", "url": later["url"],
                 "title": later["title"], "shared_files": sorted(mine & {f["path"] for f in later["files"]["nodes"]})[:5]}
                for later in data[f"f{j}"]["nodes"]
                if later and later["number"] != pr["number"] and FOLLOW_UP_TITLE.search(later["title"])
                and mine & {f["path"] for f in later["files"]["nodes"]}
            ]


def fetch_window(start, end, now, errors):
    prs = []
    for host in github_hosts():
        try:
            me = json.loads(gh(host, "api", "user"))["login"]
            found = my_merged_prs(host, start, end)
            details = pr_details(host, me, found, errors)
            add_follow_ups(host, details, now, errors)
            prs += details
        except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            errors.append(f"{host} {start}..{end}: {error}")
    return prs


def quality_log():
    try:
        return [json.loads(line) for line in open(QUALITY_LOG)]
    except OSError:
        return []


def renames(entries):
    renamed_from = {}
    for e in entries:
        if e.get("kind") == "rename":
            renamed_from.setdefault((e["repo"], e["to"]), set()).add(e["name"])
    return renamed_from


def branch_names(renamed_from, repo, branch):
    """The branch and every earlier name its rename lines record."""
    names, pending = set(), [branch]
    while pending:  # a branch renamed twice, each time after files moved, logs two renames
        current = pending.pop()
        if current not in names:
            names.add(current)
            pending += renamed_from.get((repo, current), ())
    return names


def local_names(sessions):
    """GitHub owner/name (lowercased) -> the names the quality log knows it by: its main checkout's directory, which
    need not match the GitHub name (~/.agents is agents-kit)."""
    names = {}
    for session in sessions:
        for repo, local in (session.get("repos") or {}).items():
            names.setdefault(repo.lower(), set()).update(local)
    return names


def repo_names(pr, local):
    """The quality log's names for the PR's repo: its checkouts' names, or the GitHub name when no session saw one (a
    fork's checkout named like the repo would otherwise lend it its renames)."""
    return set(local.get(pr["repo"].lower(), ())) or {pr["repo"].split("/")[1]}


def pr_branch_names(pr, local, renamed_from):
    """The PR's branch and its earlier names, following renames logged under any name of its repo."""
    repos = repo_names(pr, local)
    earlier = {}
    for (repo, new), old in renamed_from.items():
        if repo in repos:
            earlier.setdefault(new, set()).update(old)
    return branch_names({("", new): old for new, old in earlier.items()}, "", pr["branch"])


def attach_escapes(prs, local):
    """follow-pr's escapes per PR, and whether the lens that should have caught each one ran on the branch, under any
    of its earlier names: the spec review often runs on a session branch that is renamed before the PR."""
    entries = quality_log()
    renamed_from = renames(entries)
    for pr in prs:
        repos = repo_names(pr, local)
        names = pr_branch_names(pr, local, renamed_from)
        mine = [e for e in entries if e.get("repo") in repos and e.get("branch") in names]
        lenses_run = {e["name"] for e in mine if e.get("kind") == "lens"}
        pr["escapes"] = [{"source": e["name"], "category": e.get("category"), "verdict": e.get("verdict"),
                          "lens": e.get("lens"), "lens_ran": e.get("lens") in lenses_run}
                         for e in mine if e.get("kind") == "escape"]


def attach_signals(prs):
    """Each PR's latest "pr" line logged before its merge, by host, owner/repo and number: never by branch, which a later
    PR can reuse and whose checkout name (.agents) differs from the repository's."""
    lines = {}
    for e in quality_log():
        if e.get("kind") == "pr" and e.get("host") and e.get("remote"):
            try:
                logged = datetime.strptime(e["ts"], "%Y-%m-%dT%H:%M:%S%z")
            except (KeyError, TypeError, ValueError):
                continue
            lines.setdefault((e["host"].lower(), e["remote"].lower(), e.get("name")), []).append((logged, e))
    for pr in prs:
        merged = parse_time(pr["merged"])
        before = [(logged, e) for logged, e in lines.get((pr["host"].lower(), pr["repo"].lower(), pr["number"]), [])
                  if logged < merged]
        pr["signals"] = max(before, key=lambda pair: pair[0])[1] if before else None


def signal_buckets(signals):
    """{split: bucket} for one PR's signals line."""
    def size():
        if signals.get("added") is None or signals.get("deleted") is None:
            return "unknown"
        lines = signals["added"] + signals["deleted"]
        return "<50" if lines < 50 else "50-400" if lines <= 400 else ">400"

    def coverage():
        measured = signals.get("coverage")
        if measured:
            return "80_or_more" if measured["covered"] * 100 >= 80 * measured["measured"] else "below_80"
        return "unknown" if signals.get("verify") in (None, "none") else "none"  # no report read, or none measured

    def count(value):
        return "unknown" if value is None else "any" if value else "none"

    red_check = signals.get("red_check")
    tampered = signals.get("tampered")
    return {"size": size(),
            "tier": signals.get("tier") if signals.get("tier") in ("low", "standard", "high") else "unknown",
            "red_check": "unknown" if red_check is None else "skipped_or_not_run" if red_check in ("skipped", "not_run")
            else red_check,
            "coverage": coverage(),
            "tampered": count(None if tampered is None else sum(tampered.values())),
            "tests_deleted": count(signals.get("tests_deleted")),
            "test_files": count(signals.get("test_files"))}


SIGNAL_SPLITS = {"size": ("<50", "50-400", ">400", "unknown"), "tier": ("low", "standard", "high", "unknown"),
                 "red_check": ("failed", "passed", "skipped_or_not_run", "unknown"),
                 "coverage": ("80_or_more", "below_80", "none", "unknown"), "tampered": ("any", "none", "unknown"),
                 "tests_deleted": ("any", "none", "unknown"), "test_files": ("any", "none", "unknown")}


def summarize_signals(prs):
    """Settled PRs' fix and revert candidates and confirmed escapes, each counted once per PR, per bucket of each
    signal split, with the rate beside each count; PRs without signals apart, never as clean ones."""
    def cell(group):
        outcome = {"fix": sum(any(f["kind"] == "fix" for f in p["follow_ups"]) for p in group),
                   "revert": sum(any(f["kind"] == "revert" for f in p["follow_ups"]) for p in group),
                   "escape": sum(any(e["verdict"] == "confirmed" for e in p.get("escapes", [])) for p in group)}
        result = {"prs": len(group)}
        for key, count in outcome.items():
            result.update({key: count, f"{key}_rate": round(count / len(group), 2) if group else None})
        return result

    settled = [p for p in prs if isinstance(p.get("follow_ups"), list)]
    with_signals = [(p, signal_buckets(p["signals"])) for p in settled if p.get("signals")]
    return {"settled_prs": len(settled), "without_signals": cell([p for p in settled if not p.get("signals")]),
            "splits": {split: {bucket: cell([p for p, buckets in with_signals if buckets[split] == bucket])
                               for bucket in buckets} for split, buckets in SIGNAL_SPLITS.items()}}


def attach_sessions(prs, sessions):
    renamed_from = renames(quality_log())
    local = local_names(sessions)
    for pr in prs:
        names = pr_branch_names(pr, local, renamed_from)
        on_branch = [s for s in sessions if names & set(s.get("branches", []))
                     and pr["repo"].lower() in {r.lower() for r in s.get("repos") or {}}]
        if not on_branch:
            pr["sessions"] = None
            continue
        first = min(parse_time(s["start_iso"]) for s in on_branch)
        opened, merged = parse_time(pr["opened"]), parse_time(pr["merged"])
        phases = phases_on_branches(on_branch, names)
        attention = {key: round(sum(spent.get(key, 0) for spent in phases.values()), 1) for key in USER_SHARE}
        measured = all(s.get("phases") and all("prompts" in spent for ps in s["phases"].values() for spent in ps.values())
                       for s in on_branch)  # a sessions file from before the share has phases, but no prompts in them
        attention["coverage"] = "complete" if measured else "partial"
        pr["sessions"] = {
            "count": len(on_branch),
            "tokens": sum(s["tokens"].get("in", 0) + s["tokens"].get("out", 0) for s in on_branch),
            "cache_read_tokens": sum(s["tokens"].get("cache_read", 0) for s in on_branch),
            "minutes": sum(s["minutes"] for s in on_branch),
            "models": sorted({m for s in on_branch for m in s.get("models", {})}),
            "effort": sorted({e for s in on_branch for e in s.get("effort", [])}),
            "hours_to_pr": round((opened - first).total_seconds() / 3600, 1),
            "hours_to_merge": round((merged - opened).total_seconds() / 3600, 1),
            "phases": phases,
            "attention": attention,
        }


def phases_on_branches(sessions, branches):
    """Tokens, active minutes and the user's share per phase of the flow, summed over the sessions' turns on these
    branches."""
    total = {}
    for session in sessions:
        for branch in branches:
            for phase, spent in (session.get("phases") or {}).get(branch, {}).items():
                into = total.setdefault(phase, dict.fromkeys(spent, 0))
                for key, value in spent.items():
                    into[key] = round(into.get(key, 0) + value, 1)
    return total


def summarize(prs, signal_prs=None):
    """signal_prs, when given, is the wider list the signals block reads instead of prs."""
    def mean(values):
        values = list(values)
        return round(sum(values) / len(values), 2) if values else None

    def median(values):
        values = sorted(values)
        return round((values[(len(values) - 1) // 2] + values[len(values) // 2]) / 2, 2) if values else None

    settled = [p for p in prs if isinstance(p.get("follow_ups"), list)]
    with_sessions = [p for p in prs if p.get("sessions")]
    # A phase can hold prompts alone; the cost per phase stays over PRs that spent tokens.
    with_phases = [p for p in with_sessions if any(spent.get("out") for spent in p["sessions"]["phases"].values())]
    complete = [p for p in with_sessions if p["sessions"]["attention"]["coverage"] == "complete"]
    escapes = [e for p in prs for e in p.get("escapes", [])]
    return {
        "merged_prs": len(prs),
        "per_pr": {key: mean(p[key] for p in prs)
                   for key in ("review_threads", "changes_requested", "red_pushes", "commits_after_first_review")},
        "follow_ups": {"settled_prs": len(settled), "pending_prs": len(prs) - len(settled),
                       "prs_with_revert_candidate": sum(any(f["kind"] == "revert" for f in p["follow_ups"]) for p in settled),
                       "prs_with_fix_candidate": sum(any(f["kind"] == "fix" for f in p["follow_ups"]) for p in settled)},
        "escapes_logged": {"total": len(escapes), "confirmed": sum(e["verdict"] == "confirmed" for e in escapes),
                           "confirmed_with_lens_run": sum(e["verdict"] == "confirmed" and e["lens_ran"] for e in escapes)},
        "sessions_joined": {"prs": len(with_sessions),
                            "tokens_per_pr": mean(p["sessions"]["tokens"] for p in with_sessions),
                            "minutes_per_pr": mean(p["sessions"]["minutes"] for p in with_sessions),
                            "hours_to_pr": mean(p["sessions"]["hours_to_pr"] for p in with_sessions),
                            "hours_to_merge": mean(p["sessions"]["hours_to_merge"] for p in with_sessions),
                            "prs_with_phases": len(with_phases),
                            "per_phase": {phase: {key: mean(p["sessions"]["phases"].get(phase, {}).get(key, 0) for p in with_phases)
                                                  for key in ("out", "cache_read", "cache_write", "minutes")}
                                          for phase in sorted({ph for p in with_phases for ph in p["sessions"]["phases"]})},
                            "attention": {"complete_prs": len(complete), "partial_prs": len(with_sessions) - len(complete),
                                          "unknown_prs": len(prs) - len(with_sessions),
                                          "per_pr": {key: mean(p["sessions"]["attention"][key] for p in complete) for key in USER_SHARE},
                                          # An overnight wait dwarfs a day of quick replies: the mean alone misleads.
                                          "median_per_pr": {key: median(p["sessions"]["attention"][key] for p in complete)
                                                            for key in USER_SHARE}}},
        "signals": summarize_signals(prs if signal_prs is None else signal_prs),
    }


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 1
    since, sessions_path, output = sys.argv[1][:10], sys.argv[2], sys.argv[3]
    now = datetime.now(timezone.utc)
    # GitHub's merged:A..B is inclusive and dated in UTC; tomorrow keeps today's merges in any timezone.
    tomorrow = f"{now + timedelta(days=1):%Y-%m-%d}"
    baseline_start = f"{parse_time(KIT_START + 'T00:00:00Z') - timedelta(days=30 * BASELINE_MONTHS):%Y-%m-%d}"
    errors = []

    try:
        baseline = json.load(open(BASELINE_CACHE))
    except (OSError, ValueError):
        baseline = None
    if not baseline or baseline.get("window") != [baseline_start, KIT_START] or baseline.get("pending"):
        prs = fetch_window(baseline_start, KIT_START, now, errors)
        baseline = {"window": [baseline_start, KIT_START], "prs": prs,
                    "pending": any(p.get("follow_ups") in ("pending", "unknown") for p in prs) or bool(errors)}
        with open(BASELINE_CACHE, "w") as fh:
            json.dump(baseline, fh)

    window_start = max(since, KIT_START)
    signals_start = max(f"{parse_time(since + 'T00:00:00Z') - timedelta(days=FOLLOW_UP_DAYS):%Y-%m-%d}", KIT_START)
    with_earlier = fetch_window(signals_start, tomorrow, now, errors)
    try:
        sessions = json.load(open(sessions_path))
    except (OSError, ValueError) as error:
        sessions = []
        errors.append(f"sessions: {error}")
    attach_escapes(with_earlier, local_names(sessions))
    attach_sessions(with_earlier, sessions)
    attach_signals(with_earlier)
    attach_sessions(baseline["prs"], sessions)
    kit = [p for p in with_earlier if parse_time(p["merged"]) >= parse_time(window_start + "T00:00:00Z")]

    result = {"generated": now.isoformat(timespec="seconds"), "kit_start": KIT_START,
              "follow_up_days": FOLLOW_UP_DAYS, "errors": errors,
              "summary": {"baseline": summarize(baseline["prs"]), "since_kit": summarize(kit, with_earlier)},
              "baseline_window": baseline["window"], "window": [window_start, tomorrow],
              "signals_window": [signals_start, tomorrow],
              "prs": [{k: v for k, v in p.items() if k != "files"} for p in kit]}
    with open(output, "w") as fh:
        json.dump(result, fh, indent=1)
    print(json.dumps(result["summary"], indent=1))
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
