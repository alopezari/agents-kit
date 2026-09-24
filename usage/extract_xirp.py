"""Join Xirp sessions to the pull requests they opened, for the monthly model/effort analysis.

Usage: extract_xirp.py SINCE_ISO OUTPUT.json
A PR belongs to a session when the session's own `gh pr create` printed its URL (Claude transcripts).
Otherwise, only a session with its own worktree falls back to its branch: in the main checkout Xirp
reports whatever branch the checkout has now, not the one the session worked on.
cli_session_id matches the harness session id in sessions.json (extract_sessions.py), which carries the effort.
"""
import glob
import json
import os
import re
import subprocess
import sys

DEFAULT_BRANCHES = {"main", "master", "trunk", "develop", "HEAD"}
PR_URL = re.compile(r"https://([^/\s\"\\]+)/([^/\s\"\\]+)/([^/\s\"\\]+)/pull/(\d+)")


def gh_env(target):
    """gh ignores git's proxy settings; hosts reachable only through one (e.g. behind a SOCKS tunnel) need it passed on."""
    host = re.search(r"(?:https://)?([^/]+)", target)[1]
    proxy = subprocess.run(["git", "config", "--get-urlmatch", "http.proxy", f"https://{host}"],
                           capture_output=True, text=True).stdout.strip()
    return {**os.environ, "HTTPS_PROXY": proxy} if proxy else None


def run(*cmd, env=None):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}: {out.stderr.strip()[:200]}")
    return out.stdout


def items(raw, key):
    data = json.loads(raw)
    return data if isinstance(data, list) else data[key]


def repo_slug(path):
    """HOST/OWNER/REPO for gh -R, from the checkout's origin remote."""
    url = run("git", "-C", path, "remote", "get-url", "origin").strip()
    m = re.search(r"(?:@|://)([^/:]+)[/:]([^/]+)/([^/]+?)(?:\.git)?$", url)
    return f"{m[1]}/{m[2]}/{m[3]}" if m else None


def created_pr_urls(session_id):
    """URLs printed by `gh pr create` calls in a Claude Code transcript."""
    paths = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl")) if session_id else []
    create_ids, urls = set(), set()
    for path in paths:
        for line in open(path, errors="ignore"):
            try:
                content = (json.loads(line).get("message") or {}).get("content")
            except json.JSONDecodeError:
                continue
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and re.search(r"\bgh\s+pr\s+create\b", str(block.get("input", {}).get("command", ""))):
                    create_ids.add(block.get("id"))
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in create_ids:
                    urls.update("https://{}/{}/{}/pull/{}".format(*m) for m in PR_URL.findall(json.dumps(block.get("content"))))
    return sorted(urls)


def pr_state(url):
    try:
        return {"url": url, **json.loads(run("gh", "pr", "view", url, "--json", "number,state,createdAt,mergedAt,closedAt",
                                            env=gh_env(url)))}
    except (RuntimeError, subprocess.TimeoutExpired):
        return {"url": url, "state": "LOOKUP_FAILED"}


def branch_pr(slug, branch):
    try:
        hit = json.loads(run("gh", "pr", "list", "-R", slug, "--head", branch, "--state", "all",
                             "--json", "number,state,createdAt,mergedAt,closedAt,url", "--limit", "1", env=gh_env(slug)))
        return hit[0] if hit else None
    except (RuntimeError, subprocess.TimeoutExpired):
        return {"state": "LOOKUP_FAILED"}


def main(since, output):
    sessions = [s for s in items(run("xirp", "session", "list", "--all", "--limit", "1000", "--json"), "sessions")
                if s["createdAt"] >= since]
    projects = {p["id"]: p["path"] for p in items(run("xirp", "project", "list", "--json"), "projects")}
    slugs, rows = {}, []

    for s in sessions:
        path = projects.get(s.get("projectId"))
        if path and path not in slugs:
            try:
                slugs[path] = repo_slug(path)
            except (RuntimeError, subprocess.TimeoutExpired):
                slugs[path] = None
        slug, branch = slugs.get(path), s.get("branch")
        own_worktree = s.get("worktreeIsMain") is False

        prs, attribution = [pr_state(u) for u in created_pr_urls(s.get("cliSessionId"))], "gh pr create"
        if not prs and own_worktree and slug and branch and branch not in DEFAULT_BRANCHES:
            hit = branch_pr(slug, branch)
            prs, attribution = ([hit], "worktree branch") if hit else ([], None)
        rows.append({
            "xirp_id": s["id"], "cli_session_id": s.get("cliSessionId"), "agent": s.get("currentAgent"),
            "model": s.get("model"), "created": s["createdAt"], "archived": s.get("lifecycleState") == "ended",
            "repo": slug, "own_worktree": own_worktree,
            "tokens": {"in": s.get("inputTokens"), "out": s.get("outputTokens"), "context": s.get("contextTokens")},
            "prs": prs, "pr_attribution": attribution if prs else None,
        })

    json.dump(rows, open(output, "w"), indent=1)
    all_prs = [p for r in rows for p in r["prs"]]
    print(f"{len(rows)} Xirp sessions since {since}; {sum(bool(r['prs']) for r in rows)} opened PRs "
          f"({len(all_prs)} PRs, {sum(p.get('state') == 'MERGED' for p in all_prs)} merged); "
          f"{sum(p.get('state') == 'LOOKUP_FAILED' for p in all_prs)} PR lookups failed")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
