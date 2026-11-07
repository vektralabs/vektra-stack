// The no-HTML-sink rule (plan 9): trusted types are Chromium-only, so on Firefox and Safari this
// check is what holds. No source file may use innerHTML, outerHTML, insertAdjacentHTML,
// document.write or DOMParser: text goes in with textContent and DOM construction only.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const FORBIDDEN = /\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write|DOMParser)\b/;
const bad = [];
function walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path);
    else if (/\.(ts|js|mjs)$/.test(name)) {
      readFileSync(path, "utf8")
        .split("\n")
        .forEach((line, i) => {
          if (FORBIDDEN.test(line)) bad.push(`${path}:${i + 1}: ${line.trim()}`);
        });
    }
  }
}
walk("src");
if (bad.length) {
  console.error("HTML sinks are forbidden in the discussion UI:\n" + bad.join("\n"));
  process.exit(1);
}
console.log("no HTML sinks");
