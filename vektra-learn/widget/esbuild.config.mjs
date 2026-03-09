import * as esbuild from "esbuild";

await esbuild.build({
  entryPoints: ["src/index.js"],
  bundle: true,
  minify: true,
  target: "es2020",
  outfile: "../static/vektra-chat.js",
  format: "iife",
});
