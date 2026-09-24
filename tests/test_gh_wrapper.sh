#!/bin/bash
# bin/gh passes git's per-host proxy to gh only for the host a call targets, and fails fast when it's down.
# A fake gh prints the proxy it received, so no network or real gh is involved.
set -uo pipefail
t=$(mktemp -d "${TMPDIR:-/tmp}/agents-gh-XXXXXX")
trap 'kill $listener 2>/dev/null; rm -rf "$t"' EXIT
mkdir -p "$t/wrapper" "$t/real" "$t/repo"
ln -s "$HOME/.agents/bin/gh" "$t/wrapper/gh"
printf '#!/bin/sh\necho "proxy=${HTTPS_PROXY:-none}"\n' > "$t/real/gh"; chmod +x "$t/real/gh"
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
python3 -c "import socket,time; s=socket.socket(); s.bind(('127.0.0.1', $port)); s.listen(); time.sleep(60)" & listener=$!
sleep 0.5
cd "$t/repo" && git init -q && git remote add origin git@ghe.example:org/app.git
git config --file "$t/gitconfig" http.https://ghe.example.proxy "socks5://127.0.0.1:$port"
run() { GIT_CONFIG_GLOBAL="$t/gitconfig" PATH="$t/wrapper:$t/real:/usr/bin:/bin" HTTPS_PROXY= https_proxy= ALL_PROXY= all_proxy= gh "$@" 2>&1; }
fail=0
check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected '$3', got '$2'"; fail=1; fi; }
check "proxied host from the checkout's remote" "$(run pr list)" "proxy=socks5://127.0.0.1:$port"
check "proxied host from --hostname" "$(cd /; run api user --hostname ghe.example)" "proxy=socks5://127.0.0.1:$port"
check "proxied host from -R HOST/OWNER/REPO" "$(cd /; run pr list -R ghe.example/org/app)" "proxy=socks5://127.0.0.1:$port"
check "github.com via -R OWNER/REPO gets no proxy" "$(run pr list -R cli/cli)" "proxy=none"
check "a PR URL on github.com gets no proxy" "$(run pr view https://github.com/cli/cli/pull/1)" "proxy=none"
kill $listener; wait $listener 2>/dev/null
out=$(run pr list); code=$?
check "proxy down fails fast with the reason" "$code $(echo "$out" | grep -c "isn't answering")" "1 1"
exit $fail
