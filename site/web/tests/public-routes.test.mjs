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
