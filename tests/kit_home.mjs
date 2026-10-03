// Imported first by every node test: the tests reach the kit through ~/.agents, so it must be this checkout.
import { realpathSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const checkout = realpathSync(join(dirname(fileURLToPath(import.meta.url)), ".."));
let installed = "missing";
try { installed = realpathSync(join(homedir(), ".agents")); } catch {}
if (process.env.AGENTS_KIT_UNDER_TEST !== checkout && checkout !== installed) {
  console.error(`error: these tests run the kit at ~/.agents (${installed}), not this checkout (${checkout}).\n`
    + `Run ${checkout}/tests/run.sh <section>, or set HOME to a directory whose .agents links to this checkout.`);
  process.exit(2);
}
process.env.AGENTS_KIT_UNDER_TEST = checkout;
