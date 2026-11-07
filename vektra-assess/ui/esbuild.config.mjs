// Builds the discussion UI bundle (plan 9): content-hashed JS and CSS plus manifest.json into
// the Python package's ui/static/, served by the UI router from the manifest allowlist only.
// No source maps: nothing of src/ reaches the image.
import * as esbuild from "esbuild";
import { mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { basename } from "node:path";

const outdir = "../src/vektra_assess/ui/static";
rmSync(outdir, { recursive: true, force: true });
mkdirSync(outdir, { recursive: true });

const result = await esbuild.build({
  entryPoints: { "assess-ui": "src/main.ts" },
  bundle: true,
  minify: true,
  target: "es2020",
  format: "iife",
  outdir,
  entryNames: "[name].[hash]",
  sourcemap: false,
  metafile: true,
  legalComments: "none",
});

const outputs = Object.keys(result.metafile.outputs).map((p) => basename(p));
const js = outputs.find((n) => n.endsWith(".js"));
const css = outputs.find((n) => n.endsWith(".css"));
if (!js || !css) throw new Error(`expected one js and one css output, got ${outputs}`);
writeFileSync(`${outdir}/manifest.json`, JSON.stringify({ js, css }) + "\n");
console.log("built", readdirSync(outdir).join(" "));
