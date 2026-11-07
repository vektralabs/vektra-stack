import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? sources(p) : /\.ts$/.test(n) ? [p] : [];
  });
}

describe("messages.it.ts", () => {
  it("holds every user-facing Italian sentence: no other source has one", () => {
    // an Italian sentence: a quoted literal with a capitalised word followed by spaces/letters
    const prose = /["'`][A-ZÀ-Ý][a-zà-ÿ']+ [a-zà-ÿ' ]{6,}/;
    const offenders = sources("src")
      .filter((p) => !p.endsWith("messages.it.ts"))
      .flatMap((p) =>
        readFileSync(p, "utf8")
          .split("\n")
          .filter((l) => !l.trim().startsWith("//") && prose.test(l))
          .map((l) => `${p}: ${l.trim()}`),
      );
    expect(offenders).toEqual([]);
  });
});
