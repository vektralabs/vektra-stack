// Entry point of the discussion UI (plan 9). Boots from the JSON config block the page carries.
import "./styles.css";
import { App } from "./app";
import { Bridge } from "./bridge";

export interface PageConfig {
  site: string;
  moodleOrigin: string;
  apiBase: string;
}

export function readConfig(doc: Document = document): PageConfig {
  const el = doc.getElementById("cfg");
  if (!el || !el.textContent) throw new Error("missing page config");
  const cfg = JSON.parse(el.textContent) as PageConfig;
  if (!cfg.moodleOrigin || !cfg.apiBase) throw new Error("incomplete page config");
  return cfg;
}

function boot(): void {
  const cfg = readConfig();
  const root = document.getElementById("app");
  if (!root) return;
  new App(cfg, new Bridge(window, cfg.moodleOrigin), root).start();
}

if (typeof window !== "undefined" && document.getElementById("cfg")) {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
}
