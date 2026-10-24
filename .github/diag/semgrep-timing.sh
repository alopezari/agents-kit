#!/bin/bash
# TEMPORARY (PR #132): times `semgrep --version` on the runner in each way install.sh could call it. Remove before merge.
t() { local label=$1; shift; local s=$(python3 -c 'import time;print(time.time())')
  out=$("$@" 2>&1 | head -c 300); code=$?
  echo "$label: $(python3 -c "import time;print(f'{time.time()-$s:.1f}s')") exit=$code out=[$out]"; }
which -a semgrep; file "$(command -v semgrep)"; head -3 "$(command -v semgrep)" 2>/dev/null | cut -c1-200
t plain semgrep --version
t envoff env SEMGREP_ENABLE_VERSION_CHECK=0 semgrep --version
t stdin-null bash -c 'semgrep --version </dev/null'
t setsid python3 -c 'import subprocess,sys,time
s=time.time(); p=subprocess.Popen(["semgrep","--version"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
try: p.wait(timeout=90); print("exited", f"{time.time()-s:.1f}s", p.returncode)
except subprocess.TimeoutExpired: print("no exit in 90s")
import os,signal
print(subprocess.run(["ps","-o","pid,pgid,stat,etime,command","-g",str(p.pid)],capture_output=True,text=True).stdout[:1500])
try: os.killpg(p.pid,9)
except ProcessLookupError: pass
o,e=p.communicate(); print("out",o[:200],"err",e[:300])'
t nosetsid python3 -c 'import subprocess
p=subprocess.run(["semgrep","--version"],stdin=subprocess.DEVNULL,capture_output=True,timeout=90); print(p.returncode,p.stdout[:100],p.stderr[:300])'
t doctor bash -c '~/.agents/install.sh --doctor 2>&1 | grep -i semgrep'
