/// <reference types="node" />

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { AUTHENTICATED_ROUTES } from "./_surface";

const ROUTES_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(ROUTES_DIR, "../../../..");
const SITE_SPEC = resolve(REPO_ROOT, "docs/specs-site/01-landing-and-demo-embed.md");

function siteDemoStartPaths(): string[] {
  const siteSpec = readFileSync(SITE_SPEC, "utf8");
  const tableRows = siteSpec.matchAll(/^\| [^|\n]+ \| "[^"\n]+" \| `([^`\n]+)` \|$/gm);
  return [...tableRows].map((match) => match[1]!);
}

describe("site demo intent routes", () => {
  it("use workspace-relative app routes, not undocumented demo aliases", () => {
    const appRoutes = new Set<string>(AUTHENTICATED_ROUTES);
    const starts = siteDemoStartPaths();

    expect(starts).toEqual([
      "/schedule",
      "/dashboard",
      "/stays",
      "/dashboard",
      "/schedule",
      "/dashboard",
      "/pay",
      "/organizations",
      "/today",
      "/today?focus=next-task",
      "/schedule",
      "/chat",
    ]);
    expect(starts.map((start) => start.split("?")[0])).toSatisfy(
      (paths: string[]) => paths.every((path) => appRoutes.has(path)),
    );
  });
});
