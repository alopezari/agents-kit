// Parse every ```mermaid block in the given Markdown files with mermaid's own parser, the one GitHub renders with.
// Exit 1 on the first file with a block that doesn't parse.
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const { window } = new JSDOM("");
globalThis.window = window;
globalThis.document = window.document;
const { default: mermaid } = await import("mermaid");

let failed = 0;
for (const file of process.argv.slice(2)) {
  const blocks = [...readFileSync(file, "utf8").matchAll(/```mermaid\n([\s\S]*?)```/g)].map((m) => m[1]);
  for (const [i, block] of blocks.entries()) {
    try {
      await mermaid.parse(block);
    } catch (error) {
      failed++;
      console.log(`FAIL ${file}: diagram ${i + 1}: ${String(error.message ?? error).split("\n").slice(0, 3).join(" | ")}`);
    }
  }
  if (!failed) console.log(`ok   ${file}: ${blocks.length} diagrams parse`);
}
process.exit(failed ? 1 : 0);
