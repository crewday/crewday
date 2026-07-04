import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";

const distDir = new URL("../dist/", import.meta.url);
const publicRoutes = [
  "/",
  "/for-owners",
  "/for-agencies",
  "/for-housekeepers",
  "/pricing",
  "/why-crewday",
  "/suggest",
];

function routeFile(route) {
  return route === "/"
    ? join(distDir.pathname, "index.html")
    : join(distDir.pathname, route.slice(1), "index.html");
}

for (const route of publicRoutes) {
  test(`${route} renders with the landing shell`, async () => {
    const html = await readFile(routeFile(route), "utf8");
    assert.match(html, /class="landing-masthead"/u);
    assert.match(html, /class="landing-imprint"/u);
    assert.doesNotMatch(html, /Not found \| crew\.day/u);
  });
}

test("/suggest renders the public suggestion board", async () => {
  const html = await readFile(routeFile("/suggest"), "utf8");
  assert.match(html, /Suggestion box \| crew\.day/u);
  assert.match(html, /Public board/u);
  assert.match(html, /Log in to submit or vote/u);
});

test("/suggest labels the sample board as illustrative", async () => {
  const html = await readFile(routeFile("/suggest"), "utf8");
  // The fabricated cluster counts must be unmistakably flagged as sample
  // data so a visitor cannot read them as real feedback volumes.
  assert.match(html, /suggest-board--sample/u);
  assert.match(html, /illustrative placeholders/u);
  const flags = html.match(/class="suggest-cluster__flag"/gu) ?? [];
  const clusters = html.match(/class="suggest-cluster"/gu) ?? [];
  assert.ok(clusters.length > 0, "expected at least one sample cluster");
  assert.equal(flags.length, clusters.length, "every fabricated count needs a Sample flag");
});

for (const route of publicRoutes) {
  test(`${route} carries canonical + Open Graph head tags`, async () => {
    const html = await readFile(routeFile(route), "utf8");
    const expectedUrl = `https://crew.day${route === "/" ? "/" : `${route}/`}`;
    assert.match(html, new RegExp(`<link rel="canonical" href="${expectedUrl}">`, "u"));
    assert.match(html, new RegExp(`<meta property="og:url" content="${expectedUrl}">`, "u"));
    assert.match(html, /<meta property="og:image" content="https:\/\/crew\.day\/og-default\.png">/u);
    assert.match(html, /<meta name="twitter:card" content="summary_large_image">/u);
  });
}
