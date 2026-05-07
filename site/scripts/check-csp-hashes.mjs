#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const siteRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const distDir = join(siteRoot, "web", "dist");
const caddyfile = join(siteRoot, "Caddyfile");

function walkHtmlFiles(dir, files = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      walkHtmlFiles(path, files);
    } else if (extname(entry.name) === ".html") {
      files.push(path);
    }
  }
  return files;
}

function inlineScriptHashes() {
  const hashes = new Set();
  const scriptPattern = /<script(?<attrs>[^>]*)>(?<body>[\s\S]*?)<\/script>/gi;

  for (const file of walkHtmlFiles(distDir).sort()) {
    const html = readFileSync(file, "utf8");
    for (const match of html.matchAll(scriptPattern)) {
      const attrs = match.groups?.attrs ?? "";
      const body = match.groups?.body ?? "";
      if (/\ssrc\s*=/i.test(attrs) || body.length === 0) {
        continue;
      }
      const hash = createHash("sha256").update(body).digest("base64");
      hashes.add(`sha256-${hash}`);
    }
  }

  return [...hashes].sort();
}

function caddyScriptHashes() {
  const body = readFileSync(caddyfile, "utf8");
  const cspLine = body
    .split("\n")
    .find((line) => line.includes("Content-Security-Policy"));
  if (!cspLine) {
    throw new Error("site/Caddyfile does not define Content-Security-Policy");
  }

  return [...cspLine.matchAll(/'sha256-([^']+)'/g)]
    .map((match) => `sha256-${match[1]}`)
    .sort();
}

const builtHashes = inlineScriptHashes();
const configuredHashes = caddyScriptHashes();
const missing = builtHashes.filter((hash) => !configuredHashes.includes(hash));
const stale = configuredHashes.filter((hash) => !builtHashes.includes(hash));

if (missing.length > 0 || stale.length > 0) {
  console.error("Caddy CSP hashes do not match Astro inline script output.");
  console.error(`Built hashes from ${relative(process.cwd(), distDir)}:`);
  for (const hash of builtHashes) {
    console.error(`  '${hash}'`);
  }
  if (missing.length > 0) {
    console.error("Missing from site/Caddyfile:");
    for (const hash of missing) {
      console.error(`  '${hash}'`);
    }
  }
  if (stale.length > 0) {
    console.error("Stale in site/Caddyfile:");
    for (const hash of stale) {
      console.error(`  '${hash}'`);
    }
  }
  process.exit(1);
}

console.log(
  `Caddy CSP inline script hashes match Astro output (${builtHashes.length}).`,
);
