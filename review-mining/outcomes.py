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
  sessions            agent sessions on the PR's branch: count, input+output tokens, wall-clock minutes
                      (idle time included), and hours from the first session to opening the PR and from
                      opening to merging; before the kit, only where transcripts still exist
Since the kit, also:
  escapes             follow-pr's logged CI failures and review comments (logs/quality.jsonl, kind "escape"),
                      each with whether the lens that should have caught it ran on that branch

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

KIT_START = "2026-09-24"
BASELINE_MONTHS = 6
FOLLOW_UP_DAYS = 14
# Larger batches make GitHub's GraphQL time out (a follow-up search with files is the heaviest part).
DETAIL_BATCH = 10
FOLLOW_UP_BATCH = 5
SEARCH_LIMIT = 1000
GH = os.path.expanduser("~/.agents/bin/gh")
QUALITY_LOG = os.path.expanduser("~/.agents/logs/quality.jsonl")
BASELINE_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json")
BOT = re.compile(r"\[bot\]$|copilot|coderabbit|claude|codex|gemini|github-actions|dependabot", re.I)
FOLLOW_UP_TITLE = re.compile(r"\b(revert|fix|hotfix|regression|bug)", re.I)
# Files nearly every PR touches; sharing only these doesn't make a later PR a follow-up.
INCIDENTAL_FILE = re.compile(r"(^|/)(changelog[^/]*|readme[^/]*|package-lock\.json|composer\.lock|pnpm-lock\.yaml|yarn\.lock)$", re.I)

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


def attach_escapes(prs):
    """follow-pr's escapes per PR, and whether the lens that should have caught each one ran on the branch."""
    try:
        entries = [json.loads(line) for line in open(QUALITY_LOG)]
    except OSError:
        entries = []
    for pr in prs:
        name, branch = pr["repo"].split("/")[1], pr["branch"]
        mine = [e for e in entries if e.get("repo") == name and e.get("branch") == branch]
        lenses_run = {e["name"] for e in mine if e.get("kind") == "lens"}
        pr["escapes"] = [{"source": e["name"], "category": e.get("category"), "verdict": e.get("verdict"),
                          "lens": e.get("lens"), "lens_ran": e.get("lens") in lenses_run}
                         for e in mine if e.get("kind") == "escape"]


def attach_sessions(prs, sessions):
    for pr in prs:
        name = pr["repo"].split("/")[1]
        on_branch = [s for s in sessions if pr["branch"] in s.get("branches", []) and name in (s.get("project") or "")]
        if not on_branch:
            pr["sessions"] = None
            continue
        first = min(parse_time(s["start_iso"]) for s in on_branch)
        opened, merged = parse_time(pr["opened"]), parse_time(pr["merged"])
        pr["sessions"] = {
            "count": len(on_branch),
            "tokens": sum(s["tokens"].get("in", 0) + s["tokens"].get("out", 0) for s in on_branch),
            "cache_read_tokens": sum(s["tokens"].get("cache_read", 0) for s in on_branch),
            "minutes": sum(s["minutes"] for s in on_branch),
            "models": sorted({m for s in on_branch for m in s.get("models", {})}),
            "effort": sorted({e for s in on_branch for e in s.get("effort", [])}),
            "hours_to_pr": round((opened - first).total_seconds() / 3600, 1),
            "hours_to_merge": round((merged - opened).total_seconds() / 3600, 1),
        }


def summarize(prs):
    def mean(values):
        values = list(values)
        return round(sum(values) / len(values), 2) if values else None

    settled = [p for p in prs if isinstance(p.get("follow_ups"), list)]
    with_sessions = [p for p in prs if p.get("sessions")]
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
                            "hours_to_merge": mean(p["sessions"]["hours_to_merge"] for p in with_sessions)},
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

    kit = fetch_window(max(since, KIT_START), tomorrow, now, errors)
    attach_escapes(kit)
    try:
        sessions = json.load(open(sessions_path))
    except (OSError, ValueError) as error:
        sessions = []
        errors.append(f"sessions: {error}")
    attach_sessions(kit, sessions)
    attach_sessions(baseline["prs"], sessions)

    result = {"generated": now.isoformat(timespec="seconds"), "kit_start": KIT_START,
              "follow_up_days": FOLLOW_UP_DAYS, "errors": errors,
              "summary": {"baseline": summarize(baseline["prs"]), "since_kit": summarize(kit)},
              "baseline_window": baseline["window"], "window": [max(since, KIT_START), tomorrow],
              "prs": [{k: v for k, v in p.items() if k != "files"} for p in kit]}
    with open(output, "w") as fh:
        json.dump(result, fh, indent=1)
    print(json.dumps(result["summary"], indent=1))
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
