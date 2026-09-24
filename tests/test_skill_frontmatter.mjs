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
  console.log("skip pi not installed, so nothing drops skills on strict YAML");
  process.exit(0);
}
const piPackage = join(dirname(piCli), "..", "..");
const YAML = createRequire(join(piPackage, "package.json"))("yaml");

const skillsDir = join(homedir(), ".agents", "skills");
let failed = 0;
for (const skill of readdirSync(skillsDir).sort()) {
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
