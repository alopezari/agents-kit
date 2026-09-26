// Every skill's frontmatter must parse as strict YAML. Pi silently drops a skill whose frontmatter
// doesn't (follow-pr's unquoted "run: CI failures" did), while Codex and Claude Code load it anyway.
// Uses the yaml package Pi itself ships, so the check matches what Pi accepts.
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { readFileSync, readdirSync, existsSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { homedir } from "node:os";

let piCli;
try {
  piCli = realpathSync(execFileSync("which", ["pi"], { encoding: "utf8" }).trim());
} catch {
  // CI installs Pi, so a skip there means the check silently stopped running.
  if (process.env.CI) { console.log("FAIL pi not installed in CI: skill frontmatter went unchecked"); process.exit(1); }
  console.log("skip pi not installed, so nothing drops skills on strict YAML");
  process.exit(0);
}
const piPackage = join(dirname(piCli), "..", "..");
const YAML = createRequire(join(piPackage, "package.json"))("yaml");

// The sample profile's skills too: people copy them as the model for their own.
const skillsDirs = [join(homedir(), ".agents", "skills"), join(homedir(), ".agents", "examples", "sample-profile", "skills")];
let failed = 0;
for (const [skillsDir, skill] of skillsDirs.flatMap((dir) => readdirSync(dir).sort().map((name) => [dir, name]))) {
  const file = join(skillsDir, skill, "SKILL.md");
  if (!existsSync(file)) continue;
  const match = readFileSync(file, "utf8").match(/^---\n([\s\S]*?)\n---/);
  let problem = match ? null : "no frontmatter";
  if (match) {
    try {
      const fields = YAML.parse(match[1]);
      if (fields?.name !== skill) problem = `name is "${fields?.name}", expected "${skill}"`;
      else if (typeof fields.description !== "string" || !fields.description) problem = "no description";
    } catch (error) {
      problem = error.message.split("\n")[0];
    }
  }
  if (problem) {
    failed++;
    console.log(`FAIL ${skill}: ${problem}`);
  } else {
    console.log(`ok   ${skill}`);
  }
}
process.exit(failed ? 1 : 0);
