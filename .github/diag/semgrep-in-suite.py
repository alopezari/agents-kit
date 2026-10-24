# TEMPORARY (PR #132): run inside tests/run.sh's doctor section; times semgrep --version and, if it hangs, shows why.
import os, subprocess, sys, time
def probe(label, env):
    s = time.time()
    p = subprocess.Popen(["semgrep", "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, start_new_session=True)
    try:
        p.wait(timeout=45); print(f"diag {label}: exited {p.returncode} in {time.time()-s:.1f}s")
    except subprocess.TimeoutExpired:
        print(f"diag {label}: no exit in 45s")
        tree = subprocess.run(["ps", "-o", "pid,ppid,stat,%cpu,etime,command", "-g", str(p.pid)], capture_output=True, text=True).stdout
        print(tree[:2000])
        for pid in [l.split()[0] for l in tree.splitlines()[1:]]:
            print(subprocess.run(["lsof", "-n", "-p", pid], capture_output=True, text=True).stdout[-3000:])
            print(subprocess.run(["sample", pid, "2"], capture_output=True, text=True).stdout[:6000])
    try: os.killpg(p.pid, 9)
    except ProcessLookupError: pass
    o, e = p.communicate(); print(f"diag {label}: out={o[:60]!r} err={e[-300:]!r}")
print("diag TMPDIR =", os.environ.get("TMPDIR"), "exists", os.path.isdir(os.environ.get("TMPDIR", "")))
probe("suite env", dict(os.environ, SEMGREP_ENABLE_VERSION_CHECK="0"))
probe("TMPDIR unset", {k: v for k, v in os.environ.items() if k != "TMPDIR"} | {"SEMGREP_ENABLE_VERSION_CHECK": "0"})
probe("suite env again", dict(os.environ, SEMGREP_ENABLE_VERSION_CHECK="0"))
