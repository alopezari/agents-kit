// axe-core accessibility check for the validate skill. Usage: node check.mjs [--all] <url>...
// Reports serious and critical violations (all impacts with --all); exits 1 when any are found.
// For pages behind a login, pass cookies as `A11Y_COOKIES='name=value; name2=value2'`.
import { chromium } from "playwright-core";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const axeSource = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const args = process.argv.slice(2);
const all = args.includes("--all");
const urls = args.filter((a) => !a.startsWith("--"));
if (!urls.length) {
  console.error("usage: a11y-check [--all] <url>...");
  process.exit(2);
}

async function launch() {
  // Prefer an installed Chrome; fall back to a Playwright-managed Chromium.
  for (const options of [{ channel: "chrome" }, {}]) {
    try {
      return await chromium.launch({ headless: true, ...options });
    } catch {}
  }
  throw new Error("No Chromium available: install Chrome or run `npx playwright install chromium`.");
}

const browser = await launch();
const context = await browser.newContext({ ignoreHTTPSErrors: true });
if (process.env.A11Y_COOKIES) {
  const cookies = process.env.A11Y_COOKIES.split(";").map((c) => c.trim()).filter(Boolean);
  for (const url of new Set(urls.map((u) => new URL(u).origin))) {
    await context.addCookies(cookies.map((c) => {
      const [name, ...rest] = c.split("=");
      return { name, value: rest.join("="), url };
    }));
  }
}

let failing = 0;
for (const url of urls) {
  const page = await context.newPage();
  await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
  await page.addScriptTag({ content: axeSource });
  const result = await page.evaluate(async () => await window.axe.run(document, { resultTypes: ["violations"] }));
  const violations = result.violations.filter((v) => all || ["serious", "critical"].includes(v.impact));
  console.log(`${url}: ${violations.length} ${all ? "" : "serious/critical "}violations`);
  for (const v of violations) {
    failing++;
    console.log(`  [${v.impact}] ${v.id}: ${v.help} (${v.nodes.length} elements)`);
    for (const node of v.nodes.slice(0, 3)) console.log(`      ${node.target.join(" ")}`);
  }
  await page.close();
}
await browser.close();
process.exit(failing ? 1 : 0);
