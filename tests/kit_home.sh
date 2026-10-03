# Sourced first by every shell test: the tests reach the kit through ~/.agents, so it must be this checkout.
kit_checkout=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
if [ "${AGENTS_KIT_UNDER_TEST:-}" != "$kit_checkout" ]; then
  kit_installed=$(cd "$HOME/.agents" 2>/dev/null && pwd -P)
  if [ "$kit_checkout" != "$kit_installed" ]; then
    echo "error: these tests run the kit at ~/.agents (${kit_installed:-missing}), not this checkout ($kit_checkout)." >&2
    echo "Run $kit_checkout/tests/run.sh <section>, or set HOME to a directory whose .agents links to this checkout." >&2
    exit 2
  fi
  export AGENTS_KIT_UNDER_TEST="$kit_checkout"
fi
