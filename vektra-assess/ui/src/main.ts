// Entry point of the discussion UI (plan 9). Boots from the JSON config block the page carries.
import "./styles.css";

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
