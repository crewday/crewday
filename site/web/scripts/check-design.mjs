import { readdir, readFile, copyFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(siteRoot, "../..");

const sourceTokensPath = path.join(repoRoot, "mocks/web/src/styles/tokens.css");
const siteTokensPath = path.join(siteRoot, "src/styles/tokens.css");
const siteGlobalsPath = path.join(siteRoot, "src/styles/globals.css");
const siteIconsPath = path.join(siteRoot, "src/icons/index.ts");
const siteSrcPath = path.join(siteRoot, "src");
const siteShellPath = path.join(siteRoot, "src/components/SiteShell.astro");

const args = new Set(process.argv.slice(2));
const knownArgs = new Set(["--check", "--write"]);
const unknownArgs = [...args].filter((arg) => !knownArgs.has(arg));

if (unknownArgs.length > 0) {
  console.error(`Unknown argument: ${unknownArgs.join(", ")}`);
  process.exit(2);
}

const shouldWrite = args.has("--write");
const failures = [];

if (shouldWrite) {
  await copyFile(sourceTokensPath, siteTokensPath);
}

await checkTokenDrift();
await checkStyleEntrypoint();
await checkGlobals();
await checkIcons();

if (failures.length > 0) {
  console.error(failures.map((failure) => `- ${failure}`).join("\n"));
  process.exit(1);
}

console.log("Design primitives are in sync.");

async function checkTokenDrift() {
  const [sourceTokens, siteTokens] = await Promise.all([
    readFile(sourceTokensPath, "utf8"),
    readFile(siteTokensPath, "utf8"),
  ]);

  if (sourceTokens !== siteTokens) {
    failures.push(
      "site/web/src/styles/tokens.css differs from mocks/web/src/styles/tokens.css. Run `npm run sync:design` in site/web.",
    );
  }
}

async function checkGlobals() {
  const globals = await readFile(siteGlobalsPath, "utf8");
  const utilityClasses = [...extractCssClasses(globals)].filter(isUtilityClass);

  if (utilityClasses.length > 0) {
    failures.push(
      `site/web/src/styles/globals.css contains utility-style selectors: ${utilityClasses.join(", ")}.`,
    );
  }
}

async function checkStyleEntrypoint() {
  const shell = await readFile(siteShellPath, "utf8");
  const imports = extractSideEffectImports(shell);
  const requiredImports = ["../styles/tokens.css", "../styles/globals.css"];
  const missingImports = requiredImports.filter((importPath) => !imports.has(importPath));

  if (missingImports.length > 0) {
    failures.push(
      `site build entrypoint must import site token/global CSS from src/styles: ${missingImports.join(", ")}.`,
    );
  }
}

async function checkIcons() {
  const icons = await readFile(siteIconsPath, "utf8");

  if (!/export\s*\{[\s\S]*\}\s*from\s+["']lucide-react["']/u.test(icons)) {
    failures.push("site/web/src/icons/index.ts must re-export Lucide icons.");
  }

  if (/<svg\b|createElement\(["']svg["']/u.test(icons)) {
    failures.push("site/web/src/icons/index.ts must not define hand-drawn SVG icons.");
  }

  const sourceFiles = await listSourceFiles(siteSrcPath);
  const directLucideImports = [];

  for (const filePath of sourceFiles) {
    if (filePath === siteIconsPath || !/\.(?:astro|ts|tsx)$/u.test(filePath)) {
      continue;
    }

    const source = await readFile(filePath, "utf8");
    if (/(?:from\s+["']lucide-react["']|import\s*\(\s*["']lucide-react["']\s*\))/u.test(source)) {
      directLucideImports.push(path.relative(siteRoot, filePath));
    }
  }

  if (directLucideImports.length > 0) {
    failures.push(
      `site UI icons must import from "@/icons", not lucide-react directly: ${directLucideImports.join(", ")}.`,
    );
  }
}

function extractSideEffectImports(source) {
  const imports = new Set();
  const importPattern = /^\s*import\s+["']([^"']+)["'];?\s*$/gmu;
  let match = importPattern.exec(source);

  while (match !== null) {
    imports.add(match[1]);
    match = importPattern.exec(source);
  }

  return imports;
}

function extractCssClasses(css) {
  const classes = new Set();
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//gu, "");
  const classPattern = /(?<![A-Za-z0-9_-])\.((?:\\.|[_a-zA-Z0-9-])+)/gu;
  let match = classPattern.exec(withoutComments);

  while (match !== null) {
    classes.add(unescapeCssClass(match[1]));
    match = classPattern.exec(withoutComments);
  }

  return classes;
}

function unescapeCssClass(className) {
  return className.replace(/\\([^0-9a-fA-F\r\n\f])/gu, "$1");
}

function isUtilityClass(className) {
  const normalized = className.split(":").at(-1)?.replace(/^!/, "").replace(/^-/, "");
  if (!normalized) {
    return false;
  }

  return [
    /^(?:m|mt|mr|mb|ml|mx|my|p|pt|pr|pb|pl|px|py)-.+$/u,
    /^(?:flex|inline-flex|grid|inline-grid|block|inline-block|hidden)$/u,
    /^(?:items|justify|content|self|place)-(?:start|end|center|between|around|evenly|stretch)$/u,
    /^(?:absolute|fixed|relative|sticky|sr-only)$/u,
    /^(?:text|bg|border|rounded|shadow|opacity|z|order|gap|space|w|h|min-w|min-h|max-w|max-h|basis|inset|top|right|bottom|left|overflow)-.+$/u,
  ].some((pattern) => pattern.test(normalized));
}

async function listSourceFiles(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = await Promise.all(
    entries.map(async (entry) => {
      const entryPath = path.join(dir, entry.name);
      return entry.isDirectory() ? listSourceFiles(entryPath) : entryPath;
    }),
  );

  return files.flat();
}
