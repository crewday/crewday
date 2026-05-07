/// <reference types="node" />

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(SRC_DIR, "../../..");
const SOURCE_EXTENSIONS = new Set([".astro", ".js", ".jsx", ".py", ".ts", ".tsx"]);
const SKIP_DIRS = new Set([
  ".git",
  ".mypy_cache",
  ".pytest_cache",
  "__pycache__",
  "dist",
  "node_modules",
]);

function sourceFiles(root: string): string[] {
  const files: string[] = [];
  const visit = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      if (SKIP_DIRS.has(entry)) continue;
      const path = join(dir, entry);
      const stat = statSync(path);
      if (stat.isDirectory()) {
        visit(path);
        continue;
      }
      const dot = entry.lastIndexOf(".");
      const ext = dot >= 0 ? entry.slice(dot) : "";
      if (SOURCE_EXTENSIONS.has(ext)) files.push(path);
    }
  };
  visit(root);
  return files;
}

function importSpecifiers(source: string): string[] {
  const specs: string[] = [];
  const tsImport =
    /\b(?:import|export)\s+(?:type\s+)?(?:[^'"]*?\s+from\s*)?["']([^"']+)["']/g;
  const dynamicImport = /\bimport\(\s*["']([^"']+)["']\s*\)/g;
  const pyFrom = /^from\s+([A-Za-z_][\w.]*)\s+import\b/gm;
  const pyImport = /^import\s+([A-Za-z_][\w.]*)(?:\s+as\b|\s*,|\s*$)/gm;

  for (const match of source.matchAll(tsImport)) specs.push(match[1]!);
  for (const match of source.matchAll(dynamicImport)) specs.push(match[1]!);
  for (const match of source.matchAll(pyFrom)) specs.push(match[1]!);
  for (const match of source.matchAll(pyImport)) specs.push(match[1]!);
  return specs;
}

function violations(
  root: string,
  isForbidden: (specifier: string) => boolean,
): string[] {
  return sourceFiles(root).flatMap((file) => {
    const source = readFileSync(file, "utf8");
    return importSpecifiers(source)
      .filter(isForbidden)
      .map((specifier) => `${relative(REPO_ROOT, file).split(sep).join("/")}: ${specifier}`);
  });
}

describe("app/site import boundaries", () => {
  it("keeps app code from importing the public site", () => {
    expect(violations(resolve(REPO_ROOT, "app"), (specifier) => {
      return specifier === "site_api"
        || specifier.startsWith("site_api.")
        || specifier.startsWith("site/")
        || specifier.includes("../site");
    })).toEqual([]);
  });

  it("keeps public site code from importing the app", () => {
    expect(violations(resolve(REPO_ROOT, "site"), (specifier) => {
      return specifier === "app"
        || specifier.startsWith("app.")
        || specifier.startsWith("app/")
        || specifier.includes("../app");
    })).toEqual([]);
  });
});
